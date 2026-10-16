"""Agents: the workers that fill each stage of an episode.

Every agent has the same shape:
  * build_prompt()  the text you give an AI (paste it into any free chat AI)
  * Output          the JSON shape the answer must have (checked by Pydantic)
  * apply()         merges the answer into the agent's OWN section, with safety rules in code
  * mock()          a fixed, free answer for testing (every text is marked [MOCK])

Hard rules enforced here, never left to the AI:
  * New claims always start as needs_review. Only a human approves a fact (studio/review.py).
  * The fact checker only SUGGESTS: it writes notes and issues, never claim statuses.
  * Shots are only planned here; generating images/video comes in later milestones.
"""
import json
import math
import re
from collections.abc import Sequence
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import declines
from .config import Settings
from .db import EpisodeRepository
from .errors import AgentOutputError, StudioError
from .models import (
    Claim,
    ClaimStatus,
    ContentKind,
    Episode,
    FactCheck,
    Importance,
    Reliability,
    Research,
    Script,
    Segment,
    SegmentRole,
    Sentence,
    Shot,
    ShotStatus,
    Source,
    SourceType,
    Status,
    Storyboard,
)
from .prompts import render
from .review import set_claim_status, set_fact_check
from .state_machine import next_stage
from .validators import Issue, validate_for_transition


class Lenient(BaseModel):
    """Base for AI answers: unknown extra fields (e.g. "confidence") are ignored, not errors."""
    model_config = ConfigDict(extra="ignore")


# ------------------------------------------------------------ answer shapes
class SourceIn(Lenient):
    source_id: str = Field(min_length=1)
    title: str
    publisher: str = Field(min_length=1)
    url: str = Field(pattern=r"^https?://\S+$")
    published_date: str | None = None
    source_type: SourceType = SourceType.OTHER
    reliability: Reliability = Reliability.MEDIUM
    evidence_summary: str = ""


class ClaimIn(Lenient):
    claim_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    importance: Importance = Importance.MEDIUM
    source_ids: list[str] = Field(default_factory=list)


class ResearchOutput(Lenient):
    sources: list[SourceIn]
    claims: list[ClaimIn] = Field(min_length=1)
    open_questions: list[str] = Field(default_factory=list)


class VerdictIn(Lenient):
    claim_id: str
    verdict: Literal["supported", "unsupported", "unclear"]
    reason: str = ""


class FactCheckOutput(Lenient):
    verdicts: list[VerdictIn] = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)


class SentenceIn(Lenient):
    text: str = Field(min_length=1)
    kind: ContentKind
    claim_ids: list[str] = Field(default_factory=list)


class SegmentIn(Lenient):
    segment_id: str = Field(min_length=1)
    role: SegmentRole
    duration_sec: float = Field(ge=0.5)
    sentences: list[SentenceIn] = Field(min_length=1)
    visual_intent: str = ""
    emotion: str = ""


class ScriptOutput(Lenient):
    hook: str = Field(min_length=1)
    segments: list[SegmentIn] = Field(min_length=1)


class ShotIn(Lenient):
    shot_id: str = Field(min_length=1)
    duration_sec: float = Field(ge=0.5)
    segment_ids: list[str] = Field(min_length=1)
    purpose: str = ""
    location: str = ""
    characters: list[str] = Field(default_factory=list)
    action: str = ""
    camera: str = ""
    continuity_keys: list[str] = Field(default_factory=list)
    generation_prompt: str = Field(min_length=1)


class StoryboardOutput(Lenient):
    shots: list[ShotIn] = Field(min_length=1)


# ------------------------------------------------------------------ helpers
WORDS_PER_SEC = 2.5  # narration speed used everywhere (about 150 words a minute)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "topic"


def _target_duration(ep: Episode, s: Settings) -> float:
    """The episode's target length, kept safely inside the allowed range."""
    low, high = s.min_duration_sec + 1, s.max_duration_sec - 1
    return float(min(max(ep.metadata.target_duration_sec, low), high))


LINK_FIELDS = {"link_status", "checked_at", "final_url", "page_title", "link_note",
               "verified_by", "verified_note"}


def _url_key(url: str) -> str:
    return url.strip().rstrip("/").lower()


def _next_id(prefix: str, taken: set[str]) -> str:
    n = 1
    while f"{prefix}{n:03d}" in taken:
        n += 1
    return f"{prefix}{n:03d}"


def with_style(prompt: str, style: str) -> str:
    """The image prompt with the channel's style sentence, unless it is already in it."""
    if not style.strip():
        return prompt
    key = " ".join(style.lower().split()[:3])  # e.g. "rich painterly food"
    if key.rstrip(",.") in prompt.lower():
        return prompt
    return f"{prompt.rstrip().rstrip('.')}. Style: {style.strip()}"


def _format_errors(err: ValidationError) -> list[str]:
    problems = []
    for e in err.errors():
        where = ""
        for part in e["loc"]:
            where += f"[{part}]" if isinstance(part, int) else (f".{part}" if where else str(part))
        problems.append(f"{where or 'answer'}: {e['msg']}")
    return problems


# ------------------------------------------------------------------- agents
class Agent:
    name: str
    label: str
    stage: Status
    sections: frozenset[str]
    output_model: type[Lenient]
    # How free the AI is to vary its wording: low where facts must be exact, higher where
    # writing benefits from variety (used when the app asks an AI service itself).
    temperature: float = 0.3

    def context(self, ep: Episode, s: Settings) -> dict[str, str]:
        return {}

    def apply(self, ep: Episode, out, s: Settings | None = None) -> Episode:
        raise NotImplementedError

    def mock(self, ep: Episode, s: Settings):
        raise NotImplementedError

    def current_answer(self, ep: Episode) -> dict | None:
        """The agent's section as an answer, for editing by hand. None if not editable."""
        return None

    def build_prompt(self, ep: Episode, s: Settings, blocked: Sequence[dict] = ()) -> str:
        """`blocked` is repo.blocked_sites(): sites the researcher must not use."""
        m = ep.metadata
        values = {
            "blocked_sites": declines.blocked_list_text(list(blocked)),
            "topic": m.topic,
            "story_angle": m.story_angle or "(none yet - suggest one)",
            "category": m.category.value,
            "series": m.series or "(none)",
            "tone": m.tone,
            "target_duration": f"{_target_duration(ep, s):g}",
            "min_duration": f"{s.min_duration_sec:g}",
            "max_duration": f"{s.max_duration_sec:g}",
            "min_sources": str(s.high_importance_min_sources),
            "tolerance": f"{s.duration_tolerance_sec:g}",
            "visual_style": s.visual_style,
            "shots_target": str(s.shots_target),
            "shots_max": str(s.shots_max),
            "hook_max_words": str(s.hook_max_words or 14),
            **self.context(ep, s),
        }
        return render(self.name, values)


class Researcher(Agent):
    name, label, stage = "researcher", "Researcher", Status.RESEARCH
    sections = frozenset({"research"})
    output_model = ResearchOutput
    temperature = 0.2  # facts and real links: no creativity wanted

    def apply(self, ep: Episode, out: ResearchOutput, s: Settings | None = None) -> Episode:
        # Keep every claim a human already decided on (approved OR rejected) and its sources,
        # so a new answer can neither duplicate them nor bring a rejected fact back.
        kept_claims = [c for c in ep.research.claims if c.status != ClaimStatus.NEEDS_REVIEW]
        cited = {sid for c in kept_claims for sid in c.source_ids}
        # Declined sources stay too: a new answer citing the same page gets it declined again.
        kept_sources = [x for x in ep.research.sources if x.source_id in cited or x.declined]

        taken_s = {x.source_id for x in kept_sources}
        kept_urls = {_url_key(x.url): x.source_id for x in kept_sources}
        old_by_url = {_url_key(x.url): x for x in ep.research.sources}
        source_map: dict[str, str] = {}
        sources = list(kept_sources)
        for x in out.sources:
            same = kept_urls.get(_url_key(x.url))
            if same:  # already kept: reuse it instead of adding a copy
                source_map[x.source_id] = same
                continue
            new_id = x.source_id if x.source_id not in taken_s else _next_id("S", taken_s)
            source_map[x.source_id] = new_id
            taken_s.add(new_id)
            # Same URL as before: keep its link check. A new or changed URL starts unchecked.
            old = old_by_url.get(_url_key(x.url))
            check = old.model_dump(include=LINK_FIELDS) if old else {}
            sources.append(Source(**x.model_dump(exclude={"source_id"}), source_id=new_id,
                                  accessed_date=date.today().isoformat(), **check))

        taken_c = {c.claim_id for c in kept_claims}
        kept_statements = {c.statement.strip() for c in kept_claims}
        claims = list(kept_claims)
        for c in out.claims:
            if c.statement.strip() in kept_statements:  # already decided by a human
                continue
            new_id = c.claim_id if c.claim_id not in taken_c else _next_id("C", taken_c)
            taken_c.add(new_id)
            source_ids = [source_map.get(sid, sid) for sid in c.source_ids]
            claims.append(Claim(
                claim_id=new_id, statement=c.statement, importance=c.importance,
                source_ids=source_ids, evidence=self._carry_evidence(ep, c.statement, source_ids,
                                                                     sources),
                status=ClaimStatus.NEEDS_REVIEW))  # ALWAYS: only a human approves facts

        research = Research(claims=claims, sources=sources, open_questions=out.open_questions)
        return ep.model_copy(update={"research": research})

    @staticmethod
    def _carry_evidence(ep: Episode, statement: str, source_ids: list[str],
                        sources: list[Source]) -> list:
        """An unchanged claim keeps its quotes from sources whose URL is unchanged."""
        old = next((c for c in ep.research.claims if c.statement == statement), None)
        if old is None:
            return []
        old_urls = {x.source_id: _url_key(x.url) for x in ep.research.sources}
        new_ids = {_url_key(x.url): x.source_id for x in sources if x.source_id in source_ids}
        carried = []
        for e in old.evidence:
            sid = new_ids.get(old_urls.get(e.source_id, ""))
            if sid:
                carried.append(e.model_copy(update={"source_id": sid}))
        return carried

    def current_answer(self, ep: Episode) -> dict | None:
        r = ep.research
        return {"sources": [x.model_dump(mode="json", exclude={"accessed_date", *LINK_FIELDS})
                            for x in r.sources],
                "claims": [c.model_dump(mode="json", exclude={"status", "notes", "evidence"})
                           for c in r.claims],
                "open_questions": r.open_questions}

    def mock(self, ep: Episode, s: Settings) -> ResearchOutput:
        topic, slug = ep.metadata.topic, _slug(ep.metadata.topic)
        return ResearchOutput(
            sources=[
                SourceIn(source_id="S001", title=f"[MOCK] Encyclopedia entry: {topic}",
                         publisher="Mock Publisher A", url=f"https://example.org/mock/{slug}-a",
                         source_type=SourceType.OTHER, reliability=Reliability.HIGH,
                         evidence_summary="[MOCK] Placeholder evidence."),
                SourceIn(source_id="S002", title=f"[MOCK] Museum article: {topic}",
                         publisher="Mock Publisher B", url=f"https://example.org/mock/{slug}-b",
                         source_type=SourceType.MUSEUM, reliability=Reliability.HIGH,
                         evidence_summary="[MOCK] Placeholder evidence."),
            ],
            claims=[
                ClaimIn(claim_id="C001", importance=Importance.HIGH, source_ids=["S001", "S002"],
                        statement=f"[MOCK] The central fact about {topic}."),
                ClaimIn(claim_id="C002", importance=Importance.MEDIUM, source_ids=["S001"],
                        statement=f"[MOCK] A surprising detail about {topic}."),
                ClaimIn(claim_id="C003", importance=Importance.MEDIUM, source_ids=["S002"],
                        statement=f"[MOCK] What happened next with {topic}."),
            ],
            open_questions=["[MOCK] Which source is the earliest?"])


class FactChecker(Agent):
    name, label, stage = "fact_checker", "Fact checker", Status.FACT_CHECK
    sections = frozenset({"research", "fact_check"})
    output_model = FactCheckOutput
    temperature = 0.0  # the same claims should get the same verdicts

    def context(self, ep: Episode, s: Settings) -> dict[str, str]:
        sources = {x.source_id: x for x in ep.research.sources}
        lines = []
        for c in ep.research.claims:
            if c.status == ClaimStatus.REJECTED:
                continue
            lines.append(f"- {c.claim_id} ({c.importance.value}): {c.statement}")
            for sid in c.source_ids:
                x = sources.get(sid)
                lines.append(f"    - {sid}: {x.publisher} | {x.title} | {x.url} | "
                             f"evidence: {x.evidence_summary or '-'}" if x
                             else f"    - {sid}: (unknown source)")
        return {"claims": "\n".join(lines) or "(no claims)"}

    def apply(self, ep: Episode, out: FactCheckOutput, s: Settings | None = None) -> Episode:
        known = {c.claim_id for c in ep.research.claims}
        unknown = [v.claim_id for v in out.verdicts if v.claim_id not in known]
        if unknown:
            raise AgentOutputError([f"verdicts: unknown claim_id {cid}" for cid in unknown])
        verdicts = {v.claim_id: v for v in out.verdicts}
        claims = []
        for c in ep.research.claims:
            v = verdicts.get(c.claim_id)
            if v is None:
                claims.append(c)
                continue
            human_notes = [ln for ln in c.notes.splitlines() if not ln.startswith("AI check:")]
            ai_note = f"AI check: {v.verdict}" + (f" - {v.reason}" if v.reason else "")
            # Only the note changes. The status stays exactly as the human left it.
            claims.append(c.model_copy(update={"notes": "\n".join(human_notes + [ai_note])}))
        issues = list(out.issues) + [
            f"{v.claim_id}: AI says {v.verdict}" + (f" - {v.reason}" if v.reason else "")
            for v in out.verdicts if v.verdict != "supported"]
        return ep.model_copy(update={
            "research": ep.research.model_copy(update={"claims": claims}),
            "fact_check": FactCheck(approved=ep.fact_check.approved, issues=issues)})

    def mock(self, ep: Episode, s: Settings) -> FactCheckOutput:
        return FactCheckOutput(verdicts=[
            VerdictIn(claim_id=c.claim_id, verdict="supported",
                      reason="[MOCK] Matches the cited sources.")
            for c in ep.research.claims if c.status != ClaimStatus.REJECTED])


class Scriptwriter(Agent):
    name, label, stage = "scriptwriter", "Scriptwriter", Status.SCRIPT
    sections = frozenset({"script"})
    output_model = ScriptOutput
    temperature = 0.7  # storytelling: varied, lively wording (the facts stay locked)

    def context(self, ep: Episode, s: Settings) -> dict[str, str]:
        approved = [c for c in ep.research.claims if c.status == ClaimStatus.APPROVED]
        return {"claims": "\n".join(f"- {c.claim_id}: {c.statement}" for c in approved)
                or "(no approved claims - write nothing factual)",
                # the AI can't count well: Python gives the word budget (2.5 words a second)
                "target_words": str(round(_target_duration(ep, s) * WORDS_PER_SEC)),
                "min_words": str(math.ceil(s.min_duration_sec * WORDS_PER_SEC)),
                "max_words": str(math.floor(s.max_duration_sec * WORDS_PER_SEC))}

    def apply(self, ep: Episode, out: ScriptOutput, s: Settings | None = None) -> Episode:
        segments = [Segment(segment_id=g.segment_id, role=g.role, duration_sec=g.duration_sec,
                            visual_intent=g.visual_intent, emotion=g.emotion,
                            sentences=[Sentence(**x.model_dump()) for x in g.sentences])
                    for g in out.segments]
        return ep.model_copy(update={"script": Script(hook=out.hook, segments=segments)})

    def current_answer(self, ep: Episode) -> dict | None:
        return ep.script.model_dump(mode="json")

    def mock(self, ep: Episode, s: Settings) -> ScriptOutput:
        approved = [c.claim_id for c in ep.research.claims if c.status == ClaimStatus.APPROVED]
        if not approved:
            raise AgentOutputError(["The mock scriptwriter needs at least one approved claim."])
        pick = lambda i: [approved[i % len(approved)]]  # noqa: E731
        total, topic = _target_duration(ep, s), ep.metadata.topic
        F, C = ContentKind.FACTUAL, ContentKind.CREATIVE
        plan = [  # role, share of the length, sentence kind, claim ids
            (SegmentRole.HOOK, 0.10, C, []), (SegmentRole.SETUP, 0.25, F, pick(0)),
            (SegmentRole.DEVELOPMENT, 0.30, F, pick(1)), (SegmentRole.PAYOFF, 0.22, F, pick(2)),
            (SegmentRole.ENDING, 0.13, C, [])]
        hook = f"[MOCK] What if {topic} was not what you think?"
        segments = []
        for n, (role, share, kind, cids) in enumerate(plan, start=1):
            text = hook if role == SegmentRole.HOOK else f"[MOCK] {role.value} line about {topic}."
            segments.append(SegmentIn(
                segment_id=f"SEG{n:02d}", role=role, duration_sec=round(total * share, 1),
                sentences=[SentenceIn(text=text, kind=kind, claim_ids=cids)],
                visual_intent=f"[MOCK] {role.value} visual", emotion="curiosity"))
        return ScriptOutput(hook=hook, segments=segments)


class Storyboarder(Agent):
    name, label, stage = "storyboarder", "Storyboarder", Status.STORYBOARD
    sections = frozenset({"storyboard"})
    output_model = StoryboardOutput
    temperature = 0.6  # pictures: some imagination, within the channel style

    def context(self, ep: Episode, s: Settings) -> dict[str, str]:
        lines = [f"- {g.segment_id} ({g.role.value}, {g.duration_sec:g}s): {g.text}"
                 + (f" [visual: {g.visual_intent}]" if g.visual_intent else "")
                 for g in ep.script.segments]
        plan = [f"- {g.segment_id}: {n} shot{'s' if n != 1 else ''} of about "
                f"{g.duration_sec / n:.1f}s"
                for g, n in zip(ep.script.segments, shots_per_segment(ep, s.shots_target),
                                strict=True)]
        return {"segments": "\n".join(lines) or "(no script yet)",
                "narration_sec": f"{ep.script.total_estimated_sec:g}",
                # the AI can't add up well: Python plans how many shots each segment gets
                "shot_plan": "\n".join(plan) or "(no script yet)"}

    def apply(self, ep: Episode, out: StoryboardOutput, s: Settings | None = None) -> Episode:
        """Shots start as planned. With settings, every prompt carries the channel style
        (added if the AI forgot it), so all pictures of a video look alike."""
        shots = []
        for x in out.shots:
            data = x.model_dump()
            if s is not None:
                data["generation_prompt"] = with_style(data["generation_prompt"], s.visual_style)
                data["visual_style"] = s.visual_style_name
            shots.append(Shot(**data, status=ShotStatus.PLANNED, asset_path=None))
        shots = fit_durations(shots, ep.script.total_estimated_sec)
        return ep.model_copy(update={"storyboard": Storyboard(shots=shots)})

    def current_answer(self, ep: Episode) -> dict | None:
        return {"shots": [x.model_dump(mode="json", exclude={"status", "asset_path", "visual_style"})
                          for x in ep.storyboard.shots]}

    def mock(self, ep: Episode, s: Settings) -> StoryboardOutput:
        """shots_target shots, spread over the segments by length (each segment gets >= 1)."""
        shots: list[ShotIn] = []
        for g, count in zip(ep.script.segments, shots_per_segment(ep, s.shots_target), strict=True):
            for k in range(count):
                n = len(shots) + 1
                shots.append(ShotIn(
                    shot_id=f"SHOT{n:02d}", duration_sec=round(g.duration_sec / count, 2),
                    segment_ids=[g.segment_id], purpose=g.role.value,
                    action=g.visual_intent or g.role.value, camera="slow push-in",
                    generation_prompt=f"[MOCK] Vertical 9:16 shot {k + 1} of {count}: "
                                      f"{g.visual_intent or g.text}. No text on screen."))
        return StoryboardOutput(shots=shots)


FIT_WITHIN = 0.25  # shot lengths off by at most 25% are scaled to the narration by Python


def fit_durations(shots: list, narration_sec: float) -> list:
    """AIs are bad at adding up: when the shot lengths miss the narration by a little
    (<= 25%), scale them so they add up exactly (keeping their proportions). A bigger
    difference is left alone so the rule shows it."""
    total = sum(x.duration_sec for x in shots)
    if not shots or total <= 0 or narration_sec <= 0 or total == narration_sec:
        return shots
    if abs(total - narration_sec) / narration_sec > FIT_WITHIN:
        return shots
    factor = narration_sec / total
    out = [x.model_copy(update={"duration_sec": round(x.duration_sec * factor, 2)})
           for x in shots]
    rest = round(narration_sec - sum(x.duration_sec for x in out[:-1]), 2)
    out[-1] = out[-1].model_copy(update={"duration_sec": rest})
    return out


def shots_per_segment(ep: Episode, target: int) -> list[int]:
    """How many shots each segment gets: proportional to its length, at least 1 each."""
    segs = ep.script.segments
    if not segs:
        return []
    target = max(target, len(segs))
    total = sum(g.duration_sec for g in segs)
    counts = [max(1, round(g.duration_sec / total * target)) for g in segs]
    while sum(counts) > target:  # take from the segment with the most shots per second
        spare = [j for j in range(len(segs)) if counts[j] > 1]
        if not spare:
            break
        i = max(spare, key=lambda j: counts[j] / segs[j].duration_sec)
        counts[i] -= 1
    while sum(counts) < target:  # give to the segment with the longest shots
        i = max(range(len(segs)), key=lambda j: segs[j].duration_sec / counts[j])
        counts[i] += 1
    return counts


AGENTS: dict[str, Agent] = {a.name: a for a in
                            (Researcher(), FactChecker(), Scriptwriter(), Storyboarder())}


def get_agent(name: str) -> Agent:
    if name not in AGENTS:
        raise StudioError(f"Unknown agent '{name}'. Choose from: {', '.join(AGENTS)}.")
    return AGENTS[name]


def agent_for_status(status: Status) -> Agent | None:
    return next((a for a in AGENTS.values() if a.stage == status), None)


# ------------------------------------------------------- parse, preview, run
def parse_response(agent: Agent, text: str):
    """Read a pasted answer for this agent."""
    return parse_answer(agent.output_model, text)


def parse_answer(model: type[Lenient], text: str):
    """Read a pasted answer. Tolerates ```json fences and chatter around the JSON."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise AgentOutputError(["No JSON object found. The answer must contain {...}."])
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise AgentOutputError([f"The JSON is broken near line {e.lineno}, column {e.colno}: "
                                f"{e.msg}."]) from None
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise AgentOutputError(_format_errors(e)) from None


def check_stage(agent: Agent, ep: Episode) -> None:
    if ep.status != agent.stage:
        raise StudioError(f"The {agent.label.lower()} works at {agent.stage.value}; "
                          f"{ep.episode_id} is at {ep.status.value}.")


def build(agent: Agent, ep: Episode, out, s: Settings | None = None,
          blocked: Sequence[dict] = ()) -> Episode:
    """The episode with the answer applied, fully re-validated. Nothing is saved.
    Research answers also get blocked sites and earlier-declined pages declined."""
    check_stage(agent, ep)
    try:
        new = agent.apply(ep, out, s)
        if agent.stage == Status.RESEARCH:
            new = declines.enforce(new, list(blocked))
        return Episode.model_validate(new.model_dump())
    except ValidationError as e:
        raise AgentOutputError(_format_errors(e)) from None


def preview(repo: EpisodeRepository, episode_id: str, agent: Agent, out
            ) -> tuple[Episode, list[Issue]]:
    """What the episode would look like, and what would still block the next step."""
    new = build(agent, repo.get_episode(episode_id), out, repo.settings, repo.blocked_sites())
    target = next_stage(new.status)
    return new, (validate_for_transition(new, target, repo.settings) if target else [])


def run_agent(repo: EpisodeRepository, episode_id: str, agent: Agent, out, *,
              actor: str, note: str) -> Episode:
    """Apply an answer and save it as a new version. Only the agent's sections may change."""
    new = build(agent, repo.get_episode(episode_id), out, repo.settings, repo.blocked_sites())
    return repo.save(new, actor=actor, note=f"{agent.name}: {note}",
                     allowed_sections=set(agent.sections))


def run_mock(repo: EpisodeRepository, episode_id: str, agent: Agent) -> Episode:
    ep = repo.get_episode(episode_id)
    check_stage(agent, ep)
    return run_agent(repo, episode_id, agent, agent.mock(ep, repo.settings),
                     actor=f"agent:{agent.name}", note="mock")


def run_mock_pipeline(repo: EpisodeRepository, episode_id: str) -> Episode:
    """FOR TESTING: fill each empty stage with mocks and walk the episode to PRODUCTION.

    The originality gate and every validator still apply. Approvals are recorded as actor
    'mock', and only [MOCK] claims are ever auto-approved: real research waits for you.
    """
    ep = repo.get_episode(episode_id)
    while ep.status in (Status.IDEA, Status.RESEARCH, Status.FACT_CHECK, Status.SCRIPT,
                        Status.STORYBOARD):
        if ep.status == Status.RESEARCH and not ep.research.claims:
            run_mock(repo, episode_id, AGENTS["researcher"])
        elif ep.status == Status.FACT_CHECK:
            # Real research is never touched: no mock notes, no mock approvals.
            real = [c.claim_id for c in ep.research.claims
                    if c.status != ClaimStatus.REJECTED and not c.statement.startswith("[MOCK]")]
            if real:
                raise StudioError(f"Stopped at FACT_CHECK: real claims need YOUR review "
                                  f"({', '.join(real)}). The mock pipeline only handles "
                                  f"[MOCK] research.")
            run_mock(repo, episode_id, AGENTS["fact_checker"])
            ep = repo.get_episode(episode_id)
            pending = [c for c in ep.research.claims if c.status == ClaimStatus.NEEDS_REVIEW]
            for c in pending:
                set_claim_status(repo, episode_id, c.claim_id, "approve", actor="mock",
                                 note="mock approval")
            set_fact_check(repo, episode_id, True, actor="mock", note="mock approval")
        elif ep.status == Status.SCRIPT and not ep.script.segments:
            run_mock(repo, episode_id, AGENTS["scriptwriter"])
        elif ep.status == Status.STORYBOARD and not ep.storyboard.shots:
            run_mock(repo, episode_id, AGENTS["storyboarder"])
        target = next_stage(ep.status)
        if target is None:  # pragma: no cover - the loop only runs on early stages
            break
        ep = repo.transition(episode_id, target, actor="mock")
    return ep
