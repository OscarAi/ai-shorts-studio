"""What each platform expects, and a ready-to-paste post text for each.

Numbers checked 2026-10-13: AI-label rules from the platforms' own help pages; limits
cross-checked across several current guides. Platforms change these: re-check now and then.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, NamedTuple

from .models import ClaimStatus, Episode
from .timeutil import local_now, parse_local, utc_to_local

PLATFORMS = {"youtube_shorts": "YouTube Shorts", "tiktok": "TikTok",
             "instagram_reels": "Instagram Reels"}
TITLE_MAX = 100  # YouTube's title limit (the one title is used everywhere)

_DOMAINS = {"youtube_shorts": ("youtube.com", "youtu.be"),
            "tiktok": ("tiktok.com",),
            "instagram_reels": ("instagram.com",)}


@dataclass(frozen=True)
class PlatformRules:
    text_max: int                 # characters in the description / caption
    visible_chars: int | None     # shown before "more" (None: not cut that way)
    hashtags_max: int | None      # hard limit (None: no small hard limit)
    hashtags_best: tuple[int, int]
    tips: tuple[str, ...]
    sources: tuple[str, ...] = field(default=())


# Same advice everywhere (docs/STRATEGY.md); your own numbers decide in the end.
WHEN_TO_POST = ("When: early evening in your viewers' time zone (about 18:00-21:00) is a common "
                "best time; late at night few people are scrolling. Check Analytics → compare by "
                "\"Posted (time of day)\".")

RULES = {
    "youtube_shorts": PlatformRules(
        text_max=5000, visible_chars=None, hashtags_max=None, hashtags_best=(3, 5),
        tips=("Title: up to 100 characters; the first ~40 matter most.",
              "The first 3 hashtags of the description show above the title.",
              "Details → \"AI use\": Yes for realistic AI visuals (YouTube requires it; for clearly "
              "stylized pictures it's optional, but recommended for trust).",
              "Subtitles: upload the .srt (Download it below).",
              "Visibility → Schedule to pick the go-live time, then come back and mark it scheduled.",
              WHEN_TO_POST),
        sources=("https://support.google.com/youtube/answer/14328491",)),
    "tiktok": PlatformRules(
        text_max=2200, visible_chars=100, hashtags_max=None, hashtags_best=(3, 5),
        tips=("Put the hook first: only about the first 100 characters show before \"more\".",
              "More options → turn on \"AI-generated content\" (required for realistic AI).",
              "3-5 relevant hashtags work better than many.", WHEN_TO_POST),
        sources=("https://www.tiktok.com/creator-academy/en/article/ai-generated-content-label",)),
    "instagram_reels": PlatformRules(
        text_max=2200, visible_chars=125, hashtags_max=5, hashtags_best=(3, 5),
        tips=("Instagram allows at most 5 hashtags (since December 2025).",
              "Only about the first 125 characters show before \"more\".",
              "Turn on the \"AI info\" label (Advanced settings) when posting AI visuals.",
              "Choose a cover frame that shows the hook.", WHEN_TO_POST),
        sources=("https://transparency.meta.com/governance/tracking-impact/labeling-ai-content/",)),
}

SHORT_AI_NOTE = "AI-made visuals; facts checked against the sources."


def platform_of(url: str) -> str | None:
    """Which platform a link belongs to, from its address."""
    host = url.lower().split("//", 1)[-1].split("/", 1)[0]
    for platform, domains in _DOMAINS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return platform
    return None


def posted_platforms(ep: Episode) -> list[str]:
    """Platforms this episode has a link for (= really posted there), in PLATFORMS order."""
    found = {platform_of(u) for u in ep.publication.urls}
    return [p for p in PLATFORMS if p in found]


# ------------------------------------------------- where the video is live, and since when
class GoLive(NamedTuple):
    """One platform's state: ("live", since) or ("scheduled", when it goes live)."""
    state: Literal["live", "scheduled"]
    when: datetime | None


def went_live(ep: Episode, platform: str) -> datetime | None:
    """When the video went live on this platform (local time): the time recorded when its link
    was added, else its schedule, else when the episode was marked published."""
    pub = ep.publication
    when = pub.live_at.get(platform) or pub.schedule.get(platform)
    if when:
        return parse_local(when)
    return utc_to_local(pub.published_at) if pub.published_at else None


def platform_states(ep: Episode, now: datetime | None = None) -> dict[str, GoLive]:
    """Every platform with a link, live or still scheduled. An episode can be live on one
    platform and scheduled on another; it counts as published once any is live."""
    now = now or local_now()
    out = {}
    for p in posted_platforms(ep):
        start = went_live(ep, p)
        out[p] = GoLive("scheduled" if start is not None and start > now else "live", start)
    return out


def live_platforms(ep: Episode, now: datetime | None = None) -> list[str]:
    return [p for p, s in platform_states(ep, now).items() if s.state == "live"]


def clean_hashtags(text: str) -> list[str]:
    """Every hashtag in any spelling: '#a#b c,d' -> ['#a', '#b', '#c', '#d'], no duplicates."""
    tags: list[str] = []
    for word in re.findall(r"\w+", text):
        if f"#{word}".lower() not in (t.lower() for t in tags):
            tags.append(f"#{word}")
    return tags


def source_publishers(ep: Episode) -> list[str]:
    """Publishers behind the approved facts, in order, once each."""
    cited = {sid for c in ep.research.claims if c.status == ClaimStatus.APPROVED
             for sid in c.source_ids}
    out: list[str] = []
    for x in ep.research.sources:
        if x.source_id in cited and x.publisher not in out:
            out.append(x.publisher)
    return out


def _tags(ep: Episode, platform: str) -> list[str]:
    # cleaned again: episodes saved before the hashtag fix may hold "#a#b#c" as one tag
    tags = clean_hashtags(" ".join(ep.publication.hashtags)) or [f"#{ep.metadata.category.value}"]
    if platform == "youtube_shorts" and "#shorts" not in (t.lower() for t in tags):
        tags = tags[:4] + ["#Shorts"]
    limit = RULES[platform].hashtags_max or RULES[platform].hashtags_best[1]
    return tags[:limit]


def draft_post(ep: Episode, platform: str) -> str:
    """A ready-to-paste description/caption built only from the episode's approved content."""
    hook = ep.script.hook or ep.publication.title or ep.metadata.topic
    angle = ep.metadata.story_angle.strip().rstrip(".")
    pubs = source_publishers(ep)
    sources = f"Sources: {'; '.join(pubs)}" if pubs else ""
    series = (f"Follow for more {ep.metadata.series}." if ep.metadata.series
              else "Follow for more stories like this.")
    ai = SHORT_AI_NOTE if ep.publication.ai_disclosure else ""
    tags = " ".join(_tags(ep, platform))
    if platform == "youtube_shorts":
        parts = [hook, f"{angle}." if angle else "", sources, ai, tags]
    else:  # TikTok and Instagram: the hook must be in the visible first line
        parts = [hook, f"{angle}." if angle else "", series, sources, ai, tags]
    return "\n\n".join(p for p in parts if p)


def check_post(platform: str, text: str) -> list[str]:
    """Plain-word warnings about a post text for this platform (empty = fine)."""
    r = RULES[platform]
    warnings = []
    if len(text) > r.text_max:
        warnings.append(f"Too long: {len(text)} of {r.text_max} characters.")
    first = text.strip().split("\n", 1)[0] if text.strip() else ""
    if r.visible_chars and len(first) > r.visible_chars:
        warnings.append(f"The first line is {len(first)} characters; only about "
                        f"{r.visible_chars} show before \"more\".")
    n = len([w for w in text.split() if w.startswith("#")])
    if r.hashtags_max and n > r.hashtags_max:
        warnings.append(f"{n} hashtags: {PLATFORMS[platform]} allows at most {r.hashtags_max}.")
    elif n and not r.hashtags_best[0] <= n <= r.hashtags_best[1] + 1:
        warnings.append(f"{n} hashtags: {r.hashtags_best[0]}-{r.hashtags_best[1]} usually work best.")
    return warnings
