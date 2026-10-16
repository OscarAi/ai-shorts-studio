import json

import pytest
from helpers import open_gate

from studio.agents import (
    AGENTS,
    agent_for_status,
    get_agent,
    parse_response,
    preview,
    run_agent,
    run_mock,
    run_mock_pipeline,
)
from studio.errors import AgentOutputError, StudioError
from studio.models import ClaimStatus, Status


def at_research(repo, topic="The accidental invention of the microwave"):
    ep = repo.create_episode(topic)
    open_gate(repo, ep.episode_id)
    return repo.transition(ep.episode_id, Status.RESEARCH, actor="test")


def approve_all(repo, episode_id):
    ep = repo.get_episode(episode_id)
    claims = [c.model_copy(update={"status": ClaimStatus.APPROVED}) for c in ep.research.claims]
    ep = ep.model_copy(update={
        "research": ep.research.model_copy(update={"claims": claims}),
        "fact_check": ep.fact_check.model_copy(update={"approved": True})})
    return repo.save(ep, actor="test", allowed_sections={"research", "fact_check"})


RESEARCH_ANSWER = {
    "sources": [
        {"source_id": "S001", "title": "Percy Spencer", "publisher": "Smithsonian",
         "url": "https://example.com/a", "confidence": 0.9},          # extra field: ignored
        {"source_id": "S002", "title": "Raytheon history", "publisher": "Raytheon",
         "url": "https://example.com/b"}],
    "claims": [
        {"claim_id": "C001", "statement": "Percy Spencer noticed a melted candy bar.",
         "importance": "high", "source_ids": ["S001", "S002"], "status": "approved"}],
}


# ---------------------------------------------------------------- parsing
def test_parse_tolerates_fences_and_chatter():
    text = "Sure! Here you go:\n```json\n" + json.dumps(RESEARCH_ANSWER) + "\n```\nHope it helps."
    out = parse_response(get_agent("researcher"), text)
    assert out.claims[0].claim_id == "C001"


def test_parse_reports_broken_json():
    with pytest.raises(AgentOutputError) as err:
        parse_response(get_agent("researcher"), '{"claims": [}')
    assert "broken" in err.value.problems[0]


def test_parse_reports_missing_fields_with_location():
    bad = {"sources": [], "claims": [{"claim_id": "C001"}]}
    with pytest.raises(AgentOutputError) as err:
        parse_response(get_agent("researcher"), json.dumps(bad))
    assert any(p.startswith("claims[0].statement") for p in err.value.problems)


def test_parse_reports_no_json():
    with pytest.raises(AgentOutputError):
        parse_response(get_agent("researcher"), "I could not find anything.")


# ----------------------------------------------------------- safety rules
def test_researcher_can_never_approve_claims(repo):
    ep = at_research(repo)
    out = parse_response(get_agent("researcher"), json.dumps(RESEARCH_ANSWER))
    saved = run_agent(repo, ep.episode_id, get_agent("researcher"), out,
                      actor="human", note="manual paste")
    assert saved.research.claims[0].status == ClaimStatus.NEEDS_REVIEW
    assert saved.research.sources[0].accessed_date  # filled in by code


def test_researcher_keeps_approved_claims_and_renumbers(repo):
    ep = at_research(repo)
    run_mock(repo, ep.episode_id, get_agent("researcher"))
    ep = repo.get_episode(ep.episode_id)
    first = ep.research.claims[0].model_copy(update={"status": ClaimStatus.APPROVED})
    ep = repo.save(ep.model_copy(update={"research": ep.research.model_copy(
        update={"claims": [first] + ep.research.claims[1:]})}), actor="human")

    out = parse_response(get_agent("researcher"), json.dumps(RESEARCH_ANSWER))
    saved = run_agent(repo, ep.episode_id, get_agent("researcher"), out, actor="human", note="x")
    ids = [c.claim_id for c in saved.research.claims]
    assert ids[0] == "C001" and saved.research.claims[0].statement.startswith("[MOCK]")
    assert len(ids) == len(set(ids)) == 2  # the new C001 was renumbered
    new = saved.research.claims[1]
    known_sources = {x.source_id for x in saved.research.sources}
    assert set(new.source_ids) <= known_sources


def test_fact_checker_never_changes_statuses(repo):
    ep = at_research(repo)
    run_mock(repo, ep.episode_id, get_agent("researcher"))
    repo.transition(ep.episode_id, Status.FACT_CHECK, actor="test")
    answer = {"verdicts": [{"claim_id": "C001", "verdict": "unsupported", "reason": "Myth."},
                           {"claim_id": "C002", "verdict": "supported"}]}
    out = parse_response(get_agent("fact_checker"), json.dumps(answer))
    saved = run_agent(repo, ep.episode_id, get_agent("fact_checker"), out, actor="human", note="x")
    assert all(c.status == ClaimStatus.NEEDS_REVIEW for c in saved.research.claims)
    assert "AI check: unsupported - Myth." in saved.research.claims[0].notes
    assert saved.fact_check.approved is False
    assert any("C001" in i for i in saved.fact_check.issues)


def test_fact_checker_rejects_unknown_claims(repo):
    ep = at_research(repo)
    run_mock(repo, ep.episode_id, get_agent("researcher"))
    repo.transition(ep.episode_id, Status.FACT_CHECK, actor="test")
    out = parse_response(get_agent("fact_checker"),
                         '{"verdicts": [{"claim_id": "C999", "verdict": "supported"}]}')
    with pytest.raises(AgentOutputError):
        run_agent(repo, ep.episode_id, get_agent("fact_checker"), out, actor="human", note="x")


def test_agent_refuses_wrong_stage(repo):
    ep = repo.create_episode("Penicillin")
    with pytest.raises(StudioError, match="works at RESEARCH"):
        run_mock(repo, ep.episode_id, get_agent("researcher"))


def test_script_errors_are_readable(repo):
    ep = at_research(repo)
    run_mock(repo, ep.episode_id, get_agent("researcher"))
    repo.transition(ep.episode_id, Status.FACT_CHECK, actor="test")
    approve_all(repo, ep.episode_id)
    repo.transition(ep.episode_id, Status.SCRIPT, actor="test")
    bad = {"hook": "Hi", "segments": [{"segment_id": "SEG01", "role": "hook", "duration_sec": 40,
           "sentences": [{"text": "A fact.", "kind": "factual"}]}]}  # factual without claims
    out = parse_response(get_agent("scriptwriter"), json.dumps(bad))
    with pytest.raises(AgentOutputError) as err:
        preview(repo, ep.episode_id, get_agent("scriptwriter"), out)
    assert any("claim_id" in p for p in err.value.problems)


# ---------------------------------------------------------------- prompts
def test_every_agent_has_a_filled_prompt(repo, full_episode):
    for agent in AGENTS.values():
        prompt = agent.build_prompt(full_episode, repo.settings)
        assert "Penicillin" in prompt and "{topic}" not in prompt and '"' in prompt


def test_scriptwriter_prompt_has_only_approved_claims(repo, full_episode):
    claims = [c.model_copy(update={"status": ClaimStatus.REJECTED}) if c.claim_id == "C002" else c
              for c in full_episode.research.claims]
    ep = full_episode.model_copy(update={"research": full_episode.research.model_copy(
        update={"claims": claims})})
    prompt = get_agent("scriptwriter").build_prompt(ep, repo.settings)
    assert "C001" in prompt and "C002" not in prompt


# ------------------------------------------------------------- mock walk
def test_mocks_walk_an_idea_to_production(repo):
    ep = at_research(repo)
    run_mock(repo, ep.episode_id, get_agent("researcher"))
    repo.transition(ep.episode_id, Status.FACT_CHECK, actor="test")
    run_mock(repo, ep.episode_id, get_agent("fact_checker"))
    approve_all(repo, ep.episode_id)
    repo.transition(ep.episode_id, Status.SCRIPT, actor="test")
    run_mock(repo, ep.episode_id, get_agent("scriptwriter"))
    repo.transition(ep.episode_id, Status.STORYBOARD, actor="test")
    run_mock(repo, ep.episode_id, get_agent("storyboarder"))
    final = repo.transition(ep.episode_id, Status.PRODUCTION, actor="test")
    assert final.status == Status.PRODUCTION
    assert all(c.statement.startswith("[MOCK]") for c in final.research.claims)
    actors = {v["actor"] for v in repo.list_versions(ep.episode_id)}
    assert {"agent:researcher", "agent:storyboarder"} <= actors


def test_agent_for_status():
    assert agent_for_status(Status.SCRIPT).name == "scriptwriter"
    assert agent_for_status(Status.QA) is None


def test_editing_research_as_json_does_not_duplicate_approved_claims(repo):
    ep = at_research(repo)
    run_mock(repo, ep.episode_id, get_agent("researcher"))
    ep = repo.get_episode(ep.episode_id)
    first = ep.research.claims[0].model_copy(update={"status": ClaimStatus.APPROVED})
    ep = repo.save(ep.model_copy(update={"research": ep.research.model_copy(
        update={"claims": [first] + ep.research.claims[1:]})}), actor="human")

    agent = get_agent("researcher")
    answer = agent.current_answer(ep)
    answer["claims"][1]["statement"] = "[MOCK] Edited by hand."
    saved = run_agent(repo, ep.episode_id, agent, parse_response(agent, json.dumps(answer)),
                      actor="human", note="edited as JSON")
    assert [c.claim_id for c in saved.research.claims] == ["C001", "C002", "C003"]
    assert saved.research.claims[0].status == ClaimStatus.APPROVED
    assert saved.research.claims[1].statement == "[MOCK] Edited by hand."
    assert len(saved.research.sources) == 2


def test_script_and_storyboard_round_trip_as_json(repo):
    ep = at_research(repo)
    ep = run_mock_pipeline(repo, ep.episode_id)  # PRODUCTION; check the JSON shapes only
    for name in ("scriptwriter", "storyboarder"):
        agent = get_agent(name)
        out = parse_response(agent, json.dumps(agent.current_answer(ep)))
        rebuilt = agent.apply(ep, out, repo.settings)  # as real saves do
        section = "script" if name == "scriptwriter" else "storyboard"
        assert getattr(rebuilt, section) == getattr(ep, section)
    assert get_agent("fact_checker").current_answer(ep) is None


def test_researcher_rerun_keeps_rejected_claims_rejected(repo):
    ep = at_research(repo)
    agent = get_agent("researcher")
    out = parse_response(agent, json.dumps(RESEARCH_ANSWER))
    ep = run_agent(repo, ep.episode_id, agent, out, actor="human", note="first paste")
    claims = [ep.research.claims[0].model_copy(update={"status": ClaimStatus.REJECTED})]
    ep = repo.save(ep.model_copy(update={"research": ep.research.model_copy(
        update={"claims": claims})}), actor="human")
    again = run_agent(repo, ep.episode_id, agent, out, actor="human", note="same paste again")
    assert [(c.claim_id, c.status) for c in again.research.claims] == [
        ("C001", ClaimStatus.REJECTED)]
