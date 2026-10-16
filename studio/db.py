"""SQLite storage for episodes (the 'repository').

Design in plain words:
  * `episodes`          one row per episode: fast search columns + current version number.
  * `episode_versions`  APPEND-ONLY. Every save adds a full JSON snapshot. Nothing is overwritten.
  * `events`            audit log: who did what, when, and why.
  * `claims`, `sources` library-wide index rebuilt from each save (powers the originality gate later).
  * `rejections`        why an episode was rejected, and whether it was revived.
  * `originality_checks` every originality-gate run, plus your accept/reject decision.
  * `metrics`           APPEND-ONLY performance snapshots (views, likes...) per video and platform.
  * `blocked_sites`     sites you never want as a source (the researcher is told; matches are declined).
  * `ai_calls`          APPEND-ONLY log of every AI request: prompt, answer, service, model, problems.
  * `ai_usage`          one row per request sent to an AI service (for the daily limits).

All other code talks to this class only, so moving to another database later is a contained change.
"""
import logging
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Settings
from .declines import clean_domain
from .errors import (
    ConcurrentEditError,
    EpisodeNotFound,
    ImmutableEpisodeError,
    ProtectedChangeError,
    SectionOwnershipError,
    StudioError,
    ValidationFailed,
)
from .models import (
    NUMBER_FIELDS,
    Category,
    ClaimStatus,
    Episode,
    Metadata,
    MetricSnapshot,
    Rejection,
    Status,
    changed_sections,
    utc_now,
)
from .originality import OriginalityReport, SemanticJudge, Verdict, evaluate, fingerprint
from .platforms import PLATFORMS, posted_platforms
from .state_machine import assert_transition, is_backward
from .textnorm import normalize_text
from .timeutil import local_midnight_utc
from .validators import Issue, has_errors, validate_for_transition

log = logging.getLogger("studio.db")

SCHEMA_VERSION = 3

# The originality gate guards both moves where a topic can still change before facts are fixed.
GATED_MOVES = {(Status.IDEA, Status.RESEARCH), (Status.RESEARCH, Status.FACT_CHECK)}
GATE_STAGES = ("idea", "research")
TOPIC_EDITABLE = {Status.IDEA, Status.RESEARCH, Status.REJECTED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    episode_id      TEXT PRIMARY KEY,
    channel_id      TEXT NOT NULL,
    status          TEXT NOT NULL,
    topic           TEXT NOT NULL,
    category        TEXT NOT NULL,
    series          TEXT NOT NULL DEFAULT '',
    current_version INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_status   ON episodes(status);
CREATE INDEX IF NOT EXISTS idx_episodes_category ON episodes(category);
CREATE INDEX IF NOT EXISTS idx_episodes_channel  ON episodes(channel_id);

CREATE TABLE IF NOT EXISTS episode_versions (
    episode_id    TEXT NOT NULL REFERENCES episodes(episode_id),
    version       INTEGER NOT NULL,
    saved_at      TEXT NOT NULL,
    actor         TEXT NOT NULL,
    change_note   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY (episode_id, version)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id TEXT NOT NULL,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_episode ON events(episode_id);

CREATE TABLE IF NOT EXISTS claims (
    episode_id           TEXT NOT NULL REFERENCES episodes(episode_id),
    claim_id             TEXT NOT NULL,
    statement            TEXT NOT NULL,
    normalized_statement TEXT NOT NULL,
    importance           TEXT NOT NULL,
    status               TEXT NOT NULL,
    PRIMARY KEY (episode_id, claim_id)
);
CREATE INDEX IF NOT EXISTS idx_claims_norm ON claims(normalized_statement);

CREATE TABLE IF NOT EXISTS sources (
    episode_id  TEXT NOT NULL REFERENCES episodes(episode_id),
    source_id   TEXT NOT NULL,
    url         TEXT NOT NULL,
    publisher   TEXT NOT NULL,
    title       TEXT NOT NULL,
    reliability TEXT NOT NULL,
    PRIMARY KEY (episode_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_sources_url ON sources(url);

CREATE TABLE IF NOT EXISTS originality_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id    TEXT NOT NULL REFERENCES episodes(episode_id),
    stage         TEXT NOT NULL,
    checked_at    TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    report_json   TEXT NOT NULL,
    decision      TEXT,
    decision_note TEXT,
    decided_by    TEXT,
    decided_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_orig_episode ON originality_checks(episode_id);

CREATE TABLE IF NOT EXISTS rejections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  TEXT NOT NULL REFERENCES episodes(episode_id),
    from_status TEXT NOT NULL,
    reason      TEXT NOT NULL,
    rejected_at TEXT NOT NULL,
    revived_at  TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id       TEXT NOT NULL REFERENCES episodes(episode_id),
    platform         TEXT NOT NULL,
    captured_at      TEXT NOT NULL,
    day              INTEGER NOT NULL,
    views            INTEGER,
    engaged_views    INTEGER,
    likes            INTEGER,
    comments         INTEGER,
    shares           INTEGER,
    saves            INTEGER,
    followers_gained INTEGER,
    avg_view_sec     REAL,
    stayed_pct       REAL,
    completion_pct   REAL,
    source           TEXT NOT NULL,
    actor            TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_metrics_episode ON metrics(episode_id, platform);

CREATE TABLE IF NOT EXISTS ai_usage (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    provider TEXT NOT NULL,
    model    TEXT NOT NULL,
    kind     TEXT NOT NULL,
    outcome  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_ts ON ai_usage(ts);

CREATE TABLE IF NOT EXISTS ai_calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id TEXT NOT NULL,
    ts         TEXT NOT NULL,
    agent      TEXT NOT NULL,
    attempt    INTEGER NOT NULL,
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    seconds    REAL NOT NULL DEFAULT 0,
    prompt     TEXT NOT NULL,
    answer     TEXT NOT NULL DEFAULT '',
    problems   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ai_calls_episode ON ai_calls(episode_id);

CREATE TABLE IF NOT EXISTS blocked_sites (
    domain     TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    blocked_by TEXT NOT NULL,
    blocked_at TEXT NOT NULL
);
"""


# Every episode's current version (the newest snapshot).
CURRENT_SNAPSHOTS = ("SELECT v.snapshot_json FROM episodes e JOIN episode_versions v "
                     "ON v.episode_id = e.episode_id AND v.version = e.current_version")


class EpisodeRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db_path = Path(settings.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------- connections
    def _init_schema(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction: everything inside succeeds together or not at all."""
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _rows(self, sql: str, params: tuple | list = ()) -> list[dict]:
        """Run a read query; every row as a plain dict."""
        with self._read() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    @staticmethod
    def _event(conn: sqlite3.Connection, episode_id: str, actor: str, event_type: str,
               detail: str = "", ts: str | None = None) -> None:
        """One line in the audit log (who did what, when, and why)."""
        conn.execute(
            "INSERT INTO events(episode_id, ts, actor, event_type, detail) VALUES (?,?,?,?,?)",
            (episode_id, ts or utc_now(), actor, event_type, detail))

    # ------------------------------------------------------ internals
    def _load_current(self, conn: sqlite3.Connection, episode_id: str) -> Episode:
        row = conn.execute(CURRENT_SNAPSHOTS + " WHERE e.episode_id = ?",
                           (episode_id,)).fetchone()
        if row is None:
            raise EpisodeNotFound(f"No episode with id {episode_id}.")
        return Episode.model_validate_json(row["snapshot_json"])

    def _write_version(self, conn: sqlite3.Connection, ep: Episode, actor: str,
                       note: str, event_type: str) -> None:
        """Append a snapshot and refresh the search/index tables."""
        conn.execute(
            "INSERT INTO episode_versions(episode_id, version, saved_at, actor, change_note, "
            "status, snapshot_json) VALUES (?,?,?,?,?,?,?)",
            (ep.episode_id, ep.version, ep.updated_at, actor, note,
             ep.status.value, ep.model_dump_json()))
        conn.execute(
            "UPDATE episodes SET status=?, topic=?, category=?, series=?, current_version=?, "
            "updated_at=? WHERE episode_id=?",
            (ep.status.value, ep.metadata.topic, ep.metadata.category.value,
             ep.metadata.series, ep.version, ep.updated_at, ep.episode_id))
        conn.execute("DELETE FROM claims WHERE episode_id=?", (ep.episode_id,))
        for c in ep.research.claims:
            conn.execute(
                "INSERT INTO claims VALUES (?,?,?,?,?,?)",
                (ep.episode_id, c.claim_id, c.statement, normalize_text(c.statement),
                 c.importance.value, c.status.value))
        conn.execute("DELETE FROM sources WHERE episode_id=?", (ep.episode_id,))
        for s in ep.research.sources:
            conn.execute(
                "INSERT INTO sources VALUES (?,?,?,?,?,?)",
                (ep.episode_id, s.source_id, s.url, s.publisher, s.title, s.reliability.value))
        self._event(conn, ep.episode_id, actor, event_type, note, ts=ep.updated_at)

    @staticmethod
    def _check_protected(old: Episode, new: Episode) -> None:
        """Approved claims may not be silently reworded or deleted."""
        new_claims = {c.claim_id: c for c in new.research.claims}
        for c in old.research.claims:
            if c.status == ClaimStatus.APPROVED:
                n = new_claims.get(c.claim_id)
                if n is None or n.statement != c.statement:
                    raise ProtectedChangeError(
                        f"Approved claim {c.claim_id} was changed or removed. "
                        "Re-submit it as a NEW claim, or pass allow_claim_changes=True on purpose.")

    # ------------------------------------------------------ create / read
    def create_episode(self, topic: str, category: Category | str = Category.OTHER, *,
                       series: str = "", channel_id: str | None = None,
                       actor: str = "human", **metadata_fields) -> Episode:
        now = utc_now()
        metadata_fields.setdefault("target_duration_sec", self.settings.target_duration_sec)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT MAX(CAST(SUBSTR(episode_id, 4) AS INTEGER)) AS m FROM episodes").fetchone()
            number = (row["m"] or 0) + 1
            ep = Episode(
                episode_id=f"EP_{number:06d}",
                channel_id=channel_id or self.settings.default_channel_id,
                created_at=now, updated_at=now,
                metadata=Metadata(topic=topic, category=Category(category), series=series,
                                  **metadata_fields))
            conn.execute(
                "INSERT INTO episodes(episode_id, channel_id, status, topic, category, series, "
                "current_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ep.episode_id, ep.channel_id, ep.status.value, ep.metadata.topic,
                 ep.metadata.category.value, ep.metadata.series, ep.version, now, now))
            self._write_version(conn, ep, actor, "created", "created")
        log.info("episode created", extra={"ctx": {"episode_id": ep.episode_id, "actor": actor}})
        return ep

    def get_episode(self, episode_id: str) -> Episode:
        with self._read() as conn:
            return self._load_current(conn, episode_id)

    def get_version(self, episode_id: str, version: int) -> Episode:
        with self._read() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM episode_versions WHERE episode_id=? AND version=?",
                (episode_id, version)).fetchone()
        if row is None:
            raise EpisodeNotFound(f"No version {version} for episode {episode_id}.")
        return Episode.model_validate_json(row["snapshot_json"])

    def list_versions(self, episode_id: str) -> list[dict]:
        return self._rows("SELECT version, saved_at, actor, change_note, status "
                          "FROM episode_versions WHERE episode_id=? ORDER BY version",
                          (episode_id,))

    def list_events(self, episode_id: str) -> list[dict]:
        return self._rows("SELECT ts, actor, event_type, detail FROM events "
                          "WHERE episode_id=? ORDER BY id", (episode_id,))

    def list_episodes(self, *, status: Status | str | None = None,
                      category: Category | str | None = None, series: str | None = None,
                      search: str | None = None, limit: int = 500) -> list[dict]:
        sql = ("SELECT episode_id, channel_id, status, topic, category, series, "
               "current_version, updated_at FROM episodes WHERE 1=1")
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(Status(status).value)
        if category:
            sql += " AND category = ?"
            params.append(Category(category).value)
        if series:
            sql += " AND series = ?"
            params.append(series)
        if search:
            sql += " AND (topic LIKE ? OR series LIKE ? OR episode_id LIKE ?)"
            like = f"%{search}%"
            params += [like, like, like]
        sql += " ORDER BY episode_id DESC LIMIT ?"
        params.append(limit)
        return self._rows(sql, params)

    def count_by_status(self) -> dict[str, int]:
        rows = self._rows("SELECT status, COUNT(*) AS n FROM episodes GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # ------------------------------------------------------------- save
    def save(self, episode: Episode, *, actor: str, note: str = "",
             allowed_sections: set[str] | None = None,
             allow_claim_changes: bool = False, publication_only: bool = False) -> Episode:
        """Save edits as a NEW version. Never overwrites history.

        - `episode` must be a copy you loaded earlier (stale copies are rejected).
        - `status` cannot change here; use transition().
        - `allowed_sections`: if given, only those top-level sections may differ
          (this is how an agent is restricted to its own section).
        - SCHEDULED: the video is already uploaded, so only `publication` may change.
        - PUBLISHED is locked; the one exception is `publication_only=True`, used to add the
          links of platforms you post on later (nothing outside `publication` may change).
        """
        with self._tx() as conn:
            current = self._load_current(conn, episode.episode_id)
            if current.status == Status.PUBLISHED and not publication_only:
                raise ImmutableEpisodeError(
                    f"{current.episode_id} is PUBLISHED and can no longer be edited.")
            if current.status in (Status.SCHEDULED, Status.PUBLISHED) or publication_only:
                outside = set(changed_sections(current, episode)) - {"publication"}
                if outside:
                    raise ImmutableEpisodeError(
                        f"{current.episode_id} is {current.status.value}: only the publication "
                        f"details can change, not {', '.join(sorted(outside))}.")
            if episode.version != current.version:
                raise ConcurrentEditError(
                    f"{current.episode_id} is at version {current.version} but you edited "
                    f"version {episode.version}. Reload and re-apply your change.")
            if episode.status != current.status:
                raise StudioError("Status can only change through transition().")
            if allowed_sections is not None:
                extra = set(changed_sections(current, episode)) - set(allowed_sections)
                if extra:
                    raise SectionOwnershipError(
                        f"{actor} may not edit: {', '.join(sorted(extra))}.")
            if not allow_claim_changes:
                self._check_protected(current, episode)
            if (current.status not in TOPIC_EDITABLE
                    and fingerprint(episode) != fingerprint(current)):
                raise ProtectedChangeError(
                    f"The topic and story angle are locked once research is done "
                    f"({current.episode_id} is {current.status.value}). For a new topic, "
                    "create a new episode.")
            new = episode.model_copy(
                update={"version": current.version + 1, "updated_at": utc_now()}, deep=True)
            self._write_version(conn, new, actor, note or "saved", "saved")
        log.info("episode saved", extra={"ctx": {"episode_id": new.episode_id,
                                                  "version": new.version, "actor": actor}})
        return new

    # ------------------------------------------------------- transition
    def transition(self, episode_id: str, to_status: Status | str, *, actor: str,
                   reason: str = "") -> Episode:
        """Move an episode to a new status, if the rules allow it."""
        to_status = Status(to_status)
        with self._tx() as conn:
            ep = self._load_current(conn, episode_id)
            from_status = ep.status
            assert_transition(from_status, to_status)

            needs_reason = (Status.REJECTED in (to_status, from_status) or is_backward(from_status, to_status))
            if needs_reason and not reason.strip():
                raise StudioError(
                    f"A reason is required for {from_status.value} -> {to_status.value}.")

            issues = validate_for_transition(ep, to_status, self.settings)
            if (from_status, to_status) in GATED_MOVES:
                issues = self._originality_issues(conn, ep) + issues
            if has_errors(issues):
                raise ValidationFailed([i for i in issues if i.severity == "error"])

            now = utc_now()
            updates: dict = {"status": to_status, "version": ep.version + 1, "updated_at": now}
            if to_status == Status.REJECTED:
                updates["rejection"] = Rejection(
                    reason=reason, from_status=from_status, rejected_at=now)
                conn.execute(
                    "INSERT INTO rejections(episode_id, from_status, reason, rejected_at) "
                    "VALUES (?,?,?,?)", (episode_id, from_status.value, reason, now))
            elif from_status == Status.REJECTED:
                updates["rejection"] = None
                conn.execute(
                    "UPDATE rejections SET revived_at=? WHERE episode_id=? AND revived_at IS NULL",
                    (now, episode_id))
            if to_status == Status.PUBLISHED:
                updates["publication"] = ep.publication.model_copy(update={"published_at": now})

            new = ep.model_copy(update=updates, deep=True)
            note = f"{from_status.value} -> {to_status.value}" + (f": {reason}" if reason else "")
            self._write_version(conn, new, actor, note, "transition")
        log.info("episode transition", extra={"ctx": {
            "episode_id": episode_id, "from": from_status.value, "to": to_status.value,
            "actor": actor}})
        return new

    # ------------------------------------------------------ originality
    def all_episodes(self) -> list[Episode]:
        """Every episode at its current version (the library)."""
        return [Episode.model_validate_json(r["snapshot_json"])
                for r in self._rows(CURRENT_SNAPSHOTS)]

    @staticmethod
    def _check_row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        report = OriginalityReport.model_validate_json(d.pop("report_json"))
        d["report"] = report.model_copy(update={"check_id": d["id"]})
        return d

    def check_originality(self, episode_id: str, *, stage: str = "idea", actor: str = "system",
                          judge: SemanticJudge | None = None) -> OriginalityReport:
        """Compare an episode with the rest of the library and store the result."""
        ep = self.get_episode(episode_id)
        report = evaluate(ep, self.all_episodes(), self.settings, judge=judge, stage=stage)
        now = utc_now()
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO originality_checks(episode_id, stage, checked_at, fingerprint, "
                "verdict, report_json) VALUES (?,?,?,?,?,?)",
                (episode_id, stage, now, fingerprint(ep), report.verdict.value,
                 report.model_dump_json()))
            check_id = cur.lastrowid
            self._event(conn, episode_id, actor, "originality_check",
                        f"{stage}: {report.verdict.value} ({len(report.matches)} matches)", ts=now)
        log.info("originality check", extra={"ctx": {
            "episode_id": episode_id, "stage": stage, "verdict": report.verdict.value}})
        return report.model_copy(update={"check_id": check_id})

    def latest_originality(self, episode_id: str, stage: str = "idea") -> dict | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM originality_checks WHERE episode_id=? AND stage=? "
                "ORDER BY id DESC LIMIT 1", (episode_id, stage)).fetchone()
        return self._check_row_to_dict(row) if row else None

    def list_originality_checks(self, episode_id: str) -> list[dict]:
        return self._rows("SELECT id, stage, checked_at, verdict, decision, decision_note, "
                          "decided_by FROM originality_checks WHERE episode_id=? ORDER BY id",
                          (episode_id,))

    def decide_originality(self, check_id: int, decision: str, *, actor: str,
                           note: str = "") -> None:
        """Record YOUR decision on a check: 'accept' (go ahead) or 'reject' (not original enough)."""
        if decision not in ("accept", "reject"):
            raise StudioError("Decision must be 'accept' or 'reject'.")
        with self._tx() as conn:
            row = conn.execute("SELECT episode_id, verdict FROM originality_checks WHERE id=?",
                               (check_id,)).fetchone()
            if row is None:
                raise StudioError(f"No originality check with id {check_id}.")
            if row["verdict"] != Verdict.PASS.value and not note.strip():
                raise StudioError("Please write a short note explaining your decision.")
            now = utc_now()
            conn.execute(
                "UPDATE originality_checks SET decision=?, decision_note=?, decided_by=?, "
                "decided_at=? WHERE id=?", (decision, note, actor, now, check_id))
            self._event(conn, row["episode_id"], actor, "originality_decision",
                        f"check {check_id}: {decision}" + (f" - {note}" if note else ""), ts=now)

    @staticmethod
    def _gate_state(ep: Episode, check: dict | None) -> str:
        if check is None:
            return "none"
        if check["fingerprint"] != fingerprint(ep):
            return "stale"
        if check["verdict"] == Verdict.PASS.value:
            return "pass"
        return {"accept": "accepted", "reject": "declined"}.get(check["decision"], "needs_review")

    @staticmethod
    def _latest_gate_check(conn: sqlite3.Connection, episode_id: str) -> dict | None:
        """The newest idea- or research-stage check: either one can open the gate."""
        row = conn.execute(
            "SELECT * FROM originality_checks WHERE episode_id=? AND stage IN (?, ?) "
            "ORDER BY id DESC LIMIT 1", (episode_id, *GATE_STAGES)).fetchone()
        return dict(row) if row else None

    def originality_gate_state(self, episode_id: str) -> str:
        """One of: none, stale, pass, accepted, needs_review, declined."""
        with self._read() as conn:
            return self._gate_state(self._load_current(conn, episode_id),
                                    self._latest_gate_check(conn, episode_id))

    def _originality_issues(self, conn: sqlite3.Connection, ep: Episode) -> list[Issue]:
        state = self._gate_state(ep, self._latest_gate_check(conn, ep.episode_id))
        messages = {
            "none": ("ORIGINALITY_NOT_CHECKED", "Run the originality check first."),
            "stale": ("ORIGINALITY_STALE",
                      "The topic or angle changed since the last originality check. Run it again."),
            "needs_review": ("ORIGINALITY_NEEDS_DECISION",
                             "The originality check flagged this idea. Accept or reject it first."),
            "declined": ("ORIGINALITY_DECLINED",
                         "You marked this idea as not original enough."),
        }
        if state in messages:
            code, msg = messages[state]
            return [Issue(code, msg)]
        return []

    def originality_issues(self, episode_id: str) -> list[Issue]:
        """What the originality gate would say about leaving IDEA or RESEARCH right now."""
        with self._read() as conn:
            return self._originality_issues(conn, self._load_current(conn, episode_id))

    def list_rejections(self, episode_id: str) -> list[dict]:
        return self._rows("SELECT from_status, reason, rejected_at, revived_at FROM rejections "
                          "WHERE episode_id=? ORDER BY id", (episode_id,))

    # ---------------------------------------------------------- metrics
    _METRIC_COLUMNS = ("episode_id", "platform", "captured_at", "day") + NUMBER_FIELDS + (
        "source", "actor", "note")

    def add_metrics(self, snap: MetricSnapshot, *, actor: str) -> MetricSnapshot:
        """Store one snapshot of a published video's numbers (never edits an old one)."""
        with self._tx() as conn:
            ep = self._load_current(conn, snap.episode_id)
            if ep.status != Status.PUBLISHED:
                raise StudioError(f"Numbers can be added once {ep.episode_id} is PUBLISHED; "
                                  f"it is {ep.status.value}.")
            posted = posted_platforms(ep)
            if snap.platform not in posted:
                name = PLATFORMS.get(snap.platform, snap.platform)
                raise StudioError(f"{ep.episode_id} has no {name} link yet. Add the link first "
                                  "(Add a link), then its numbers.")
            row = snap.model_dump() | {"actor": actor}
            cols = self._METRIC_COLUMNS
            conn.execute(f"INSERT INTO metrics({', '.join(cols)}) "
                         f"VALUES ({', '.join('?' * len(cols))})", [row[c] for c in cols])
            filled = ", ".join(f"{f} {row[f]:g}" for f in NUMBER_FIELDS if row[f] is not None)
            self._event(conn, ep.episode_id, actor, "metrics",
                        f"{PLATFORMS.get(snap.platform, snap.platform)} day {snap.day}: {filled}",
                        ts=snap.captured_at)
        log.info("metrics added", extra={"ctx": {"episode_id": snap.episode_id,
                                                  "platform": snap.platform, "day": snap.day}})
        return snap

    def list_metrics(self, episode_id: str | None = None) -> list[MetricSnapshot]:
        """Snapshots, oldest first (one episode, or all)."""
        sql = f"SELECT {', '.join(c for c in self._METRIC_COLUMNS if c != 'actor')} FROM metrics"
        rows = (self._rows(sql + " WHERE episode_id=? ORDER BY id", (episode_id,)) if episode_id
                else self._rows(sql + " ORDER BY id"))
        return [MetricSnapshot(**r) for r in rows]

    # ------------------------------------------------------------ AI log
    def log_ai_call(self, *, episode_id: str, agent: str, attempt: int, provider: str,
                    model: str, seconds: float, prompt: str, answer: str,
                    problems: list[str]) -> None:
        """Remember one AI request and what came back (the key is never part of it)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO ai_calls(episode_id, ts, agent, attempt, provider, model, seconds, "
                "prompt, answer, problems) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (episode_id, utc_now(), agent, attempt, provider, model, seconds, prompt,
                 answer, "\n".join(problems)))

    def record_ai_request(self, *, provider: str, model: str, kind: str, outcome: str) -> None:
        """Every request sent to an AI service, answered or not (the daily limits count these)."""
        with self._tx() as conn:
            conn.execute("INSERT INTO ai_usage(ts, provider, model, kind, outcome) "
                         "VALUES (?,?,?,?,?)", (utc_now(), provider, model, kind, outcome))

    def ai_recent_failure(self, provider: str, model: str, minutes: float,
                          outcomes: tuple[str, ...]) -> bool:
        """Did this model fail like that in the last `minutes`? (used to skip it for a while)"""
        since = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        marks = ",".join("?" * len(outcomes))
        with self._read() as conn:
            row = conn.execute(
                f"SELECT 1 FROM ai_usage WHERE provider=? AND model=? AND ts >= ? "
                f"AND outcome IN ({marks}) LIMIT 1", (provider, model, since, *outcomes)).fetchone()
        return row is not None

    def ai_requests_today(self, provider: str | None = None) -> int:
        """Requests sent since local midnight (all services, or one)."""
        sql, args = "SELECT COUNT(*) FROM ai_usage WHERE ts >= ?", [local_midnight_utc()]
        if provider:
            sql, args = sql + " AND provider = ?", args + [provider]
        with self._read() as conn:
            return conn.execute(sql, args).fetchone()[0]

    def list_ai_calls(self, episode_id: str) -> list[dict]:
        return self._rows("SELECT * FROM ai_calls WHERE episode_id=? ORDER BY id", (episode_id,))

    # ---------------------------------------------------- blocked sites
    def block_site(self, site: str, *, reason: str, actor: str) -> str:
        """Never use this site (and its subdomains) as a source again. Returns the domain."""
        domain = clean_domain(site)
        if not domain or "." not in domain:
            raise StudioError(f"'{site}' is not a website address (e.g. example.com).")
        if not reason.strip():
            raise StudioError("Say why you block this site.")
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO blocked_sites VALUES (?,?,?,?)",
                         (domain, reason.strip(), actor, utc_now()))
        log.info("site blocked", extra={"ctx": {"domain": domain, "actor": actor}})
        return domain

    def unblock_site(self, site: str) -> bool:
        with self._tx() as conn:
            gone = conn.execute("DELETE FROM blocked_sites WHERE domain=?",
                                (clean_domain(site),)).rowcount
        return bool(gone)

    def blocked_sites(self) -> list[dict]:
        """[{domain, reason, blocked_by, blocked_at}], alphabetical."""
        return self._rows("SELECT * FROM blocked_sites ORDER BY domain")

    # ----------------------------------------------------------- backup
    def auto_backup(self, today: datetime | None = None) -> Path | None:
        """Once a day: copy the database (not the media; small and fast) to backups/auto and
        keep only the newest `auto_backup_keep` copies. Returns the new copy, or None."""
        keep = self.settings.auto_backup_keep
        if keep == 0 or not self.db_path.exists():
            return None
        folder = Path(self.settings.backup_dir) / "auto"
        target = folder / f"studio_{(today or datetime.now()):%Y%m%d}.db"
        if target.exists():
            return None
        folder.mkdir(parents=True, exist_ok=True)
        self._copy_db(target)
        for old in sorted(folder.glob("studio_*.db"))[:-keep]:
            old.unlink()
        log.info("daily backup created", extra={"ctx": {"path": str(target)}})
        return target

    def _copy_db(self, target: Path) -> None:
        """A consistent copy, safe even while the app is open (SQLite's backup API)."""
        src = sqlite3.connect(self.db_path)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    def backup(self, dest_dir: Path | None = None) -> Path:
        """Copy the database to a timestamped file (safe even while the app is open), and the
        media files (data/assets) to a folder with the same name next to it."""
        dest = Path(dest_dir or self.settings.backup_dir)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / f"studio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        self._copy_db(target)
        assets = Path(self.settings.data_dir) / "assets"
        if assets.exists():
            shutil.copytree(assets, target.with_name(target.stem + "_assets"),
                            ignore=shutil.ignore_patterns("_build"))
        log.info("backup created", extra={"ctx": {"path": str(target)}})
        return target
