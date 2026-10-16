"""Topic scout: an AI suggests episode ideas, Python screens them for originality.

Works like the agents (prompt -> paste the answer -> checked by Python, or a free mock),
but it runs BEFORE an episode exists. Every suggestion is compared with the whole library
(and with the other suggestions) by the same originality engine as the gate, in memory.
Nothing is saved until you pick an idea and create it.
"""
from pydantic import Field, field_validator

from .agents import Lenient, parse_answer
from .db import EpisodeRepository
from .models import Category, Episode, Metadata, Status
from .originality import OriginalityReport, evaluate
from .prompts import render

LEVELS = ("low", "medium", "high")


class IdeaIn(Lenient):
    topic: str = Field(min_length=3)
    story_angle: str = ""
    hook_style: str = ""
    category: Category = Category.OTHER
    why_it_works: str = ""
    fact_risk: str = "medium"   # how hard the facts will be to verify
    familiarity: str = "medium"  # how many viewers already know the story (low is better)

    @field_validator("category", mode="before")
    @classmethod
    def _known_category(cls, v):
        """An AI may invent a category: "food history" -> food, "cooking" -> other."""
        words = str(v).strip().lower().replace("-", " ").split()
        return next((c.value for c in Category if c.value in words), Category.OTHER.value)

    @field_validator("fact_risk", "familiarity", mode="before")
    @classmethod
    def _level(cls, v):
        v = str(v).strip().lower()
        return v if v in LEVELS else "medium"


class IdeasOutput(Lenient):
    ideas: list[IdeaIn] = Field(min_length=1)


def library_lines(repo: EpisodeRepository) -> str:
    lines = []
    for ep in repo.all_episodes():
        angle = f" (angle: {ep.metadata.story_angle})" if ep.metadata.story_angle else ""
        rejected = " [rejected]" if ep.status == Status.REJECTED else ""
        lines.append(f"- {ep.metadata.topic}{angle}{rejected}")
    return "\n".join(lines) or "(no episodes yet)"


def build_ideas_prompt(repo: EpisodeRepository, *, series: str = "", category: str = "",
                       theme: str = "", count: int = 5) -> str:
    return render("topic_scout", {
        "series": series or "(none - standalone episodes)",
        "category": category or "any",
        "theme": theme or "(none - surprise me)",
        "count": str(count),
        "library": library_lines(repo),
        "categories": ", ".join(c.value for c in Category),
    })


def parse_ideas(text: str) -> IdeasOutput:
    return parse_answer(IdeasOutput, text)


def mock_ideas() -> IdeasOutput:
    return IdeasOutput(ideas=[
        IdeaIn(topic="[MOCK] The accidental invention of the Post-it note",
               story_angle="A glue that failed became a bestseller", hook_style="question",
               category=Category.INVENTIONS, why_it_works="[MOCK] Everyone owns one.",
               fact_risk="low", familiarity="high"),
        IdeaIn(topic="[MOCK] The accidental invention of the Slinky",
               story_angle="A navy engineer dropped a spring", hook_style="visual surprise",
               category=Category.INVENTIONS, why_it_works="[MOCK] Very visual.",
               fact_risk="medium"),
        IdeaIn(topic="[MOCK] The accidental invention of the Slinky toy",
               story_angle="A navy engineer dropped a spring", hook_style="question",
               category=Category.INVENTIONS, why_it_works="[MOCK] A repeat, to show screening.",
               fact_risk="medium"),
    ])


def _candidate(n: int, idea: IdeaIn) -> Episode:
    return Episode(episode_id=f"IDEA_{n}", metadata=Metadata(
        topic=idea.topic, story_angle=idea.story_angle, category=idea.category))


def screen_ideas(repo: EpisodeRepository, out: IdeasOutput
                 ) -> list[tuple[IdeaIn, OriginalityReport]]:
    """Each idea's originality verdict against the library AND the ideas before it."""
    library = repo.all_episodes()
    screened = []
    for n, idea in enumerate(out.ideas, start=1):
        candidate = _candidate(n, idea)
        screened.append((idea, evaluate(candidate, library, repo.settings, stage="idea")))
        library = library + [candidate]
    return screened


def create_from_idea(repo: EpisodeRepository, idea: IdeaIn, *, series: str = "",
                     actor: str) -> tuple[Episode, OriginalityReport]:
    """Make the episode, then run (and store) its originality check right away."""
    ep = repo.create_episode(idea.topic, idea.category, series=series, actor=actor,
                             story_angle=idea.story_angle, hook_style=idea.hook_style,
                             originality_note=idea.why_it_works)
    return ep, repo.check_originality(ep.episode_id, actor=actor)
