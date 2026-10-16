import pytest
from helpers import walk_to

from studio import declines
from studio.errors import StudioError
from studio.models import ClaimStatus, Evidence, Status
from studio.review import decline_source


@pytest.fixture
def at_fact_check(repo, full_episode):
    """The sample: S001 NobelPrize.org, S002 ACS; C001 (high) cites both, C002 only S002,
    C003 only S001; all approved, fact check approved."""
    ep_id = full_episode.episode_id
    ep = repo.get_episode(ep_id)
    claims = [c.model_copy(update={"evidence": [Evidence(source_id=sid, quote="q", score=0.5)
                                                for sid in c.source_ids]})
              for c in ep.research.claims]
    repo.save(ep.model_copy(update={"research": ep.research.model_copy(
        update={"claims": claims})}), actor="t")
    walk_to(repo, ep_id, Status.FACT_CHECK)
    return ep_id


def claims(ep):
    return {c.claim_id: c for c in ep.research.claims}


def test_declining_removes_what_depended_only_on_that_source(repo, at_fact_check):
    ep = decline_source(repo, at_fact_check, "S002", reason="not a source I trust",
                        actor="human")
    src = {x.source_id: x for x in ep.research.sources}["S002"]
    assert src.declined and not src.usable and src.declined_note == "not a source I trust"
    c = claims(ep)
    # C002 relied only on S002: rejected, with the reason in its notes
    assert c["C002"].status == ClaimStatus.REJECTED and c["C002"].source_ids == []
    assert "its only source (S002) was declined" in c["C002"].notes
    # C001 still has S001: kept, but back to review; S002's quote is gone
    assert c["C001"].source_ids == ["S001"] and c["C001"].status == ClaimStatus.NEEDS_REVIEW
    assert [e.source_id for e in c["C001"].evidence] == ["S001"]
    # C003 never used S002: untouched
    assert c["C003"].status == ClaimStatus.APPROVED
    # the facts changed, so the fact check must be approved again
    assert not ep.fact_check.approved and "S002 was declined" in ep.fact_check.issues[-1]
    last = repo.list_versions(at_fact_check)[-1]
    assert "source S002 (acs.org) declined: not a source I trust; rejected C002" in last["change_note"]


def test_preview_says_what_will_change_without_saving(repo, at_fact_check):
    ep = repo.get_episode(at_fact_check)
    p = declines.preview(ep, {"S002"})
    assert [c.claim_id for c in p.rejected] == ["C002"]
    assert [c.claim_id for c in p.back_to_review] == ["C001"]
    assert p.lines()[0].startswith('Will be rejected (no other source): C002 "')
    assert repo.get_episode(at_fact_check).version == ep.version


def test_the_rules_still_hold_after_declining(repo, at_fact_check):
    """A high-importance claim left with one publisher is flagged by the existing rule."""
    from studio.validators import check_claims
    ep = decline_source(repo, at_fact_check, "S002", reason="no", actor="human")
    codes = {i.code for i in check_claims(ep, repo.settings)}
    assert "HIGH_CLAIM_WEAK_SOURCING" in codes and "SOURCE_BROKEN" not in codes


def test_declining_needs_a_reason_the_right_stage_and_a_real_source(repo, at_fact_check):
    with pytest.raises(StudioError, match="Say why"):
        decline_source(repo, at_fact_check, "S001", reason="  ", actor="human")
    with pytest.raises(StudioError, match="no source S009"):
        decline_source(repo, at_fact_check, "S009", reason="x", actor="human")
    decline_source(repo, at_fact_check, "S001", reason="x", actor="human")
    with pytest.raises(StudioError, match="already declined"):
        decline_source(repo, at_fact_check, "S001", reason="x", actor="human")


def test_not_after_the_script_stage(repo, full_episode):
    ep_id = walk_to(repo, full_episode.episode_id, Status.SCRIPT).episode_id
    with pytest.raises(StudioError, match="Go back to Fact check first"):
        decline_source(repo, ep_id, "S001", reason="x", actor="human")


@pytest.mark.parametrize("url, domain, expected", [
    ("https://www.example.com/a", "example.com", True),
    ("https://news.example.com/a", "www.example.com", True),
    ("https://example.com.evil.net/a", "example.com", False),
    ("https://notexample.com/", "example.com", False),
    ("https://EXAMPLE.com", "https://www.example.com/page", True),
])
def test_site_matching(url, domain, expected):
    assert declines.site_matches(url, domain) is expected


def test_clean_domain():
    assert declines.clean_domain("https://www.History.com/topics/x") == "history.com"
    assert declines.clean_domain(" Example.org ") == "example.org"


# ------------------------------------------------------------- blocked sites
def research_answer(repo, ep_id, answer):
    import json

    from studio.agents import get_agent, parse_response, run_agent
    agent = get_agent("researcher")
    return run_agent(repo, ep_id, agent, parse_response(agent, json.dumps(answer)),
                     actor="human", note="paste")


def test_block_unblock_and_list(repo):
    assert repo.block_site("https://www.Tabloid.com/x", reason="gossip", actor="human") == \
        "tabloid.com"
    assert [b["domain"] for b in repo.blocked_sites()] == ["tabloid.com"]
    with pytest.raises(StudioError, match="not a website"):
        repo.block_site("hello", reason="x", actor="human")
    with pytest.raises(StudioError, match="Say why"):
        repo.block_site("a.com", reason=" ", actor="human")
    assert repo.unblock_site("www.tabloid.com") and repo.blocked_sites() == []


def test_decline_with_block_site_tells_the_researcher(repo, at_fact_check):
    from studio.agents import get_agent
    decline_source(repo, at_fact_check, "S002", reason="not for us", actor="human",
                   block_site=True)
    assert repo.blocked_sites()[0]["domain"] == "acs.org"
    prompt = get_agent("researcher").build_prompt(repo.get_episode(at_fact_check),
                                                  repo.settings, repo.blocked_sites())
    assert "Never use these sites (the channel owner blocked them): acs.org" in prompt


def test_a_research_answer_from_a_blocked_site_is_declined_at_once(repo):
    from test_sources import ANSWER, NEWS, researched
    repo.block_site(NEWS, reason="state media", actor="human")
    ep = repo.get_episode(researched(repo, ANSWER))
    news = next(x for x in ep.research.sources if x.url == NEWS)
    assert news.declined_by == "system:blocked-site" and "is blocked: state media" in \
        news.declined_note
    c = claims(ep)  # C001 keeps the paper; C002 keeps the other source
    assert news.source_id not in c["C001"].source_ids + c["C002"].source_ids
    assert c["C001"].status == ClaimStatus.NEEDS_REVIEW


def test_a_rerun_cannot_bring_a_declined_page_back(repo):
    from test_sources import ANSWER, NEWS, researched
    ep_id = researched(repo, ANSWER)
    news = next(x for x in repo.get_episode(ep_id).research.sources if x.url == NEWS)
    decline_source(repo, ep_id, news.source_id, reason="prefer primary sources", actor="human")
    only_news = {"sources": [{"source_id": "S001", "title": "t", "publisher": "People's Daily",
                              "url": NEWS}],
                 "claims": [{"claim_id": "C001", "statement": "A new fact only that page says.",
                             "importance": "medium", "source_ids": ["S001"]}]}
    ep = research_answer(repo, ep_id, only_news)
    again = [x for x in ep.research.sources if x.url == NEWS]
    assert len(again) == 1 and again[0].declined  # kept once, still declined
    new = next(c for c in ep.research.claims if c.statement.startswith("A new fact"))
    assert new.status == ClaimStatus.REJECTED and "prefer primary sources" in new.notes


def test_cli_decline_and_sites(tmp_path, repo, at_fact_check):
    from test_cli import run
    out = run(tmp_path, "decline", at_fact_check, "S002", "--reason", "nope", "--block-site")
    assert out.returncode == 0, out.stderr
    assert "Will be rejected (no other source): C002" in out.stdout
    assert "acs.org" in run(tmp_path, "blocked-sites").stdout
    assert "Unblocked." in run(tmp_path, "unblock-site", "acs.org").stdout
