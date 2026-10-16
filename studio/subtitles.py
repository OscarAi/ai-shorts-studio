"""Subtitles: the approved script, timed word by word to the narration.

The TEXT always comes from the approved script. Speech recognition (faster-whisper) is only
used to find WHEN each word is spoken, so a misheard word can never put an unapproved fact
on screen. Words the recognizer missed (e.g. "2,500" heard as "two thousand five hundred")
get times spread between their neighbours.

Cues are short (max 3 words / ~20 characters), never span two sentences, last at least
0.4 s, and tiny gaps are closed so the text doesn't flicker.
"""
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol

import numpy as np

from .assets import abs_path, episode_dir, find_tool, narration_text, new_asset_id
from .db import EpisodeRepository
from .errors import StudioError
from .models import Asset, AssetKind, Episode, Status, latest_asset

MAX_WORDS = 3
MAX_CHARS = 20
MIN_CUE_SEC = 0.4
CLOSE_GAP_SEC = 0.25
MIN_MATCH_SHARE = 0.6

# Burned-in style: 1080x1920 canvas, bold white with a black outline, centred, lifted above
# the bottom ~25% where YouTube/TikTok/Instagram put their buttons.
# "Hook": the first line as big yellow text at the top for the first seconds, below the apps'
# top bar, so a viewer scrolling with the sound off still gets the hook.
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,0,2,80,80,520,1
Style: Hook,Arial,88,&H0000E6FF,&H0000E6FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,3,8,90,90,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Cue:
    start: float
    end: float
    text: str


class Aligner(Protocol):
    def words(self, audio_path: Path) -> list[Word]:
        """Every word heard in the recording, with its start and end time."""
        ...


Progress = Callable[[float, str], None]


# --------------------------------------------------------------- alignment
def _key(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower())


def script_words(text: str) -> list[str]:
    return [w for w in text.split() if _key(w)]


def align(script: list[str], heard: list[Word]) -> list[Word]:
    """Script words with times taken from the matching heard words."""
    if not script:
        raise StudioError("The script is empty: no subtitles to make.")
    if not heard:
        raise StudioError("No speech was found in the narration.")
    a, b = [_key(w) for w in script], [_key(h.text) for h in heard]
    times: list[tuple[float, float] | None] = [None] * len(script)
    for block in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            h = heard[block.b + k]
            times[block.a + k] = (h.start, h.end)
    matched = sum(t is not None for t in times)
    if matched / len(script) < MIN_MATCH_SHARE:
        raise StudioError(f"Only {matched} of {len(script)} script words were heard in the "
                          "narration. Is it the right recording for this script?")

    # Spread unmatched runs over the gap between their matched neighbours, by length.
    i = 0
    while i < len(times):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(times) and times[j] is None:
            j += 1
        before, after = times[i - 1] if i > 0 else None, times[j] if j < len(times) else None
        gap_start = before[1] if before else heard[0].start
        gap_end = after[0] if after else heard[-1].end
        gap_end = max(gap_end, gap_start)
        lengths = [len(script[k]) for k in range(i, j)]
        t = gap_start
        for k, n in zip(range(i, j), lengths, strict=True):
            step = (gap_end - gap_start) * n / sum(lengths)
            times[k] = (t, t + step)
            t += step
        i = j
    filled = [t for t in times if t is not None]  # every slot is filled by now
    return [Word(w, round(s, 3), round(e, 3)) for w, (s, e) in zip(script, filled, strict=True)]


# ----------------------------------------------------------------- chunking
def chunk(words: list[Word]) -> list[Cue]:
    """Short cues: max 3 words / ~20 characters, never across a sentence end."""
    groups: list[list[Word]] = []
    current: list[Word] = []
    for w in words:
        chars = sum(len(x.text) + 1 for x in current) + len(w.text)
        if current and (len(current) >= MAX_WORDS or chars > MAX_CHARS):
            groups.append(current)
            current = []
        current.append(w)
        if w.text.endswith((".", "?", "!")) or (w.text.endswith((",", ";", ":"))
                                                 and len(current) >= 2):
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    cues = [Cue(g[0].start, g[-1].end, " ".join(x.text for x in g)) for g in groups]
    for n, cue in enumerate(cues):
        nxt = cues[n + 1].start if n + 1 < len(cues) else None
        cue.end = max(cue.end, cue.start + MIN_CUE_SEC)
        if nxt is not None and (nxt - cue.end < CLOSE_GAP_SEC or cue.end > nxt):
            cue.end = nxt  # close tiny gaps, never overlap the next cue
        cue.start, cue.end = round(cue.start, 3), round(cue.end, 3)
    return cues


# ------------------------------------------------------------------- files
def _srt_time(t: float) -> str:
    ms = round(t * 1000)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _ass_time(t: float) -> str:
    cs = round(t * 100)
    return f"{cs // 360_000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def to_srt(cues: list[Cue]) -> str:
    return "\n".join(f"{n}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{c.text}\n"
                     for n, c in enumerate(cues, start=1))


def to_ass(cues: list[Cue], hook: str = "", hook_sec: float = 0.0) -> str:
    """The burned-in subtitles; with a hook, it also shows at the top from the very first
    frame (so it is in the preview picture too) for `hook_sec` seconds, then fades out."""
    def clean(text: str) -> str:  # braces and backslashes are ASS control characters
        return re.sub(r"[{}\\]", "", text)
    lines = [f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Default,,0,0,0,,{clean(c.text)}\n"
             for c in cues]
    if hook.strip() and hook_sec > 0:
        lines.insert(0, f"Dialogue: 1,{_ass_time(0)},{_ass_time(hook_sec)},Hook,,0,0,0,,"
                        f"{{\\fad(0,300)}}{clean(hook.strip())}\n")
    return ASS_HEADER + "".join(lines)


def parse_srt(text: str) -> list[Cue]:
    cues = []
    pattern = re.compile(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)")
    for block in text.strip().split("\n\n"):
        lines = block.strip().splitlines()
        m = pattern.match(lines[1]) if len(lines) >= 3 else None
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        cues.append(Cue(g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000,
                        g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000, " ".join(lines[2:])))
    return cues


# ---------------------------------------------------------------- aligners
WHISPER_MODEL = "base.en"
LICENSE_NOTE = ("Text from the approved script; word timing by faster-whisper "
                f"({WHISPER_MODEL}, MIT).")


class WhisperAligner:
    """faster-whisper on the CPU. The model (~145 MB) is downloaded once into model_dir."""
    name = f"faster-whisper {WHISPER_MODEL}"

    def __init__(self, model_dir: Path, progress: Progress | None = None):
        self.model_dir = Path(model_dir)
        self.progress = progress
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel, download_model
            except ImportError:
                raise StudioError("The subtitle timer is not installed. Run: "
                                  "pip install faster-whisper") from None
            if not (self.model_dir / "model.bin").exists():
                if self.progress:
                    self.progress(0.0, "Downloading the speech model (~145 MB, only once)...")
                try:
                    download_model(WHISPER_MODEL, output_dir=str(self.model_dir))
                except Exception as e:
                    raise StudioError(f"Could not download the speech model: {e}") from None
            self._model = WhisperModel(str(self.model_dir), device="cpu", compute_type="int8")
        return self._model

    @staticmethod
    def _audio(audio_path: Path):
        """16 kHz mono samples decoded by FFmpeg (not PyAV: newer PyAV releases break
        faster-whisper's own decoder)."""
        out = subprocess.run([find_tool("ffmpeg"), "-v", "error", "-i", str(audio_path),
                              "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                             check=False, capture_output=True, timeout=300)
        if out.returncode != 0 or not out.stdout:
            raise StudioError(f"Could not read the narration audio ({Path(audio_path).name}).")
        return np.frombuffer(out.stdout, dtype=np.float32)

    def words(self, audio_path: Path) -> list[Word]:
        model = self._load()
        if self.progress:
            self.progress(1.0, "Listening to the narration...")
        segments, _ = model.transcribe(self._audio(audio_path), language="en",
                                       word_timestamps=True)
        return [Word(w.word.strip(), round(w.start, 3), round(w.end, 3))
                for seg in segments for w in (seg.words or []) if w.word.strip()]


def whisper_dir(settings) -> Path:
    """Where the subtitle-timing model lives (downloaded on first use)."""
    return settings.model_dir / f"whisper-{WHISPER_MODEL}"


def default_aligner(repo: EpisodeRepository, progress: Progress | None = None) -> WhisperAligner:
    return WhisperAligner(whisper_dir(repo.settings),
                          progress)


# ---------------------------------------------------------------- episode
def make_subtitles(repo: EpisodeRepository, episode_id: str, aligner: Aligner, *,
                   actor: str) -> Episode:
    """Time the approved script to the current narration and save .srt + .ass."""
    ep = repo.get_episode(episode_id)
    if ep.status != Status.PRODUCTION:
        raise StudioError(f"Subtitles are made at PRODUCTION; {episode_id} is {ep.status.value}.")
    voice = latest_asset(ep, AssetKind.VOICE)
    if voice is None:
        raise StudioError("Add the narration first: subtitles are timed to it.")
    heard = aligner.words(abs_path(repo, voice))
    cues = chunk(align(script_words(narration_text(ep)), heard))

    asset_id = new_asset_id(repo, ep)
    folder = episode_dir(repo, episode_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{asset_id}.srt").write_text(to_srt(cues), encoding="utf-8")
    (folder / f"{asset_id}.ass").write_text(
        to_ass(cues, ep.script.hook, repo.settings.hook_overlay_sec), encoding="utf-8")
    asset = Asset(asset_id=asset_id, kind=AssetKind.CAPTIONS,
                  path=(folder / f"{asset_id}.srt").relative_to(
                      Path(repo.settings.data_dir)).as_posix(),
                  provider=getattr(aligner, "name", "aligner"), license_note=LICENSE_NOTE,
                  duration_sec=cues[-1].end if cues else None, built_from=[voice.asset_id])
    assets = [a for a in ep.assets if a.kind != AssetKind.CAPTIONS] + [asset]
    return repo.save(ep.model_copy(update={"assets": assets}), actor=actor,
                     note=f"subtitles made: {len(cues)} cues ({asset_id})",
                     allowed_sections={"assets"})


def ass_path(repo: EpisodeRepository, captions: Asset) -> Path:
    return abs_path(repo, captions).with_suffix(".ass")


def read_cues(repo: EpisodeRepository, captions: Asset) -> list[Cue]:
    return parse_srt(abs_path(repo, captions).read_text(encoding="utf-8"))


class FakeAligner:
    """For tests: 'hears' the given words evenly spaced (or exactly the given Words)."""
    name = "fake aligner"

    def __init__(self, heard: Sequence[Word | str], seconds_per_word: float = 0.4):
        self.heard = [h if isinstance(h, Word) else
                      Word(h, i * seconds_per_word, (i + 1) * seconds_per_word - 0.05)
                      for i, h in enumerate(heard)]

    def words(self, audio_path: Path) -> list[Word]:
        return list(self.heard)
