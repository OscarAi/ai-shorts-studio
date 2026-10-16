"""How published videos perform: snapshots of each platform's numbers, check-in reminders,
rates and comparisons.

Ideas taken from how creators and tools (YouTube Studio, Metricool, Later) measure short videos:
  * Numbers are read at fixed ages (day 1, 7, 30), so a new and an old video are compared fairly.
    Shorts get most of their views in the first days.
  * Rates beat raw counts: likes / shares per 1,000 views, and how many viewers stayed.
  * Views are compared within one platform only. Since 31 Mar 2025 YouTube counts every play as
    a Shorts "view"; its old count is now "engaged views".
Numbers are typed in by hand for now; an API can fill the same table later (source="youtube_api").
"""
import csv
import io
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from pydantic import ValidationError

from .db import EpisodeRepository
from .errors import StudioError
from .models import NUMBER_FIELDS, AssetKind, Episode, MetricSnapshot, Status, latest_asset
from .platforms import (
    PLATFORMS,
    live_platforms,
    platform_of,
    platform_states,
    went_live,
)
from .timeutil import local_now

CHECKPOINTS = (1, 7, 30)      # days after going live
TRUST_MIN_VIDEOS = 3          # fewer videos per group: differences are mostly luck

# What each platform shows, in its own words (fields not listed there are hidden in forms).
LABELS = {
    "youtube_shorts": {"views": "Views", "engaged_views": "Engaged views", "likes": "Likes",
                       "comments": "Comments", "shares": "Shares",
                       "avg_view_sec": "Average view duration (0:26 or seconds)",
                       "stayed_pct": "Stayed to watch %",
                       "followers_gained": "Subscribers gained"},
    "tiktok": {"views": "Views", "likes": "Likes", "comments": "Comments", "shares": "Shares",
               "saves": "Saves", "avg_view_sec": "Average watch time (0:26 or seconds)",
               "completion_pct": "Watched full video %", "followers_gained": "New followers"},
    "instagram_reels": {"views": "Views", "likes": "Likes", "comments": "Comments",
                        "shares": "Shares", "saves": "Saves",
                        "avg_view_sec": "Average watch time (0:26 or seconds)",
                        "followers_gained": "Follows"},
}
WHERE = {
    "youtube_shorts": "YouTube Studio → Content → Shorts → the video → Analytics. Engaged views "
                      "and shares are under \"Advanced mode\"; \"Stayed to watch\" is on the "
                      "Engagement tab.",
    "tiktok": "TikTok Studio (or the video → ⋯ → Analytics): views, likes, comments, shares, "
              "saves, average watch time, watched full video, new followers.",
    "instagram_reels": "Open the reel → View insights (needs a professional account): views, "
                       "likes, comments, shares, saves, average watch time, follows.",
}
# ------------------------------------------------------------- what to compare by
def _voice(ep: Episode, platform: str) -> str:
    voice = latest_asset(ep, AssetKind.VOICE)
    return voice.provider if voice and voice.provider else ""


def _picture_style(ep: Episode, platform: str) -> str:
    styles = sorted({s.visual_style for s in ep.storyboard.shots})
    return ", ".join(styles)


def _length(ep: Episode, platform: str) -> str:
    """The video's length in buckets: completion rate depends a lot on it."""
    video = latest_asset(ep, AssetKind.FINAL_VIDEO)
    seconds = video.duration_sec if video else None
    if seconds is None:
        return ""
    return next(label for limit, label in ((30, "under 30 s"), (40, "30-39 s"), (50, "40-49 s"),
                                           (float("inf"), "50 s or more")) if seconds < limit)


def _posted(ep: Episode, platform: str) -> str:
    """When it went live on this platform (your local time)."""
    start = went_live(ep, platform)
    if start is None:
        return ""
    hour = start.hour
    return ("morning (6-12)" if 6 <= hour < 12 else "afternoon (12-18)" if 12 <= hour < 18
            else "evening (18-23)" if 18 <= hour < 23 else "night (23-6)")


def _metadata(field: str):
    return lambda ep, platform: str(getattr(ep.metadata, field))


# name: label. _GROUPERS reads each one from an episode on a platform; the values are
# compared ignoring case and surrounding spaces.
GROUP_BY = {
    "hook_style": "Hook style", "category": "Category", "series": "Series",
    "picture_style": "Picture style", "voice": "Voice", "length": "Length",
    "posted": "Posted (time of day)",
}
_GROUPERS = {"hook_style": _metadata("hook_style"), "category": _metadata("category"),
             "series": _metadata("series"), "picture_style": _picture_style, "voice": _voice,
             "length": _length, "posted": _posted}


# ------------------------------------------------------------------ age
def days(n: int) -> str:
    return f"{n} day" + ("" if n == 1 else "s")


def age_days(ep: Episode, platform: str, now: datetime | None = None) -> int:
    start = went_live(ep, platform)
    if start is None:
        return 0
    return max(0, ((now or local_now()) - start).days)


# ------------------------------------------------------------------ add
def add_snapshot(repo: EpisodeRepository, episode_id: str, platform: str,
                 numbers: dict[str, float | int | str | None], *, actor: str, note: str = "",
                 now: datetime | None = None, source: str = "manual"
                 ) -> tuple[MetricSnapshot, list[str]]:
    """Save a platform's numbers for a published video. Returns (snapshot, warnings)."""
    ep = repo.get_episode(episode_id)
    unknown = set(numbers) - set(NUMBER_FIELDS)
    if unknown:
        raise StudioError(f"Unknown number(s): {', '.join(sorted(unknown))}.")
    filled: dict[str, float | int | str] = {
        k: v for k, v in numbers.items() if v is not None and v != ""}
    if isinstance(filled.get("avg_view_sec"), str):  # typed like the app shows it: "0:26"
        seconds = parse_duration(str(filled.pop("avg_view_sec")))
        if seconds is not None:
            filled["avg_view_sec"] = seconds
    state, when = platform_states(ep, now).get(platform, ("live", None))
    if state == "scheduled":
        raise StudioError(f"{PLATFORMS.get(platform, platform)} goes live {when:%Y-%m-%d %H:%M}; "
                          "add its numbers after that.")
    try:
        snap = MetricSnapshot(episode_id=episode_id, platform=platform,
                              day=age_days(ep, platform, now), source=source,
                              note=note.strip(), **filled)
    except ValidationError as e:
        problems = [f"{'.'.join(str(p) for p in x['loc']) or 'numbers'}: "
                    f"{x['msg'].removeprefix('Value error, ')}" for x in e.errors()]
        raise StudioError("These numbers don't look right:\n- " + "\n- ".join(problems)) from None
    repo.add_metrics(snap, actor=actor)
    return snap, sanity_warnings(ep, snap)


def sanity_warnings(ep: Episode, snap: MetricSnapshot) -> list[str]:
    video = latest_asset(ep, AssetKind.FINAL_VIDEO)
    length = video.duration_sec if video else None
    if snap.avg_view_sec is not None and 0 < snap.avg_view_sec < 1:
        return [f"Average watch time {snap.avg_view_sec:g} s is under one second. The apps "
                f"write minutes:seconds, so \"0:26\" means 26 seconds: if that was it, add a "
                f"corrected reading with 0:26 (or 26)."]
    if snap.avg_view_sec and length and snap.avg_view_sec > length * 1.5:
        return [f"Average watch time {snap.avg_view_sec:g} s is much longer than the video "
                f"({length:.0f} s). Possible (replays), but check the box."]
    return []


def parse_duration(text: str | float | int | None) -> float | None:
    """A watch time as the apps show it: "0:26", "1:05", "26" or "26.5" -> seconds.
    (Typing YouTube's "0:26" as 0.26 was an easy mistake.)"""
    if text is None or isinstance(text, int | float):
        return None if text is None else float(text)
    text = text.strip().replace(",", ".")
    if not text:
        return None
    try:
        if ":" in text:
            minutes, seconds = text.split(":", 1)
            return round(int(minutes or 0) * 60 + float(seconds), 2)
        return float(text)
    except ValueError:
        raise StudioError(f"'{text}' is not a watch time: write it like 0:26 or 26.") from None


# ---------------------------------------------------------------- read
def latest(snaps: list[MetricSnapshot]) -> dict[str, MetricSnapshot]:
    """The newest snapshot per platform."""
    out: dict[str, MetricSnapshot] = {}
    for s in snaps:  # oldest first, so later ones win
        out[s.platform] = s
    return out


def rates(s: MetricSnapshot) -> dict[str, float | None]:
    """Per-1,000-view rates and engagement %, comparable between videos of different reach."""
    def per_1k(n):
        return round(n * 1000 / s.views, 1) if n is not None and s.views else None
    actions = [n for n in (s.likes, s.comments, s.shares, s.saves) if n is not None]
    return {"likes_per_1k": per_1k(s.likes), "comments_per_1k": per_1k(s.comments),
            "shares_per_1k": per_1k(s.shares),
            "engagement_pct": (round(sum(actions) * 100 / s.views, 2)
                               if actions and s.views else None)}


# ------------------------------------------------------------- check-ins
@dataclass(frozen=True)
class CheckIn:
    episode_id: str
    topic: str
    platform: str
    checkpoint: int      # the day-1/7/30 check-in this is
    age: int             # how old the video is now (days)
    url: str


def _url_for(ep: Episode, platform: str) -> str:
    return next((u for u in ep.publication.urls if platform_of(u) == platform), "")


def next_checkin(snaps: list[MetricSnapshot], age: int) -> tuple[int | None, bool]:
    """(checkpoint, due_now). Only the latest passed checkpoint counts: a missed day-1 reading
    can't be taken later, so on day 9 the day-7 one is due, not day 1."""
    best = max((s.day for s in snaps), default=-1)
    passed = [c for c in CHECKPOINTS if c <= age]
    if passed and best < passed[-1]:
        return passed[-1], True
    upcoming = [c for c in CHECKPOINTS if c > age]
    return (upcoming[0] if upcoming else None), False


def due_checkins(repo: EpisodeRepository, now: datetime | None = None) -> list[CheckIn]:
    """Every published video × platform whose numbers should be read today."""
    snaps = repo.list_metrics()
    due = []
    for ep in repo.all_episodes():
        if ep.status != Status.PUBLISHED:
            continue
        for p in live_platforms(ep, now):
            age = age_days(ep, p, now)
            mine = [s for s in snaps if s.episode_id == ep.episode_id and s.platform == p]
            checkpoint, is_due = next_checkin(mine, age)
            if is_due and checkpoint is not None:
                due.append(CheckIn(ep.episode_id, ep.metadata.topic, p, checkpoint, age,
                                   _url_for(ep, p)))
    return sorted(due, key=lambda c: (c.episode_id, list(PLATFORMS).index(c.platform)))


# ---------------------------------------------------------------- tables
def video_rows(repo: EpisodeRepository) -> list[dict]:
    """The newest numbers of every published video on every platform, with rates."""
    topics = {e.episode_id: e.metadata.topic for e in repo.all_episodes()}
    by_video: dict[tuple[str, str], MetricSnapshot] = {}
    for s in repo.list_metrics():
        by_video[(s.episode_id, s.platform)] = s
    rows = []
    for (ep_id, p), s in sorted(by_video.items()):
        r = rates(s)
        rows.append({"Episode": ep_id, "Topic": topics.get(ep_id, ""),
                     "Platform": PLATFORMS.get(p, p), "Day": s.day, "Views": s.views,
                     "Likes": s.likes, "Shares": s.shares, "Comments": s.comments,
                     "Stayed %": s.stayed_pct, "Avg watch (s)": s.avg_view_sec,
                     "Likes / 1k views": r["likes_per_1k"],
                     "Shares / 1k views": r["shares_per_1k"],
                     "Engagement %": r["engagement_pct"]})
    return rows


def _closest(snaps: list[MetricSnapshot], day: int) -> MetricSnapshot | None:
    """The snapshot read nearest to `day` (ties: the later one)."""
    return min(snaps, key=lambda s: (abs(s.day - day), -s.day), default=None)


def compare(repo: EpisodeRepository, *, by: str, platform: str, day: int = 7) -> list[dict]:
    """Which hook styles / categories / series do better, on one platform, at about `day`."""
    if by not in GROUP_BY:
        raise StudioError(f"Compare by one of: {', '.join(GROUP_BY)}.")
    eps = {e.episode_id: e for e in repo.all_episodes()}
    snaps = [s for s in repo.list_metrics() if s.platform == platform]
    groups: dict[str, list[MetricSnapshot]] = {}
    for ep_id in sorted({s.episode_id for s in snaps}):
        pick = _closest([s for s in snaps if s.episode_id == ep_id], day)
        ep = eps.get(ep_id)
        if pick is None or ep is None:
            continue
        key = _GROUPERS[by](ep, platform).strip().lower() or "(not set)"
        groups.setdefault(key, []).append(pick)

    def med(values):
        values = [v for v in values if v is not None]
        return round(median(values), 1) if values else None

    rows = []
    for key, picks in sorted(groups.items()):
        rows.append({GROUP_BY[by]: key, "Videos": len(picks),
                     "Median views": med(s.views for s in picks),
                     "Median stayed %": med(s.stayed_pct for s in picks),
                     "Median avg watch (s)": med(s.avg_view_sec for s in picks),
                     "Median shares / 1k views": med(rates(s)["shares_per_1k"] for s in picks),
                     "Trust": ("ok" if len(picks) >= TRUST_MIN_VIDEOS
                               else "too few videos to trust yet")})
    return rows


def export_csv(repo: EpisodeRepository) -> str:
    """Every snapshot, for a spreadsheet."""
    topics = {e.episode_id: e.metadata.topic for e in repo.all_episodes()}
    out = io.StringIO()
    cols = ["episode_id", "topic", "platform", "day", "captured_at", *NUMBER_FIELDS,
            "source", "note"]
    w = csv.DictWriter(out, fieldnames=cols)
    w.writeheader()
    for s in repo.list_metrics():
        w.writerow(s.model_dump(include=set(cols)) | {"topic": topics.get(s.episode_id, "")})
    return out.getvalue()
