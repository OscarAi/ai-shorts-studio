"""Declining sources you don't want, and removing what depended only on them.

Pure functions on an Episode (no database), used by review.decline_source (a human's choice)
and by the researcher agent (sources from blocked sites are declined automatically).

What declining does:
  * the source stays in the research, marked declined (with who and why), never usable again;
  * every claim loses that source and its evidence quotes;
  * a claim left with no other source is rejected ("its only source was declined");
  * an approved claim that still has other sources goes back to needs_review (support changed);
  * an approved fact check is withdrawn, so a human approves the facts again.
"""
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .models import Claim, ClaimStatus, Episode


# ------------------------------------------------------------------ sites
def site_of(url: str) -> str:
    """The site a link belongs to: 'https://www.History.com/a/b' -> 'history.com'."""
    host = (urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    return host.removeprefix("www.")


def site_matches(url: str, domain: str) -> bool:
    """True when the link is on that site or one of its subdomains."""
    host, domain = site_of(url), clean_domain(domain)
    return bool(domain) and (host == domain or host.endswith("." + domain))


def clean_domain(text: str) -> str:
    """What a person typed ('https://www.example.com/page', 'Example.com') -> 'example.com'."""
    text = text.strip()
    return site_of(text if "//" in text else f"https://{text}")


# --------------------------------------------------------------- preview
@dataclass
class DeclinePreview:
    source_ids: list[str]
    rejected: list[Claim] = field(default_factory=list)        # no source left
    back_to_review: list[Claim] = field(default_factory=list)  # approved, still has sources
    loses_source: list[Claim] = field(default_factory=list)    # still has other sources

    def lines(self) -> list[str]:
        out = [f"Will be rejected (no other source): {c.claim_id} \"{c.statement}\""
               for c in self.rejected]
        out += [f"Back to review (support changed): {c.claim_id} \"{c.statement}\""
                for c in self.back_to_review]
        out += [f"Keeps its other sources: {c.claim_id}" for c in self.loses_source
                if c not in self.back_to_review]
        return out or ["No claim uses this source."]


def preview(ep: Episode, source_ids: set[str]) -> DeclinePreview:
    """What declining these sources would change, before anything is saved."""
    p = DeclinePreview(sorted(source_ids))
    for c in ep.research.claims:
        if c.status == ClaimStatus.REJECTED or not set(c.source_ids) & source_ids:
            continue
        left = [sid for sid in c.source_ids if sid not in source_ids]
        if not left:
            p.rejected.append(c)
            continue
        p.loses_source.append(c)
        if c.status == ClaimStatus.APPROVED:
            p.back_to_review.append(c)
    return p


# ---------------------------------------------------------------- decline
def decline(ep: Episode, source_ids: set[str], *, reason: str, actor: str) -> Episode:
    """The episode with these sources declined and everything that depended only on them
    removed (see the module docstring). Unknown ids raise KeyError."""
    known = {x.source_id for x in ep.research.sources}
    missing = source_ids - known
    if missing:
        raise KeyError(", ".join(sorted(missing)))
    names = ", ".join(sorted(source_ids))
    sources = [x.model_copy(update={"declined_by": actor, "declined_note": reason})
               if x.source_id in source_ids and not x.declined else x
               for x in ep.research.sources]
    claims, changed = [], False
    for c in ep.research.claims:
        if not set(c.source_ids) & source_ids:
            claims.append(c)
            continue
        changed = changed or c.status != ClaimStatus.REJECTED
        left = [sid for sid in c.source_ids if sid not in source_ids]
        update: dict = {"source_ids": left,
                        "evidence": [e for e in c.evidence if e.source_id not in source_ids]}
        if c.status != ClaimStatus.REJECTED:
            if not left:
                update["status"] = ClaimStatus.REJECTED
                update["notes"] = _note(c, f"Rejected: its only source ({names}) was declined"
                                           f" by {actor}: {reason}")
            elif c.status == ClaimStatus.APPROVED:
                update["status"] = ClaimStatus.NEEDS_REVIEW
                update["notes"] = _note(c, f"Back to review: source {names} was declined "
                                           f"by {actor}; check the remaining sources.")
        claims.append(c.model_copy(update=update))
    research = ep.research.model_copy(update={"sources": sources, "claims": claims})
    update = {"research": research}
    if changed and ep.fact_check.approved:
        update["fact_check"] = ep.fact_check.model_copy(update={
            "approved": False,
            "issues": ep.fact_check.issues + [f"Source {names} was declined: approve the "
                                              "facts again."]})
    return ep.model_copy(update=update)


def _note(c: Claim, text: str) -> str:
    return f"{c.notes}\n{text}".strip()


# ---------------------------------------------------------------- enforce
def enforce(ep: Episode, blocked: list[dict]) -> Episode:
    """After a researcher answer: decline sources on blocked sites, and remove a declined
    source again if new claims cite it (a re-run can't bring a declined page back).
    `blocked` is repo.blocked_sites()."""
    active = {sid for c in ep.research.claims if c.status != ClaimStatus.REJECTED
              for sid in c.source_ids}
    for src in ep.research.sources:
        if src.declined:
            if src.source_id in active:
                ep = decline(ep, {src.source_id}, reason=src.declined_note,
                             actor=src.declined_by or "system")
            continue
        site = next((b for b in blocked if site_matches(src.url, b["domain"])), None)
        if site:
            ep = decline(ep, {src.source_id}, actor="system:blocked-site",
                         reason=f"{site['domain']} is blocked: {site['reason']}")
    return ep


def blocked_list_text(blocked: list[dict]) -> str:
    """For the researcher prompt."""
    return ", ".join(b["domain"] for b in blocked) or "(none)"
