"""Deterministic rules. NO AI in this file - plain Python checks only.

Each check_* function answers: "is the episode ready to ENTER the next stage?"
Moving forward runs the checks for the target stage AND every earlier stage, so
a problem introduced early can never slip through later.
"""
from collections.abc import Callable
from dataclasses import dataclass

from .config import Settings
from .models import (
    AssetKind,
    ClaimStatus,
    ContentKind,
    Episode,
    LinkStatus,
    ShotStatus,
    Status,
    latest_asset,
    subtitles_are_current,
    video_is_current,
)
from .state_machine import STAGE_ORDER, is_forward
from .textnorm import normalize_text
from .timeutil import EXAMPLE, local_now, parse_local


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    severity: str = "error"  # "error" blocks the transition, "warning" does not


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)


# ---------------------------------------------------------------- checks
def check_topic(ep: Episode, s: Settings) -> list[Issue]:
    if len(ep.metadata.topic.strip()) < 3:
        return [Issue("TOPIC_EMPTY", "The episode needs a topic (at least 3 characters).")]
    return check_angle(ep, s)


def check_angle(ep: Episode, s: Settings) -> list[Issue]:
    """Leaving IDEA: say what makes THIS video different. The scriptwriter and the
    originality check both use the angle.

    Only checked while the episode is still an idea: older episodes that are already
    past IDEA (when the angle is locked) are not blocked retroactively.
    """
    if ep.status != Status.IDEA:
        return []
    angle = ep.metadata.story_angle.strip()
    if len(angle) < s.min_story_angle_chars:
        return [Issue("ANGLE_MISSING",
                      f"Write a story angle (at least {s.min_story_angle_chars} characters): the "
                      "specific twist this video tells. Use 'Edit idea'.")]
    if normalize_text(angle) == normalize_text(ep.metadata.topic):
        return [Issue("ANGLE_SAME_AS_TOPIC",
                      "The story angle repeats the topic. Say what is surprising or specific.")]
    return []


def check_claims(ep: Episode, s: Settings) -> list[Issue]:
    """Entering FACT_CHECK: the research packet must be well formed."""
    issues: list[Issue] = []
    active = [c for c in ep.research.claims if c.status != ClaimStatus.REJECTED]
    if not active:
        return [Issue("NO_CLAIMS", "Research has no usable claims.")]
    sources = {src.source_id: src for src in ep.research.sources}
    for c in active:
        if not c.source_ids:
            issues.append(Issue("CLAIM_NO_SOURCE", f"Claim {c.claim_id} has no sources."))
            continue
        missing = [sid for sid in c.source_ids if sid not in sources]
        if missing:
            issues.append(Issue("SOURCE_MISSING",
                                f"Claim {c.claim_id} cites unknown sources: {', '.join(missing)}."))
        if c.importance.value == "high":
            # Only sources whose link works (or a human vouched for) count.
            publishers = {sources[sid].publisher.strip().lower()
                          for sid in c.source_ids if sid in sources and sources[sid].usable}
            if len(publishers) < s.high_importance_min_sources:
                issues.append(Issue(
                    "HIGH_CLAIM_WEAK_SOURCING",
                    f"High-importance claim {c.claim_id} needs {s.high_importance_min_sources} "
                    f"different publishers with working sources, has {len(publishers)}."))
    return issues + check_links(ep, s)


LINK_ISSUES = {
    LinkStatus.NOT_CHECKED: ("SOURCE_NOT_CHECKED", "{sid} has not been checked yet. Run 'Verify sources'."),
    LinkStatus.BROKEN: ("SOURCE_BROKEN", "{sid} is a broken link ({url}). Replace or remove it."),
    LinkStatus.BLOCKED: ("SOURCE_BLOCKED", "{sid} blocks automatic checks. Open {url} yourself and "
                                           "mark it as checked if it is fine."),
    LinkStatus.ERROR: ("SOURCE_CHECK_FAILED", "{sid} could not be checked ({url}). Try again, or open it "
                                                "yourself and mark it as checked."),
}


def check_links(ep: Episode, s: Settings) -> list[Issue]:
    """Every source an active claim relies on must load, or be vouched for by a human."""
    cited = {sid for c in ep.research.claims if c.status != ClaimStatus.REJECTED
             for sid in c.source_ids}
    issues = []
    for src in ep.research.sources:
        if src.source_id in cited and not src.usable:
            code, msg = LINK_ISSUES[src.link_status]
            issues.append(Issue(code, msg.format(sid=src.source_id, url=src.url)))
    return issues


def check_fact_gate(ep: Episode, s: Settings) -> list[Issue]:
    """Entering SCRIPT: the fact check must be finished and approved."""
    issues: list[Issue] = []
    if not ep.fact_check.approved:
        issues.append(Issue("FACT_CHECK_NOT_APPROVED", "Fact check has not approved this episode."))
    pending = [c.claim_id for c in ep.research.claims if c.status == ClaimStatus.NEEDS_REVIEW]
    if pending:
        issues.append(Issue("CLAIMS_PENDING",
                            f"Claims still need review: {', '.join(pending)}."))
    if not any(c.status == ClaimStatus.APPROVED for c in ep.research.claims):
        issues.append(Issue("NO_APPROVED_CLAIMS", "There are no approved claims to write from."))
    return issues


# Openers viewers have heard a thousand times: they say "an ordinary video" and get swiped.
GENERIC_OPENERS = ("what if", "have you ever", "did you know", "ever wondered", "ever wonder",
                   "imagine", "in this video", "today we", "today i", "let's talk", "let me tell",
                   "here's the story", "this is the story", "hey", "hi")


def check_hook(hook: str, s: Settings) -> list[Issue]:
    """The first line decides whether people stay (warnings: the human may disagree)."""
    issues: list[Issue] = []
    words = len(hook.split())
    if s.hook_max_words and words > s.hook_max_words:
        issues.append(Issue("HOOK_TOO_LONG", f"The hook has {words} words; at most "
                            f"{s.hook_max_words} are read in the first 2-3 seconds. Shorten it.",
                            "warning"))
    start = normalize_text(hook) + " "  # whole words only: "hi" is not "history"
    opener = next((o for o in GENERIC_OPENERS if start.startswith(normalize_text(o) + " ")), None)
    if opener:
        issues.append(Issue("HOOK_GENERIC", f"The hook opens with \"{opener}\", a "
                            "generic opener. Start with the most surprising concrete thing "
                            "about the subject instead.", "warning"))
    return issues


def check_script(ep: Episode, s: Settings) -> list[Issue]:
    """Entering STORYBOARD: every fact in the script traces to an approved claim."""
    issues: list[Issue] = []
    sc = ep.script
    if not sc.hook.strip():
        issues.append(Issue("HOOK_EMPTY", "The script has no hook."))
    else:
        issues += check_hook(sc.hook, s)
    if not sc.segments:
        return issues + [Issue("NO_SEGMENTS", "The script has no segments.")]
    claims = {c.claim_id: c for c in ep.research.claims}
    for seg in sc.segments:
        if not seg.sentences:
            issues.append(Issue("SEGMENT_EMPTY", f"Segment {seg.segment_id} has no sentences."))
        for n, sent in enumerate(seg.sentences, start=1):
            for cid in sent.claim_ids:
                claim = claims.get(cid)
                if claim is None:
                    issues.append(Issue("CLAIM_UNKNOWN",
                                        f"{seg.segment_id} sentence {n} cites unknown claim {cid}."))
                elif claim.status != ClaimStatus.APPROVED:
                    issues.append(Issue("CLAIM_NOT_APPROVED",
                                        f"{seg.segment_id} sentence {n} uses claim {cid} "
                                        f"which is {claim.status.value}."))
    if not any(x.kind == ContentKind.FACTUAL for seg in sc.segments for x in seg.sentences):
        issues.append(Issue("NO_FACTUAL_SENTENCES", "The script contains no factual sentences.",
                            "warning"))
    total = sc.total_estimated_sec
    if not (s.min_duration_sec <= total <= s.max_duration_sec):
        issues.append(Issue("DURATION_OUT_OF_RANGE",
                            f"Script is {total}s; allowed range is "
                            f"{s.min_duration_sec}-{s.max_duration_sec}s."))
    return issues


def check_storyboard(ep: Episode, s: Settings) -> list[Issue]:
    """Entering PRODUCTION: shots cover the whole script and are ready to generate."""
    issues: list[Issue] = []
    shots = ep.storyboard.shots
    if not shots:
        return [Issue("NO_SHOTS", "The storyboard has no shots.")]
    if len(shots) > s.shots_max:
        issues.append(Issue("TOO_MANY_SHOTS", f"{len(shots)} shots, but you can make at most "
                                              f"{s.shots_max} pictures a day. Merge some shots."))
    elif len(shots) < s.shots_target - 2:
        issues.append(Issue("FEW_SHOTS", f"Only {len(shots)} shots; about {s.shots_target} keep "
                                         "the video lively.", "warning"))
    seg_ids = {seg.segment_id for seg in ep.script.segments}
    covered: set[str] = set()
    for shot in shots:
        if not shot.segment_ids:
            issues.append(Issue("SHOT_NO_SEGMENT", f"Shot {shot.shot_id} supports no segment."))
        for sid in shot.segment_ids:
            if sid not in seg_ids:
                issues.append(Issue("SHOT_UNKNOWN_SEGMENT",
                                    f"Shot {shot.shot_id} references unknown segment {sid}."))
            covered.add(sid)
        if not shot.generation_prompt.strip():
            issues.append(Issue("SHOT_NO_PROMPT", f"Shot {shot.shot_id} has no generation prompt."))
    uncovered = sorted(seg_ids - covered)
    if uncovered:
        issues.append(Issue("SEGMENT_NO_SHOT",
                            f"Segments with no shot: {', '.join(uncovered)}."))
    shot_total = round(sum(sh.duration_sec for sh in shots), 2)
    narration = ep.script.total_estimated_sec
    if abs(shot_total - narration) > s.duration_tolerance_sec:
        issues.append(Issue("SHOT_DURATION_MISMATCH",
                            f"Shots add up to {shot_total}s but narration is {narration}s "
                            f"(tolerance {s.duration_tolerance_sec}s)."))
    return issues


def check_production(ep: Episode, s: Settings) -> list[Issue]:
    """Entering QA: every shot has its file, the narration fits, and the final video was
    built from exactly the current files. Uses the numbers measured at upload time."""
    issues: list[Issue] = []
    for shot in ep.storyboard.shots:
        if shot.status not in (ShotStatus.GENERATED, ShotStatus.APPROVED):
            issues.append(Issue("SHOT_NOT_READY",
                                f"Shot {shot.shot_id} has no picture or clip yet."))
        elif not shot.asset_path:
            issues.append(Issue("SHOT_NO_ASSET", f"Shot {shot.shot_id} has no asset_path."))

    voice = latest_asset(ep, AssetKind.VOICE)
    if voice is None:
        issues.append(Issue("NO_NARRATION", "Add the narration audio."))
    elif not s.length_ok(voice.duration_sec):
        issues.append(Issue("NARRATION_LENGTH",
                            f"The narration is {voice.duration_sec}s; it must be "
                            f"{s.min_duration_sec:g}-{s.max_duration_sec:g}s."))

    captions = latest_asset(ep, AssetKind.CAPTIONS)
    if captions is None:
        if s.subtitles_required:
            issues.append(Issue("NO_SUBTITLES", "Make the subtitles."))
    elif not subtitles_are_current(ep):
        issues.append(Issue("SUBTITLES_OUTDATED", "The narration changed after the subtitles "
                                                  "were made. Make them again."))

    video = latest_asset(ep, AssetKind.FINAL_VIDEO)
    if video is None:
        issues.append(Issue("NO_FINAL_VIDEO", "Build the video."))
    else:
        if not video_is_current(ep):
            issues.append(Issue("VIDEO_OUTDATED", "A shot or the narration changed after the "
                                                  "video was built. Build it again."))
        if not (video.width and video.height and video.height > video.width):
            issues.append(Issue("VIDEO_NOT_VERTICAL", "The video must be vertical (9:16)."))
        if not s.length_ok(video.duration_sec):
            issues.append(Issue("VIDEO_LENGTH", f"The video is {video.duration_sec}s; it must "
                                                f"be {s.min_duration_sec:g}-{s.max_duration_sec:g}s."))

    issues.extend(Issue("ASSET_NO_LICENSE", f"{a.asset_id} ({a.kind.value}) has no "
                                            "source/license note.")
                  for a in ep.assets if not a.license_note.strip())
    return issues


def check_qa(ep: Episode, s: Settings) -> list[Issue]:
    """Entering HUMAN_REVIEW: QA must have passed."""
    issues: list[Issue] = []
    if not ep.qa.approved:
        issues.append(Issue("QA_NOT_APPROVED", "QA has not approved this episode."))
    elif ep.qa.issues:
        issues.append(Issue("QA_HAS_NOTES", "QA approved but left notes - read them.", "warning"))
    return issues


def check_publication(ep: Episode, s: Settings) -> list[Issue]:
    """Entering PUBLISHED: the publication package is complete."""
    issues: list[Issue] = []
    if not ep.publication.title.strip():
        issues.append(Issue("PUB_NO_TITLE", "Publication title is empty."))
    if not ep.publication.platforms:
        issues.append(Issue("PUB_NO_PLATFORM", "No target platforms chosen."))
    if not ep.publication.urls:
        issues.append(Issue("PUB_NO_URL", "Paste the link of the published video."))
    return issues


def check_schedule(ep: Episode, s: Settings) -> list[Issue]:
    """Entering SCHEDULED: at least one go-live time, in YYYY-MM-DD HH:MM (local time)."""
    sched = ep.publication.schedule
    if not sched:
        return [Issue("NO_SCHEDULE", "Give the date and time the video goes live.")]
    issues = []
    for platform, when in sched.items():
        moment = parse_local(when)
        if moment is None:
            issues.append(Issue("BAD_SCHEDULE", f"{platform}: '{when}' is not like {EXAMPLE}."))
            continue
        if moment < local_now():
            issues.append(Issue("SCHEDULE_IN_PAST", f"{platform}: {when} is already in the past.",
                                "warning"))
    return issues


# Which checks must pass to ENTER each stage.
CHECKS_BY_STAGE: dict[Status, list[Callable[[Episode, Settings], list[Issue]]]] = {
    Status.RESEARCH: [check_topic],
    Status.FACT_CHECK: [check_claims],
    Status.SCRIPT: [check_fact_gate],
    Status.STORYBOARD: [check_script],
    Status.PRODUCTION: [check_storyboard],
    Status.QA: [check_production],
    Status.HUMAN_REVIEW: [check_qa],
    Status.APPROVED: [],
    Status.SCHEDULED: [check_publication, check_schedule],
    Status.PUBLISHED: [check_publication],
}


def validate_for_transition(ep: Episode, target: Status, settings: Settings) -> list[Issue]:
    """All issues that would block moving `ep` to `target`. Backward moves are not checked."""
    if not is_forward(ep.status, target):
        return []
    issues: list[Issue] = []
    for stage in STAGE_ORDER[: STAGE_ORDER.index(target) + 1]:
        if stage == Status.SCHEDULED and target != Status.SCHEDULED:
            continue  # publishing straight away does not need a schedule
        for check in CHECKS_BY_STAGE.get(stage, []):
            issues.extend(check(ep, settings))
    return issues
