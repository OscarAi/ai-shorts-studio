"""Health check ("doctor"): everything that can quietly go wrong, in one list with a fix for each.

Used by `python cli.py doctor` and the dashboard's Health page. Every check is read-only.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata
from pathlib import Path

from .config import PROJECT_ROOT
from .db import EpisodeRepository
from .errors import StudioError
from .llm import load_ai_config
from .subtitles import whisper_dir
from .tools import load_tools
from .tts import MODEL_FILES

OK, WARN, ERROR = "ok", "warning", "error"
ICON = {OK: "✅", WARN: "⚠️", ERROR: "❌"}
BACKUP_WARN_DAYS = 7
FREE_SPACE_WARN_GB = 5


@dataclass(frozen=True)
class Check:
    name: str
    level: str
    detail: str
    fix: str = ""


def _python() -> Check:
    in_venv = sys.prefix != sys.base_prefix
    version = f"Python {sys.version_info.major}.{sys.version_info.minor}"
    if sys.version_info[:2] != (3, 13):
        return Check("Python", WARN, f"{version} (the project is tested on 3.13)",
                     "Install Python 3.13 and run setup.ps1.")
    if not in_venv:
        return Check("Python", WARN, f"{version}, not the project's .venv",
                     r"Start the dashboard with run_app.bat (double-click it in File Explorer, "
                     r"or type .\run_app.bat); in the terminal, "
                     r"start commands with .venv\Scripts\python.")
    return Check("Python", OK, f"{version} in the project's .venv")


def locked_versions(lock: Path) -> dict[str, str]:
    out = {}
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            out[name.strip().lower().replace("_", "-")] = version.strip()
    return out


def _libraries(lock: Path = PROJECT_ROOT / "requirements-lock.txt") -> Check:
    if not lock.exists():
        return Check("Libraries", WARN, "requirements-lock.txt is missing", "Restore it from Git.")
    wrong = []
    for name, want in locked_versions(lock).items():
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            have = "missing"
        if have != want:
            wrong.append(f"{name} {have} (tested: {want})")
    if wrong:
        shown = ", ".join(wrong[:4]) + (f" and {len(wrong) - 4} more" if len(wrong) > 4 else "")
        return Check("Libraries", WARN, f"Not the tested versions: {shown}",
                     "Run setup.ps1 (installs the exact tested versions).")
    return Check("Libraries", OK, "Exactly the tested versions")


def _ffmpeg() -> Check:
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        return Check("FFmpeg", ERROR, f"Not found: {', '.join(missing)} (needed to measure and "
                     "build videos)", "Install it: scoop install ffmpeg, then reopen VS Code.")
    return Check("FFmpeg", OK, "ffmpeg and ffprobe found")


def _models(settings) -> list[Check]:
    models = settings.model_dir
    kokoro_ok = all((models / n).exists() and (models / n).stat().st_size == size
                    for n, size in MODEL_FILES.items())
    whisper_ok = (whisper_dir(settings) / "model.bin").exists()
    return [
        Check("Narration model", OK if kokoro_ok else WARN,
              "Kokoro downloaded" if kokoro_ok else "Not downloaded yet (~350 MB)",
              "" if kokoro_ok else "It downloads by itself the first time you make narration."),
        Check("Subtitle model", OK if whisper_ok else WARN,
              "Whisper downloaded" if whisper_ok else "Not downloaded yet (~145 MB)",
              "" if whisper_ok else "It downloads by itself the first time you make subtitles."),
    ]


def _database(repo: EpisodeRepository) -> Check:
    size_mb = repo.db_path.stat().st_size / 1e6 if repo.db_path.exists() else 0
    con = sqlite3.connect(repo.db_path)
    try:
        result = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    if result != "ok":
        return Check("Database", ERROR, f"Integrity problem: {result}",
                     "Stop using the app, and restore the newest backup (README: Backing up).")
    return Check("Database", OK, f"Healthy, {len(repo.list_episodes())} episodes, {size_mb:.1f} MB")


def newest_backup(backup_dir: Path) -> Path | None:
    found = list(backup_dir.glob("studio_*.db")) + list((backup_dir / "auto").glob("studio_*.db"))
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


def _backups(repo: EpisodeRepository, now: datetime) -> Check:
    newest = newest_backup(Path(repo.settings.backup_dir))
    off_pc = "Also copy data/backups to OneDrive or a USB drive now and then."
    if newest is None:
        return Check("Backups", WARN, "No backup yet", "Click 'Back up database'. " + off_pc)
    age = (now - datetime.fromtimestamp(newest.stat().st_mtime)).days
    if age > BACKUP_WARN_DAYS:
        return Check("Backups", WARN, f"Newest backup is {age} days old ({newest.name})",
                     "Click 'Back up database'. " + off_pc)
    return Check("Backups", OK, f"Newest: {newest.name}", off_pc)


def _disk(data_dir: Path) -> Check:
    free_gb = shutil.disk_usage(data_dir if data_dir.exists() else PROJECT_ROOT).free / 1e9
    if free_gb < FREE_SPACE_WARN_GB:
        return Check("Disk space", WARN, f"Only {free_gb:.1f} GB free",
                     "Delete old final_vN.mp4 builds or old backups you have copied elsewhere.")
    return Check("Disk space", OK, f"{free_gb:.0f} GB free")


def _licences(today) -> Check:
    tools = load_tools()
    stale = [t.name for t in tools.image_tools + tools.voice_tools if t.stale(today=today)]
    if stale:
        return Check("Tool licences", WARN, f"Terms due for a re-check: {', '.join(stale)}",
                     "Open each tool's terms page, then update 'checked' in config/tools.yaml.")
    return Check("Tool licences", OK, "All tool terms checked recently")


def _ai(env: Mapping[str, str] | None = None) -> Check:
    """Which AI services have a key (no request is sent here; see 'Test connection')."""
    environ: Mapping[str, str] = os.environ if env is None else env
    try:
        cfg = load_ai_config()
    except StudioError as e:
        return Check("AI services", ERROR, str(e), "Fix config/ai.yaml (or restore it from Git).")
    ready = [p.name for p in cfg.providers if environ.get(p.key_env, "").strip()]
    if not ready:
        first = cfg.providers[0]
        return Check("AI services", WARN, "No AI key yet: AI steps work by copy-paste only",
                     f"Get a free key at {first.signup_url} and put {first.key_env}=... in .env "
                     "(README: 'AI inside the app').")
    if cfg.allow_paid:
        return Check("AI services", WARN, f"Keys found: {', '.join(ready)}, and allow_paid is "
                     "true: paid models may be used", "Set allow_paid: false in config/ai.yaml "
                     "unless you really want to pay.")
    return Check("AI services", OK, f"Keys found: {', '.join(ready)}; free models only, at most "
                 f"{cfg.daily_limit_total} requests a day (test them with 'Test connection' "
                 "or: python cli.py ai-test)")


def _key_safety() -> Check | None:
    """Keys must stay out of Git: .env ignored and not tracked, the secret guard installed."""
    if not shutil.which("git") or not (PROJECT_ROOT / ".git").exists():
        return None
    def git(*args):
        try:
            return subprocess.run(["git", *args], check=False, cwd=PROJECT_ROOT,
                                  capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
    tracked = git("ls-files", "--error-unmatch", ".env")
    if tracked is not None and tracked.returncode == 0:
        return Check("Key safety", ERROR, ".env is tracked by Git: your keys could reach GitHub",
                     "Ask Claude to remove .env from Git and to replace the keys.")
    ignored = git("check-ignore", "-q", ".env")
    if ignored is not None and ignored.returncode != 0:
        return Check("Key safety", ERROR, ".env is not ignored by Git",
                     "Restore .gitignore from GitHub (it must list .env).")
    if not (PROJECT_ROOT / ".git" / "hooks" / "pre-commit").exists():
        return Check("Key safety", WARN, "The secret guard is not installed",
                     "Run setup.ps1 once (it blocks commits that contain a key).")
    return Check("Key safety", OK, ".env stays out of Git; the secret guard is on")


def _git() -> Check | None:
    if not shutil.which("git") or not (PROJECT_ROOT / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--branch"], check=False,
                             cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = out.splitlines()
    head = lines[0] if lines else ""
    changed = len(lines) - 1
    if "..." not in head:
        return Check("Code backup", WARN, "Not connected to GitHub yet",
                     "git push -u origin main (a browser window asks you to log in once).")
    if "ahead" in head:
        return Check("Code backup", WARN, "Some commits are not on GitHub yet", "git push")
    if changed:
        return Check("Code backup", WARN, f"{changed} changed file(s) not committed",
                     "Ask Claude to commit them, or: git status")
    return Check("Code backup", OK, "Everything is on GitHub")


def run_checks(repo: EpisodeRepository, now: datetime | None = None) -> list[Check]:
    now = now or datetime.now()
    data_dir = Path(repo.settings.data_dir)
    checks = [_python(), _libraries(), _ffmpeg(), *_models(repo.settings), _database(repo),
              _backups(repo, now), _disk(data_dir), _licences(now.date()), _ai()]
    extra = [_key_safety(), _git()]
    return checks + [c for c in extra if c]


def summary(checks: list[Check]) -> str:
    worst = ERROR if any(c.level == ERROR for c in checks) else (
        WARN if any(c.level == WARN for c in checks) else OK)
    n = sum(c.level != OK for c in checks)
    return {OK: "✅ Everything looks good.",
            WARN: f"⚠️ {n} thing(s) to look at.",
            ERROR: f"❌ {n} thing(s) to fix; something won't work until then."}[worst]
