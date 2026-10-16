"""Narration made inside the app: text -> speech with Kokoro-82M, on your own computer.

Kokoro-82M is an Apache-2.0 model (commercial use allowed); kokoro-onnx (MIT) runs it on
Python 3.13. The model files (~350 MB) are downloaded once into data/models/.

Generating gives a PREVIEW. Nothing is saved until you choose "use this narration", which
goes through assets.add_narration, so every Milestone 5 rule still applies.
"""
import io
import math
import urllib.request
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .assets import add_narration, narration_text
from .db import EpisodeRepository
from .errors import StudioError
from .models import Episode, Status
from .tools import Tools, load_tools

BUILT_IN_VOICE_TOOL = "Kokoro (built in)"
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
MODEL_FILES = {  # name: exact size in bytes (checked 2026-10-12), so a broken download is caught
    "kokoro-v1.0.onnx": 325_532_387,
    "voices-v1.0.bin": 28_214_398,
}
# Friendly name -> (Kokoro voice id, description). English voices only.
VOICES = {
    "heart": ("af_heart", "Heart (US, female, warm)"),
    "bella": ("af_bella", "Bella (US, female)"),
    "michael": ("am_michael", "Michael (US, male)"),
    "george": ("bm_george", "George (UK, male)"),
    "emma": ("bf_emma", "Emma (UK, female)"),
}
SPEED_RANGE = (0.8, 1.2)

Progress = Callable[[float, str], None]


class TTSProvider(Protocol):
    def synthesize(self, text: str, voice: str, speed: float) -> tuple[bytes, float]:
        """(WAV bytes, seconds)"""
        ...


def to_wav(samples, sample_rate: int) -> bytes:
    """float samples in -1..1 -> 16-bit mono WAV."""
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# ------------------------------------------------------------------- Kokoro
def download_models(model_dir: Path, progress: Progress | None = None,
                    opener=urllib.request.urlopen) -> None:
    """Fetch the model files once. A partial or wrong-sized download is deleted."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, size in MODEL_FILES.items():
        target = model_dir / name
        if target.exists() and target.stat().st_size == size:
            continue
        part = target.with_suffix(target.suffix + ".part")
        try:
            with opener(RELEASE + name, timeout=60) as resp, open(part, "wb") as out:
                done = 0
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(min(done / size, 1.0), f"Downloading {name}: "
                                                         f"{done // 1_000_000} of {size // 1_000_000} MB")
            if part.stat().st_size != size:
                raise StudioError(f"{name} downloaded incompletely. Try again.")
            part.replace(target)
        except StudioError:
            part.unlink(missing_ok=True)
            raise
        except Exception as e:
            part.unlink(missing_ok=True)
            raise StudioError(f"Could not download the voice model ({name}): {e}") from None


class KokoroTTS:
    def __init__(self, model_dir: Path, progress: Progress | None = None):
        self.model_dir = Path(model_dir)
        self.progress = progress
        self._engine = None

    def _load(self):
        if self._engine is None:
            try:
                from kokoro_onnx import Kokoro
            except ImportError:
                raise StudioError("The voice generator is not installed. Run: "
                                  "pip install kokoro-onnx") from None
            download_models(self.model_dir, self.progress)
            self._engine = Kokoro(str(self.model_dir / "kokoro-v1.0.onnx"),
                                  str(self.model_dir / "voices-v1.0.bin"))
        return self._engine

    def synthesize(self, text: str, voice: str, speed: float) -> tuple[bytes, float]:
        engine = self._load()
        if self.progress:
            self.progress(1.0, "Speaking the narration...")
        samples, rate = engine.create(text, voice=VOICES[voice][0], speed=speed, lang="en-us")
        return to_wav(samples, rate), round(len(samples) / rate, 2)


class FakeTTS:
    """For tests: a quiet tone, about 2.5 words per second (like a real narrator)."""

    def synthesize(self, text: str, voice: str, speed: float) -> tuple[bytes, float]:
        seconds = round(max(1.0, len(text.split()) / 2.5 / speed), 2)
        rate = 8000
        t = np.arange(int(seconds * rate)) / rate
        return to_wav(0.2 * np.sin(2 * math.pi * 220 * t), rate), seconds


# --------------------------------------------------------------- narration
@dataclass
class NarrationPreview:
    episode_id: str
    voice: str
    speed: float
    wav: bytes
    seconds: float


def _check(ep: Episode, voice: str, speed: float) -> str:
    if ep.status != Status.PRODUCTION:
        raise StudioError(f"Narration is made at PRODUCTION; {ep.episode_id} is {ep.status.value}.")
    if voice not in VOICES:
        raise StudioError(f"Unknown voice '{voice}'. Choose from: {', '.join(VOICES)}.")
    if not SPEED_RANGE[0] <= speed <= SPEED_RANGE[1]:
        raise StudioError(f"Speed must be {SPEED_RANGE[0]}-{SPEED_RANGE[1]}.")
    text = narration_text(ep).replace("\n", " ").strip()
    if not text:
        raise StudioError("The script is empty: nothing to narrate.")
    return text


def generate_narration(repo: EpisodeRepository, episode_id: str, *, voice: str, speed: float,
                       tts: TTSProvider) -> NarrationPreview:
    """Speak the script. Returns a preview; NOTHING is saved yet."""
    text = _check(repo.get_episode(episode_id), voice, speed)
    wav, seconds = tts.synthesize(text, voice, speed)
    return NarrationPreview(episode_id, voice, speed, wav, seconds)


def use_narration(repo: EpisodeRepository, preview: NarrationPreview, *, actor: str,
                  tools: Tools | None = None) -> Episode:
    """Attach a preview as the episode's narration (normal rules, license note recorded)."""
    tool = Tools.find((tools or load_tools()).voice_tools, BUILT_IN_VOICE_TOOL)
    note = (tool.license_note if tool else "Kokoro-82M (Apache-2.0), generated locally.")
    note += f" Voice: {VOICES[preview.voice][1]}, speed {preview.speed:g}."
    return add_narration(repo, preview.episode_id, preview.wav,
                         f"narration_{preview.voice}.wav", license_note=note, actor=actor,
                         provider=f"Kokoro ({preview.voice})")


def default_tts(repo: EpisodeRepository, progress: Progress | None = None) -> KokoroTTS:
    return KokoroTTS(repo.settings.model_dir, progress)
