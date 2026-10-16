"""Production files: a picture or clip for each shot, and the narration audio.

Files are stored in data/assets/<episode>/<asset_id>.<ext> (git-ignored). Every file is
measured with ffprobe when it is added, so the rules can check lengths and sizes without
opening files again. Each asset must say where it came from and what its license allows.
"""
import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from .db import EpisodeRepository
from .errors import StudioError
from .models import (
    SHOT_KINDS,
    Asset,
    AssetKind,
    Episode,
    ShotStatus,
    Status,
    latest_asset,  # noqa: F401 (re-exported)
    shot_files,
)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".webm"}
AUDIO_EXT = {".mp3", ".wav", ".m4a"}


@dataclass
class MediaInfo:
    duration_sec: float | None
    width: int | None
    height: int | None
    has_video: bool
    has_audio: bool


class MediaProbe(Protocol):
    def probe(self, path: Path) -> MediaInfo: ...


def find_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise StudioError(f"{name} was not found. Install FFmpeg (e.g. 'scoop install ffmpeg' "
                          f"or 'winget install ffmpeg') and restart the terminal.")
    return path


class FfprobeProbe:
    """Reads duration and size with ffprobe (part of FFmpeg)."""

    def probe(self, path: Path) -> MediaInfo:
        out = subprocess.run(
            [find_tool("ffprobe"), "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
            check=False, capture_output=True, text=True, timeout=60)  # returncode read below
        if out.returncode != 0:
            raise StudioError(f"Could not read {path.name} as a media file.")
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams", [])
        video: dict = next((s for s in streams if s.get("codec_type") == "video"), {})
        duration = data.get("format", {}).get("duration")
        return MediaInfo(
            duration_sec=round(float(duration), 2) if duration not in (None, "N/A") else None,
            width=video.get("width"), height=video.get("height"),
            has_video=bool(video), has_audio=any(s.get("codec_type") == "audio" for s in streams))


# ------------------------------------------------------------------ paths
def episode_dir(repo: EpisodeRepository, episode_id: str) -> Path:
    return Path(repo.settings.data_dir) / "assets" / episode_id


def abs_path(repo: EpisodeRepository, asset: Asset) -> Path:
    return Path(repo.settings.data_dir) / asset.path


def new_asset_id(repo: EpisodeRepository, ep: Episode) -> str:
    """A001, A002... never reused, even for files replaced earlier (still on disk)."""
    folder = episode_dir(repo, ep.episode_id)
    taken = {a.asset_id for a in ep.assets}
    if folder.exists():
        taken |= {p.stem for p in folder.iterdir()}
    n = 1
    while f"A{n:03d}" in taken:
        n += 1
    return f"A{n:03d}"


def _store(repo: EpisodeRepository, ep: Episode, data: bytes, ext: str,
           probe: MediaProbe) -> tuple[str, str, MediaInfo]:
    """Write the file, measure it, and remove it again if it can't be read."""
    asset_id = new_asset_id(repo, ep)
    folder = episode_dir(repo, ep.episode_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{asset_id}{ext}"
    target.write_bytes(data)
    try:
        info = probe.probe(target)
    except StudioError:
        target.unlink(missing_ok=True)
        raise
    rel = target.relative_to(Path(repo.settings.data_dir)).as_posix()
    return asset_id, rel, info


def _check_common(ep: Episode, license_note: str) -> None:
    if ep.status != Status.PRODUCTION:
        raise StudioError(f"Files are added at PRODUCTION; {ep.episode_id} is {ep.status.value}.")
    if not license_note.strip():
        raise StudioError("Say where the file came from and what its license allows "
                          "(e.g. 'Gemini free tier, commercial use OK' or 'my own recording').")


# ------------------------------------------------------------- shot files
def add_shot_asset(repo: EpisodeRepository, episode_id: str, shot_id: str, data: bytes,
                   filename: str, *, provider: str, license_note: str, actor: str,
                   probe: MediaProbe | None = None) -> Episode:
    """Attach a picture or clip to a shot (replaces the shot's previous file)."""
    ep = repo.get_episode(episode_id)
    _check_common(ep, license_note)
    if shot_id not in {s.shot_id for s in ep.storyboard.shots}:
        raise StudioError(f"{episode_id} has no shot {shot_id}.")
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXT:
        kind = AssetKind.IMAGE
    elif ext in VIDEO_EXT:
        kind = AssetKind.VIDEO
    else:
        raise StudioError(f"{filename}: use an image ({', '.join(sorted(IMAGE_EXT))}) "
                          f"or a clip ({', '.join(sorted(VIDEO_EXT))}).")
    asset_id, rel, info = _store(repo, ep, data, ext, probe or FfprobeProbe())
    if not info.has_video or not info.width:
        abs_target = Path(repo.settings.data_dir) / rel
        abs_target.unlink(missing_ok=True)
        raise StudioError(f"{filename} has no picture in it.")

    asset = Asset(asset_id=asset_id, kind=kind, path=rel, shot_id=shot_id,
                  provider=provider.strip(), license_note=license_note.strip(),
                  duration_sec=info.duration_sec if kind == AssetKind.VIDEO else None,
                  width=info.width, height=info.height)
    assets = [a for a in ep.assets if not (a.shot_id == shot_id and a.kind in SHOT_KINDS)]
    shots = [s.model_copy(update={"asset_path": rel, "status": ShotStatus.GENERATED})
             if s.shot_id == shot_id else s for s in ep.storyboard.shots]
    new = ep.model_copy(update={
        "assets": assets + [asset],
        "storyboard": ep.storyboard.model_copy(update={"shots": shots})})
    return repo.save(new, actor=actor, note=f"{shot_id}: added {filename} as {asset_id}",
                     allowed_sections={"assets", "storyboard"})


def add_narration(repo: EpisodeRepository, episode_id: str, data: bytes, filename: str, *,
                  license_note: str, actor: str, provider: str = "",
                  probe: MediaProbe | None = None) -> Episode:
    """Attach the narration audio (replaces the previous narration)."""
    ep = repo.get_episode(episode_id)
    _check_common(ep, license_note)
    ext = Path(filename).suffix.lower()
    if ext not in AUDIO_EXT:
        raise StudioError(f"{filename}: narration must be {', '.join(sorted(AUDIO_EXT))}.")
    asset_id, rel, info = _store(repo, ep, data, ext, probe or FfprobeProbe())
    if not info.has_audio or not info.duration_sec:
        (Path(repo.settings.data_dir) / rel).unlink(missing_ok=True)
        raise StudioError(f"{filename} has no sound in it.")
    asset = Asset(asset_id=asset_id, kind=AssetKind.VOICE, path=rel, provider=provider.strip(),
                  license_note=license_note.strip(), duration_sec=info.duration_sec)
    assets = [a for a in ep.assets if a.kind != AssetKind.VOICE] + [asset]
    return repo.save(ep.model_copy(update={"assets": assets}), actor=actor,
                     note=f"narration: added {filename} as {asset_id} ({info.duration_sec}s)",
                     allowed_sections={"assets"})


# --------------------------------------------------------------- helpers
def narration_text(ep: Episode) -> str:
    """The words to record: every script sentence, in order."""
    return "\n".join(s.text for seg in ep.script.segments for s in seg.sentences)


# ------------------------------------------------------------ daily budget
def made_today(repo: EpisodeRepository) -> dict[str, int]:
    """Pictures/clips added today (your local date) per tool, across all episodes.
    A replaced picture no longer counts, so this can be slightly low after replacements."""
    today = date.today()
    counts: dict[str, int] = {}
    for ep in repo.all_episodes():
        for a in ep.assets:
            if a.kind in SHOT_KINDS and a.provider and \
                    datetime.fromisoformat(a.created_at).astimezone().date() == today:
                counts[a.provider] = counts.get(a.provider, 0) + 1
    return counts


def suggest_tools(ep: Episode, tools, used: dict[str, int]) -> dict[str, str | None]:
    """For each shot still without a picture: a tool that has budget left today, in the
    order of tools.yaml (e.g. 6 x Adobe Firefly, then 6 x ChatGPT). None = budget used up."""
    have = shot_files(ep)
    left = {t.name: t.daily_limit - used.get(t.name, 0) for t in tools if t.daily_limit}
    plan: dict[str, str | None] = {}
    for shot in ep.storyboard.shots:
        if shot.shot_id in have:
            continue
        name = next((n for n, k in left.items() if k > 0), None)
        if name:
            left[name] -= 1
        plan[shot.shot_id] = name
    return plan
