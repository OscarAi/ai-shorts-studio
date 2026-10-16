import json

from studio.ideas import build_ideas_prompt, create_from_idea, mock_ideas, parse_ideas, screen_ideas
from studio.models import Category
from studio.originality import Verdict


def test_prompt_lists_the_library_to_avoid(repo, full_episode):
    prompt = build_ideas_prompt(repo, series="Invented by Accident", count=7)
    assert "Penicillin" in prompt and "Suggest 7 episode ideas" in prompt
    assert "{library}" not in prompt and '"ideas"' in prompt


def test_parse_is_forgiving_about_category_and_risk():
    text = 'Here are ideas:\n{"ideas": [{"topic": "Velcro", "category": "Food History", ' \
           '"fact_risk": "LOW", "familiarity": "Very", "extra": 1}, ' \
           '{"topic": "Bread", "category": "Cooking"}]}'
    first, second = parse_ideas(text).ideas
    assert first.category == Category.FOOD and first.fact_risk == "low"
    assert first.familiarity == "medium"  # not a level -> the middle
    assert second.category == Category.OTHER


def test_the_scout_favours_little_known_food_stories(repo):
    prompt = build_ideas_prompt(repo)
    assert "little-known" in prompt and "familiarity" in prompt
    assert "food" in prompt.split("A good idea")[0]


def test_screening_flags_repeats_of_the_library_and_of_each_other(repo):
    repo.create_episode("The accidental invention of the microwave")
    answer = {"ideas": [
        {"topic": "The accidental invention of the microwave"},      # already made
        {"topic": "How the Great Wall was really built"},            # new
        {"topic": "How the Great Wall was really built!"}]}          # repeat inside the batch
    verdicts = [r.verdict for _, r in screen_ideas(repo, parse_ideas(json.dumps(answer)))]
    assert verdicts == [Verdict.SUGGEST_BLOCK, Verdict.PASS, Verdict.SUGGEST_BLOCK]


def test_mock_ideas_show_a_flagged_repeat(repo):
    verdicts = [r.verdict for _, r in screen_ideas(repo, mock_ideas())]
    assert verdicts[0] == Verdict.PASS and verdicts[-1] != Verdict.PASS


def test_create_from_idea_fills_the_idea_and_checks_it(repo):
    idea = mock_ideas().ideas[0]
    ep, report = create_from_idea(repo, idea, series="Invented by Accident", actor="human")
    assert ep.metadata.story_angle == idea.story_angle
    assert ep.metadata.hook_style == "question" and ep.metadata.series == "Invented by Accident"
    assert report.check_id and repo.originality_gate_state(ep.episode_id) == "pass"


def test_a_typo_in_the_default_category_is_reported(tmp_path):
    import pytest

    from studio.config import load_settings
    from studio.errors import StudioError
    config = tmp_path / "settings.yaml"
    config.write_text(f"data_dir: {tmp_path.as_posix()}\ndefault_category: fod\n",
                      encoding="utf-8")
    with pytest.raises(StudioError, match="default_category"):
        load_settings(config)
