"""Times, in one place.

Two kinds of time, never mixed:
  * Stored timestamps (versions, events, logs, AI requests): UTC ISO strings, from utc_now().
  * Times a person types or reads (schedules, "went live"): LOCAL time as "YYYY-MM-DD HH:MM".
"""
from datetime import UTC, datetime

from .errors import StudioError

LOCAL_FORMAT = "%Y-%m-%d %H:%M"
EXAMPLE = "2026-10-14 18:00"


def utc_now() -> str:
    """Now, for storing: '2026-10-14T16:00:00+00:00'."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def local_now() -> datetime:
    return datetime.now()


def local_now_text() -> str:
    """Now, as a person writes it: '2026-10-14 18:00'."""
    return local_now().strftime(LOCAL_FORMAT)


def tidy(text: str) -> str:
    """'2026-10-14   18:00 ' -> '2026-10-14 18:00' (extra spaces are a common typo)."""
    return " ".join(text.split())


def parse_local(text: str) -> datetime | None:
    """'2026-10-14 18:00' -> datetime, or None when it isn't written like that."""
    try:
        return datetime.strptime(tidy(text), LOCAL_FORMAT)
    except ValueError:
        return None


def require_local(text: str, what: str = "the time") -> datetime:
    """Like parse_local, but a plain-word error when the text isn't a valid local time."""
    moment = parse_local(text)
    if moment is None:
        raise StudioError(f"Write {what} like {EXAMPLE}.")
    return moment


def has_passed(text: str) -> bool:
    """Is this local time now or earlier? (An unreadable time has not passed.)"""
    moment = parse_local(text)
    return moment is not None and moment <= local_now()


def local_midnight_utc() -> str:
    """Today's local midnight, as a stored (UTC) timestamp: 'since midnight' queries."""
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).isoformat(timespec="seconds")


def utc_to_local(stamp: str) -> datetime:
    """A stored UTC timestamp -> local time without timezone (to compare with typed times)."""
    return datetime.fromisoformat(stamp).astimezone().replace(tzinfo=None)
