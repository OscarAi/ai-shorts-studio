"""Shortcuts for tests: set up an episode at any stage, through the real rules."""
from studio.models import Category, Status
from studio.originality import Verdict
from studio.samples import sample_sections
from studio.state_machine import STAGE_ORDER

TEST_ANGLE = "Test angle: a clearly different twist on the story"
YT = "https://youtube.com/shorts/abc"


def open_gate(repo, episode_id):
    """Make an idea ready to leave IDEA (test shortcut): give it a story angle if it has
    none, run the originality check and, if it was flagged, accept it."""
    ep = repo.get_episode(episode_id)
    if not ep.metadata.story_angle:
        ep.metadata.story_angle = TEST_ANGLE
        repo.save(ep, actor="test", allowed_sections={"metadata"})
    report = repo.check_originality(episode_id, actor="test")
    if report.verdict != Verdict.PASS:
        repo.decide_originality(report.check_id, "accept", actor="test", note="test")
    return report


def sample_episode(repo, topic: str = "Penicillin", **metadata) -> str:
    """An idea with the complete sample content (approved claims, script, storyboard...)."""
    metadata = {"category": Category.INVENTIONS, "series": "Invented by Accident",
                "story_angle": "A messy lab bench that changed medicine", **metadata}
    ep = repo.create_episode(topic, metadata.pop("category"), series=metadata.pop("series"),
                             **metadata)
    return repo.save(ep.model_copy(update=sample_sections()), actor="test",
                     note="sample content").episode_id


def walk_to(repo, episode_id: str, status: Status):
    """Move an episode forward along the happy path up to `status` (SCHEDULED is skipped
    unless it is the target). Every rule still applies: content must be good enough."""
    ep = repo.get_episode(episode_id)
    if ep.status == Status.IDEA:
        open_gate(repo, episode_id)
    for stage in STAGE_ORDER[STAGE_ORDER.index(ep.status) + 1: STAGE_ORDER.index(status) + 1]:
        if stage == Status.SCHEDULED and status != Status.SCHEDULED:
            continue
        ep = repo.transition(episode_id, stage, actor="test")
    return repo.get_episode(episode_id)


def publish(repo, episode_id, urls=(YT,)):
    """Walk a complete episode (e.g. the full_episode fixture) to PUBLISHED with these links."""
    from studio.review import mark_published
    walk_to(repo, episode_id, Status.APPROVED)
    return mark_published(repo, episode_id, list(urls), actor="test")
