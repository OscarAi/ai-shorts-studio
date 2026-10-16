from helpers import open_gate
from media import needs_ffmpeg, produced

from studio.agents import get_agent, run_mock, run_mock_pipeline
from studio.guide import guide, stepper
from studio.models import AssetKind, Status


def labels(g, done=None):
    return [s.label for s in g.steps if done is None or s.done == done]


def test_new_idea_guide(repo):
    ep = repo.create_episode("Velcro")
    g = guide(ep, repo)
    assert g.stage_number == 1 and g.target == Status.RESEARCH and not g.ready
    assert all(not s.done for s in g.steps)
    assert g.headline.startswith("Write the story angle")


def test_idea_becomes_ready_when_rules_agree(repo):
    ep = repo.create_episode("Velcro", story_angle="Burrs on a dog after a mountain walk")
    open_gate(repo, ep.episode_id)
    g = guide(repo.get_episode(ep.episode_id), repo)
    assert all(s.done for s in g.steps) and g.ready and g.headline == "Ready for Research"


def test_research_steps_tick_as_work_is_done(repo):
    ep = repo.create_episode("Velcro", story_angle="Burrs on a dog after a mountain walk")
    open_gate(repo, ep.episode_id)
    repo.transition(ep.episode_id, Status.RESEARCH, actor="t")
    assert labels(guide(repo.get_episode(ep.episode_id), repo), done=True) == []
    run_mock(repo, ep.episode_id, get_agent("researcher"))  # mock links count as checked
    g = guide(repo.get_episode(ep.episode_id), repo)
    assert all(s.done for s in g.steps) and g.ready


def test_production_counts_pictures(repo):
    ep = repo.create_episode("Velcro", story_angle="Burrs on a dog after a mountain walk")
    open_gate(repo, ep.episode_id)
    ep = run_mock_pipeline(repo, ep.episode_id)
    g = guide(ep, repo)
    assert g.steps[0].label == "A picture for every shot (0 of 12)"
    assert [s.label for s in g.steps][1:] == ["Narration (30-55 s)", "Subtitles", "Build the video"]
    assert not g.ready and g.blockers


@needs_ffmpeg
def test_produced_episode_is_ready_for_qa(repo, tmp_path):
    eid = produced(repo, tmp_path)
    g = guide(repo.get_episode(eid), repo)
    assert all(s.done for s in g.steps) and g.ready and g.target == Status.QA


def test_published_and_approved_have_no_continue(repo, full_episode):
    ep = full_episode.model_copy(update={"status": Status.APPROVED})
    assert guide(ep, repo).target is None  # publishing needs the links: "Mark as published"


def test_stepper_marks_the_current_stage():
    line = stepper(Status.SCRIPT)
    assert line.startswith("✅ Idea → ✅ Research → ✅ Fact check → 🔵 **Script**")
    assert stepper(Status.REJECTED) == "⛔ Rejected"


@needs_ffmpeg
def test_a_rule_added_later_points_back_to_production(repo, tmp_path):
    """Like EP_000003: video built and QA approved before subtitles were required."""
    eid = produced(repo, tmp_path)
    repo.transition(eid, Status.QA, actor="t")
    ep = repo.get_episode(eid)
    no_subs = [a for a in ep.assets if a.kind != AssetKind.CAPTIONS]
    ep = repo.save(ep.model_copy(update={"assets": no_subs}), actor="t",
                   allowed_sections={"assets"})
    g = guide(ep, repo)
    assert not g.ready and g.go_back_to == Status.PRODUCTION
    assert g.headline == "Back to Production: Make the subtitles."
