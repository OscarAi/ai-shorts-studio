import pytest
from helpers import YT, walk_to

from studio.errors import ImmutableEpisodeError, StudioError, ValidationFailed
from studio.guide import guide
from studio.models import Status
from studio.platforms import platform_of
from studio.review import add_links, mark_live, mark_published, mark_scheduled

FUTURE = "2099-01-01 18:00"


@pytest.fixture
def approved(repo, full_episode):
    return walk_to(repo, full_episode.episode_id, Status.APPROVED).episode_id


def test_schedule_then_go_live(repo, approved):
    ep = mark_scheduled(repo, approved, {"youtube_shorts": FUTURE}, [YT], actor="human")
    assert ep.status == Status.SCHEDULED and ep.publication.schedule == {"youtube_shorts": FUTURE}
    g = guide(ep, repo)
    assert g.headline == f"YouTube Shorts goes live {FUTURE}" and g.target == Status.PUBLISHED
    ep = mark_live(repo, approved, actor="human")
    assert ep.status == Status.PUBLISHED and ep.publication.published_at


def test_past_schedule_says_confirm(repo, approved):
    ep = mark_scheduled(repo, approved, {"youtube_shorts": "2020-01-01 10:00"}, [YT],
                        actor="human")
    assert guide(ep, repo).headline == "Should be live now: confirm it"


def test_schedule_needs_a_valid_time_and_link(repo, approved):
    with pytest.raises(StudioError, match="like 2026-10-14 18:00"):
        mark_scheduled(repo, approved, {"youtube_shorts": "tomorrow 6pm"}, [YT], actor="human")
    with pytest.raises(StudioError, match="https"):
        mark_scheduled(repo, approved, {"youtube_shorts": FUTURE}, [], actor="human")
    with pytest.raises(ValidationFailed) as err:  # the rule itself, not only the helper
        repo.transition(approved, Status.SCHEDULED, actor="t")
    assert "NO_SCHEDULE" in {i.code for i in err.value.issues}


def test_publishing_straight_away_needs_no_schedule(repo, approved):
    ep = mark_published(repo, approved, [YT], actor="human")
    assert ep.status == Status.PUBLISHED


def test_while_scheduled_only_publication_can_change(repo, approved):
    ep = mark_scheduled(repo, approved, {"youtube_shorts": FUTURE}, [YT], actor="human")
    with pytest.raises(ImmutableEpisodeError, match="only the publication"):
        repo.save(ep.model_copy(update={"script": ep.script.model_copy(update={"hook": "x"})}),
                  actor="human")
    ok = repo.save(ep.model_copy(update={"publication": ep.publication.model_copy(
        update={"description": "new"})}), actor="human")
    assert ok.publication.description == "new"


def test_unscheduling_needs_a_reason(repo, approved):
    mark_scheduled(repo, approved, {"youtube_shorts": FUTURE}, [YT], actor="human")
    with pytest.raises(StudioError, match="reason"):
        repo.transition(approved, Status.APPROVED, actor="t")
    assert repo.transition(approved, Status.APPROVED, actor="t",
                           reason="moved the date").status == Status.APPROVED


def test_links_can_be_added_after_publishing_but_nothing_else(repo, approved):
    ep = mark_published(repo, approved, [YT], actor="human")
    ep = add_links(repo, approved, ["https://www.tiktok.com/@me/video/1", YT], actor="human")
    assert ep.publication.urls[-2:] == [YT, "https://www.tiktok.com/@me/video/1"]
    assert ep.publication.urls.count(YT) == 1  # no duplicates
    assert repo.get_episode(approved).status == Status.PUBLISHED
    with pytest.raises(ImmutableEpisodeError):
        repo.save(ep.model_copy(update={"script": ep.script.model_copy(update={"hook": "x"})}),
                  actor="human", publication_only=True)
    with pytest.raises(ImmutableEpisodeError):
        repo.save(ep.model_copy(update={"publication": ep.publication.model_copy(
            update={"title": "sneaky"})}), actor="human")  # without publication_only: locked


def test_platform_of_link():
    assert platform_of("https://youtube.com/shorts/cNVNP2HSm6Q?feature=share") == "youtube_shorts"
    assert platform_of("https://youtu.be/x") == "youtube_shorts"
    assert platform_of("https://www.tiktok.com/@me/video/1") == "tiktok"
    assert platform_of("https://www.instagram.com/reel/x/") == "instagram_reels"
    assert platform_of("https://example.com/x") is None


# ------------------------------------------------- some platforms live, others scheduled
IG = "https://www.instagram.com/p/abc/"
TT = "https://www.tiktok.com/@me/video/1"


def test_owners_case_instagram_link_added_while_scheduled_publishes(repo, approved):
    """YouTube + TikTok scheduled for tomorrow, then Instagram posted right away."""
    from studio.analytics import platform_states
    mark_scheduled(repo, approved, {"youtube_shorts": FUTURE, "tiktok": FUTURE}, [YT, TT],
                   actor="human")
    ep = add_links(repo, approved, [IG], actor="human")
    assert ep.status == Status.PUBLISHED  # it is out on Instagram
    assert ep.publication.schedule == {"youtube_shorts": FUTURE, "tiktok": FUTURE}
    states = {p: s for p, (s, _) in platform_states(ep).items()}
    assert states == {"youtube_shorts": "scheduled", "tiktok": "scheduled",
                      "instagram_reels": "live"}
    assert "live on Instagram Reels" in repo.list_versions(approved)[-1]["change_note"]


def test_schedule_form_can_say_live_now_for_one_platform(repo, approved):
    ep = mark_scheduled(repo, approved, {"youtube_shorts": FUTURE, "instagram_reels": FUTURE},
                        [YT, IG], actor="human", live_now=["instagram_reels"])
    assert ep.status == Status.PUBLISHED
    assert ep.publication.schedule == {"youtube_shorts": FUTURE}  # IG is live, not scheduled
    assert set(ep.publication.live_at) == {"instagram_reels"}


def test_live_now_needs_its_link(repo, approved):
    with pytest.raises(StudioError, match="Paste the link for Instagram Reels"):
        mark_scheduled(repo, approved, {"youtube_shorts": FUTURE}, [YT], actor="human",
                       live_now=["instagram_reels"])


def test_already_scheduled_and_live_somewhere_the_guide_says_publish(repo, approved):
    """Episodes saved before this fix (like EP_000004): SCHEDULED with a live platform."""
    ep = mark_scheduled(repo, approved, {"youtube_shorts": FUTURE}, [YT], actor="human")
    pub = ep.publication.model_copy(update={"urls": [YT, IG],
                                            "live_at": {"instagram_reels": "2026-10-13 23:33"}})
    ep = repo.save(ep.model_copy(update={"publication": pub}), actor="t")
    g = guide(ep, repo)
    assert g.headline == "Live on Instagram Reels: mark it as published"
    assert g.steps[0].label == "Instagram Reels is live" and g.steps[0].done
    assert mark_live(repo, approved, actor="human").publication.schedule == {
        "youtube_shorts": FUTURE}


def test_a_link_with_a_later_time_is_scheduled_and_has_no_numbers_yet(repo, approved):
    from studio import analytics
    mark_published(repo, approved, [YT], actor="human")
    ep = add_links(repo, approved, [TT], actor="human", posted_at=FUTURE)
    assert ep.publication.schedule["tiktok"] == FUTURE and "tiktok" not in ep.publication.live_at
    with pytest.raises(StudioError, match=f"TikTok goes live {FUTURE}"):
        analytics.add_snapshot(repo, approved, "tiktok", {"views": 1}, actor="human")
    assert analytics.live_platforms(ep) == ["youtube_shorts"]
