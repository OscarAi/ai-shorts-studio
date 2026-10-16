"""Data models (Pydantic). These are the single source of truth for the episode shape.

The JSON Schema in docs/ is GENERATED from these classes (python cli.py export-schema),
so the two can never drift apart.
"""
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .timeutil import utc_now


class Strict(BaseModel):
    """Base class: unknown fields are errors, and assignments are re-validated."""
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _no_duplicates(label: str, ids: list[str]) -> None:
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate {label}")


# ----------------------------------------------------------------- enums
class Status(StrEnum):
    IDEA = "IDEA"
    RESEARCH = "RESEARCH"
    FACT_CHECK = "FACT_CHECK"
    SCRIPT = "SCRIPT"
    STORYBOARD = "STORYBOARD"
    PRODUCTION = "PRODUCTION"
    QA = "QA"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"   # uploaded, set to go live later
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"


class Category(StrEnum):
    FOOD = "food"  # the channel's focus (docs/STRATEGY.md)
    HISTORY = "history"
    SCIENCE = "science"
    GEOGRAPHY = "geography"
    INVENTIONS = "inventions"
    TECHNOLOGY = "technology"
    OTHER = "other"


class Importance(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ClaimStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class SourceType(StrEnum):
    MUSEUM = "museum"
    GOVERNMENT = "government"
    UNIVERSITY = "university"
    PRIMARY = "primary"
    REPUTABLE_MEDIA = "reputable_media"
    OTHER = "other"


class Reliability(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SegmentRole(StrEnum):
    """Structural role of a segment in the story."""
    HOOK = "hook"
    SETUP = "setup"
    DEVELOPMENT = "development"
    PAYOFF = "payoff"
    ENDING = "ending"


class ContentKind(StrEnum):
    """What a single SENTENCE is. This is how we trace facts sentence by sentence."""
    FACTUAL = "factual"        # must cite approved claim IDs
    CREATIVE = "creative"      # opinion / flourish / question - asserts no new fact
    TRANSITION = "transition"  # connecting words


class ShotStatus(StrEnum):
    PLANNED = "planned"
    GENERATING = "generating"
    GENERATED = "generated"
    FAILED = "failed"
    APPROVED = "approved"


class AssetKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    VOICE = "voice"
    MUSIC = "music"
    SFX = "sfx"
    CAPTIONS = "captions"
    FINAL_VIDEO = "final_video"


class LinkStatus(StrEnum):
    """What happened when Python opened the source's URL (studio/sources.py)."""
    NOT_CHECKED = "not_checked"
    OK = "ok"              # the page loaded
    BROKEN = "broken"      # 404 / gone / the site does not exist
    BLOCKED = "blocked"    # the site refuses robots (403, captcha): a human must check it
    ERROR = "error"        # timeout or other failure: try again
    MOCK = "mock"          # [MOCK] test link, never fetched


MOCK_URL_PREFIX = "https://example.org/mock/"


# -------------------------------------------------------------- research
class Source(Strict):
    source_id: str = Field(min_length=1)
    title: str
    publisher: str = Field(min_length=1)
    url: str = Field(pattern=r"^https?://\S+$")
    published_date: str | None = None
    accessed_date: str
    source_type: SourceType = SourceType.OTHER
    reliability: Reliability = Reliability.MEDIUM
    evidence_summary: str = ""  # short summary or excerpt - never whole articles
    # Filled in by the link check (Milestone 4)
    link_status: LinkStatus = LinkStatus.NOT_CHECKED
    checked_at: str | None = None
    final_url: str | None = None
    page_title: str = ""
    link_note: str = ""              # e.g. "page title does not match the cited title"
    verified_by: str | None = None   # a human who opened the page and vouches for it
    verified_note: str = ""
    declined_by: str | None = None   # a human decided not to use this source (studio/declines.py)
    declined_note: str = ""

    @property
    def declined(self) -> bool:
        return bool(self.declined_by)

    @property
    def usable(self) -> bool:
        """May a claim rely on this source? Not declined, and loaded fine, mock, or vouched
        for by a human."""
        if self.declined:
            return False
        return (self.link_status in (LinkStatus.OK, LinkStatus.MOCK)
                or self.url.startswith(MOCK_URL_PREFIX) or bool(self.verified_by))


class Evidence(Strict):
    """The sentence(s) on a source's page that best match a claim (a short quote only)."""
    source_id: str
    quote: str = Field(max_length=400)
    score: float = Field(ge=0, le=1)  # share of the claim's key words found in the quote


class Claim(Strict):
    claim_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    importance: Importance = Importance.MEDIUM
    source_ids: list[str] = Field(default_factory=list)
    status: ClaimStatus = ClaimStatus.NEEDS_REVIEW
    notes: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


class Research(Strict):
    claims: list[Claim] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self):
        _no_duplicates("claim_id in research.claims", [c.claim_id for c in self.claims])
        _no_duplicates("source_id in research.sources", [s.source_id for s in self.sources])
        return self


class FactCheck(Strict):
    approved: bool = False
    issues: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- script
class Sentence(Strict):
    text: str = Field(min_length=1)
    kind: ContentKind
    claim_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _factual_needs_claims(self):
        if self.kind == ContentKind.FACTUAL and not self.claim_ids:
            raise ValueError("A factual sentence must list at least one claim_id.")
        return self


class Segment(Strict):
    segment_id: str = Field(min_length=1)
    role: SegmentRole
    sentences: list[Sentence] = Field(default_factory=list)
    duration_sec: float = Field(ge=0.5)
    visual_intent: str = ""
    emotion: str = ""

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.sentences)

    @property
    def claim_ids(self) -> list[str]:
        """Every claim the segment uses, once each, in order."""
        return list(dict.fromkeys(cid for s in self.sentences for cid in s.claim_ids))


class Script(Strict):
    hook: str = ""
    segments: list[Segment] = Field(default_factory=list)

    @property
    def total_estimated_sec(self) -> float:
        """Derived from the segments, so it can never disagree with them."""
        return round(sum(s.duration_sec for s in self.segments), 2)


# ------------------------------------------------------------ storyboard
class Shot(Strict):
    shot_id: str = Field(min_length=1)
    duration_sec: float = Field(ge=0.5)
    segment_ids: list[str] = Field(default_factory=list)
    purpose: str = ""
    location: str = ""
    characters: list[str] = Field(default_factory=list)
    action: str = ""
    camera: str = ""
    visual_style: str = "channel_style_v1"
    continuity_keys: list[str] = Field(default_factory=list)
    generation_prompt: str = ""
    asset_path: str | None = None
    status: ShotStatus = ShotStatus.PLANNED


class Storyboard(Strict):
    shots: list[Shot] = Field(default_factory=list)


# ----------------------------------------------------- assets, qa, publish
class Asset(Strict):
    asset_id: str = Field(min_length=1)
    kind: AssetKind
    path: str
    shot_id: str | None = None
    provider: str = ""          # e.g. "mock", "gemini", "veo"
    cost_usd: float = Field(0.0, ge=0)
    license_note: str = ""      # source + license + proof, esp. for music/sfx
    created_at: str = Field(default_factory=utc_now)
    # Measured by ffprobe when the file is added (Milestone 5)
    duration_sec: float | None = None
    width: int | None = None
    height: int | None = None
    built_from: list[str] = Field(default_factory=list)  # final video: the asset ids it used


class QAResult(Strict):
    deterministic: list[dict[str, Any]] = Field(default_factory=list)
    ai_checks: list[dict[str, Any]] = Field(default_factory=list)
    approved: bool = False
    issues: list[str] = Field(default_factory=list)


class Publication(Strict):
    title: str = ""
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    ai_disclosure: bool = True   # platforms want realistic synthetic media labelled
    published_at: str | None = None
    urls: list[str] = Field(default_factory=list)
    schedule: dict[str, str] = Field(default_factory=dict)  # platform -> "YYYY-MM-DD HH:MM" local
    captions: dict[str, str] = Field(default_factory=dict)  # platform -> post text you saved
    live_at: dict[str, str] = Field(default_factory=dict)   # platform -> "YYYY-MM-DD HH:MM" local


# --------------------------------------------------------------- analytics
COUNT_FIELDS = ("views", "engaged_views", "likes", "comments", "shares", "saves",
                "followers_gained")
NUMBER_FIELDS = COUNT_FIELDS + ("avg_view_sec", "stayed_pct", "completion_pct")


class MetricSnapshot(Strict):
    """A platform's numbers for one video at one moment, copied by hand (later: from an API).
    Snapshots are only ever added, never edited: a typo is fixed by adding a corrected one."""
    episode_id: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    captured_at: str = Field(default_factory=utc_now)
    day: int = Field(0, ge=0)            # the video's age in days when the numbers were read
    views: int | None = Field(None, ge=0)
    engaged_views: int | None = Field(None, ge=0)   # YouTube: the old "view" (watched a while)
    likes: int | None = Field(None, ge=0)
    comments: int | None = Field(None, ge=0)
    shares: int | None = Field(None, ge=0)
    saves: int | None = Field(None, ge=0)
    followers_gained: int | None = Field(None, ge=0)
    avg_view_sec: float | None = Field(None, ge=0)
    stayed_pct: float | None = Field(None, ge=0, le=100)      # YouTube "stayed to watch"
    completion_pct: float | None = Field(None, ge=0, le=100)  # TikTok "watched full video"
    source: str = "manual"
    note: str = ""

    @model_validator(mode="after")
    def _sane(self) -> "MetricSnapshot":
        if all(getattr(self, f) is None for f in NUMBER_FIELDS):
            raise ValueError("Enter at least one number.")
        if self.views is not None:
            for f in ("engaged_views", "likes", "comments", "shares", "saves"):
                n = getattr(self, f)
                if n is not None and n > self.views:
                    raise ValueError(f"{f.replace('_', ' ')} ({n}) can't be more than views "
                                     f"({self.views}). Swapped two boxes?")
        return self


class Rejection(Strict):
    reason: str = Field(min_length=1)
    from_status: Status
    rejected_at: str


# --------------------------------------------------------------- episode
class Metadata(Strict):
    topic: str = Field(min_length=1)
    category: Category = Category.OTHER
    story_angle: str = ""
    series: str = ""            # e.g. "Invented by Accident"
    language: str = "en"
    target_audience: str = "general_adult"
    target_duration_sec: int = Field(35, gt=0)  # allowed range lives in settings.yaml
    tone: str = "curious_cinematic"
    hook_style: str = ""        # lets analytics learn which hook styles work
    originality_note: str = ""


# The top-level sections an agent may be given permission to edit.
SECTIONS = (
    "metadata", "research", "fact_check", "script",
    "storyboard", "assets", "qa", "publication",
)


class Episode(Strict):
    episode_id: str = Field(min_length=1)       # permanent, e.g. EP_000001
    channel_id: str = "main"
    version: int = Field(1, ge=1)
    status: Status = Status.IDEA
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    metadata: Metadata
    research: Research = Field(default_factory=Research)
    fact_check: FactCheck = Field(default_factory=FactCheck)
    script: Script = Field(default_factory=Script)
    storyboard: Storyboard = Field(default_factory=Storyboard)
    assets: list[Asset] = Field(default_factory=list)
    qa: QAResult = Field(default_factory=QAResult)
    publication: Publication = Field(default_factory=Publication)
    rejection: Rejection | None = None

    @model_validator(mode="after")
    def _unique_ids(self):
        _no_duplicates("segment_id in script", [s.segment_id for s in self.script.segments])
        _no_duplicates("shot_id in storyboard", [s.shot_id for s in self.storyboard.shots])
        _no_duplicates("asset_id in assets", [a.asset_id for a in self.assets])
        return self

    # Copies with one part changed (episodes are never edited in place: every save is a
    # new version). Like model_copy, these do not re-validate.
    def with_research(self, **changes) -> "Episode":
        return self.model_copy(update={"research": self.research.model_copy(update=changes)})

    def with_publication(self, **changes) -> "Episode":
        return self.model_copy(update={"publication": self.publication.model_copy(update=changes)})


SHOT_KINDS = (AssetKind.IMAGE, AssetKind.VIDEO)


def latest_asset(ep: Episode, kind: AssetKind) -> Asset | None:
    return next((a for a in reversed(ep.assets) if a.kind == kind), None)


def shot_files(ep: Episode) -> dict[str, Asset]:
    """The picture or clip of each shot that has one: {shot_id: asset}."""
    return {a.shot_id: a for a in ep.assets if a.kind in SHOT_KINDS and a.shot_id}


def subtitles_are_current(ep: Episode) -> bool:
    """Were the latest subtitles timed to the latest narration?"""
    voice, captions = latest_asset(ep, AssetKind.VOICE), latest_asset(ep, AssetKind.CAPTIONS)
    return voice is not None and captions is not None and captions.built_from == [voice.asset_id]


def video_is_current(ep: Episode) -> bool:
    """Was the latest video built from exactly the current shot files, narration and
    subtitles?"""
    video = latest_asset(ep, AssetKind.FINAL_VIDEO)
    return video is not None and sorted(video.built_from) == production_inputs(ep)


def production_inputs(ep: Episode) -> list[str]:
    """Asset ids a final video must be built from right now (shot files, narration and
    subtitles if any)."""
    shot_ids = {s.shot_id for s in ep.storyboard.shots}
    return sorted(a.asset_id for a in ep.assets
                  if (a.kind in SHOT_KINDS and a.shot_id in shot_ids)
                  or a.kind in (AssetKind.VOICE, AssetKind.CAPTIONS))


def changed_sections(old: Episode, new: Episode) -> list[str]:
    """Which top-level sections differ between two versions of an episode."""
    return [name for name in SECTIONS if getattr(old, name) != getattr(new, name)]
