"""Build the final vertical video with FFmpeg: shots + narration (+ subtitles) -> MP4.

1. Each shot becomes a 1080x1920, 30 fps segment. Pictures get a gentle slow zoom;
   clips are trimmed, or hold their last frame if they are too short.
2. Shot lengths are scaled so the pictures end exactly when the narration ends.
3. The segments are joined and the narration is added: H.264 + AAC, "fast start",
   which is what YouTube Shorts, Reels and TikTok expect.
4. If the episode has subtitles, they are burned into the picture.
Every build is a new file (final_v1.mp4, final_v2.mp4, ...) recorded as an asset.
"""
import shutil
import subprocess
from pathlib import Path

from .assets import FfprobeProbe, MediaProbe, abs_path, episode_dir, find_tool
from .db import EpisodeRepository
from .errors import StudioError
from .models import (
    Asset,
    AssetKind,
    Episode,
    Status,
    latest_asset,
    production_inputs,
    shot_files,
    subtitles_are_current,
)
from .subtitles import ass_path

WIDTH, HEIGHT, FPS = 1080, 1920, 30
ZOOM_END = 1.08          # pictures slowly zoom from 100% to 108%
VIDEO_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
              "-r", str(FPS)]
FILL = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},setsar=1")


def _run(args: list[str], cwd: Path | None = None) -> None:
    out = subprocess.run([find_tool("ffmpeg"), "-y", "-v", "error", *args],
                         check=False, capture_output=True, text=True, timeout=600, cwd=cwd)
    if out.returncode != 0:
        tail = "\n".join(out.stderr.strip().splitlines()[-5:])
        raise StudioError(f"FFmpeg could not build the video:\n{tail}")


def frame_plan(shot_secs: list[float], narration_sec: float) -> list[int]:
    """Frames per shot, scaled so the total matches the narration exactly."""
    total_frames = round(narration_sec * FPS)
    planned = sum(shot_secs)
    frames = [max(1, round(s / planned * total_frames)) for s in shot_secs]
    frames[-1] = max(1, total_frames - sum(frames[:-1]))  # rounding goes into the last shot
    return frames


def _image_segment(src: Path, frames: int, out: Path) -> None:
    # Upscale first so the zoom is smooth, then zoom towards the centre.
    step = (ZOOM_END - 1) / max(frames, 1)
    zoom = (f"zoompan=z='min(1+{step:.6f}*on,{ZOOM_END})':d=1:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={WIDTH}x{HEIGHT}:fps={FPS}")
    big = f"scale={WIDTH * 3 // 2}:{HEIGHT * 3 // 2}:force_original_aspect_ratio=increase," \
          f"crop={WIDTH * 3 // 2}:{HEIGHT * 3 // 2}"
    _run(["-loop", "1", "-framerate", str(FPS), "-i", str(src), "-vf",
          f"{big},{zoom},setsar=1", "-frames:v", str(frames), "-an", *VIDEO_ARGS, str(out)])


def _clip_segment(src: Path, frames: int, out: Path) -> None:
    hold = frames / FPS
    _run(["-i", str(src), "-vf", f"{FILL},fps={FPS},tpad=stop_mode=clone:stop_duration={hold}",
          "-frames:v", str(frames), "-an", *VIDEO_ARGS, str(out)])


def _next_final_name(folder: Path) -> str:
    n = 1
    while (folder / f"final_v{n}.mp4").exists():
        n += 1
    return f"final_v{n}.mp4"


def missing_for_build(ep: Episode) -> list[str]:
    """What still has to be added before a video can be built."""
    have = shot_files(ep)
    missing = [f"a picture or clip for {s.shot_id}" for s in ep.storyboard.shots
               if s.shot_id not in have]
    if latest_asset(ep, AssetKind.VOICE) is None:
        missing.append("the narration")
    if not ep.storyboard.shots:
        missing.append("a storyboard")
    return missing


def build_video(repo: EpisodeRepository, episode_id: str, *, actor: str,
                probe: MediaProbe | None = None) -> Episode:
    ep = repo.get_episode(episode_id)
    if ep.status != Status.PRODUCTION:
        raise StudioError(f"Videos are built at PRODUCTION; {episode_id} is {ep.status.value}.")
    missing = missing_for_build(ep)
    if missing:
        raise StudioError("Still needed before building: " + ", ".join(missing) + ".")

    voice = latest_asset(ep, AssetKind.VOICE)
    if voice is None or voice.duration_sec is None:  # missing_for_build already says so
        raise StudioError("Add the narration first.")
    captions = latest_asset(ep, AssetKind.CAPTIONS)
    if captions is not None and not subtitles_are_current(ep):
        raise StudioError("The narration changed after the subtitles were made. "
                          "Make the subtitles again before building.")
    by_shot = shot_files(ep)
    shots = ep.storyboard.shots
    frames = frame_plan([s.duration_sec for s in shots], voice.duration_sec)

    folder = episode_dir(repo, episode_id)
    work = folder / "_build"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        segments = []
        for n, (shot, count) in enumerate(zip(shots, frames, strict=True), start=1):
            asset = by_shot[shot.shot_id]
            out = work / f"seg{n:03d}.mp4"
            if asset.kind == AssetKind.IMAGE:
                _image_segment(abs_path(repo, asset), count, out)
            else:
                _clip_segment(abs_path(repo, asset), count, out)
            segments.append(out)
        listing = work / "segments.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in segments), encoding="utf-8")
        final = folder / _next_final_name(folder)
        if captions is None:
            video = ["-c:v", "copy"]
        else:  # burn the subtitles in; run inside `work` so the filter needs no Windows path
            shutil.copyfile(ass_path(repo, captions), work / "subs.ass")
            video = ["-vf", "ass=subs.ass", *VIDEO_ARGS]
        _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(abs_path(repo, voice)),
              "-map", "0:v", "-map", "1:a", *video, "-c:a", "aac", "-b:a", "160k",
              "-shortest", "-movflags", "+faststart", str(final)], cwd=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    info = (probe or FfprobeProbe()).probe(final)
    inputs = production_inputs(ep)
    asset = Asset(asset_id=final.stem, kind=AssetKind.FINAL_VIDEO,
                  path=final.relative_to(Path(repo.settings.data_dir)).as_posix(),
                  provider="ffmpeg", license_note="Built by FFmpeg from " + ", ".join(inputs),
                  duration_sec=info.duration_sec, width=info.width, height=info.height,
                  built_from=inputs)
    return repo.save(ep.model_copy(update={"assets": ep.assets + [asset]}), actor=actor,
                     note=f"video built: {final.name} ({info.duration_sec}s)",
                     allowed_sections={"assets"})
