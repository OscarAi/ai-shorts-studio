"""Tiny real media files for tests, generated with FFmpeg (tests skip without FFmpeg)."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from helpers import open_gate

from studio.agents import run_mock_pipeline
from studio.assets import add_narration, add_shot_asset, narration_text
from studio.config import Settings
from studio.db import EpisodeRepository
from studio.render import build_video
from studio.subtitles import FakeAligner, make_subtitles, script_words

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg is not installed")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, timeout=60)


def image(path: Path, w: int = 1080, h: int = 1920, color: str = "orange") -> bytes:
    _ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}", "-frames:v", "1", str(path))
    return path.read_bytes()


def tone(path: Path, seconds: float) -> bytes:
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", str(path))
    return path.read_bytes()


def clip(path: Path, seconds: float, w: int = 720, h: int = 1280) -> bytes:
    _ffmpeg("-f", "lavfi", "-i", f"testsrc=s={w}x{h}:d={seconds}:r=30", "-pix_fmt", "yuv420p",
             str(path))
    return path.read_bytes()


def probe_video(path: Path) -> dict:
    """Width, height, codecs and duration of a finished video."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration:stream=codec_type,codec_name,width,height",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), {})
    return {"width": v["width"], "height": v["height"], "vcodec": v["codec_name"],
            "acodec": a.get("codec_name"), "duration": float(data["format"]["duration"])}


def fake_subtitles(repo, episode_id: str, seconds: float) -> None:
    """Subtitles for the current narration, as if every script word was heard evenly."""
    words = script_words(narration_text(repo.get_episode(episode_id)))
    make_subtitles(repo, episode_id, FakeAligner(words, seconds / len(words)), actor="human")


_TEMPLATES: dict[float, tuple[Path, str]] = {}  # built once per test process


def produced(repo, tmp_path, narration_sec: float = 31) -> str:
    """An episode at PRODUCTION with every shot filled, narration, subtitles and a video.

    Building the video takes FFmpeg 15-50 s, so it is built ONCE per test process and copied
    (database + media files) into each test's own empty data folder."""
    if repo.list_episodes():  # not an empty database: build it here
        return _build_produced(repo, tmp_path, narration_sec)
    if narration_sec not in _TEMPLATES:
        folder = Path(tempfile.mkdtemp(prefix="produced_"))
        _TEMPLATES[narration_sec] = (folder, _build_produced(
            EpisodeRepository(Settings(data_dir=folder)), folder / "_inputs", narration_sec))
    folder, episode_id = _TEMPLATES[narration_sec]
    data = Path(repo.settings.data_dir)
    shutil.copyfile(folder / "studio.db", repo.db_path)
    shutil.copytree(folder / "assets", data / "assets", dirs_exist_ok=True)
    return episode_id


def _build_produced(repo, tmp_path, narration_sec: float) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    ep = repo.create_episode("The accidental invention of velcro")
    open_gate(repo, ep.episode_id)
    ep = run_mock_pipeline(repo, ep.episode_id)
    for shot in ep.storyboard.shots:
        add_shot_asset(repo, ep.episode_id, shot.shot_id, image(tmp_path / f"{shot.shot_id}.png"),
                       f"{shot.shot_id}.png", provider="test", license_note="test file",
                       actor="human")
    add_narration(repo, ep.episode_id, tone(tmp_path / "v.wav", narration_sec), "v.wav",
                  license_note="test file", actor="human")
    fake_subtitles(repo, ep.episode_id, narration_sec)
    build_video(repo, ep.episode_id, actor="human")
    return ep.episode_id
