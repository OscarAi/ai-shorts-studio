from datetime import datetime, timedelta

import pytest
from helpers import publish, sample_episode
from test_cli import run

from studio import analytics
from studio.errors import StudioError
from studio.models import MetricSnapshot
from studio.review import add_links

TT = "https://www.tiktok.com/@me/video/123"


def later(days):
    return datetime.now() + timedelta(days=days, minutes=5)


def published_episode(repo, topic, hook_style="", url="https://youtube.com/shorts/abc"):
    episode_id = sample_episode(repo, topic, story_angle=f"An unexpected twist about {topic}",
                                hook_style=hook_style)
    return publish(repo, episode_id, [url])


def test_the_videos_age_is_counted_per_platform(repo, full_episode):
    ep = publish(repo, full_episode.episode_id)
    snap, warnings = analytics.add_snapshot(repo, ep.episode_id, "youtube_shorts",
                                            {"views": 1200, "likes": 80, "stayed_pct": None},
                                            actor="human", now=later(8))
    assert snap.day == 8 and snap.stayed_pct is None and warnings == []
    two_days_ago = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    add_links(repo, ep.episode_id, [TT], actor="human", posted_at=two_days_ago)
    snap, _ = analytics.add_snapshot(repo, ep.episode_id, "tiktok", {"views": 50},
                                     actor="human")
    assert snap.day == 2


def test_typos_get_a_plain_message(repo, full_episode):
    ep = publish(repo, full_episode.episode_id)
    with pytest.raises(StudioError, match="Swapped two boxes"):
        analytics.add_snapshot(repo, ep.episode_id, "youtube_shorts",
                               {"views": 80, "likes": 1200}, actor="human")
    with pytest.raises(StudioError, match="Unknown number"):
        analytics.add_snapshot(repo, ep.episode_id, "youtube_shorts", {"vews": 1},
                               actor="human")


def test_rates_per_thousand_views():
    s = MetricSnapshot(episode_id="E", platform="tiktok", views=2000, likes=100, shares=10,
                       comments=4)
    assert analytics.rates(s) == {"likes_per_1k": 50.0, "comments_per_1k": 2.0,
                                  "shares_per_1k": 5.0, "engagement_pct": 5.7}
    assert analytics.rates(MetricSnapshot(episode_id="E", platform="tiktok", likes=3)) == {
        "likes_per_1k": None, "comments_per_1k": None, "shares_per_1k": None,
        "engagement_pct": None}


@pytest.mark.parametrize("days, age, expected", [
    ([], 0, (1, False)),      # not a day old yet: day 1 comes next
    ([], 1, (1, True)),
    ([1], 3, (7, False)),
    ([1], 9, (7, True)),
    ([], 9, (7, True)),       # a missed day 1 is skipped: the day-7 reading is what's due
    ([8], 40, (30, True)),
    ([31], 40, (None, False)),
])
def test_which_checkin_is_next(days, age, expected):
    snaps = [MetricSnapshot(episode_id="E", platform="tiktok", day=d, views=1) for d in days]
    assert analytics.next_checkin(snaps, age) == expected


def test_due_checkins_clear_once_numbers_are_in(repo, full_episode):
    ep = publish(repo, full_episode.episode_id)
    assert analytics.due_checkins(repo) == []  # went live minutes ago
    due = analytics.due_checkins(repo, now=later(1))
    assert [(c.platform, c.checkpoint, c.url) for c in due] == [
        ("youtube_shorts", 1, "https://youtube.com/shorts/abc")]
    analytics.add_snapshot(repo, ep.episode_id, "youtube_shorts", {"views": 10},
                           actor="human", now=later(1))
    assert analytics.due_checkins(repo, now=later(1)) == []
    assert [c.checkpoint for c in analytics.due_checkins(repo, now=later(7))] == [7]


def test_compare_groups_by_hook_style_near_day_7(repo):
    a = published_episode(repo, "Velcro", "question")
    b = published_episode(repo, "Microwave", "question", "https://youtube.com/shorts/b")
    c = published_episode(repo, "Post-it notes", " Shock fact", "https://youtube.com/shorts/c")
    for ep, early, week in ((a, 100, 1000), (b, 50, 3000), (c, 10, 500)):
        analytics.add_snapshot(repo, ep.episode_id, "youtube_shorts", {"views": early},
                               actor="h", now=later(1))
        analytics.add_snapshot(repo, ep.episode_id, "youtube_shorts",
                               {"views": week, "shares": week // 100}, actor="h", now=later(8))
    rows = analytics.compare(repo, by="hook_style", platform="youtube_shorts", day=7)
    assert [(r["Hook style"], r["Videos"], r["Median views"]) for r in rows] == [
        ("question", 2, 2000.0), ("shock fact", 1, 500.0)]
    assert rows[0]["Median shares / 1k views"] == 10.0
    assert all(r["Trust"] == "too few videos to trust yet" for r in rows)
    with pytest.raises(StudioError, match="Compare by"):
        analytics.compare(repo, by="colour", platform="youtube_shorts")


def test_video_table_and_csv(repo, full_episode):
    ep = publish(repo, full_episode.episode_id)
    analytics.add_snapshot(repo, ep.episode_id, "youtube_shorts",
                           {"views": 2000, "likes": 100}, actor="h", note="first day")
    rows = analytics.video_rows(repo)
    assert rows[0]["Topic"] == "Penicillin" and rows[0]["Likes / 1k views"] == 50.0
    csv_text = analytics.export_csv(repo)
    assert csv_text.splitlines()[0].startswith("episode_id,topic,platform,day")
    assert "Penicillin" in csv_text and "first day" in csv_text


def test_cli_stats_commands(tmp_path, repo, full_episode):
    publish(repo, full_episode.episode_id)
    ep_id = full_episode.episode_id
    out = run(tmp_path, "stats", ep_id, "--platform", "youtube_shorts", "--views", "1200",
              "--likes", "80", "--stayed", "71.5")
    assert out.returncode == 0 and "Saved YouTube Shorts numbers" in out.stdout
    out = run(tmp_path, "stats-show", ep_id)
    assert "Views 1200" in out.stdout and "Stayed to watch % 71.5" in out.stdout
    assert "per 1k views: 66.7 likes" in out.stdout and "None" not in out.stdout
    assert run(tmp_path, "stats-due").returncode == 0
    bad = run(tmp_path, "stats", ep_id, "--platform", "tiktok", "--views", "5")
    assert bad.returncode == 1 and "no TikTok link yet" in bad.stderr


# ------------------------------------------------- watch time as the apps write it
@pytest.mark.parametrize("text, seconds", [("0:26", 26), ("1:05", 65), ("26", 26),
                                           ("26,5", 26.5), (" 0:07.5 ", 7.5), ("", None)])
def test_watch_time_is_read_like_the_apps_write_it(text, seconds):
    assert analytics.parse_duration(text) == seconds


def test_a_watch_time_typo_gets_a_plain_message():
    with pytest.raises(StudioError, match="like 0:26"):
        analytics.parse_duration("half a minute")


def test_0_26_typed_as_text_is_26_seconds_and_0_26_seconds_is_questioned(repo, full_episode):
    ep = publish(repo, full_episode.episode_id)
    snap, warnings = analytics.add_snapshot(repo, ep.episode_id, "youtube_shorts",
                                            {"views": 85, "avg_view_sec": "0:26"}, actor="h")
    assert snap.avg_view_sec == 26 and warnings == []
    _, warnings = analytics.add_snapshot(repo, ep.episode_id, "youtube_shorts",
                                         {"views": 85, "avg_view_sec": 0.26}, actor="h")
    assert "\"0:26\" means 26 seconds" in warnings[0]


def test_compare_by_what_the_video_was_made_with(repo):
    """Picture style, voice, length and posting time come from the episode itself."""
    ep = published_episode(repo, "Velcro", "question")
    analytics.add_snapshot(repo, ep.episode_id, "youtube_shorts", {"views": 500},
                           actor="h", now=later(8))
    rows = {by: analytics.compare(repo, by=by, platform="youtube_shorts")[0]
            for by in ("picture_style", "voice", "length", "posted")}
    assert rows["picture_style"]["Picture style"] == "channel_style_v1"
    assert rows["voice"]["Voice"] == "sample"
    assert rows["length"]["Length"] == "40-49 s"  # the sample video is 40 s
    assert rows["posted"]["Posted (time of day)"].split(" ")[0] in (
        "morning", "afternoon", "evening", "night")
    assert all(r["Videos"] == 1 for r in rows.values())


def test_cli_accepts_0_26_for_watch_time(tmp_path, repo, full_episode):
    publish(repo, full_episode.episode_id)
    out = run(tmp_path, "stats", full_episode.episode_id, "--platform", "youtube_shorts",
              "--views", "85", "--avg-view-sec", "0:26")
    assert out.returncode == 0, out.stderr
    assert repo.list_metrics(full_episode.episode_id)[-1].avg_view_sec == 26
    bad = run(tmp_path, "stats", full_episode.episode_id, "--platform", "youtube_shorts",
              "--avg-view-sec", "soon")
    assert bad.returncode == 2 and "like 0:26" in bad.stderr
