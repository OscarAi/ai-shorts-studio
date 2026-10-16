"""Structured logging: readable lines on screen, JSON lines in a log file."""
import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if ctx:
            payload.update(ctx)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    """Safe to call more than once (it won't add duplicate handlers)."""
    root = logging.getLogger("studio")
    if getattr(root, "_studio_configured", False):
        return
    root.setLevel(level.upper())
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_dir / "studio.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s  %(message)s"))
    console.setLevel(logging.WARNING)  # keep the screen quiet; details go to the file

    root.addHandler(file_handler)
    root.addHandler(console)
    root._studio_configured = True  # type: ignore[attr-defined]
