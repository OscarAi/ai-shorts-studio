"""Optional "make it catchier": an AI writes the title and the three post texts.

Same pattern as the agents: a prompt to paste into any chat AI (it contains ONLY the approved
facts and must add none), a checked JSON answer, and a mock for tests. The answer only
pre-fills the texts; you read and edit them before saving.
"""
from pydantic import Field, field_validator

from .agents import Lenient, parse_answer
from .models import ClaimStatus, Episode
from .platforms import RULES, TITLE_MAX, clean_hashtags, source_publishers
from .prompts import render


class CopyOutput(Lenient):
    youtube_title: str = Field(min_length=1)
    youtube_description: str = ""
    tiktok_caption: str = ""
    instagram_caption: str = ""
    hashtags: list[str] = Field(default_factory=list)

    @field_validator("youtube_title")
    @classmethod
    def _title_fits(cls, v: str) -> str:
        v = v.strip()
        return v if len(v) <= TITLE_MAX else v[:TITLE_MAX - 1].rsplit(" ", 1)[0] + "…"

    @property
    def captions(self) -> dict[str, str]:
        """Post texts with the hashtags added at the end (Instagram: at most 5)."""
        tags = clean_hashtags(" ".join(self.hashtags))
        out = {}
        for platform, text in (("youtube_shorts", self.youtube_description),
                               ("tiktok", self.tiktok_caption),
                               ("instagram_reels", self.instagram_caption)):
            if text.strip():
                limit = RULES[platform].hashtags_max or RULES[platform].hashtags_best[1]
                out[platform] = text.strip() + ("\n\n" + " ".join(tags[:limit]) if tags else "")
        return out


def build_copy_prompt(ep: Episode) -> str:
    approved = [c for c in ep.research.claims if c.status == ClaimStatus.APPROVED]
    return render("copywriter", {
        "topic": ep.metadata.topic,
        "story_angle": ep.metadata.story_angle or "(none)",
        "hook": ep.script.hook or "(none)",
        "series": ep.metadata.series or "(none)",
        "title": ep.publication.title or "(none yet)",
        "claims": "\n".join(f"- {c.statement}" for c in approved) or "(no approved facts)",
        "sources": "; ".join(source_publishers(ep)) or "(none)",
    })


def parse_copy(text: str) -> CopyOutput:
    return parse_answer(CopyOutput, text)


def mock_copy(ep: Episode) -> CopyOutput:
    hook = ep.script.hook or ep.metadata.topic
    return CopyOutput(
        youtube_title=f"[MOCK] {ep.metadata.topic}",
        youtube_description=f"[MOCK] {hook}\n\nSources: {'; '.join(source_publishers(ep))}",
        tiktok_caption=f"[MOCK] {hook}",
        instagram_caption=f"[MOCK] {hook}\n\nFollow for more.",
        hashtags=["#mock", "#history", "#facts"])
