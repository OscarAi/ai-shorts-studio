"""Human review steps: the decisions an AI is never allowed to make.

Approving a claim, the fact check, QA and publishing are recorded with the actor's name, so
the history always shows who vouched for what.
"""
from collections.abc import Iterable

from . import declines
from .db import EpisodeRepository
from .errors import StudioError
from .models import ClaimStatus, Episode, Publication, QAResult, Status
from .platforms import (
    PLATFORMS,
    TITLE_MAX,
    clean_hashtags,
    live_platforms,
    platform_of,
    source_publishers,
)
from .timeutil import local_now, local_now_text, require_local, tidy
from .validators import check_production

DECISIONS = {"approve": ClaimStatus.APPROVED, "reject": ClaimStatus.REJECTED,
             "reset": ClaimStatus.NEEDS_REVIEW}
DECLINE_STATUSES = (Status.RESEARCH, Status.FACT_CHECK)


# ---------------------------------------------------------------- helpers
def _with_note(text: str, note: str) -> str:
    return text + (f": {note}" if note else "")


def _check_platforms(platforms: Iterable[str]) -> None:
    unknown = [p for p in platforms if p not in PLATFORMS]
    if unknown:
        raise StudioError(f"Unknown platform(s): {', '.join(unknown)}.")


def _clean_title(title: str) -> str:
    title = title.strip()
    if not title:
        raise StudioError("Write a title.")
    if len(title) > TITLE_MAX:
        raise StudioError(f"The title is {len(title)} characters; keep it to {TITLE_MAX}.")
    return title


def _save_publication(repo: EpisodeRepository, ep: Episode, pub: Publication, *, actor: str,
                      note: str, publication_only: bool = False) -> Episode:
    """Every publishing step changes only the publication section (also once published)."""
    return repo.save(ep.model_copy(update={"publication": pub}), actor=actor, note=note,
                     allowed_sections={"publication"}, publication_only=publication_only)


# ------------------------------------------------------------- facts
def set_claim_statuses(repo: EpisodeRepository, episode_id: str,
                       decisions: dict[str, tuple[str, str]], *, actor: str) -> Episode:
    """Several claim decisions at once, as ONE new version: {claim_id: (decision, note)}.
    decision: approve | reject | reset (back to needs_review)."""
    for decision, _ in decisions.values():
        if decision not in DECISIONS:
            raise StudioError(f"Decision must be one of: {', '.join(DECISIONS)}.")
    ep = repo.get_episode(episode_id)
    unknown = sorted(set(decisions) - {c.claim_id for c in ep.research.claims})
    if unknown:
        raise StudioError(f"{episode_id} has no claim {', '.join(unknown)}.")
    claims, summary = [], []
    for claim in ep.research.claims:
        if claim.claim_id not in decisions:
            claims.append(claim)
            continue
        decision, note = decisions[claim.claim_id]
        status = DECISIONS[decision]
        notes = claim.notes + (f"\nReviewer ({actor}): {note}" if note else "")
        claims.append(claim.model_copy(update={"status": status, "notes": notes.strip()}))
        summary.append(_with_note(f"claim {claim.claim_id} -> {status.value}", note))
    return repo.save(ep.with_research(claims=claims), actor=actor, note="; ".join(summary),
                     allowed_sections={"research"})


def set_claim_status(repo: EpisodeRepository, episode_id: str, claim_id: str,
                     decision: str, *, actor: str, note: str = "") -> Episode:
    """decision: approve | reject | reset (back to needs_review)."""
    return set_claim_statuses(repo, episode_id, {claim_id: (decision, note)}, actor=actor)


def decline_source(repo: EpisodeRepository, episode_id: str, source_id: str, *, reason: str,
                   actor: str, block_site: bool = False) -> Episode:
    """A human doesn't want this source: mark it declined and remove what relied only on it
    (claims with no other source are rejected; see studio/declines.py)."""
    ep = repo.get_episode(episode_id)
    if ep.status not in DECLINE_STATUSES:
        raise StudioError(f"Sources can be declined at Research or Fact check; {episode_id} is "
                          f"{ep.status.value}. Go back to Fact check first (Other moves), then "
                          "rewrite the sentences that used its facts.")
    reason = reason.strip()
    if not reason:
        raise StudioError("Say why you decline this source (it is kept in the history).")
    src = next((x for x in ep.research.sources if x.source_id == source_id), None)
    if src is None:
        raise StudioError(f"{episode_id} has no source {source_id}.")
    if src.declined:
        raise StudioError(f"{source_id} is already declined.")
    rejected = [c.claim_id for c in declines.preview(ep, {source_id}).rejected]
    new = declines.decline(ep, {source_id}, reason=reason, actor=actor)
    summary = f"; rejected {', '.join(rejected)}" if rejected else ""
    saved = repo.save(new, actor=actor, allowed_sections={"research", "fact_check"},
                      note=f"source {source_id} ({declines.site_of(src.url)}) declined: "
                           f"{reason}{summary}")
    if block_site:  # "never use this site again", for every future episode too
        repo.block_site(src.url, reason=reason, actor=actor)
    return saved


def set_fact_check(repo: EpisodeRepository, episode_id: str, approved: bool, *,
                   actor: str, note: str = "") -> Episode:
    ep = repo.get_episode(episode_id)
    new = ep.model_copy(update={"fact_check": ep.fact_check.model_copy(
        update={"approved": approved})})
    return repo.save(new, actor=actor,
                     note=_with_note(f"fact check {'approved' if approved else 'not approved'}",
                                     note),
                     allowed_sections={"fact_check"})


# ---------------------------------------------------------------------- QA
QA_CHECKS = {
    "narration": "The narration matches the approved script",
    "facts": "Nothing on screen states a wrong or unapproved fact",
    "visuals": "No garbled text, watermark, or deformed hands or faces",
    "people": "No real living person is shown",
    "licenses": "Every file's source and license allow this use",
}


def automatic_qa(ep: Episode, settings) -> list[dict]:
    """What Python can check by itself before a human watches the video."""
    problems = check_production(ep, settings)
    return [{"check": "automatic", "ok": not problems,
             "detail": "; ".join(i.message for i in problems) or "files, lengths and size OK"}]


def set_qa(repo: EpisodeRepository, episode_id: str, ticked: dict[str, bool], *,
           actor: str, note: str = "") -> Episode:
    """A human watched the video and ticked the checklist. Approved only if every box is
    ticked and Python's own checks passed; unticked items are kept as QA issues."""
    ep = repo.get_episode(episode_id)
    if ep.status != Status.QA:
        raise StudioError(f"QA happens at the QA stage; {episode_id} is {ep.status.value}.")
    note = note.strip()
    auto = automatic_qa(ep, repo.settings)
    human = [{"check": key, "ok": bool(ticked.get(key)), "label": label, "by": actor}
             for key, label in QA_CHECKS.items()]
    issues = [f"Not confirmed: {c['label']}" for c in human if not c["ok"]]
    issues += [f"Automatic check failed: {c['detail']}" for c in auto if not c["ok"]]
    if note and issues:  # explains what is wrong; an approval's note goes in history
        issues.append(f"Note from {actor}: {note}")
    approved = all(c["ok"] for c in human + auto)
    qa = QAResult(deterministic=auto + human, approved=approved, issues=issues)
    return repo.save(ep.model_copy(update={"qa": qa}), actor=actor,
                     note=_with_note("QA approved" if approved else "QA not approved", note),
                     allowed_sections={"qa"})


# ------------------------------------------------------------- publication
AI_NOTE = "Visuals made with AI tools. Facts checked by a human against the sources above."


def default_description(ep: Episode, ai_disclosure: bool = True) -> str:
    """Hook + the publishers behind the approved facts (+ the AI note)."""
    publishers = source_publishers(ep)
    parts = [ep.script.hook or ep.metadata.topic]
    if publishers:
        parts.append("Sources: " + "; ".join(publishers))
    if ai_disclosure:
        parts.append(AI_NOTE)
    return "\n\n".join(parts)


def set_publication(repo: EpisodeRepository, episode_id: str, *, title: str,
                    description: str, hashtags: list[str], platforms: list[str],
                    ai_disclosure: bool, actor: str) -> Episode:
    ep = repo.get_episode(episode_id)
    if ep.status not in (Status.HUMAN_REVIEW, Status.APPROVED):
        raise StudioError("The publication package is prepared at HUMAN_REVIEW or APPROVED.")
    title = _clean_title(title)
    _check_platforms(platforms)
    pub = ep.publication.model_copy(update={
        "title": title, "description": description.strip(),
        "hashtags": clean_hashtags(" ".join(hashtags)), "platforms": list(platforms),
        "ai_disclosure": ai_disclosure})
    return _save_publication(repo, ep, pub, actor=actor, note="publication package saved")


def set_captions(repo: EpisodeRepository, episode_id: str, captions: dict[str, str], *,
                 actor: str, title: str | None = None,
                 hashtags: list[str] | None = None) -> Episode:
    """Save the per-platform post texts (also after publishing, to post elsewhere later)."""
    ep = repo.get_episode(episode_id)
    if ep.status not in (Status.HUMAN_REVIEW, Status.APPROVED, Status.SCHEDULED,
                         Status.PUBLISHED):
        raise StudioError("Post texts are prepared from HUMAN_REVIEW onwards.")
    _check_platforms(captions)
    update: dict = {"captions": {**ep.publication.captions,
                                 **{p: t.strip() for p, t in captions.items()}}}
    if title is not None:
        if not title.strip() or len(title.strip()) > TITLE_MAX:
            raise StudioError(f"The title must be 1-{TITLE_MAX} characters.")
        update["title"] = title.strip()
    if hashtags is not None:
        update["hashtags"] = clean_hashtags(" ".join(hashtags))
    return _save_publication(repo, ep, ep.publication.model_copy(update=update), actor=actor,
                             note="post texts saved: " + ", ".join(PLATFORMS[p] for p in captions),
                             publication_only=ep.status == Status.PUBLISHED)


# --------------------------------------------------- uploaded: links and go-live times
def _clean_links(existing: list[str], new: list[str]) -> list[str]:
    """Existing links + new full https links, no duplicates. Refuses anything else."""
    new = [u.strip() for u in new if u.strip()]
    if not new or any(not u.startswith(("https://", "http://")) for u in new):
        raise StudioError("Paste the full link(s) of the video, starting with https://")
    return existing + [u for u in new if u not in existing]


def _live_times(pub: Publication, links: list[str], when: str) -> dict[str, str]:
    """When each newly linked platform went live (analytics counts the video's age from it).
    A scheduled platform's go-live time is its schedule, so it is not repeated here."""
    live = dict(pub.live_at)
    for url in links:
        p = platform_of(url)
        if p and p not in live and p not in pub.schedule:
            live[p] = when
    return live


def _publish_if_live(repo: EpisodeRepository, ep: Episode, actor: str) -> Episode:
    """SCHEDULED, but a platform is already live: the video is out, so it is PUBLISHED.
    Platforms still scheduled keep their go-live times."""
    live = live_platforms(ep)
    if ep.status != Status.SCHEDULED or not live:
        return ep
    return repo.transition(ep.episode_id, Status.PUBLISHED, actor=actor,
                           reason=f"live on {', '.join(PLATFORMS[p] for p in live)}")


def mark_published(repo: EpisodeRepository, episode_id: str, urls: list[str], *,
                   actor: str) -> Episode:
    """After YOU uploaded the video: record the links, then lock the episode as PUBLISHED."""
    ep = repo.get_episode(episode_id)
    if ep.status != Status.APPROVED:
        raise StudioError(f"Only an APPROVED episode can be published; {episode_id} is "
                          f"{ep.status.value}.")
    links = _clean_links(ep.publication.urls, urls)
    pub = ep.publication.model_copy(update={
        "urls": links, "live_at": _live_times(ep.publication, links, local_now_text())})
    _save_publication(repo, ep, pub, actor=actor, note="published links saved")
    return repo.transition(episode_id, Status.PUBLISHED, actor=actor)


def mark_scheduled(repo: EpisodeRepository, episode_id: str, schedule: dict[str, str],
                   urls: list[str], *, actor: str, live_now: Iterable[str] = ()) -> Episode:
    """After YOU uploaded the video: record the links, when each platform goes live, and which
    are live already (`live_now`, e.g. an app without scheduling). If any platform is live the
    episode is PUBLISHED (the rest keep their times), otherwise SCHEDULED."""
    ep = repo.get_episode(episode_id)
    if ep.status != Status.APPROVED:
        raise StudioError(f"Only an APPROVED episode can be scheduled; {episode_id} is "
                          f"{ep.status.value}.")
    _check_platforms(schedule)
    live_now = [p for p in live_now if p in PLATFORMS]
    schedule = {p: tidy(w) for p, w in schedule.items() if w.strip() and p not in live_now}
    for p, when in schedule.items():
        require_local(when, f"the {PLATFORMS[p]} time")
    links = _clean_links(ep.publication.urls, urls)
    linked = {platform_of(u) for u in links}
    missing = [PLATFORMS[p] for p in live_now if p not in linked]
    if missing:
        raise StudioError(f"Paste the link for {', '.join(missing)} (it is live now).")
    now = local_now_text()
    pub = ep.publication.model_copy(update={
        "urls": links,
        "schedule": {**ep.publication.schedule, **schedule},
        "live_at": {**ep.publication.live_at, **dict.fromkeys(live_now, now)}})
    parts = [f"{PLATFORMS[p]} {w}" for p, w in schedule.items()]
    parts += [f"{PLATFORMS[p]} live now" for p in live_now]
    _save_publication(repo, ep, pub, actor=actor, note="schedule saved: " + ", ".join(parts))
    target = Status.PUBLISHED if live_now else Status.SCHEDULED
    return repo.transition(episode_id, target, actor=actor)


def mark_live(repo: EpisodeRepository, episode_id: str, *, actor: str) -> Episode:
    """The scheduled video is now public: SCHEDULED -> PUBLISHED (locked)."""
    ep = repo.get_episode(episode_id)
    if ep.status != Status.SCHEDULED:
        raise StudioError(f"{episode_id} is not scheduled; it is {ep.status.value}.")
    return repo.transition(episode_id, Status.PUBLISHED, actor=actor)


def add_links(repo: EpisodeRepository, episode_id: str, urls: list[str], *,
              actor: str, posted_at: str | None = None) -> Episode:
    """Add the links of platforms posted on later (allowed when SCHEDULED or PUBLISHED).
    The only change a published episode accepts; it is recorded in the history.
    `posted_at` ("YYYY-MM-DD HH:MM", default now) is when that post went live; a later time
    is recorded as that platform's schedule."""
    ep = repo.get_episode(episode_id)
    if ep.status not in (Status.SCHEDULED, Status.PUBLISHED):
        raise StudioError("Links are added here after scheduling or publishing.")
    when = tidy(posted_at or local_now_text())
    moment = require_local(when, "when it was posted")
    links = _clean_links(ep.publication.urls, urls)
    added = [u for u in links if u not in ep.publication.urls]  # older links keep their time
    if moment > local_now():  # goes live later
        schedule = dict(ep.publication.schedule)
        for u in added:
            p = platform_of(u)
            if p and p not in ep.publication.live_at:
                schedule[p] = when
        pub = ep.publication.model_copy(update={"urls": links, "schedule": schedule})
    else:
        pub = ep.publication.model_copy(update={
            "urls": links, "live_at": _live_times(ep.publication, added, when)})
    saved = _save_publication(repo, ep, pub, actor=actor, publication_only=True,
                              note="link(s) added: " + ", ".join(u for u in urls if u.strip()))
    return _publish_if_live(repo, saved, actor)
