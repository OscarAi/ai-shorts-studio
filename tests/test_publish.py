import pytest
from media import needs_ffmpeg, produced

from studio.errors import ImmutableEpisodeError, StudioError, ValidationFailed
from studio.models import Status
from studio.platforms import clean_hashtags
from studio.review import QA_CHECKS, default_description, mark_published, set_publication, set_qa

pytestmark = needs_ffmpeg
ALL = dict.fromkeys(QA_CHECKS, True)


@pytest.fixture
def at_qa(repo, tmp_path):
    eid = produced(repo, tmp_path)
    repo.transition(eid, Status.QA, actor="t")
    return eid


def test_qa_needs_every_box_ticked(repo, at_qa):
    ep = set_qa(repo, at_qa, {**ALL, "people": False}, actor="human", note="a face in shot 3")
    assert not ep.qa.approved
    assert any("living person" in i for i in ep.qa.issues)
    assert any("a face in shot 3" in i for i in ep.qa.issues)
    with pytest.raises(ValidationFailed):
        repo.transition(at_qa, Status.HUMAN_REVIEW, actor="t")

    ep = set_qa(repo, at_qa, ALL, actor="human", note="watched twice")
    assert ep.qa.approved and not ep.qa.issues
    assert repo.list_versions(at_qa)[-1]["change_note"] == "QA approved: watched twice"
    assert repo.transition(at_qa, Status.HUMAN_REVIEW, actor="t").status == Status.HUMAN_REVIEW


def test_qa_only_at_qa(repo, full_episode):
    with pytest.raises(StudioError, match="QA stage"):
        set_qa(repo, full_episode.episode_id, ALL, actor="human")


def approved(repo, eid):
    set_qa(repo, eid, ALL, actor="human")
    repo.transition(eid, Status.HUMAN_REVIEW, actor="t")
    return repo.transition(eid, Status.APPROVED, actor="t")


def test_publication_package_and_publish(repo, at_qa):
    ep = approved(repo, at_qa)
    description = default_description(ep)
    assert "Sources: Mock Publisher A; Mock Publisher B" in description and "AI" in description
    with pytest.raises(StudioError, match="100"):
        set_publication(repo, at_qa, title="x" * 101, description="", hashtags=[],
                        platforms=["youtube_shorts"], ai_disclosure=True, actor="human")
    ep = set_publication(repo, at_qa, title="Burrs on a dog", description=description,
                         hashtags=["inventions", "#history", "inventions"],
                         platforms=["youtube_shorts", "tiktok"], ai_disclosure=True,
                         actor="human")
    assert ep.publication.hashtags == ["#inventions", "#history"]

    with pytest.raises(StudioError, match="https"):
        mark_published(repo, at_qa, ["youtube.com/shorts/abc"], actor="human")
    ep = mark_published(repo, at_qa, ["https://youtube.com/shorts/abc"], actor="human")
    assert ep.status == Status.PUBLISHED and ep.publication.published_at
    with pytest.raises(ImmutableEpisodeError):
        repo.save(ep, actor="human")


def test_publishing_needs_a_title_and_platform(repo, at_qa):
    approved(repo, at_qa)
    with pytest.raises(ValidationFailed) as err:
        mark_published(repo, at_qa, ["https://youtube.com/shorts/abc"], actor="human")
    assert {"PUB_NO_TITLE", "PUB_NO_PLATFORM"} <= {i.code for i in err.value.issues}


def test_unknown_platform_is_refused(repo, at_qa):
    approved(repo, at_qa)
    with pytest.raises(StudioError, match="Unknown platform"):
        set_publication(repo, at_qa, title="t", description="", hashtags=[],
                        platforms=["myspace"], ai_disclosure=True, actor="human")


def test_hashtags_typed_without_spaces_are_split():
    """The exact text from EP_000003: it used to become ONE hashtag."""
    assert clean_hashtags("#history#food#dumplings#funfact#fyi") == [
        "#history", "#food", "#dumplings", "#funfact", "#fyi"]
    assert clean_hashtags("history, #Food  food #fyi") == ["#history", "#Food", "#fyi"]
