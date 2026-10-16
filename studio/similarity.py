"""Plain text-similarity helpers (no AI, no external libraries)."""
from difflib import SequenceMatcher

from .textnorm import normalize_text

STOPWORDS = frozenset(
    ["a", "an", "the", "of", "and", "or", "to", "in", "on", "at", "for", "with", "by", "from", "is", "are", "was", "were", "s", "it", "its", "this", "that", "as", "be", "into", "than", "then", "their", "his", "her", "how", "why", "what", "when", "who"]
)


def tokens(text: str, ignore: frozenset[str] = frozenset()) -> set[str]:
    """Meaningful lowercase words (stopwords, `ignore` words and 1-letter words removed)."""
    return {w for w in normalize_text(text).split()
            if w not in STOPWORDS and w not in ignore and len(w) > 1}


def strip_words(text: str, ignore: frozenset[str]) -> str:
    """Normalized text with the `ignore` words taken out (e.g. a series' boilerplate)."""
    return " ".join(w for w in normalize_text(text).split() if w not in ignore)


def jaccard(a: set, b: set) -> float:
    """Overlap between two sets: 0 = nothing shared, 1 = identical."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: set, b: set) -> float:
    """How much of the SMALLER set is inside the other: 1 = all of it.
    Unlike jaccard, a short topic fully inside a long one scores 1, not 1/5."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def text_ratio(a: str, b: str) -> float:
    """How alike two strings are as sequences (good for near-duplicate titles)."""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def shingles(text: str, size: int = 3) -> set[tuple]:
    words = normalize_text(text).split()
    if len(words) < size:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def shingle_similarity(a: str, b: str) -> float:
    """Overlap of 3-word phrases: catches reworded copies of the same script."""
    return jaccard(shingles(a), shingles(b))
