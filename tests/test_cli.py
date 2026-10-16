"""The command line: run cli.py for real against a temporary database."""
import os
import subprocess
import sys

from studio.config import PROJECT_ROOT


def run(tmp_path, *args):
    env = {"SHORTS_DATA_DIR": str(tmp_path), "PYTHONIOENCODING": "utf-8", "SHORTS_NO_DOTENV": "1",
           "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}  # Windows needs SYSTEMROOT
    return subprocess.run([sys.executable, str(PROJECT_ROOT / "cli.py"), *args],
                          check=False, capture_output=True, text=True, encoding="utf-8",
                          env=env,
                          cwd=PROJECT_ROOT, timeout=60)


def test_every_command_has_help(tmp_path):
    out = run(tmp_path, "--help")
    assert out.returncode == 0
    for cmd in ("originality", "prompt", "paste", "verify", "source-ok", "ideas", "ideas-paste",
                "asset", "narration", "render", "qa", "publish-info", "published", "tools",
                "voices", "narrate", "subtitles", "next", "post", "schedule", "live",
                "add-link", "stats", "stats-due", "stats-show", "decline", "blocked-sites",
                "block-site", "unblock-site", "ai", "ai-test"):
        assert cmd in out.stdout


def test_new_check_and_mock_pipeline(tmp_path):
    assert run(tmp_path, "new", "--topic", "The accidental invention of velcro",
               "--angle", "Burrs on a dog after a mountain walk").returncode == 0
    assert "PASS" in run(tmp_path, "originality", "EP_000001").stdout
    out = run(tmp_path, "mock-pipeline", "EP_000001")
    assert out.returncode == 0 and "PRODUCTION" in out.stdout


def test_an_idea_without_angle_cannot_start_research(tmp_path):
    run(tmp_path, "new", "--topic", "Velcro")
    run(tmp_path, "originality", "EP_000001")
    out = run(tmp_path, "advance", "EP_000001", "RESEARCH")
    assert out.returncode == 2 and "ANGLE_MISSING" in out.stderr


def test_rules_are_reported_not_crashing(tmp_path):
    run(tmp_path, "new", "--topic", "Velcro")
    out = run(tmp_path, "advance", "EP_000001", "RESEARCH")
    assert out.returncode == 2 and "ORIGINALITY_NOT_CHECKED" in out.stderr


def test_ideas_prompt_and_paste(tmp_path):
    assert "topic scout" in run(tmp_path, "ideas", "--count", "3").stdout
    answer = tmp_path / "ideas.txt"
    answer.write_text('{"ideas": [{"topic": "The Slinky", "category": "inventions"}]}',
                      encoding="utf-8")
    out = run(tmp_path, "ideas-paste", str(answer), "--create", "1")
    assert out.returncode == 0 and "Created EP_000001" in out.stdout


def test_tools_list_and_asset_with_tool(tmp_path):
    out = run(tmp_path, "tools")
    assert out.returncode == 0 and "Adobe Firefly" in out.stdout and "Kokoro" in out.stdout
    assert "Bing" not in out.stdout
    out = run(tmp_path, "asset", "EP_000001", "SHOT01", "x.png", "--tool", "Nope")
    assert out.returncode == 1  # no such episode/file/tool: a clear error, not a crash
