"""The originality gate: is this idea meaningfully different from what we already made?

Three levels, from strict to loose (all deterministic, no AI required):
  1. Duplicate     - near-identical titles or reworded copies of a script
  2. Topic/angle   - overlapping subject; same subject with a clearly different angle is fine
  3. Facts/sources - reused claims and reused source links across episodes
An optional AI "semantic judge" can be plugged in to add a fourth signal.

Verdicts:  PASS (go ahead) | REVIEW (you decide) | SUGGEST_BLOCK (probably a repeat).
The gate only SUGGESTS. You always make the final call.
"""
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pydantic import Field

from .config import Settings
from .models import Episode, Status, Strict
from .similarity import containment, jaccard, shingle_similarity, strip_words, text_ratio, tokens
from .textnorm import normalize_text


class Verdict(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    SUGGEST_BLOCK = "SUGGEST_BLOCK"


class SemanticJudge(Protocol):
    """Plug-in point for an AI comparison (added in a later milestone)."""
    def similarity(self, text_a: str, text_b: str) -> float:  # 0.0 to 1.0
        ...


class OriginalityMatch(Strict):
    episode_id: str
    other_topic: str
    level: str          # duplicate | topic | angle | claims | sources | semantic
    kind: str           # short machine-readable label
    severity: str       # block | review | info
    score: float
    detail: str


class OriginalityReport(Strict):
    stage: str = "idea"
    verdict: Verdict = Verdict.PASS
    matches: list[OriginalityMatch] = Field(default_factory=list)
    compared_against: int = 0
    notes: list[str] = Field(default_factory=list)
    check_id: int | None = None


def fingerprint(ep: Episode) -> str:
    """What the check was based on. If this changes, the old check is stale."""
    return normalize_text(f"{ep.metadata.topic} | {ep.metadata.story_angle}")


def _script_text(ep: Episode) -> str:
    return " ".join(seg.text for seg in ep.script.segments)


def _active_claims(ep: Episode):
    return [c for c in ep.research.claims if c.status.value != "rejected"]


def _norm_url(url: str) -> str:
    return url.strip().lower().rstrip("/")


def _summary(ep: Episode) -> str:
    return f"{ep.metadata.topic}. {ep.metadata.story_angle}".strip()


@dataclass
class _Pair:
    """One comparison: the candidate `a` against an existing episode `b`."""
    a: Episode
    b: Episode
    s: Settings
    matches: list[OriginalityMatch] = field(default_factory=list)

    @property
    def ignore(self) -> frozenset[str]:
        """Series boilerplate ("the accidental invention of ...") says nothing about the
        subject, so it is left out of every comparison."""
        return frozenset(w.lower() for w in self.s.originality_ignore_words)

    def add(self, level: str, kind: str, severity: str, score: float, detail: str) -> None:
        self.matches.append(OriginalityMatch(
            episode_id=self.b.episode_id, other_topic=self.b.metadata.topic, level=level,
            kind=kind, severity=severity, score=round(score, 3), detail=detail))

    def title_ratio(self, x: str, y: str) -> float:
        return text_ratio(strip_words(x, self.ignore), strip_words(y, self.ignore))


def _duplicates(p: _Pair) -> bool:
    """Level 1: a nearly identical title, or a reworded copy of the script.
    Returns whether the title is a near duplicate (level 2 then says nothing more)."""
    a, b, s = p.a, p.b, p.s
    title_score = p.title_ratio(a.metadata.topic, b.metadata.topic)
    if a.publication.title and b.publication.title:
        title_score = max(title_score, p.title_ratio(a.publication.title, b.publication.title))
    title_dup = title_score >= s.originality_title_near_dup
    if title_dup:
        p.add("duplicate", "near_duplicate_title", "block", title_score,
              "Topic or title is nearly identical to this episode.")
    if a.script.segments and b.script.segments:
        script_score = shingle_similarity(_script_text(a), _script_text(b))
        if script_score >= s.originality_script_near_dup:
            p.add("duplicate", "near_duplicate_script", "block", script_score,
                  "Script wording overlaps heavily, even if reworded slightly.")
    return title_dup


def _topic_and_angle(p: _Pair, title_dup: bool) -> float:
    """Level 2: the same subject is fine with a clearly different angle. Returns the topic
    overlap (the optional AI judge only looks at loosely related pairs)."""
    a, b, s = p.a, p.b, p.s
    words_a, words_b = tokens(a.metadata.topic, p.ignore), tokens(b.metadata.topic, p.ignore)
    topic_score = jaccard(words_a, words_b)
    contain_score = containment(words_a, words_b)
    contained = contain_score >= s.originality_topic_contained
    angle_a, angle_b = a.metadata.story_angle.strip(), b.metadata.story_angle.strip()
    angle_score = (jaccard(tokens(angle_a, p.ignore), tokens(angle_b, p.ignore))
                   if angle_a and angle_b else None)
    angle_distinct = angle_score is not None and angle_score < s.originality_angle_similar

    if (topic_score >= s.originality_topic_overlap or contained) and not title_dup:
        if angle_distinct:
            p.add("topic", "same_subject_distinct_angle", "info", max(topic_score, contain_score),
                  "Same subject, but the story angle is clearly different. Usually fine.")
        elif topic_score >= s.originality_topic_near_dup:
            p.add("topic", "same_topic_same_angle", "block", topic_score,
                  "Same topic and no clearly different angle.")
        elif contained:
            if min(len(words_a), len(words_b)) >= s.originality_contained_block_words:
                p.add("topic", "topic_contained", "block", contain_score,
                      "Every word of the shorter topic is in this one, and no different angle.")
            else:
                p.add("topic", "topic_contained", "review", contain_score,
                      "This broad topic is already covered here. Write a story angle that "
                      "is clearly different, or review.")
        else:
            why = "no story angle written yet" if angle_score is None else "angles look similar"
            p.add("topic", "overlapping_topic", "review", topic_score,
                  f"Topics overlap and {why}. Write a distinct story angle or review.")
    elif angle_score is not None and angle_score >= s.originality_angle_review:
        p.add("angle", "similar_angle", "info", angle_score,
              "Different topic but a very similar story angle. Fine inside a series.")
    return topic_score


def _claims(p: _Pair) -> None:
    """Level 3a: facts already used in the other episode."""
    ca, cb = _active_claims(p.a), _active_claims(p.b)
    if not (ca and cb):
        return
    norm_b = [normalize_text(c.statement) for c in cb]
    shared = 0
    for c in ca:
        n = normalize_text(c.statement)
        if any(n == m or text_ratio(n, m) >= 0.9 for m in norm_b):
            shared += 1
    if not shared:
        return
    share = shared / min(len(ca), len(cb))
    if share >= p.s.originality_claim_block:
        p.add("claims", "heavy_claim_overlap", "block", share,
              f"{shared} fact(s) already used. This may be the same video in new words.")
    elif share >= p.s.originality_claim_review:
        p.add("claims", "shared_claims", "review", share, f"{shared} fact(s) already used.")
    else:
        p.add("claims", "few_shared_claims", "info", share, f"{shared} fact(s) already used.")


def _sources(p: _Pair) -> None:
    """Level 3b: source links already used in the other episode."""
    ua = {_norm_url(x.url) for x in p.a.research.sources}
    ub = {_norm_url(x.url) for x in p.b.research.sources}
    reused = ua & ub
    if reused:
        share = len(reused) / min(len(ua), len(ub))
        severity = "review" if share >= p.s.originality_source_review else "info"
        p.add("sources", "shared_sources", severity, share, f"{len(reused)} source link(s) reused.")


def compare(a: Episode, b: Episode, s: Settings, judge: SemanticJudge | None = None
            ) -> list[OriginalityMatch]:
    """Compare a candidate episode with one existing episode, level by level."""
    p = _Pair(a, b, s)
    title_dup = _duplicates(p)
    topic_score = _topic_and_angle(p, title_dup)
    _claims(p)
    _sources(p)
    # Optional AI semantic signal (only for loosely related pairs, to save cost)
    if judge is not None and topic_score >= 0.2:
        sem = float(judge.similarity(_summary(a), _summary(b)))
        if sem >= s.originality_semantic_review:
            p.add("semantic", "semantic_similarity", "review", sem,
                  "The AI judge thinks these stories are very similar.")
    return p.matches


def evaluate(candidate: Episode, library: list[Episode], s: Settings,
             judge: SemanticJudge | None = None, stage: str = "idea") -> OriginalityReport:
    """Compare a candidate against the whole library and produce a verdict."""
    matches: list[OriginalityMatch] = []
    compared = 0
    for other in library:
        if other.episode_id == candidate.episode_id:
            continue
        if other.status == Status.REJECTED and not s.originality_include_rejected:
            continue
        compared += 1
        matches.extend(compare(candidate, other, s, judge))

    order = {"block": 0, "review": 1, "info": 2}
    matches.sort(key=lambda m: (order[m.severity], -m.score))
    if any(m.severity == "block" for m in matches):
        verdict = Verdict.SUGGEST_BLOCK
    elif any(m.severity == "review" for m in matches):
        verdict = Verdict.REVIEW
    else:
        verdict = Verdict.PASS

    notes = []
    if compared == 0:
        notes.append("The library has no other episodes to compare against yet.")
    if not candidate.research.claims:
        notes.append("No claims yet, so fact overlap was not checked. Re-run after research.")
    return OriginalityReport(stage=stage, verdict=verdict, matches=matches,
                             compared_against=compared, notes=notes)
