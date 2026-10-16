from helpers import open_gate

from studio.dashboard_data import (
    allowed_moves,
    claim_rows,
    episode_summary,
    match_rows,
    next_stage,
    pipeline_counts,
    readiness,
    script_rows,
    shot_rows,
    stage_for,
)
from studio.models import Category, Status


def test_pipeline_counts_includes_zeros_in_order(repo, full_episode):
    repo.create_episode("Another one", Category.SCIENCE)
    counts = dict(pipeline_counts(repo))
    assert counts["IDEA"] == 2
    assert counts["PUBLISHED"] == 0
    assert next(s for s, _ in pipeline_counts(repo)) == "IDEA"
    assert [s for s, _ in pipeline_counts(repo)][-1] == "REJECTED"


def test_next_stage():
    assert next_stage(Status.IDEA) == Status.RESEARCH
    assert next_stage(Status.APPROVED) == Status.SCHEDULED  # optional, see the state machine
    assert next_stage(Status.SCHEDULED) == Status.PUBLISHED
    assert next_stage(Status.PUBLISHED) is None
    assert next_stage(Status.REJECTED) is None


def test_readiness_reports_blockers(repo):
    ep = repo.create_episode("Empty idea")
    open_gate(repo, ep.episode_id)
    ep = repo.transition(ep.episode_id, Status.RESEARCH, actor="test")
    target, issues = readiness(ep, repo)
    assert target == Status.FACT_CHECK
    assert any(i.code == "NO_CLAIMS" for i in issues)


def test_readiness_includes_originality_gate(repo, full_episode):
    target, issues = readiness(full_episode, repo)
    assert target == Status.RESEARCH
    assert [i.code for i in issues] == ["ORIGINALITY_NOT_CHECKED"]
    open_gate(repo, full_episode.episode_id)
    _, issues = readiness(full_episode, repo)
    assert [i for i in issues if i.severity == "error"] == []


def test_stage_for():
    assert stage_for(Status.IDEA) == "idea"
    assert stage_for(Status.FACT_CHECK) == "research"
    assert stage_for(Status.QA) == "script"


def test_match_rows(repo):
    repo.create_episode("The accidental invention of the microwave")
    b = repo.create_episode("The accidental invention of the microwave")
    rows = match_rows(repo.check_originality(b.episode_id))
    assert rows[0]["severity"] == "block" and rows[0]["kind"] == "near_duplicate_title"
    assert rows[0]["episode"].startswith("EP_000001")


def test_allowed_moves_flags_reasons():
    moves = {m["status"]: m["needs_reason"] for m in allowed_moves(Status.FACT_CHECK)}
    assert moves == {Status.SCRIPT: False, Status.RESEARCH: True, Status.REJECTED: True}
    assert allowed_moves(Status.PUBLISHED) == []
    assert allowed_moves(Status.REJECTED) == [{"status": Status.IDEA, "needs_reason": True}]


def test_rows_trace_facts(full_episode):
    claims = claim_rows(full_episode)
    assert claims[0]["claim_id"] == "C001"
    assert "NobelPrize.org" in claims[0]["sources"]
    sentences = script_rows(full_episode)
    assert any(r["kind"] == "factual" and r["claims"] for r in sentences)
    assert len(shot_rows(full_episode)) == len(full_episode.storyboard.shots)


def test_episode_summary(full_episode):
    s = episode_summary(full_episode)
    assert s["claims"] == 3 and s["approved_claims"] == 3
    assert s["script_sec"] == full_episode.script.total_estimated_sec
