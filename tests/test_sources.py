import json

import pytest
from helpers import open_gate

from studio.agents import get_agent, parse_response, run_agent, run_mock
from studio.errors import StudioError, ValidationFailed
from studio.models import LinkStatus, Status
from studio.sources import (
    FakeFetcher,
    FetchResult,
    best_evidence,
    html_to_text,
    looks_blocked,
    mark_source_verified,
    title_note,
    verify_sources,
)

PAPER = "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0045137"
NEWS = "https://en.people.cn/n3/2024/0226/c90000-20137193.html"
DEAD = "https://events.bl.uk/events/tartlets-and-pastries"
PMC = "https://pmc.ncbi.nlm.nih.gov/articles/PMC3447823/"

PAGES = {
    PAPER: FetchResult(LinkStatus.OK, PAPER, "Archaeobotanical Study of Ancient Food at Astana",
                       "The Turpan Basin is characterized by a dry climate, which has favored the "
                       "preservation of plant remains. The dumplings were made from wheat, along "
                       "with foxtail millet. Thousands of tombs date from the 3rd to 9th centuries."),
    NEWS: FetchResult(LinkStatus.OK, NEWS, "China's history of dumplings",
                      "Due to the dry climate, well-preserved pastries and dumplings from the Tang "
                      "Dynasty were found. Approximately 20 Tang dumplings have been unearthed at "
                      "Astana to date."),
    PMC: FetchResult(LinkStatus.BLOCKED, PMC, note="robot check page"),
}

# Your EP_000003 situation: C002 is "high" and cites a working source + a dead one.
ANSWER = {
    "sources": [
        {"source_id": "S001", "title": "Archaeobotanical Study of Ancient Food at Astana",
         "publisher": "PLOS ONE", "url": PAPER},
        {"source_id": "S002", "title": "China's history of dumplings",
         "publisher": "People's Daily", "url": NEWS},
        {"source_id": "S004", "title": "Tartlets and Pastries", "publisher": "British Library",
         "url": DEAD}],
    "claims": [
        {"claim_id": "C001", "statement": "About 20 Tang dynasty dumplings were found at Astana.",
         "importance": "high", "source_ids": ["S001", "S002"]},
        {"claim_id": "C002", "statement": "The dry climate preserved the food in the tombs.",
         "importance": "high", "source_ids": ["S002", "S004"]}],
}


def researched(repo, answer=ANSWER):
    ep = repo.create_episode("Dumplings")
    open_gate(repo, ep.episode_id)
    repo.transition(ep.episode_id, Status.RESEARCH, actor="t")
    agent = get_agent("researcher")
    run_agent(repo, ep.episode_id, agent, parse_response(agent, json.dumps(answer)),
              actor="human", note="paste")
    return ep.episode_id


def codes(err):
    return [i.code for i in err.value.issues]


# ------------------------------------------------------------- the rules
def test_unchecked_sources_block_fact_check(repo):
    eid = researched(repo)
    with pytest.raises(ValidationFailed) as err:
        repo.transition(eid, Status.FACT_CHECK, actor="t")
    assert codes(err).count("SOURCE_NOT_CHECKED") == 3


def test_dead_link_no_longer_counts_as_a_second_publisher(repo):
    eid = researched(repo)
    ep = verify_sources(repo, eid, FakeFetcher(PAGES))
    status = {x.source_id: x.link_status for x in ep.research.sources}
    assert status == {"S001": LinkStatus.OK, "S002": LinkStatus.OK, "S004": LinkStatus.BROKEN}
    with pytest.raises(ValidationFailed) as err:
        repo.transition(eid, Status.FACT_CHECK, actor="t")
    assert "SOURCE_BROKEN" in codes(err)
    weak = [i for i in err.value.issues if i.code == "HIGH_CLAIM_WEAK_SOURCING"]
    assert len(weak) == 1 and "C002" in weak[0].message  # C001 still has 2 working publishers


def test_blocked_page_needs_a_human_and_then_passes(repo):
    answer = json.loads(json.dumps(ANSWER))
    answer["sources"][2] = {"source_id": "S004", "title": "Archaeobotanical study",
                            "publisher": "PubMed Central", "url": PMC}
    eid = researched(repo, answer)
    verify_sources(repo, eid, FakeFetcher(PAGES))
    with pytest.raises(ValidationFailed) as err:
        repo.transition(eid, Status.FACT_CHECK, actor="t")
    assert "SOURCE_BLOCKED" in codes(err)
    with pytest.raises(StudioError):
        mark_source_verified(repo, eid, "S004", actor="human", note="")  # a note is required
    mark_source_verified(repo, eid, "S004", actor="human", note="opened it, the paper is there")
    assert repo.transition(eid, Status.FACT_CHECK, actor="t").status == Status.FACT_CHECK


def test_evidence_quotes_are_attached_to_claims(repo):
    eid = researched(repo)
    ep = verify_sources(repo, eid, FakeFetcher(PAGES))
    c001 = ep.research.claims[0]
    by_source = {e.source_id: e for e in c001.evidence}
    assert "Approximately 20 Tang dumplings" in by_source["S002"].quote
    assert by_source["S002"].score >= 0.6
    assert repo.list_versions(eid)[-1]["actor"] == "system:verify"


def test_mock_links_are_never_fetched(repo):
    ep = repo.create_episode("Velcro")
    open_gate(repo, ep.episode_id)
    repo.transition(ep.episode_id, Status.RESEARCH, actor="t")
    run_mock(repo, ep.episode_id, get_agent("researcher"))
    fetcher = FakeFetcher({})
    ep = verify_sources(repo, ep.episode_id, fetcher)
    assert fetcher.fetched == []
    assert {x.link_status for x in ep.research.sources} == {LinkStatus.MOCK}


def test_changed_url_resets_its_check_but_unchanged_keeps_it(repo):
    eid = researched(repo)
    verify_sources(repo, eid, FakeFetcher(PAGES))
    answer = json.loads(json.dumps(ANSWER))
    answer["sources"][2]["url"] = "https://www.britishmuseum.org/astana"
    agent = get_agent("researcher")
    ep = run_agent(repo, eid, agent, parse_response(agent, json.dumps(answer)),
                   actor="human", note="fixed S004")
    status = {x.source_id: x.link_status for x in ep.research.sources}
    assert status == {"S001": LinkStatus.OK, "S002": LinkStatus.OK,
                      "S004": LinkStatus.NOT_CHECKED}
    assert ep.research.claims[0].evidence  # unchanged claim keeps its quotes


def test_verify_only_at_research_or_fact_check(repo):
    ep = repo.create_episode("Velcro")
    with pytest.raises(StudioError, match="RESEARCH or FACT_CHECK"):
        verify_sources(repo, ep.episode_id, FakeFetcher({}))


# ---------------------------------------------------------- page reading
def test_html_to_text_skips_scripts_and_keeps_title():
    title, text = html_to_text("<html><head><title>Velcro  story</title><script>var x=1;"
                               "</script></head><body><p>Burrs stuck to the dog.</p>"
                               "<p>He looked closer.</p></body></html>")
    assert title == "Velcro story"
    assert text == "Burrs stuck to the dog.\nHe looked closer."


def test_robot_pages_are_detected():
    assert looks_blocked("Just a moment...", "Checking your browser before accessing the site.")
    assert not looks_blocked("Velcro", "A long article. " * 400)


def test_best_evidence_picks_the_matching_sentence():
    text = ("The museum opened in 1990. George de Mestral noticed burrs stuck to his dog in 1941. "
            "The gift shop sells postcards.")
    quote, score = best_evidence("In 1941 George de Mestral noticed burrs on his dog.", text)
    assert quote.startswith("George de Mestral noticed burrs") and score >= 0.8


def test_title_mismatch_is_noted():
    assert title_note("Raytheon company history", "Chocolate cake recipes", "Bake at 180C.")
    assert not title_note("Raytheon company history", "Raytheon history | RTX", "")


def test_rare_words_beat_common_ones():
    """On a page about dumplings, 'dumplings' proves little; 'crescent' is the evidence."""
    text = ("People have been enjoying dumplings for a very long time in China today. "
            "Chinese dumplings are popular and dumplings are eaten today at New Year. "
            "The dumplings found at the Astana Cemetery are crescent-shaped bundles. "
            "Dumplings are also sold in shops today.")
    quote, _ = best_evidence("The Astana dumplings have a crescent shape very similar to "
                             "Chinese dumplings eaten today.", text)
    assert "crescent-shaped" in quote
