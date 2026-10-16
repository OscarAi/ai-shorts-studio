import json

from helpers import open_gate

from studio.agents import (
    get_agent,
    parse_response,
    run_agent,
    run_mock_pipeline,
    shots_per_segment,
    with_style,
)
from studio.models import Status
from studio.validators import validate_for_transition

STYLE = "Warm storybook illustration, soft outlines."


def test_style_is_added_once():
    added = with_style("A desert at dawn, vertical 9:16.", STYLE)
    assert added == "A desert at dawn, vertical 9:16. Style: " + STYLE
    assert with_style(added, STYLE) == added  # not twice
    assert with_style("Warm storybook illustration of a dog", STYLE).count("storybook") == 1
    assert with_style("x", "  ") == "x"  # no style configured


def test_prompt_tells_the_ai_the_style(repo, full_episode):
    prompt = get_agent("storyboarder").build_prompt(full_episode, repo.settings)
    assert repo.settings.visual_style in prompt and "{visual_style}" not in prompt
    assert "photorealistic" in prompt  # as a word to AVOID


def test_mock_storyboard_carries_the_style(repo):
    ep = repo.create_episode("Velcro")
    open_gate(repo, ep.episode_id)
    ep = run_mock_pipeline(repo, ep.episode_id)
    style = repo.settings
    assert all(s.visual_style == style.visual_style_name for s in ep.storyboard.shots)
    assert all(style.visual_style in s.generation_prompt for s in ep.storyboard.shots)


def test_pasted_storyboard_without_style_gets_it(repo):
    ep = repo.create_episode("Velcro")
    open_gate(repo, ep.episode_id)
    ep = run_mock_pipeline(repo, ep.episode_id)  # at PRODUCTION; go back to storyboard
    repo.transition(ep.episode_id, Status.STORYBOARD, actor="t", reason="restyle")
    ep = repo.get_episode(ep.episode_id)
    answer = {"shots": [{"shot_id": f"S{n}", "duration_sec": seg.duration_sec,
                         "segment_ids": [seg.segment_id],
                         "generation_prompt": "A very realistic photo of burrs on a dog"}
                        for n, seg in enumerate(ep.script.segments)]}
    agent = get_agent("storyboarder")
    saved = run_agent(repo, ep.episode_id, agent, parse_response(agent, json.dumps(answer)),
                      actor="human", note="paste")
    for shot in saved.storyboard.shots:
        assert shot.generation_prompt.endswith("Style: " + repo.settings.visual_style)


# ------------------------------------------------- 12 pictures per video
def test_mock_storyboard_makes_twelve_shots_that_add_up(repo):
    ep = repo.create_episode("Velcro")
    open_gate(repo, ep.episode_id)
    ep = run_mock_pipeline(repo, ep.episode_id)
    shots = ep.storyboard.shots
    assert len(shots) == 12
    assert {sid for s in shots for sid in s.segment_ids} == {g.segment_id for g in ep.script.segments}
    assert abs(sum(s.duration_sec for s in shots) - ep.script.total_estimated_sec) < 0.1


def test_shots_are_spread_by_segment_length(full_episode):
    counts = shots_per_segment(full_episode, 12)  # sample segments: 4, 10, 12, 9, 5 seconds
    assert sum(counts) == 12 and min(counts) >= 1
    assert counts[2] >= counts[1] >= counts[0]  # the longest segment gets the most
    assert shots_per_segment(full_episode, 3) == [1, 1, 1, 1, 1]  # never below one each


def test_more_shots_than_the_daily_budget_are_blocked(repo, full_episode):
    shots = full_episode.storyboard.shots
    many = [s.model_copy(update={"shot_id": f"X{i}", "duration_sec": 40 / 13})
            for i, s in enumerate(shots * 3)][:13]
    ep = full_episode.model_copy(update={"storyboard": full_episode.storyboard.model_copy(
        update={"shots": many}), "status": Status.STORYBOARD})
    codes = {i.code: i.severity for i in validate_for_transition(ep, Status.PRODUCTION,
                                                                   repo.settings)}
    assert codes.get("TOO_MANY_SHOTS") == "error"


def test_few_shots_only_warn(repo, full_episode):
    ep = full_episode.model_copy(update={"status": Status.STORYBOARD})  # sample: 5 shots
    codes = {i.code: i.severity for i in validate_for_transition(ep, Status.PRODUCTION,
                                                                   repo.settings)}
    assert codes.get("FEW_SHOTS") == "warning" and "TOO_MANY_SHOTS" not in codes
