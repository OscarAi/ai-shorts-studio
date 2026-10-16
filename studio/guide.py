"""The guide: what to do next for an episode, in plain words, with ticks.

Used by the dashboard (the "What to do now" box and the one-click Continue button) and by
the CLI (`python cli.py next EP_x`). It only READS the episode; the rules that actually
allow or block a move stay in validators.py / db.py.

A tick means "the rule for this is satisfied": most steps are ticked from the rules' own
results (the issue codes of validate_for_transition), so the checklist can never disagree
with what blocks the Continue button.
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from .dashboard_data import readiness
from .db import EpisodeRepository
from .models import AssetKind, ClaimStatus, Episode, Status, latest_asset
from .platforms import PLATFORMS
from .state_machine import STAGE_ORDER
from .timeutil import has_passed
from .validators import Issue

STAGE_TITLES = {
    Status.IDEA: "Idea: make it original",
    Status.RESEARCH: "Research: find the facts and their sources",
    Status.FACT_CHECK: "Fact check: you approve every fact",
    Status.SCRIPT: "Script: write the narration",
    Status.STORYBOARD: "Storyboard: plan the shots",
    Status.PRODUCTION: "Production: pictures, narration, subtitles, video",
    Status.QA: "QA: watch the video and check it",
    Status.HUMAN_REVIEW: "Final review",
    Status.APPROVED: "Publish or schedule",
    Status.SCHEDULED: "Scheduled: waiting to go live",
    Status.PUBLISHED: "Published",
    Status.REJECTED: "Rejected",
}
SHORT_NAMES = {
    Status.IDEA: "Idea", Status.RESEARCH: "Research", Status.FACT_CHECK: "Fact check",
    Status.SCRIPT: "Script", Status.STORYBOARD: "Storyboard", Status.PRODUCTION: "Production",
    Status.QA: "QA", Status.HUMAN_REVIEW: "Review", Status.APPROVED: "Approved",
    Status.SCHEDULED: "Scheduled", Status.PUBLISHED: "Published",
}

# Problems that can only be fixed back at PRODUCTION (files, narration, subtitles, video).
PRODUCTION_CODES = {"SHOT_NOT_READY", "SHOT_NO_ASSET", "NO_NARRATION", "NARRATION_LENGTH",
                    "NO_SUBTITLES", "SUBTITLES_OUTDATED", "NO_FINAL_VIDEO", "VIDEO_OUTDATED",
                    "VIDEO_NOT_VERTICAL", "VIDEO_LENGTH", "ASSET_NO_LICENSE"}
AFTER_PRODUCTION = (Status.QA, Status.HUMAN_REVIEW, Status.APPROVED)
LINK_CODES = ("SOURCE_NOT_CHECKED", "SOURCE_BROKEN", "SOURCE_BLOCKED", "SOURCE_CHECK_FAILED")


@dataclass
class Step:
    label: str
    done: bool
    optional: bool = False


@dataclass
class Guide:
    status: Status
    title: str
    stage_number: int | None          # 1..11 on the happy path, None when rejected
    steps: list[Step] = field(default_factory=list)
    target: Status | None = None      # where "Continue" goes
    blockers: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    live_on: list[str] = field(default_factory=list)  # SCHEDULED: platforms already live

    @property
    def ready(self) -> bool:
        return self.target is not None and not self.blockers

    @property
    def go_back_to(self) -> Status | None:
        """A stage to return to when what blocks us can only be fixed there
        (e.g. a rule added later, like subtitles, for a video already built)."""
        if self.status in AFTER_PRODUCTION and any(i.code in PRODUCTION_CODES
                                                    for i in self.blockers):
            return Status.PRODUCTION
        return None

    @property
    def headline(self) -> str:
        """One line for lists: what is next, or that it can move on."""
        if self.target is None:
            return self.title
        if self.status == Status.SCHEDULED:
            if self.live_on:
                return f"Live on {', '.join(self.live_on)}: mark it as published"
            waiting = [s for s in self.steps[:-1] if not s.done]
            return waiting[0].label if waiting else "Should be live now: confirm it"
        if self.ready:
            return f"Ready for {SHORT_NAMES.get(self.target, self.target.value)}"
        if self.go_back_to:  # first things first: checking this stage is pointless until then
            fixable = next(i for i in self.blockers if i.code in PRODUCTION_CODES)
            return f"Back to {SHORT_NAMES[self.go_back_to]}: {fixable.message}"
        todo = next((s.label for s in self.steps if not s.done and not s.optional), None)
        return todo or (self.blockers[0].message if self.blockers else "Not ready yet")


# ------------------------------------------------------------------ one checklist per stage
@dataclass
class _Context:
    """What a stage's checklist may look at: the episode, and the codes of the rules that
    currently fail for its next step."""
    ep: Episode
    repo: EpisodeRepository
    codes: set[str]

    def clear(self, *codes: str) -> bool:
        """None of these rules currently fails."""
        return not self.codes.intersection(codes)


def _active_claims(ep: Episode) -> list:
    return [x for x in ep.research.claims if x.status != ClaimStatus.REJECTED]


def _idea(c: _Context) -> list[Step]:
    gate = c.repo.originality_gate_state(c.ep.episode_id)
    steps = [Step("Write the story angle (what makes this video different)",
                  c.clear("ANGLE_MISSING", "ANGLE_SAME_AS_TOPIC")),
             Step("Run the originality check", gate not in ("none", "stale"))]
    if gate in ("needs_review", "declined"):
        steps.append(Step("Accept or reject the flagged idea", False))
    return steps


def _research(c: _Context) -> list[Step]:
    claims = _active_claims(c.ep)
    cited = any(x.source_ids for x in claims)
    return [Step("Paste the researcher's answer (or use a mock)", bool(claims)),
            Step("Verify the sources", cited and c.clear("SOURCE_NOT_CHECKED")),
            Step("Fix broken links / check blocked pages yourself",
                 cited and c.clear(*LINK_CODES))]


def _fact_check(c: _Context) -> list[Step]:
    claims = _active_claims(c.ep)
    return [Step("Get the AI fact-check suggestions",
                 any("AI check:" in x.notes for x in claims), optional=True),
            Step("Approve or reject every claim", bool(claims) and c.clear("CLAIMS_PENDING")),
            Step("Tick \"Fact check approved\"", c.clear("FACT_CHECK_NOT_APPROVED"))]


def _script(c: _Context) -> list[Step]:
    s, written = c.repo.settings, bool(c.ep.script.segments)
    return [Step("Paste the scriptwriter's answer", written),
            Step(f"Script length {s.min_duration_sec:g}-{s.max_duration_sec:g} s",
                 written and c.clear("DURATION_OUT_OF_RANGE"))]


def _storyboard(c: _Context) -> list[Step]:
    planned = bool(c.ep.storyboard.shots)
    return [Step("Paste the storyboarder's answer", planned),
            Step("Shot lengths add up to the narration",
                 planned and c.clear("SHOT_DURATION_MISMATCH"))]


def _production(c: _Context) -> list[Step]:
    s, shots = c.repo.settings, c.ep.storyboard.shots
    filled = sum(1 for x in shots if x.asset_path)
    steps = [Step(f"A picture for every shot ({filled} of {len(shots)})",
                  bool(shots) and c.clear("SHOT_NOT_READY", "SHOT_NO_ASSET")),
             Step(f"Narration ({s.min_duration_sec:g}-{s.max_duration_sec:g} s)",
                  c.clear("NO_NARRATION", "NARRATION_LENGTH"))]
    if s.subtitles_required or latest_asset(c.ep, AssetKind.CAPTIONS) is not None:
        steps.append(Step("Subtitles", c.clear("NO_NARRATION", "NO_SUBTITLES",
                                                "SUBTITLES_OUTDATED")))
    steps.append(Step("Build the video", c.clear("NO_FINAL_VIDEO", "VIDEO_OUTDATED")))
    return steps


def _qa(c: _Context) -> list[Step]:
    return [Step("Watch the video with sound and tick the QA checklist",
                 c.clear("QA_NOT_APPROVED"))]


def _human_review(c: _Context) -> list[Step]:
    return [Step("Watch it one last time; continue when you're happy", True),
            Step("Prepare the title and description", bool(c.ep.publication.title),
                 optional=True)]


def _approved(c: _Context) -> list[Step]:
    pub = c.ep.publication
    return [Step("Title and platforms", bool(pub.title and pub.platforms)),
            Step("Download the MP4 and upload it to each platform yourself", bool(pub.urls)),
            Step("Paste the links: mark as scheduled, or as published if it's live now",
                 bool(pub.urls))]


def _live_on(ep: Episode) -> list[str]:
    """Platforms posted on right away (a link with a go-live time, not a schedule)."""
    return [p for p in ep.publication.live_at if p not in ep.publication.schedule]


def _scheduled(c: _Context) -> list[Step]:
    pub = c.ep.publication
    steps = [Step(f"{PLATFORMS.get(p, p)} is live", True) for p in _live_on(c.ep)]
    steps += [Step(f"{PLATFORMS.get(p, p)} goes live {when}", has_passed(when))
              for p, when in sorted(pub.schedule.items(), key=lambda x: x[1])]
    return steps + [Step("When it's live anywhere, click \"It's live\" (the others keep "
                         "their times)", False)]


def _rejected(c: _Context) -> list[Step]:
    return [Step("If you change your mind: revive it to Idea (under Other moves)", False,
                 optional=True)]


STEPS: dict[Status, Callable[[_Context], list[Step]]] = {
    Status.IDEA: _idea, Status.RESEARCH: _research, Status.FACT_CHECK: _fact_check,
    Status.SCRIPT: _script, Status.STORYBOARD: _storyboard, Status.PRODUCTION: _production,
    Status.QA: _qa, Status.HUMAN_REVIEW: _human_review, Status.APPROVED: _approved,
    Status.SCHEDULED: _scheduled, Status.REJECTED: _rejected,
}


def guide(ep: Episode, repo: EpisodeRepository) -> Guide:
    target, issues = readiness(ep, repo)
    steps = STEPS[ep.status](_Context(ep, repo, {i.code for i in issues})) \
        if ep.status in STEPS else []
    if ep.status == Status.APPROVED:
        target = None  # publishing happens with "Mark as published" (it needs the links)
    return Guide(
        status=ep.status, title=STAGE_TITLES[ep.status],
        stage_number=STAGE_ORDER.index(ep.status) + 1 if ep.status in STAGE_ORDER else None,
        steps=steps, target=target,
        blockers=[i for i in issues if i.severity == "error"] if target else [],
        warnings=[i for i in issues if i.severity != "error"] if target else [],
        live_on=([PLATFORMS.get(p, p) for p in _live_on(ep)]
                 if ep.status == Status.SCHEDULED else []))


def stepper(status: Status) -> str:
    """'✅ Idea → ✅ Research → 🔵 Fact check → ○ Script → ...' for the top of the page."""
    if status not in STAGE_ORDER:
        return "⛔ Rejected"
    now = STAGE_ORDER.index(status)
    marks = []
    for n, stage in enumerate(STAGE_ORDER):
        mark = "✅" if n < now or status == Status.PUBLISHED else ("🔵" if n == now else "○")
        name = SHORT_NAMES[stage]
        marks.append(f"{mark} **{name}**" if n == now else f"{mark} {name}")
    return " → ".join(marks)
