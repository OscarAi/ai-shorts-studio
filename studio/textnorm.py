"""Tiny text helpers shared by the library and (later) the originality gate."""
import re

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Lowercase, drop punctuation, collapse spaces. Used to compare statements."""
    return _SPACES.sub(" ", _PUNCT.sub(" ", text.lower())).strip()
