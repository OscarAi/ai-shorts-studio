"""Refuse to commit secrets. Runs before every `git commit` (the pre-commit hook installed by
setup.ps1) and in CI with --all.

It blocks a commit when a file contains something that looks like an AI key (Google, Groq,
OpenRouter, OpenAI, Anthropic, GitHub), any value from your .env file, or when .env itself is
being committed. It never prints the secret, only the file and line.

  python scripts/check_secrets.py          # staged files (what the hook does)
  python scripts/check_secrets.py --all    # every file Git tracks
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = {
    "Google/Gemini key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "Groq key": re.compile(r"gsk_[0-9A-Za-z]{20,}"),
    "OpenRouter key": re.compile(r"sk-or-v1-[0-9a-f]{20,}"),
    "Anthropic key": re.compile(r"sk-ant-[0-9A-Za-z_\-]{20,}"),
    "OpenAI-style key": re.compile(r"\bsk-(?:proj-)?[0-9A-Za-z]{32,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".mp4", ".wav", ".mp3", ".onnx", ".bin", ".db"}


def env_values() -> list[str]:
    """The real values in .env (long enough to be secrets), to catch them anywhere."""
    env = ROOT / ".env"
    if not env.exists():
        return []
    out = []
    for line in env.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if len(value) >= 16:
                out.append(value)
    return out


def files(all_files: bool) -> list[str]:
    cmd = (["git", "ls-files"] if all_files
           else ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    return [f for f in subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                      check=True).stdout.splitlines() if f]


def staged_text(name: str, all_files: bool) -> str:
    if all_files:
        return (ROOT / name).read_text(encoding="utf-8", errors="replace")
    return subprocess.run(["git", "show", f":{name}"], cwd=ROOT, capture_output=True,
                          check=True).stdout.decode("utf-8", errors="replace")


def scan(all_files: bool = False) -> list[str]:
    problems, secrets = [], env_values()
    for name in files(all_files):
        if Path(name).name == ".env" or name.endswith("/.env"):
            problems.append(f"{name}: the .env file must never be committed")
            continue
        if Path(name).suffix.lower() in SKIP_SUFFIXES:
            continue
        for n, line in enumerate(staged_text(name, all_files).splitlines(), start=1):
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    problems.append(f"{name}:{n}: looks like a {label}")
            if any(s in line for s in secrets):
                problems.append(f"{name}:{n}: contains a value from your .env file")
    return problems


def main() -> int:
    problems = scan(all_files="--all" in sys.argv)
    if problems:
        print("STOPPED: this commit would publish a secret.")
        for p in problems:
            print("  " + p)
        print("Remove it from the file (keys belong only in .env), then commit again.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
