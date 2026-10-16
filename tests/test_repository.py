import pytest
from helpers import open_gate, walk_to

from studio.errors import (
    ConcurrentEditError,
    IllegalTransition,
    ImmutableEpisodeError,
    ProtectedChangeError,
    SectionOwnershipError,
    StudioError,
    ValidationFailed,
)
from studio.models import Category, Status


def walk(repo, episode_id, upto):
    walk_to(repo, episode_id, upto)


def test_ids_are_permanent_and_sequential(repo):
    a = repo.create_episode("A")
    b = repo.create_episode("B")
    assert (a.episode_id, b.episode_id) == ("EP_000001", "EP_000002")


def test_new_episode_starts_at_idea_version_1(repo):
    ep = repo.create_episode("Topic", Category.SCIENCE)
    assert ep.status == Status.IDEA and ep.version == 1


def test_save_creates_new_version_and_keeps_history(repo):
    ep = repo.create_episode("Topic")
    ep.metadata.story_angle = "angle one"
    saved = repo.save(ep, actor="human", note="add angle")
    assert saved.version == 2
    assert repo.get_version(ep.episode_id, 1).metadata.story_angle == ""
    assert repo.get_version(ep.episode_id, 2).metadata.story_angle == "angle one"
    assert len(repo.list_versions(ep.episode_id)) == 2


def test_stale_copy_is_rejected(repo):
    ep = repo.create_episode("Topic")
    repo.save(ep, actor="a")
    with pytest.raises(ConcurrentEditError):
        repo.save(ep, actor="b")


def test_status_cannot_change_through_save(repo):
    ep = repo.create_episode("Topic")
    ep = ep.model_copy(update={"status": Status.SCRIPT})
    with pytest.raises(StudioError):
        repo.save(ep, actor="x")


def test_section_ownership_is_enforced(repo):
    ep = repo.create_episode("Topic")
    ep.metadata.tone = "dark"
    with pytest.raises(SectionOwnershipError):
        repo.save(ep, actor="researcher", allowed_sections={"research"})
    repo.save(ep, actor="human", allowed_sections={"metadata"})


def test_illegal_transition_is_blocked(repo):
    ep = repo.create_episode("Topic")
    with pytest.raises(IllegalTransition):
        repo.transition(ep.episode_id, Status.SCRIPT, actor="x")


def test_rules_block_transition_and_state_is_unchanged(repo):
    ep = repo.create_episode("Topic")
    open_gate(repo, ep.episode_id)
    repo.transition(ep.episode_id, Status.RESEARCH, actor="x")
    with pytest.raises(ValidationFailed):
        repo.transition(ep.episode_id, Status.FACT_CHECK, actor="x")  # no claims yet
    assert repo.get_episode(ep.episode_id).status == Status.RESEARCH


def test_full_pipeline_walk(repo, full_episode):
    walk(repo, full_episode.episode_id, Status.PUBLISHED)
    final = repo.get_episode(full_episode.episode_id)
    assert final.status == Status.PUBLISHED
    assert final.publication.published_at is not None


def test_published_episode_is_immutable(repo, full_episode):
    walk(repo, full_episode.episode_id, Status.PUBLISHED)
    ep = repo.get_episode(full_episode.episode_id)
    ep.metadata.tone = "changed"
    with pytest.raises(ImmutableEpisodeError):
        repo.save(ep, actor="x")


def test_going_backward_requires_reason(repo, full_episode):
    walk(repo, full_episode.episode_id, Status.QA)
    with pytest.raises(StudioError):
        repo.transition(full_episode.episode_id, Status.SCRIPT, actor="x")
    ep = repo.transition(full_episode.episode_id, Status.SCRIPT, actor="x", reason="hook too weak")
    assert ep.status == Status.SCRIPT


def test_reject_requires_reason_and_is_recorded(repo):
    ep = repo.create_episode("Topic")
    with pytest.raises(StudioError):
        repo.transition(ep.episode_id, Status.REJECTED, actor="x")
    rejected = repo.transition(ep.episode_id, Status.REJECTED, actor="x", reason="weak sourcing")
    assert rejected.rejection.reason == "weak sourcing"
    assert repo.list_rejections(ep.episode_id)[0]["reason"] == "weak sourcing"


def test_rejected_episode_can_be_revived(repo):
    ep = repo.create_episode("Topic")
    repo.transition(ep.episode_id, Status.REJECTED, actor="x", reason="not now")
    revived = repo.transition(ep.episode_id, Status.IDEA, actor="x", reason="new angle found")
    assert revived.status == Status.IDEA and revived.rejection is None
    assert repo.list_rejections(ep.episode_id)[0]["revived_at"] is not None


def test_approved_claim_cannot_be_silently_changed(repo, full_episode):
    ep = repo.get_episode(full_episode.episode_id)
    ep.research.claims[0].statement = "A different, sneakier statement."
    with pytest.raises(ProtectedChangeError):
        repo.save(ep, actor="researcher")
    repo.save(ep, actor="human", allow_claim_changes=True, note="deliberate rewrite")


def test_approved_claim_cannot_be_deleted(repo, full_episode):
    ep = repo.get_episode(full_episode.episode_id)
    ep.research.claims = ep.research.claims[1:]
    with pytest.raises(ProtectedChangeError):
        repo.save(ep, actor="researcher")


def test_search_and_filter(repo):
    repo.create_episode("Eiffel Tower paint job", Category.GEOGRAPHY)
    repo.create_episode("Penicillin mold", Category.INVENTIONS, series="Invented by Accident")
    assert len(repo.list_episodes(search="eiffel")) == 1
    assert len(repo.list_episodes(category="inventions")) == 1
    assert len(repo.list_episodes(series="Invented by Accident")) == 1
    assert len(repo.list_episodes(status="IDEA")) == 2
    assert repo.count_by_status() == {"IDEA": 2}


def test_claims_are_indexed_library_wide(repo, full_episode):
    import sqlite3
    conn = sqlite3.connect(repo.db_path)
    n = conn.execute("SELECT COUNT(*) FROM claims WHERE episode_id=?",
                     (full_episode.episode_id,)).fetchone()[0]
    conn.close()
    assert n == 3


def test_events_audit_log(repo, full_episode):
    open_gate(repo, full_episode.episode_id)
    repo.transition(full_episode.episode_id, Status.RESEARCH, actor="alice")
    events = repo.list_events(full_episode.episode_id)
    assert events[0]["event_type"] == "created"
    assert events[-1]["actor"] == "alice"


def test_backup_creates_a_copy(repo, tmp_path):
    repo.create_episode("Topic")
    target = repo.backup(tmp_path / "bk")
    assert target.exists() and target.stat().st_size > 0


def test_backup_includes_media_files(repo, tmp_path):
    media = repo.settings.data_dir / "assets" / "EP_000001"
    media.mkdir(parents=True)
    (media / "A001.png").write_bytes(b"picture")
    (media / "_build").mkdir()  # half-finished build files are not backed up
    target = repo.backup(tmp_path / "bk")
    copied = target.with_name(target.stem + "_assets") / "EP_000001"
    assert (copied / "A001.png").read_bytes() == b"picture"
    assert not (copied / "_build").exists()


def test_daily_auto_backup_once_a_day_keeps_the_newest(settings):
    from datetime import datetime, timedelta

    from studio.db import EpisodeRepository
    settings = settings.model_copy(update={"auto_backup_keep": 3})
    repo = EpisodeRepository(settings)
    repo.create_episode("Velcro")
    start = datetime(2026, 10, 1)
    first = repo.auto_backup(start)
    assert first.name == "studio_20261001.db" and first.stat().st_size > 0
    assert repo.auto_backup(start) is None  # already done today
    for n in range(1, 5):
        repo.auto_backup(start + timedelta(days=n))
    names = sorted(p.name for p in first.parent.iterdir())
    assert names == ["studio_20261003.db", "studio_20261004.db", "studio_20261005.db"]
    off = EpisodeRepository(settings.model_copy(update={"auto_backup_keep": 0}))
    assert off.auto_backup(datetime(2027, 1, 1)) is None
