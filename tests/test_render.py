import subprocess

import pytest
from helpers import open_gate
from media import clip, image, needs_ffmpeg, probe_video, tone

from studio.agents import run_mock_pipeline
from studio.assets import abs_path, add_narration, add_shot_asset, narration_text
from studio.errors import StudioError
from studio.models import AssetKind, Status
from studio.render import FPS, build_video, frame_plan
from studio.subtitles import FakeAligner, Word, make_subtitles, script_words
from studio.validators import validate_for_transition

LICENSE = "test file, made with FFmpeg"


def test_frame_plan_matches_the_narration_exactly():
    frames = frame_plan([4, 10, 12, 9, 5], narration_sec=46.0)
    assert sum(frames) == 46 * FPS
    assert frames[2] > frames[1] > frames[0]  # proportions kept


@pytest.fixture
def filled(repo, tmp_path):
    """PRODUCTION episode with every shot filled: portrait, landscape and a short clip."""
    ep = repo.create_episode("The accidental invention of velcro")
    open_gate(repo, ep.episode_id)
    ep = run_mock_pipeline(repo, ep.episode_id)
    files = [(image(tmp_path / "1.png"), "1.png"),
             (image(tmp_path / "2.jpg", 1920, 1080, "blue"), "2.jpg"),  # landscape
             (clip(tmp_path / "3.mp4", 0.5), "3.mp4"),                  # too short
             (image(tmp_path / "4.png", color="green"), "4.png"),
             (image(tmp_path / "5.webp", 800, 800, "red"), "5.webp")]   # square
    for n, shot in enumerate(ep.storyboard.shots):  # the five kinds, repeated over all shots
        data, name = files[n % len(files)]
        add_shot_asset(repo, ep.episode_id, shot.shot_id, data, name, provider="test",
                       license_note=LICENSE, actor="human")
    add_narration(repo, ep.episode_id, tone(tmp_path / "v.wav", 4), "v.wav",
                  license_note=LICENSE, actor="human")
    return ep.episode_id


def codes(repo, eid):
    return {i.code for i in validate_for_transition(repo.get_episode(eid), Status.QA,
                                                    repo.settings)}


@needs_ffmpeg
def test_build_makes_a_vertical_video_as_long_as_the_narration(repo, filled):
    ep = build_video(repo, filled, actor="human")
    video = ep.assets[-1]
    assert video.kind == AssetKind.FINAL_VIDEO and video.asset_id == "final_v1"
    info = probe_video(abs_path(repo, video))
    assert (info["width"], info["height"]) == (1080, 1920)
    assert (info["vcodec"], info["acodec"]) == ("h264", "aac")
    assert info["duration"] == pytest.approx(4.0, abs=0.15)
    assert "VIDEO_OUTDATED" not in codes(repo, filled)
    assert not (abs_path(repo, video).parent / "_build").exists()  # temp files cleaned


@needs_ffmpeg
def test_changing_a_shot_makes_the_video_outdated(repo, filled, tmp_path):
    build_video(repo, filled, actor="human")
    add_shot_asset(repo, filled, "SHOT01", image(tmp_path / "new.png", color="purple"),
                   "new.png", provider="test", license_note=LICENSE, actor="human")
    assert "VIDEO_OUTDATED" in codes(repo, filled)
    ep = build_video(repo, filled, actor="human")
    assert ep.assets[-1].asset_id == "final_v2"
    assert "VIDEO_OUTDATED" not in codes(repo, filled)


@needs_ffmpeg
def test_build_says_what_is_missing(repo, tmp_path):
    ep = repo.create_episode("Velcro")
    open_gate(repo, ep.episode_id)
    ep = run_mock_pipeline(repo, ep.episode_id)
    with pytest.raises(StudioError, match=r"SHOT01.*narration"):
        build_video(repo, ep.episode_id, actor="human")


def band(video, at_sec: float, tmp_path) -> bytes:
    """Grey pixels of the subtitle band (lower middle of the frame) at a moment."""
    raw = tmp_path / f"band_{video.stem}_{at_sec}.gray"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(at_sec), "-i", str(video),
                    "-frames:v", "1", "-vf", "crop=1080:300:0:1250,format=gray",
                    "-f", "rawvideo", str(raw)], check=True)
    return raw.read_bytes()


def changed_pixels(a: bytes, b: bytes) -> int:
    """Pixels that differ a lot (text and its outline), ignoring tiny re-encoding changes."""
    return sum(1 for x, y in zip(a, b, strict=True) if abs(x - y) > 60)


@needs_ffmpeg
def test_subtitles_are_burned_in_at_the_right_time(repo, filled, tmp_path):
    plain = abs_path(repo, build_video(repo, filled, actor="human").assets[-1])
    # The narration is 4 s; "heard" words only from 2.0 s, so the first second has no subtitle.
    words = script_words(narration_text(repo.get_episode(filled)))
    heard = [Word(w, 2.0 + i * 0.05, 2.04 + i * 0.05) for i, w in enumerate(words)]
    make_subtitles(repo, filled, FakeAligner(heard), actor="human")
    assert "VIDEO_OUTDATED" in codes(repo, filled)
    subbed = abs_path(repo, build_video(repo, filled, actor="human").assets[-1])
    assert "VIDEO_OUTDATED" not in codes(repo, filled)
    no_text = changed_pixels(band(plain, 1.0, tmp_path), band(subbed, 1.0, tmp_path))
    text = changed_pixels(band(plain, 2.2, tmp_path), band(subbed, 2.2, tmp_path))
    assert no_text < 50 and text > 1000, (no_text, text)


@needs_ffmpeg
def test_outdated_subtitles_stop_the_build(repo, filled, tmp_path):
    words = script_words(narration_text(repo.get_episode(filled)))
    make_subtitles(repo, filled, FakeAligner(words, 0.05), actor="human")
    add_narration(repo, filled, tone(tmp_path / "new.wav", 4), "new.wav", license_note=LICENSE,
                  actor="human")
    with pytest.raises(StudioError, match="subtitles again"):
        build_video(repo, filled, actor="human")
