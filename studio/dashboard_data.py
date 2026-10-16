"""Plain-Python helpers behind the Streamlit dashboard.

The dashboard (dashboard.py) only draws widgets. Everything it shows is computed here,
so it can be tested without Streamlit and the rules stay in one place.
"""
from .db import GATED_MOVES, EpisodeRepository
from .models import Episode, Status
from .originality import OriginalityReport
from .state_machine import ALLOWED, STAGE_ORDER, is_backward, next_stage
from .validators import Issue, validate_for_transition

# Order used for the pipeline overview: the happy path, then REJECTED at the end.
PIPELINE_ORDER: list[Status] = STAGE_ORDER + [Status.REJECTED]


def pipeline_counts(repo: EpisodeRepository) -> list[tuple[str, int]]:
    """(status, count) for every status in pipeline order, including zeros."""
    counts = repo.count_by_status()
    return [(s.value, counts.get(s.value, 0)) for s in PIPELINE_ORDER]


def readiness(ep: Episode, repo: EpisodeRepository) -> tuple[Status | None, list[Issue]]:
    """What the next forward step is, and every rule currently blocking or warning about it."""
    target = next_stage(ep.status)
    if target is None:
        return None, []
    issues = validate_for_transition(ep, target, repo.settings)
    if (ep.status, target) in GATED_MOVES:
        issues = repo.originality_issues(ep.episode_id) + issues
    return target, issues


# Which originality-check stage fits where the episode is in the pipeline.
def stage_for(status: Status) -> str:
    if status in (Status.IDEA, Status.REJECTED):
        return "idea"
    if status in (Status.RESEARCH, Status.FACT_CHECK):
        return "research"
    return "script"


GATE_STATE_TEXT = {
    "none": "Not checked yet",
    "stale": "Out of date: the topic or angle changed since the check",
    "pass": "Passed",
    "accepted": "Flagged, accepted by you",
    "needs_review": "Flagged, needs your decision",
    "declined": "Declined by you as not original enough",
}


def match_rows(report: OriginalityReport) -> list[dict]:
    return [{
        "severity": m.severity,
        "episode": f"{m.episode_id} · {m.other_topic}",
        "level": m.level,
        "kind": m.kind,
        "score": m.score,
        "detail": m.detail,
    } for m in report.matches]


def allowed_moves(status: Status) -> list[dict]:
    """Legal targets from `status`, in pipeline order, with whether each needs a reason."""
    moves = []
    for target in PIPELINE_ORDER:
        if target not in ALLOWED[status]:
            continue
        needs_reason = (Status.REJECTED in (target, status) or is_backward(status, target))
        moves.append({"status": target, "needs_reason": needs_reason})
    return moves


def claim_rows(ep: Episode) -> list[dict]:
    publishers = {s.source_id: s.publisher for s in ep.research.sources}
    return [{
        "claim_id": c.claim_id,
        "status": c.status.value,
        "importance": c.importance.value,
        "statement": c.statement,
        "sources": ", ".join(f"{sid} ({publishers.get(sid, 'unknown')})" for sid in c.source_ids),
        "notes": c.notes,
    } for c in ep.research.claims]


LINK_LABEL = {"ok": "✅ ok", "mock": "🧪 mock", "broken": "❌ broken", "blocked": "🔒 blocked",
              "error": "⚠️ error", "not_checked": "⏳ not checked"}


def link_label(s) -> str:
    """Link status in words, plus whether a human vouched for the page."""
    label = LINK_LABEL[s.link_status.value]
    return f"{label} · ✔ checked by {s.verified_by}" if s.verified_by else label


def source_rows(ep: Episode) -> list[dict]:
    """Sources still in use (declined ones are listed by declined_rows)."""
    return [{
        "source_id": s.source_id,
        "link": link_label(s),
        "publisher": s.publisher,
        "title": s.title,
        "type": s.source_type.value,
        "reliability": s.reliability.value,
        "url": s.url,
        "note": s.verified_note or s.link_note,
    } for s in ep.research.sources if not s.declined]


def declined_rows(ep: Episode) -> list[dict]:
    return [{"source_id": s.source_id, "publisher": s.publisher, "url": s.url,
             "declined by": s.declined_by, "why": s.declined_note}
            for s in ep.research.sources if s.declined]


def sources_needing_a_human(ep: Episode) -> list:
    """Cited sources the link check could not confirm (blocked, failed or broken)."""
    cited = {sid for c in ep.research.claims if c.status.value != "rejected" for sid in c.source_ids}
    return [s for s in ep.research.sources
            if s.source_id in cited and not s.usable and s.link_status.value != "not_checked"]


def script_rows(ep: Episode) -> list[dict]:
    """One row per sentence, so every fact can be traced to its claims."""
    rows = []
    for seg in ep.script.segments:
        for n, sent in enumerate(seg.sentences, start=1):
            rows.append({
                "segment": seg.segment_id,
                "role": seg.role.value,
                "#": n,
                "kind": sent.kind.value,
                "text": sent.text,
                "claims": ", ".join(sent.claim_ids),
            })
    return rows


def shot_rows(ep: Episode) -> list[dict]:
    return [{
        "shot_id": sh.shot_id,
        "status": sh.status.value,
        "duration_sec": sh.duration_sec,
        "segments": ", ".join(sh.segment_ids),
        "purpose": sh.purpose,
        "asset_path": sh.asset_path or "",
    } for sh in ep.storyboard.shots]


def episode_summary(ep: Episode) -> dict:
    return {
        "claims": len(ep.research.claims),
        "approved_claims": sum(1 for c in ep.research.claims if c.status.value == "approved"),
        "sources": len(ep.research.sources),
        "segments": len(ep.script.segments),
        "script_sec": ep.script.total_estimated_sec,
        "shots": len(ep.storyboard.shots),
        "shot_sec": round(sum(sh.duration_sec for sh in ep.storyboard.shots), 2),
        "cost_usd": round(sum(a.cost_usd for a in ep.assets), 2),
    }
