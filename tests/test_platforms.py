import json

import pytest
from helpers import walk_to

from studio.copywriter import build_copy_prompt, mock_copy, parse_copy
from studio.errors import ImmutableEpisodeError, StudioError
from studio.models import ClaimStatus, Status
from studio.platforms import PLATFORMS, RULES, check_post, draft_post, source_publishers
from studio.review import mark_published, set_captions


def with_tags(ep, tags):
    return ep.model_copy(update={"publication": ep.publication.model_copy(
        update={"hashtags": tags})})


# ------------------------------------------------------------- drafts
def test_every_draft_fits_its_platform(full_episode):
    ep = with_tags(full_episode, ["#history", "#science", "#medicine", "#penicillin",
                                  "#mold", "#nobel", "#fleming"])  # 7 tags on purpose
    for platform in PLATFORMS:
        text = draft_post(ep, platform)
        assert check_post(platform, text) == [], (platform, check_post(platform, text))
        assert text.startswith(full_episode.script.hook)
        assert "Sources: NobelPrize.org; American Chemical Society" in text


def test_instagram_gets_at_most_five_hashtags(full_episode):
    ep = with_tags(full_episode, [f"#t{i}" for i in range(9)])
    text = draft_post(ep, "instagram_reels")
    assert len([w for w in text.split() if w.startswith("#")]) == 5


def test_youtube_draft_adds_shorts_tag_and_tiktok_mentions_series(full_episode):
    ep = with_tags(full_episode, ["#history"])
    assert "#Shorts" in draft_post(ep, "youtube_shorts")
    assert "Follow for more Invented by Accident." in draft_post(ep, "tiktok")


def test_checks_catch_long_texts_long_hooks_and_too_many_tags():
    assert any("Too long" in w for w in check_post("tiktok", "x" * 2300))
    assert any("first line" in w for w in check_post("tiktok", "y" * 150 + "\n\nmore"))
    assert any("at most 5" in w for w in check_post("instagram_reels", "hi #a #b #c #d #e #f"))


def test_sources_are_only_from_approved_claims(full_episode):
    # C001 and C003 are the claims citing S001 (NobelPrize.org); reject both.
    claims = [c.model_copy(update={"status": ClaimStatus.REJECTED})
              if c.claim_id in ("C001", "C003") else c for c in full_episode.research.claims]
    ep = full_episode.model_copy(update={"research": full_episode.research.model_copy(
        update={"claims": claims})})
    assert source_publishers(ep) == ["American Chemical Society"]


def test_every_platform_has_rules_with_tips():
    assert set(RULES) == set(PLATFORMS) and all(r.tips for r in RULES.values())


# ------------------------------------------------------------ copywriter
def test_copy_prompt_contains_only_approved_facts(full_episode):
    claims = [c.model_copy(update={"status": ClaimStatus.REJECTED}) if c.claim_id == "C002"
              else c for c in full_episode.research.claims]
    ep = full_episode.model_copy(update={"research": full_episode.research.model_copy(
        update={"claims": claims})})
    prompt = build_copy_prompt(ep)
    assert "Penicillium" not in prompt and "1928" in prompt and "{claims}" not in prompt


def test_copy_answer_fills_captions_with_tags():
    answer = {"youtube_title": "T" * 120, "youtube_description": "YT text",
              "tiktok_caption": "TT text", "instagram_caption": "IG text",
              "hashtags": ["#a#b", "c", "d", "e", "f", "g"]}
    out = parse_copy("Sure!\n" + json.dumps(answer))
    assert len(out.youtube_title) <= 100
    caps = out.captions
    assert caps["instagram_reels"].endswith("#a #b #c #d #e")  # at most 5
    assert set(caps) == set(PLATFORMS)


# ------------------------------------------------------------ saving texts
@pytest.fixture
def approved(repo, full_episode):
    return walk_to(repo, full_episode.episode_id, Status.APPROVED).episode_id


def test_captions_are_saved_and_kept_after_publishing(repo, approved):
    ep = set_captions(repo, approved, {"tiktok": " TT ", "youtube_shorts": "YT"}, actor="human",
                      title="New title", hashtags=["#x#y"])
    assert ep.publication.captions == {"tiktok": "TT", "youtube_shorts": "YT"}
    assert ep.publication.title == "New title" and ep.publication.hashtags == ["#x", "#y"]
    mark_published(repo, approved, ["https://youtube.com/shorts/a"], actor="human")
    ep = set_captions(repo, approved, {"instagram_reels": "IG later"}, actor="human")
    assert ep.status == Status.PUBLISHED and ep.publication.captions["instagram_reels"] == "IG later"


def test_captions_refuse_unknown_platforms_and_early_stages(repo, full_episode, approved):
    with pytest.raises(StudioError, match="Unknown platform"):
        set_captions(repo, approved, {"myspace": "x"}, actor="human")
    other = repo.create_episode("Velcro")
    with pytest.raises(StudioError, match="HUMAN_REVIEW"):
        set_captions(repo, other.episode_id, {"tiktok": "x"}, actor="human")


def test_mock_copy_parses(full_episode):
    assert parse_copy(mock_copy(full_episode).model_dump_json()).youtube_title.startswith("[MOCK]")


def test_publication_only_still_blocks_other_sections(repo, approved):
    ep = mark_published(repo, approved, ["https://youtube.com/shorts/a"], actor="human")
    with pytest.raises(ImmutableEpisodeError):
        repo.save(ep.model_copy(update={"metadata": ep.metadata.model_copy(
            update={"tone": "x"})}), actor="h", publication_only=True)
