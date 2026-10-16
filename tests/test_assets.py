import pytest
from helpers import open_gate
from media import clip, image, needs_ffmpeg, tone

from studio.agents import run_mock_pipeline
from studio.assets import abs_path, add_narration, add_shot_asset, narration_text
from studio.errors import StudioError, ValidationFailed
from studio.models import AssetKind, ShotStatus, Status

pytestmark = needs_ffmpeg
LICENSE = "test file, made with FFmpeg"


@pytest.fixture
def production(repo):
    ep = repo.create_episode("The accidental invention of velcro")
    open_gate(repo, ep.episode_id)
    return run_mock_pipeline(repo, ep.episode_id)  # PRODUCTION, 5 planned shots


def test_image_is_stored_measured_and_attached(repo, production, tmp_path):
    eid = production.episode_id
    ep = add_shot_asset(repo, eid, "SHOT01", image(tmp_path / "a.png"), "a.png",
                        provider="Gemini", license_note=LICENSE, actor="human")
    asset = ep.assets[-1]
    assert (asset.kind, asset.width, asset.height) == (AssetKind.IMAGE, 1080, 1920)
    assert abs_path(repo, asset).exists() and asset.path.startswith(f"assets/{eid}/")
    shot = ep.storyboard.shots[0]
    assert shot.status == ShotStatus.GENERATED and shot.asset_path == asset.path


def test_replacing_a_shot_file_keeps_the_old_one_on_disk(repo, production, tmp_path):
    eid = production.episode_id
    first = add_shot_asset(repo, eid, "SHOT01", image(tmp_path / "a.png"), "a.png",
                           provider="x", license_note=LICENSE, actor="human").assets[-1]
    ep = add_shot_asset(repo, eid, "SHOT01", clip(tmp_path / "b.mp4", 2), "b.mp4",
                        provider="x", license_note=LICENSE, actor="human")
    shot_assets = [a for a in ep.assets if a.shot_id == "SHOT01"]
    assert len(shot_assets) == 1 and shot_assets[0].kind == AssetKind.VIDEO
    assert shot_assets[0].duration_sec == pytest.approx(2, abs=0.1)
    assert abs_path(repo, first).exists() and shot_assets[0].asset_id != first.asset_id


def test_narration_is_measured(repo, production, tmp_path):
    ep = add_narration(repo, production.episode_id, tone(tmp_path / "v.wav", 3), "v.wav",
                       license_note="my own recording", actor="human")
    voice = ep.assets[-1]
    assert voice.kind == AssetKind.VOICE and voice.duration_sec == pytest.approx(3, abs=0.1)


def test_wrong_files_and_missing_license_are_refused(repo, production, tmp_path):
    eid = production.episode_id
    with pytest.raises(StudioError, match="use an image"):
        add_shot_asset(repo, eid, "SHOT01", b"hello", "notes.txt", provider="x",
                       license_note=LICENSE, actor="human")
    with pytest.raises(StudioError, match="license"):
        add_shot_asset(repo, eid, "SHOT01", image(tmp_path / "a.png"), "a.png", provider="x",
                       license_note=" ", actor="human")
    with pytest.raises(StudioError, match=r"Could not read|no picture"):
        add_shot_asset(repo, eid, "SHOT01", b"not really a png", "fake.png", provider="x",
                       license_note=LICENSE, actor="human")
    with pytest.raises(StudioError, match="no sound"):
        add_narration(repo, eid, image(tmp_path / "b.png"), "b.mp3", license_note=LICENSE,
                      actor="human")
    assert not repo.get_episode(eid).assets  # nothing half-saved


def test_files_only_at_production(repo, tmp_path):
    ep = repo.create_episode("Velcro")
    with pytest.raises(StudioError, match="PRODUCTION"):
        add_narration(repo, ep.episode_id, tone(tmp_path / "v.wav", 1), "v.wav",
                      license_note=LICENSE, actor="human")


def test_qa_needs_every_shot_narration_and_a_video(repo, production, tmp_path):
    eid = production.episode_id
    add_shot_asset(repo, eid, "SHOT01", image(tmp_path / "a.png"), "a.png", provider="x",
                   license_note=LICENSE, actor="human")
    with pytest.raises(ValidationFailed) as err:
        repo.transition(eid, Status.QA, actor="t")
    found = {i.code for i in err.value.issues}
    assert {"SHOT_NOT_READY", "NO_NARRATION", "NO_FINAL_VIDEO"} <= found


def test_narration_must_fit_the_length_limits(repo, production, tmp_path):
    add_narration(repo, production.episode_id, tone(tmp_path / "v.wav", 3), "v.wav",
                  license_note=LICENSE, actor="human")
    with pytest.raises(ValidationFailed) as err:
        repo.transition(production.episode_id, Status.QA, actor="t")
    assert "NARRATION_LENGTH" in {i.code for i in err.value.issues}


def test_narration_text_is_the_script_in_order(production):
    text = narration_text(production)
    assert text.splitlines()[0].startswith("[MOCK] What if") and len(text.splitlines()) == 5
