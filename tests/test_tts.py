import io
import wave

import pytest
from helpers import open_gate
from media import needs_ffmpeg

from studio import tts
from studio.agents import run_mock_pipeline
from studio.config import PROJECT_ROOT
from studio.errors import StudioError
from studio.models import AssetKind
from studio.tts import FakeTTS, KokoroTTS, download_models, generate_narration, use_narration

REAL_MODELS = PROJECT_ROOT / "data" / "models"


@pytest.fixture
def production(repo):
    ep = repo.create_episode("The accidental invention of velcro")
    open_gate(repo, ep.episode_id)
    return run_mock_pipeline(repo, ep.episode_id).episode_id


def wav_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / w.getframerate()


def test_preview_is_not_saved(repo, production):
    preview = generate_narration(repo, production, voice="heart", speed=1.0, tts=FakeTTS())
    assert preview.seconds == pytest.approx(wav_seconds(preview.wav), abs=0.01)
    assert not repo.get_episode(production).assets


@needs_ffmpeg
def test_using_a_preview_attaches_it_with_a_license_note(repo, production):
    preview = generate_narration(repo, production, voice="george", speed=1.1, tts=FakeTTS())
    ep = use_narration(repo, preview, actor="human")
    voice = ep.assets[-1]
    assert voice.kind == AssetKind.VOICE and voice.provider == "Kokoro (george)"
    assert "Apache-2.0" in voice.license_note and "George (UK, male), speed 1.1" in voice.license_note
    assert voice.duration_sec == pytest.approx(preview.seconds, abs=0.1)


def test_bad_voice_speed_or_stage_is_refused(repo, production):
    with pytest.raises(StudioError, match="Unknown voice"):
        generate_narration(repo, production, voice="robot", speed=1.0, tts=FakeTTS())
    with pytest.raises(StudioError, match="Speed"):
        generate_narration(repo, production, voice="heart", speed=2.0, tts=FakeTTS())
    idea = repo.create_episode("Velcro")
    with pytest.raises(StudioError, match="PRODUCTION"):
        generate_narration(repo, idea.episode_id, voice="heart", speed=1.0, tts=FakeTTS())


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_download_checks_size_and_leaves_no_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "MODEL_FILES", {"m.onnx": 10})
    with pytest.raises(StudioError, match="incompletely"):
        download_models(tmp_path, opener=lambda url, timeout: FakeResponse(b"12345"))
    assert list(tmp_path.iterdir()) == []

    seen = []
    download_models(tmp_path, progress=lambda f, msg: seen.append(f),
                    opener=lambda url, timeout: FakeResponse(b"0123456789"))
    assert (tmp_path / "m.onnx").read_bytes() == b"0123456789" and seen[-1] == 1.0
    download_models(tmp_path, opener=lambda url, timeout: pytest.fail("downloaded twice"))


def test_network_errors_become_clear_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "MODEL_FILES", {"m.onnx": 10})

    def offline(url, timeout):
        raise OSError("no internet")
    with pytest.raises(StudioError, match="Could not download the voice model"):
        download_models(tmp_path, opener=offline)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(not (REAL_MODELS / "kokoro-v1.0.onnx").exists(),
                    reason="Kokoro model not downloaded yet")
def test_real_kokoro_speaks():
    wav, seconds = KokoroTTS(REAL_MODELS).synthesize(
        "Some dumplings were never meant to be eaten.", "heart", 1.0)
    assert 1.0 < seconds < 6.0 and wav_seconds(wav) == pytest.approx(seconds, abs=0.05)
