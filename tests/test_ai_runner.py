"""Running agents with an AI: self-repair, what is never sent back, logging, nothing saved."""
import json

import pytest
from helpers import walk_to

from studio.agents import get_agent
from studio.ai_runner import ask_agent, save_ai_answer
from studio.llm import FakeLLM, NoAIAvailable
from studio.models import ClaimStatus, Status


@pytest.fixture
def researching(repo):
    ep = repo.create_episode("The accidental invention of velcro",
                             story_angle="Burrs on a dog after a mountain walk")
    return walk_to(repo, ep.episode_id, Status.RESEARCH).episode_id


@pytest.fixture
def scripting(repo, full_episode):
    return walk_to(repo, full_episode.episode_id, Status.SCRIPT).episode_id


def mock_answer(repo, ep_id, name) -> dict:
    agent = get_agent(name)
    return json.loads(agent.mock(repo.get_episode(ep_id), repo.settings).model_dump_json())


def test_an_unreadable_answer_is_sent_back_and_fixed(repo, researching):
    good = json.dumps(mock_answer(repo, researching, "researcher"))
    fake = FakeLLM(["Sure! Here are some facts about velcro.", good])
    result = ask_agent(repo, researching, get_agent("researcher"), fake, max_repairs=2)
    assert result.usable and result.repairs == 1
    assert "No JSON object found" in result.attempts[0].problems[0]
    assert "Your previous answer was:\nSure! Here are some facts" in fake.prompts[1]
    assert "- No JSON object found" in fake.prompts[1]
    assert result.summary == "Fake (fake-model) answered in 0 s · 1 repair"


def test_nothing_is_saved_until_you_save_and_claims_wait_for_you(repo, researching):
    version = repo.get_episode(researching).version
    agent = get_agent("researcher")
    result = ask_agent(repo, researching, agent,
                       FakeLLM([json.dumps(mock_answer(repo, researching, "researcher"))]))
    assert repo.get_episode(researching).version == version  # not saved
    ep = save_ai_answer(repo, researching, agent, result, actor="human")
    assert all(c.status == ClaimStatus.NEEDS_REVIEW for c in ep.research.claims)
    assert repo.list_versions(researching)[-1]["change_note"] == \
        "researcher: AI Fake fake-model, 1 try"


def test_a_script_using_an_unknown_fact_is_repaired(repo, scripting):
    bad = mock_answer(repo, scripting, "scriptwriter")
    factual = next(s for g in bad["segments"] for s in g["sentences"] if s.get("claim_ids"))
    factual["claim_ids"] = ["C999"]
    good = mock_answer(repo, scripting, "scriptwriter")
    fake = FakeLLM([json.dumps(bad), json.dumps(good)])
    result = ask_agent(repo, scripting, get_agent("scriptwriter"), fake, max_repairs=2)
    assert result.repairs == 1 and "CLAIM_UNKNOWN" in result.attempts[0].problems[0]
    assert "Do not add facts that are not in the approved list" in fake.prompts[1]
    assert not result.issues


def test_link_checks_are_never_sent_back_to_the_ai(repo, researching):
    answer = mock_answer(repo, researching, "researcher")
    for src in answer["sources"]:
        src["url"] = f"https://example.com/{src['source_id']}"  # real-looking, not checked yet
    fake = FakeLLM([json.dumps(answer)])
    result = ask_agent(repo, researching, get_agent("researcher"), fake, max_repairs=2)
    assert len(fake.prompts) == 1 and result.usable
    assert "SOURCE_NOT_CHECKED" in {i.code for i in result.issues}  # yours to do (Verify)


def test_it_stops_after_the_repair_limit(repo, researching):
    # three different problems (no JSON, broken JSON, wrong shape): it keeps trying, up to 3
    fake = FakeLLM(["no", '{"sources": [}', '{"sources": 5}', "never asked"])
    result = ask_agent(repo, researching, get_agent("researcher"), fake, max_repairs=2)
    assert len(result.attempts) == 3 and not result.usable
    assert fake.answers == ["never asked"]


def test_no_service_left_keeps_the_best_answer_so_far(repo, researching):
    result = ask_agent(repo, researching, get_agent("researcher"), FakeLLM(["no json"]),
                       max_repairs=2)
    assert len(result.attempts) == 1 and not result.usable
    with pytest.raises(NoAIAvailable):
        ask_agent(repo, researching, get_agent("researcher"), FakeLLM([]))


def test_every_attempt_is_in_the_ai_log(repo, researching):
    good = json.dumps(mock_answer(repo, researching, "researcher"))
    ask_agent(repo, researching, get_agent("researcher"), FakeLLM(["oops", good]), max_repairs=2)
    log = repo.list_ai_calls(researching)
    assert [(r["attempt"], r["agent"], r["provider"]) for r in log] == [
        (1, "researcher", "Fake"), (2, "researcher", "Fake")]
    assert log[0]["answer"] == "oops" and "No JSON object found" in log[0]["problems"]
    assert "Velcro" in log[0]["prompt"] or "velcro" in log[0]["prompt"]


def test_wrong_stage_is_refused(repo, scripting):
    from studio.errors import StudioError
    with pytest.raises(StudioError, match="works at RESEARCH"):
        ask_agent(repo, scripting, get_agent("researcher"), FakeLLM(["{}"]))


def test_cli_ai_without_keys_says_how_to_add_one(tmp_path, repo, researching):
    from test_cli import run
    out = run(tmp_path, "ai", researching)
    assert out.returncode == 1 and "Add a free key to the .env file" in out.stderr


def test_the_same_problems_again_stop_the_repairs(repo, researching):
    fake = FakeLLM(["no", "still no", "never asked"])
    result = ask_agent(repo, researching, get_agent("researcher"), fake, max_repairs=2)
    assert len(result.attempts) == 2 and fake.answers == ["never asked"]  # 1 request saved


def test_an_identical_question_reuses_the_good_answer(repo, researching):
    good = json.dumps(mock_answer(repo, researching, "researcher"))
    agent = get_agent("researcher")
    ask_agent(repo, researching, agent, FakeLLM([good]))
    again = FakeLLM(["should not be asked"])
    result = ask_agent(repo, researching, agent, again)
    assert result.reused and result.usable and again.prompts == []
    assert result.summary.startswith("Reused the earlier Fake (fake-model) answer")
    fresh = ask_agent(repo, researching, agent, FakeLLM([good]), fresh=True)
    assert not fresh.reused


def test_prompts_carry_the_numbers_python_worked_out(repo, scripting):
    ep = repo.get_episode(scripting)
    script = get_agent("scriptwriter").build_prompt(ep, repo.settings)
    assert "never fewer than 75 or\nmore than 137" in script
    assert "Before you answer, check" in script


def test_storyboard_lengths_that_are_a_little_off_are_fitted(repo, full_episode):
    from studio.agents import fit_durations
    from studio.models import Shot
    shots = [Shot(shot_id=f"S{i}", duration_sec=3, segment_ids=["SEG01"], generation_prompt="p")
             for i in range(12)]  # 36 s for a 40 s narration
    fitted = fit_durations(shots, 40)
    assert round(sum(x.duration_sec for x in fitted), 2) == 40
    assert fitted[0].duration_sec == 3.33
    assert fit_durations(shots, 80) == shots  # far off: left for the rule to show


def test_a_script_with_too_few_words_is_sent_back(repo, scripting):
    """The AI said 40 s but wrote 60 words (= 24 s): Python counts and asks for a fix."""
    short = mock_answer(repo, scripting, "scriptwriter")  # [MOCK] lines: far too few words
    fake = FakeLLM([json.dumps(short), json.dumps(short)])
    result = ask_agent(repo, scripting, get_agent("scriptwriter"), fake, max_repairs=2)
    assert "WORD_COUNT" in result.attempts[0].problems[0]
    assert "it must have 75-137 words" in fake.prompts[1]
    assert len(result.attempts) == 2  # the same problem again: stop, don't waste a 3rd request


def test_invented_research_links_are_sent_back(repo, researching):
    from studio.models import LinkStatus
    from studio.sources import FakeFetcher, FetchResult
    real, made_up, shy = ("https://real.example/a", "https://made-up.example/b",
                          "https://shy.example/c")
    fetcher = FakeFetcher({real: FetchResult(LinkStatus.OK, real, "t", "text"),
                           shy: FetchResult(LinkStatus.BLOCKED, shy, note="robot check")})
    first = mock_answer(repo, researching, "researcher")
    first["sources"][0]["url"], first["sources"][1]["url"] = made_up, shy
    fixed = mock_answer(repo, researching, "researcher")
    fixed["sources"][0]["url"], fixed["sources"][1]["url"] = real, shy
    fake = FakeLLM([json.dumps(first), json.dumps(fixed)])
    result = ask_agent(repo, researching, get_agent("researcher"), fake, max_repairs=2,
                       fetcher=fetcher)
    assert result.repairs == 1 and len(result.attempts[0].problems) == 1  # not the blocked one
    assert "BROKEN_LINK" in result.attempts[0].problems[0] and made_up in fake.prompts[1]
    assert fetcher.fetched.count(shy) == 1  # each link is opened once


def test_a_generic_hook_is_sent_back_but_never_blocks(repo, scripting):
    weak = mock_answer(repo, scripting, "scriptwriter")
    weak["hook"] = "Did you know penicillin started with a messy lab bench?"
    fake = FakeLLM([json.dumps(weak), json.dumps(weak)])
    result = ask_agent(repo, scripting, get_agent("scriptwriter"), fake, max_repairs=2)
    assert any("HOOK_GENERIC" in p for p in result.attempts[0].problems)
    assert "generic opener" in fake.prompts[1]
    assert result.usable  # a warning: the human may keep it


def test_new_episodes_aim_for_the_settings_length(repo):
    ep = repo.create_episode("Ketchup was once sold as medicine")
    assert ep.metadata.target_duration_sec == repo.settings.target_duration_sec == 35
    script = get_agent("scriptwriter").build_prompt(ep, repo.settings)
    assert "about 35 seconds" in script and "about 88 words" in script
    assert "at most 14 words" in script


def test_only_one_ai_run_at_a_time(repo, scripting):
    """A second click (or another tab) while the AI works is told to wait: no new request."""
    from studio import ai_runner
    from studio.errors import StudioError
    fake = FakeLLM([json.dumps(mock_answer(repo, scripting, "scriptwriter"))])
    with ai_runner._RUNNING, pytest.raises(StudioError, match="still working"):  # a run is going
        ask_agent(repo, scripting, get_agent("scriptwriter"), fake)
    assert fake.prompts == []
    with pytest.raises(NoAIAvailable):  # a failed run releases the lock...
        ask_agent(repo, scripting, get_agent("scriptwriter"), FakeLLM([]))
    assert ask_agent(repo, scripting, get_agent("scriptwriter"), fake).text  # ...so this runs


def test_a_run_with_busy_services_stops_at_its_budget(repo, scripting):
    from test_llm import BUSY, KEY, FakeSend, cfg, provider

    from studio import llm
    send = FakeSend(*[BUSY] * 30)
    router = llm.Router.from_config(  # 6 busy models, but only 4 requests
        cfg(provider(models=("m1", "m2", "m3")),
            provider("Groq", models=("g1", "g2", "g3"), key_env="GROQ_API_KEY")).model_copy(
            update={"max_requests_per_run": 4}),
        env={"GEMINI_API_KEY": KEY, "GROQ_API_KEY": KEY}, send=send)
    with pytest.raises(NoAIAvailable, match="already sent 4 requests"):
        ask_agent(repo, scripting, get_agent("scriptwriter"), router, max_repairs=2)
    assert len(send.calls) == 4
