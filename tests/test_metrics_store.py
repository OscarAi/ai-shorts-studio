import sqlite3

import pytest
from helpers import publish, walk_to
from pydantic import ValidationError

from studio.db import EpisodeRepository
from studio.errors import StudioError
from studio.models import MetricSnapshot, Status
from studio.review import add_links, mark_live, mark_scheduled

YT = "https://youtube.com/shorts/abc"
TT = "https://www.tiktok.com/@me/video/123"


def snap(ep_id, platform="youtube_shorts", **numbers):
    return MetricSnapshot(episode_id=ep_id, platform=platform, day=1, **(numbers or {"views": 10}))


def test_a_published_video_keeps_every_snapshot(repo, full_episode):
    ep = publish(repo, full_episode.episode_id)
    repo.add_metrics(snap(ep.episode_id, views=100, likes=5), actor="human")
    repo.add_metrics(snap(ep.episode_id, views=900, likes=40, stayed_pct=71.5), actor="human")
    got = repo.list_metrics(ep.episode_id)
    assert [s.views for s in got] == [100, 900] and got[1].stayed_pct == 71.5
    # numbers are not episode content: no new version, the lock is untouched
    assert repo.get_episode(ep.episode_id).version == ep.version
    events = [e for e in repo.list_events(ep.episode_id) if e["event_type"] == "metrics"]
    assert len(events) == 2 and "views 900" in events[1]["detail"]


def test_numbers_only_for_published_videos_with_that_platforms_link(repo, full_episode):
    ep_id = full_episode.episode_id
    with pytest.raises(StudioError, match=r"once .* is PUBLISHED"):
        repo.add_metrics(snap(ep_id), actor="human")
    publish(repo, ep_id)
    with pytest.raises(StudioError, match="no TikTok link yet"):
        repo.add_metrics(snap(ep_id, "tiktok"), actor="human")
    add_links(repo, ep_id, [TT], actor="human")
    repo.add_metrics(snap(ep_id, "tiktok"), actor="human")


@pytest.mark.parametrize("numbers, message", [
    ({}, "at least one number"),
    ({"views": -1}, "greater than or equal to 0"),
    ({"stayed_pct": 120}, "less than or equal to 100"),
    ({"views": 50, "likes": 500}, "can't be more than views"),
])
def test_impossible_numbers_are_refused(numbers, message):
    with pytest.raises(ValidationError, match=message):
        MetricSnapshot(episode_id="EP_1", platform="tiktok", **numbers)


def test_an_older_database_gains_the_metrics_table(settings, repo):
    con = sqlite3.connect(settings.db_path)
    con.execute("DROP TABLE metrics")
    con.commit()
    con.close()
    assert EpisodeRepository(settings).list_metrics() == []


def test_go_live_time_is_recorded_per_platform(repo, full_episode):
    ep = publish(repo, full_episode.episode_id)
    assert set(ep.publication.live_at) == {"youtube_shorts"}
    ep = add_links(repo, ep.episode_id, [TT], actor="human", posted_at="2026-10-12  09:30")
    assert ep.publication.live_at["tiktok"] == "2026-10-12 09:30"
    with pytest.raises(StudioError, match="like 2026-10-14 18:00"):
        add_links(repo, ep.episode_id, ["https://instagram.com/reel/x"], actor="human",
                  posted_at="yesterday")


def test_a_scheduled_platform_goes_live_at_its_schedule(repo, full_episode):
    ep_id = full_episode.episode_id
    walk_to(repo, ep_id, Status.APPROVED)
    mark_scheduled(repo, ep_id, {"youtube_shorts": "2099-01-01 18:00"}, [YT], actor="human")
    ep = mark_live(repo, ep_id, actor="human")
    assert ep.publication.live_at == {} and ep.publication.schedule


def test_adding_a_link_does_not_restamp_older_links(repo, full_episode):
    """Links saved before go-live times existed keep falling back to published_at."""
    ep = publish(repo, full_episode.episode_id)
    ep = repo.save(ep.model_copy(update={"publication": ep.publication.model_copy(
        update={"live_at": {}})}), actor="t", publication_only=True)  # like older data
    ep = add_links(repo, ep.episode_id, [TT], actor="human", posted_at="2026-10-12 18:00")
    assert ep.publication.live_at == {"tiktok": "2026-10-12 18:00"}
