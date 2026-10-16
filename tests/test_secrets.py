"""The secret guard (scripts/check_secrets.py). Fake keys are built at runtime, so this file
never contains anything that looks like a real key."""
import importlib.util

from studio.config import PROJECT_ROOT

spec = importlib.util.spec_from_file_location("check_secrets",
                                              PROJECT_ROOT / "scripts" / "check_secrets.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def found(text):
    return [label for label, p in guard.PATTERNS.items() if p.search(text)]


def test_key_shapes_are_recognised():
    assert found("GEMINI_API_KEY=" + "AIza" + "x" * 35) == ["Google/Gemini key"]
    assert found("gsk" + "_" + "a1" * 15) == ["Groq key"]
    assert found("sk-or-v1-" + "ab" * 16) == ["OpenRouter key"]
    assert found("-----BEGIN RSA " + "PRIVATE KEY-----") == ["private key"]


def test_ordinary_text_is_not_flagged():
    assert found("key_env: GEMINI_API_KEY") == []
    assert found("Please pass a valid API key") == []
    assert found("GEMINI_API_KEY=") == []


def test_every_tracked_file_is_clean_right_now():
    assert guard.scan(all_files=True) == []
