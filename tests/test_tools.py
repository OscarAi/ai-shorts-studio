from datetime import date

import pytest
from helpers import open_gate
from media import image, needs_ffmpeg

from studio.agents import run_mock_pipeline
from studio.assets import add_shot_asset, made_today, suggest_tools
from studio.errors import StudioError
from studio.tools import Tools, load_tools


def test_shipped_tools_file_is_valid_and_has_licenses():
    tools = load_tools()
    firefly = Tools.find(tools.image_tools, "adobe firefly")
    assert firefly.url.startswith("https://") and "Content Credentials" in firefly.license_note
    assert Tools.find(tools.voice_tools, "Kokoro (built in)")
    assert all(t.license_note for t in tools.image_tools + tools.voice_tools)


def test_bing_and_elevenlabs_free_are_not_offered():
    names = [t.name.lower() for t in load_tools().image_tools + load_tools().voice_tools]
    assert not any("bing" in n or "designer" in n or "elevenlabs" in n for n in names)


def test_stale_terms_are_flagged():
    firefly = Tools.find(load_tools().image_tools, "Adobe Firefly")
    assert not firefly.stale(today=date(2026, 12, 1))
    assert firefly.stale(today=date(2027, 6, 1))
    own = Tools.find(load_tools().image_tools, "My own photo or drawing")
    assert not own.stale(today=date(2030, 1, 1))  # nothing to re-check


def test_a_broken_tools_file_gives_a_clear_error(tmp_path):
    bad = tmp_path / "tools.yaml"
    bad.write_text("image_tools:\n  - name: X\n    url: not-a-link\n    license_note: y\n"
                   "voice_tools: []\n", encoding="utf-8")
    with pytest.raises(StudioError, match=r"tools.yaml has a mistake"):
        load_tools(bad)


# ------------------------------------------------------------ daily budget
def _production(repo):
    ep = repo.create_episode("Velcro")
    open_gate(repo, ep.episode_id)
    return run_mock_pipeline(repo, ep.episode_id)  # 12 empty shots


def test_fresh_day_suggests_six_firefly_then_six_chatgpt(repo):
    ep = _production(repo)
    plan = suggest_tools(ep, load_tools().image_tools, used={})
    assert list(plan.values()) == ["Adobe Firefly"] * 6 + ["ChatGPT"] * 6


def test_suggestions_respect_what_is_already_used(repo):
    ep = _production(repo)
    plan = suggest_tools(ep, load_tools().image_tools, used={"Adobe Firefly": 4, "ChatGPT": 5})
    assert list(plan.values()) == ["Adobe Firefly"] * 2 + ["ChatGPT"] + [None] * 9


@needs_ffmpeg
def test_made_today_counts_pictures_per_tool_today_only(repo, tmp_path):
    ep = _production(repo)
    for shot in ep.storyboard.shots[:3]:
        add_shot_asset(repo, ep.episode_id, shot.shot_id, image(tmp_path / f"{shot.shot_id}.png"),
                       "x.png", provider="Adobe Firefly", license_note="test", actor="human")
    assert made_today(repo) == {"Adobe Firefly": 3}
    ep = repo.get_episode(ep.episode_id)  # pretend one was made yesterday
    old = ep.assets[0].model_copy(update={"created_at": "2001-01-01T10:00:00+00:00"})
    repo.save(ep.model_copy(update={"assets": [old] + ep.assets[1:]}), actor="t",
              allowed_sections={"assets"})
    assert made_today(repo) == {"Adobe Firefly": 2}
    plan = suggest_tools(repo.get_episode(ep.episode_id), load_tools().image_tools,
                         made_today(repo))
    assert list(plan.values())[:5] == ["Adobe Firefly"] * 4 + ["ChatGPT"]
