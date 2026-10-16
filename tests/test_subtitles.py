import itertools

import pytest
from helpers import open_gate
from media import fake_subtitles, needs_ffmpeg, tone

from studio.agents import run_mock_pipeline
from studio.assets import abs_path, add_narration, narration_text
from studio.config import PROJECT_ROOT
from studio.errors import StudioError
from studio.models import AssetKind, Status
from studio.subtitles import (
    Cue,
    FakeAligner,
    WhisperAligner,
    Word,
    align,
    ass_path,
    chunk,
    make_subtitles,
    parse_srt,
    read_cues,
    script_words,
    to_ass,
    to_srt,
)
from studio.tts import KokoroTTS
from studio.validators import validate_for_transition

SCRIPT = ("Some dumplings were never meant to be eaten. In a tomb roughly 2,500 years old, "
          "archaeologists found food.")


def heard_words(*items):
    """('word', start) pairs -> Words lasting 0.3 s."""
    return [Word(w, s, s + 0.3) for w, s in items]


# ------------------------------------------------------------------ align
def test_text_comes_from_the_script_not_from_what_was_heard():
    words = script_words("Tang dynasty dumplings.")
    heard = heard_words(("tongue", 0.0), ("dynasty", 0.4), ("dumplings", 0.8))
    aligned = align(words, heard)
    assert [w.text for w in aligned] == ["Tang", "dynasty", "dumplings."]  # never "tongue"


def test_misheard_numbers_get_times_between_their_neighbours():
    words = script_words("roughly 2,500 years old")
    heard = heard_words(("roughly", 1.0), ("two", 1.4), ("thousand", 1.7), ("five", 2.0),
                        ("hundred", 2.3), ("years", 2.8), ("old", 3.2))
    aligned = align(words, heard)
    number = aligned[1]
    assert number.text == "2,500"
    assert aligned[0].end <= number.start < number.end <= aligned[2].start == 2.8


def test_extra_heard_words_are_ignored_and_missing_ones_filled():
    words = script_words("never meant to be eaten")
    heard = heard_words(("um", 0.0), ("never", 0.3), ("meant", 0.7), ("be", 1.4),
                        ("eaten", 1.8))
    aligned = align(words, heard)
    assert [w.text for w in aligned] == words
    assert aligned[1].end <= aligned[2].start <= aligned[3].start == 1.4  # "to" filled in


def test_the_wrong_recording_is_refused():
    with pytest.raises(StudioError, match="right recording"):
        align(script_words(SCRIPT), heard_words(("hello", 0), ("world", 1)))


def test_empty_inputs_are_refused():
    with pytest.raises(StudioError):
        align([], heard_words(("a", 0)))
    with pytest.raises(StudioError, match="No speech"):
        align(["hello"], [])


# ------------------------------------------------------------------ chunk
def test_cues_are_short_and_never_cross_a_sentence():
    words = script_words(SCRIPT)
    cues = chunk(align(words, FakeAligner(words).words(None)))
    assert all(len(c.text.split()) <= 3 and len(c.text) <= 26 for c in cues)
    assert "eaten. In" not in " | ".join(c.text for c in cues)
    assert any(c.text.endswith("eaten.") for c in cues)
    assert " ".join(c.text for c in cues) == " ".join(words)  # every word, once, in order


def test_cues_do_not_overlap_and_last_long_enough():
    words = [Word("A", 0.0, 0.05), Word("quick", 0.1, 0.15), Word("test.", 0.2, 0.25),
             Word("Then", 2.0, 2.2), Word("more.", 2.3, 2.5)]
    cues = chunk(words)
    assert cues[0].end - cues[0].start >= 0.4
    for a, b in itertools.pairwise(cues):
        assert a.end <= b.start


def test_tiny_gaps_are_closed():
    cues = chunk([Word("One", 0, 0.5), Word("two", 0.6, 1.0), Word("three.", 1.1, 1.5),
                  Word("Four.", 1.6, 2.0)])
    assert cues[0].end == cues[1].start  # 0.1 s gap closed: no flicker


# ------------------------------------------------------------------ files
def test_srt_round_trip_and_format():
    cues = [Cue(0.0, 1.234, "Some dumplings"), Cue(61.5, 62.0, "were never")]
    srt = to_srt(cues)
    assert "00:00:00,000 --> 00:00:01,234" in srt and "00:01:01,500 --> 00:01:02,000" in srt
    back = parse_srt(srt)
    assert [(c.start, c.end, c.text) for c in back] == [(0.0, 1.234, "Some dumplings"),
                                                       (61.5, 62.0, "were never")]


def test_ass_has_the_style_and_safe_text():
    ass = to_ass([Cue(1.0, 2.5, "Odd {text} here")])
    assert "PlayResY: 1920" in ass and "Style: Default,Arial,78" in ass
    assert "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,Odd text here" in ass
    assert ",Hook,," not in ass  # no hook given


def test_the_hook_shows_at_the_top_from_the_first_frame():
    ass = to_ass([Cue(0.2, 1.0, "Some dumplings")], hook=" Some {odd} dumplings ", hook_sec=3)
    hook_line = next(line for line in ass.splitlines() if ",Hook,," in line)
    assert hook_line.startswith("Dialogue: 1,0:00:00.00,0:00:03.00,Hook,,")
    assert hook_line.endswith("{\\fad(0,300)}Some odd dumplings")  # fades out, text made safe
    style = next(line for line in ass.splitlines() if line.startswith("Style: Hook"))
    assert style.split(",")[18] == "8"  # Alignment 8 = top centre
    assert ",Hook,," not in to_ass([], hook="Some dumplings", hook_sec=0)  # 0 = off


# ------------------------------------------------------- episode + rules
@pytest.fixture
def narrated(repo, tmp_path):
    """PRODUCTION episode with narration (no subtitles yet)."""
    ep = repo.create_episode("The accidental invention of velcro")
    open_gate(repo, ep.episode_id)
    eid = run_mock_pipeline(repo, ep.episode_id).episode_id
    add_narration(repo, eid, tone(tmp_path / "v.wav", 31), "v.wav", license_note="test",
                  actor="human")
    return eid


def qa_codes(repo, eid, settings=None):
    return {i.code for i in validate_for_transition(repo.get_episode(eid), Status.QA,
                                                    settings or repo.settings)}


@needs_ffmpeg
def test_make_subtitles_saves_srt_and_ass_from_the_script(repo, narrated):
    assert "NO_SUBTITLES" in qa_codes(repo, narrated)
    fake_subtitles(repo, narrated, 31)
    ep = repo.get_episode(narrated)
    caps = ep.assets[-1]
    voice = next(a for a in ep.assets if a.kind == AssetKind.VOICE)
    assert caps.kind == AssetKind.CAPTIONS and caps.built_from == [voice.asset_id]
    assert ass_path(repo, caps).exists() and "Apache" not in caps.license_note
    cues = read_cues(repo, caps)
    assert cues[0].text.startswith("[MOCK] What")
    assert " ".join(c.text for c in cues) == " ".join(script_words(narration_text(ep)))
    assert {"NO_SUBTITLES", "SUBTITLES_OUTDATED"}.isdisjoint(qa_codes(repo, narrated))
    ass = ass_path(repo, caps).read_text(encoding="utf-8")
    assert f",Hook,,0,0,0,,{{\\fad(0,300)}}{ep.script.hook}" in ass  # burned in, on top
    srt = abs_path(repo, caps).read_text(encoding="utf-8")  # for YouTube: spoken words only
    assert srt.count(" --> ") == len(cues) and "fad" not in srt


@needs_ffmpeg
def test_new_narration_makes_subtitles_outdated(repo, narrated, tmp_path):
    fake_subtitles(repo, narrated, 31)
    add_narration(repo, narrated, tone(tmp_path / "w.wav", 32), "w.wav", license_note="test",
                  actor="human")
    assert "SUBTITLES_OUTDATED" in qa_codes(repo, narrated)


@needs_ffmpeg
def test_subtitles_can_be_made_optional(repo, narrated):
    off = repo.settings.model_copy(update={"subtitles_required": False})
    assert "NO_SUBTITLES" not in qa_codes(repo, narrated, off)


def test_subtitles_need_narration_and_production(repo):
    ep = repo.create_episode("Velcro")
    with pytest.raises(StudioError, match="PRODUCTION"):
        make_subtitles(repo, ep.episode_id, FakeAligner(["x"]), actor="human")


WHISPER_DIR = PROJECT_ROOT / "data" / "models" / "whisper-base.en"


@needs_ffmpeg
@pytest.mark.skipif(not (WHISPER_DIR / "model.bin").exists(), reason="speech model not downloaded")
def test_real_whisper_hears_kokoro(tmp_path):
    models = PROJECT_ROOT / "data" / "models"
    if not (models / "kokoro-v1.0.onnx").exists():
        pytest.skip("Kokoro model not downloaded")
    text = "Some dumplings were never meant to be eaten."
    wav, seconds = KokoroTTS(models).synthesize(text, "heart", 1.0)
    (tmp_path / "n.wav").write_bytes(wav)
    heard = WhisperAligner(WHISPER_DIR).words(tmp_path / "n.wav")
    aligned = align(script_words(text), heard)
    assert [w.text for w in aligned] == script_words(text)
    assert 0 <= aligned[0].start < 1.0 and aligned[-1].end <= seconds + 0.2
