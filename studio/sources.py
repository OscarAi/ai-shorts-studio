"""Source checking: does each cited link really exist, and what does the page say?

No AI and no paid service: Python opens each URL with the standard library, records a link
status, and finds the sentence on the page that best matches each claim, so a human can
review a fact by reading one quote instead of searching a whole page.

Only short quotes are stored, never whole pages.
"""
import math
import re
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol

from .db import EpisodeRepository
from .errors import StudioError
from .models import MOCK_URL_PREFIX, ClaimStatus, Episode, Evidence, LinkStatus, Status, utc_now
from .similarity import containment, tokens

USER_AGENT = ("Mozilla/5.0 (compatible; ShortsStudio/0.4; personal fact-checking tool; "
              "checks only the pages cited in its own research)")
TIMEOUT_SEC = 15
MAX_BYTES = 2_000_000
QUOTE_CHARS = 300
VERIFY_STATUSES = (Status.RESEARCH, Status.FACT_CHECK)

# Pages that are really a "prove you are human" wall, not the article.
BLOCK_MARKERS = ("checking your browser", "captcha", "are you a robot", "verify you are human",
                 "access denied", "enable javascript and cookies", "unusual traffic")


@dataclass
class FetchResult:
    link_status: LinkStatus
    final_url: str = ""
    title: str = ""
    text: str = ""
    note: str = ""


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchResult: ...


# ------------------------------------------------------------ HTML to text
class _TextParser(HTMLParser):
    SKIP = frozenset({"script", "style", "noscript", "svg", "template"})
    BREAK = frozenset({"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
                       "section", "article", "blockquote", "figcaption"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """(title, visible text) of an HTML page."""
    p = _TextParser()
    p.feed(html)
    title = " ".join("".join(p.title_parts).split())
    lines = (" ".join(line.split()) for line in "".join(p.parts).split("\n"))
    return title, "\n".join(line for line in lines if line)


def looks_blocked(title: str, text: str) -> bool:
    head = f"{title} {text[:2000]}".lower()
    return len(text) < 3000 and any(m in head for m in BLOCK_MARKERS)


# ---------------------------------------------------------------- fetchers
class HttpFetcher:
    """Opens real URLs. Standard library only; one polite request per cited page."""

    def fetch(self, url: str) -> FetchResult:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                final = resp.geturl()
                ctype = resp.headers.get_content_type()
                raw = resp.read(MAX_BYTES + 1)
                charset = resp.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return FetchResult(LinkStatus.BROKEN, url, note=f"HTTP {e.code}: page not found")
            if e.code in (401, 403, 429, 451):
                return FetchResult(LinkStatus.BLOCKED, url, note=f"HTTP {e.code}: site refused")
            return FetchResult(LinkStatus.ERROR, url, note=f"HTTP {e.code}")
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.gaierror):
                return FetchResult(LinkStatus.BROKEN, url, note="the website does not exist")
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                return FetchResult(LinkStatus.ERROR, url, note=(
                    "the secure connection could not be verified (often antivirus or network "
                    "software). Open the page in your browser and mark it as checked."))
            return FetchResult(LinkStatus.ERROR, url, note=f"could not connect: {e.reason}")
        except TimeoutError:
            return FetchResult(LinkStatus.ERROR, url, note="timed out")
        except Exception as e:
            return FetchResult(LinkStatus.ERROR, url, note=f"{type(e).__name__}: {e}")

        if ctype == "application/pdf":
            return FetchResult(LinkStatus.OK, final, note="PDF: loaded, but no text was read")
        if not ctype.startswith("text/") and "xml" not in ctype:
            return FetchResult(LinkStatus.OK, final, note=f"not a web page ({ctype})")
        html = raw[:MAX_BYTES].decode(charset, errors="replace")
        title, text = html_to_text(html) if "html" in ctype else ("", html)
        if looks_blocked(title, text):
            return FetchResult(LinkStatus.BLOCKED, final, title, note="robot check page")
        return FetchResult(LinkStatus.OK, final, title, text)


class FakeFetcher:
    """For tests: {url: FetchResult}. Unknown URLs are broken. Records what was fetched."""

    def __init__(self, pages: dict[str, FetchResult]):
        self.pages = pages
        self.fetched: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.fetched.append(url)
        return self.pages.get(url, FetchResult(LinkStatus.BROKEN, url, note="HTTP 404"))


# ------------------------------------------------------------ evidence
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
# Filler words that say nothing about whether a page supports a fact.
_FILLER = frozenset(["have", "has", "had", "very", "also", "about", "been", "more", "most", "some", "such", "only", "just", "over", "would", "could", "should", "there", "these", "those", "which", "while", "after", "before", "into", "being", "does", "did", "were"])
PAIR_BONUS_NEEDED = 0.10  # two sentences must beat one by this much (they collect filler)


def _stem(word: str) -> str:
    """Very light English stemming: 'shaped'/'shapes'/'shaping' -> 'shap'."""
    for suffix in ("ing", "ed", "es", "s", "ly"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def evidence_tokens(text: str) -> set[str]:
    return {_stem(w) for w in tokens(text) if w not in _FILLER}


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for block in text.split("\n"):
        out.extend(s.strip() for s in _SENTENCE_END.split(block) if len(s.strip()) > 20)
    return out


def best_evidence(statement: str, text: str) -> tuple[str, float]:
    """The sentence (or pair of sentences) that best covers the claim's key words.

    Words are weighted by rarity on the page: on a page about dumplings, "dumplings" proves
    little, while "crescent" or "1978" is real evidence. The score is that weighted share.
    """
    want = evidence_tokens(statement)
    sents = sentences(text)
    if not want or not sents:
        return "", 0.0
    toks = [evidence_tokens(s) for s in sents]
    df: dict[str, int] = {}
    for ts in toks:
        for w in ts:
            df[w] = df.get(w, 0) + 1
    weight = {w: 1 / math.sqrt(1 + df.get(w, 0)) for w in want}
    total = sum(weight.values())

    def score(ts: set[str]) -> float:
        return sum(weight[w] for w in want & ts) / total

    best, best_score = "", 0.0
    for i, s in enumerate(sents):
        single = score(toks[i])
        if single > best_score:
            best, best_score = s, single
        if i + 1 < len(sents):
            pair = score(toks[i] | toks[i + 1])
            if pair >= max(single, score(toks[i + 1])) + PAIR_BONUS_NEEDED and pair > best_score:
                best, best_score = f"{s} {sents[i + 1]}", pair
    if len(best) > QUOTE_CHARS:
        best = best[:QUOTE_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return best, round(best_score, 2)


def title_note(cited_title: str, page_title: str, text: str) -> str:
    """A warning when the page doesn't look like the article the AI named."""
    want = tokens(cited_title)
    if not want:
        return ""
    if containment(want, tokens(f"{page_title} {text[:5000]}")) < 0.5:
        return f'Page title "{page_title[:80]}" does not look like the cited title.'
    return ""


# ------------------------------------------------------------- run checks
def verify_sources(repo: EpisodeRepository, episode_id: str, fetcher: Fetcher, *,
                   actor: str = "system:verify") -> Episode:
    """Open every cited link once, record its status and the best quote for each claim."""
    ep = repo.get_episode(episode_id)
    if ep.status not in VERIFY_STATUSES:
        raise StudioError(f"Sources are checked at RESEARCH or FACT_CHECK; "
                          f"{episode_id} is {ep.status.value}.")
    cited = {sid for c in ep.research.claims if c.status != ClaimStatus.REJECTED
             for sid in c.source_ids}
    now = utc_now()
    texts: dict[str, str] = {}
    sources = []
    for src in ep.research.sources:
        if src.source_id not in cited:
            sources.append(src)
            continue
        if src.url.startswith(MOCK_URL_PREFIX):
            sources.append(src.model_copy(update={"link_status": LinkStatus.MOCK,
                                                  "checked_at": now}))
            continue
        r = fetcher.fetch(src.url)
        texts[src.source_id] = r.text
        note = r.note
        if r.link_status == LinkStatus.OK and r.text:
            note = title_note(src.title, r.title, r.text) or note
        sources.append(src.model_copy(update={
            "link_status": r.link_status, "checked_at": now, "final_url": r.final_url or None,
            "page_title": r.title[:200], "link_note": note}))

    claims = []
    for c in ep.research.claims:
        if not any(sid in texts for sid in c.source_ids):  # nothing fetched: keep as is
            claims.append(c)
            continue
        evidence = []
        for sid in c.source_ids:
            quote, score = best_evidence(c.statement, texts.get(sid, ""))
            if quote:
                evidence.append(Evidence(source_id=sid, quote=quote, score=score))
        claims.append(c.model_copy(update={"evidence": evidence}))

    new = ep.model_copy(update={"research": ep.research.model_copy(
        update={"sources": sources, "claims": claims})})
    counts: dict[str, int] = {}
    for x in sources:
        if x.source_id in cited:
            counts[x.link_status.value] = counts.get(x.link_status.value, 0) + 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    return repo.save(new, actor=actor, note=f"sources checked: {summary}",
                     allowed_sections={"research"})


def mark_source_verified(repo: EpisodeRepository, episode_id: str, source_id: str, *,
                         actor: str, note: str) -> Episode:
    """A human opened the page in a browser and vouches that it exists and fits."""
    if not note.strip():
        raise StudioError("Write a short note on what you checked.")
    ep = repo.get_episode(episode_id)
    if source_id not in {x.source_id for x in ep.research.sources}:
        raise StudioError(f"{episode_id} has no source {source_id}.")
    sources = [x.model_copy(update={"verified_by": actor, "verified_note": note.strip()})
               if x.source_id == source_id else x for x in ep.research.sources]
    new = ep.with_research(sources=sources)
    return repo.save(new, actor=actor, note=f"source {source_id} checked by hand: {note.strip()}",
                     allowed_sections={"research"})
