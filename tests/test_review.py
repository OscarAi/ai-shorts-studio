import json

import pytest
from helpers import open_gate

from studio.agents import get_agent, parse_response, run_agent, run_mock_pipeline
from studio.errors import StudioError
from studio.models import ClaimStatus, Status
from studio.review import set_claim_status, set_claim_statuses, set_fact_check
from studio.sources import mark_source_verified


def idea(repo, topic="The accidental invention of velcro"):
    ep = repo.create_episode(topic)
    open_gate(repo, ep.episode_id)
    return ep


def test_claim_approval_is_recorded_with_actor_and_note(repo, full_episode):
    ep = set_claim_status(repo, full_episode.episode_id, "C002", "reject",
                          actor="human", note="genus wording is too loose")
    claim = next(c for c in ep.research.claims if c.claim_id == "C002")
    assert claim.status == ClaimStatus.REJECTED
    assert "Reviewer (human): genus wording is too loose" in claim.notes
    assert repo.list_versions(ep.episode_id)[-1]["change_note"].startswith("claim C002 -> rejected")


def test_unknown_claim_and_bad_decision_are_refused(repo, full_episode):
    with pytest.raises(StudioError):
        set_claim_status(repo, full_episode.episode_id, "C999", "approve", actor="human")
    with pytest.raises(StudioError):
        set_claim_status(repo, full_episode.episode_id, "C001", "maybe", actor="human")


def test_fact_check_approval(repo, full_episode):
    ep = set_fact_check(repo, full_episode.episode_id, False, actor="human")
    assert ep.fact_check.approved is False
    assert set_fact_check(repo, ep.episode_id, True, actor="human").fact_check.approved


def test_mock_pipeline_walks_to_production(repo):
    ep = run_mock_pipeline(repo, idea(repo).episode_id)
    assert ep.status == Status.PRODUCTION
    assert all(c.status == ClaimStatus.APPROVED for c in ep.research.claims)
    approvals = [v for v in repo.list_versions(ep.episode_id) if "approved" in v["change_note"]]
    assert approvals and all(v["actor"] == "mock" for v in approvals)


def test_mock_pipeline_respects_the_originality_gate(repo):
    ep = repo.create_episode("Never checked")
    with pytest.raises(StudioError, match="ORIGINALITY_NOT_CHECKED"):
        run_mock_pipeline(repo, ep.episode_id)


def test_mock_pipeline_never_approves_real_claims(repo):
    ep = idea(repo)
    repo.transition(ep.episode_id, Status.RESEARCH, actor="test")
    answer = {"sources": [{"source_id": "S001", "title": "t", "publisher": "Smithsonian",
                           "url": "https://example.com/x"}],
              "claims": [{"claim_id": "C001", "statement": "A real fact.",
                          "source_ids": ["S001"]}]}
    agent = get_agent("researcher")
    run_agent(repo, ep.episode_id, agent, parse_response(agent, json.dumps(answer)),
              actor="human", note="manual paste")
    mark_source_verified(repo, ep.episode_id, "S001", actor="human", note="opened it")
    with pytest.raises(StudioError, match="need YOUR review"):
        run_mock_pipeline(repo, ep.episode_id)
    after = repo.get_episode(ep.episode_id)
    assert after.status == Status.FACT_CHECK
    assert after.research.claims[0].status == ClaimStatus.NEEDS_REVIEW


def test_mock_pipeline_never_approves_fact_check_for_real_research(repo):
    """Even when you approved every real claim yourself, the fact check stays yours."""
    ep = idea(repo)
    repo.transition(ep.episode_id, Status.RESEARCH, actor="test")
    answer = {"sources": [{"source_id": "S001", "title": "t", "publisher": "Smithsonian",
                           "url": "https://example.com/x"}],
              "claims": [{"claim_id": "C001", "statement": "A real fact.",
                          "source_ids": ["S001"]}]}
    agent = get_agent("researcher")
    run_agent(repo, ep.episode_id, agent, parse_response(agent, json.dumps(answer)),
              actor="human", note="manual paste")
    mark_source_verified(repo, ep.episode_id, "S001", actor="human", note="opened it")
    repo.transition(ep.episode_id, Status.FACT_CHECK, actor="test")
    set_claim_status(repo, ep.episode_id, "C001", "approve", actor="human")
    with pytest.raises(StudioError, match="need YOUR review"):
        run_mock_pipeline(repo, ep.episode_id)
    after = repo.get_episode(ep.episode_id)
    assert after.fact_check.approved is False
    assert "AI check" not in after.research.claims[0].notes


def test_a_whole_review_is_one_version(repo, full_episode):
    before = repo.get_episode(full_episode.episode_id).version
    ep = set_claim_statuses(repo, full_episode.episode_id,
                            {"C001": ("approve", ""), "C002": ("reject", "too loose"),
                             "C003": ("reset", "")}, actor="human")
    assert ep.version == before + 1
    status = {c.claim_id: c.status for c in ep.research.claims}
    assert status == {"C001": ClaimStatus.APPROVED, "C002": ClaimStatus.REJECTED,
                      "C003": ClaimStatus.NEEDS_REVIEW}
    note = repo.list_versions(ep.episode_id)[-1]["change_note"]
    assert note == ("claim C001 -> approved; claim C002 -> rejected: too loose; "
                    "claim C003 -> needs_review")
    with pytest.raises(StudioError, match="has no claim C009"):
        set_claim_statuses(repo, ep.episode_id, {"C009": ("approve", "")}, actor="human")
    with pytest.raises(StudioError, match="Decision must be one of"):
        set_claim_statuses(repo, ep.episode_id, {"C001": ("maybe", "")}, actor="human")
