"""SQLite persistence for incidents, actions, diagnoses and LLM spend.

One connection guarded by a lock. Every public method is synchronous and cheap;
callers that live on the event loop wrap them in `asyncio.to_thread`.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .redact import fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id                 TEXT PRIMARY KEY,
    container_id       TEXT NOT NULL,
    container_name     TEXT NOT NULL,
    image              TEXT NOT NULL DEFAULT '',
    kind               TEXT NOT NULL,
    fingerprint        TEXT NOT NULL,
    severity           TEXT NOT NULL DEFAULT 'warning',
    rule_severity      TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'open',
    summary            TEXT NOT NULL DEFAULT '',
    opened_at          TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    last_seen_at       TEXT,
    occurrences        INTEGER NOT NULL DEFAULT 1,
    resolved_at        TEXT,
    evidence_json      TEXT NOT NULL DEFAULT '{}',
    diagnosis_json     TEXT,
    mode_at_open       TEXT NOT NULL DEFAULT 'observe'
);
CREATE INDEX IF NOT EXISTS idx_incidents_open ON incidents(status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_fp ON incidents(fingerprint, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_container ON incidents(container_name, opened_at DESC);

-- Each sighting folded into an incident: when, and the lines it fired on.
-- The incident row keeps only the first evidence and a count; this is where
-- "which files did the 14:10 scan choke on" is answered. Capped per incident
-- (OCCURRENCE_HISTORY_LIMIT), since a condition can fire every poll for days.
CREATE TABLE IF NOT EXISTS incident_occurrences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    at          TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    lines_json  TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE INDEX IF NOT EXISTS idx_occurrences_incident ON incident_occurrences(incident_id, id DESC);

-- One row per path per bucket. Polls run every 30s; keeping them all would be
-- 2880 rows a day per path to answer a question about weeks.
CREATE TABLE IF NOT EXISTS disk_samples (
    path       TEXT NOT NULL,
    bucket     TEXT NOT NULL,
    at         TEXT NOT NULL,
    used_bytes INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    PRIMARY KEY (path, bucket)
);

-- The host's own figures, one row per bucket (5 minutes by default): the
-- average over the polls in it, and the peak CPU, so a spike between two
-- stored points still shows.
CREATE TABLE IF NOT EXISTS host_samples (
    bucket        TEXT PRIMARY KEY,
    at            TEXT NOT NULL,
    polls         INTEGER NOT NULL,
    cpu_pct       REAL,
    cpu_peak      REAL,
    iowait_pct    REAL,
    mem_used_pct  REAL,
    swap_used_pct REAL,
    load1         REAL,
    temp_c        REAL
);

-- Small facts that must survive a restart: the boot time last seen, so a
-- reboot of the host - which restarts the doctor too - is still noticed.
CREATE TABLE IF NOT EXISTS host_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Per container name, the Docker timestamp of the newest log line the rules
-- have read: kept so a restart of the doctor does not read the last hour of
-- every container's log as news.
CREATE TABLE IF NOT EXISTS log_marks (
    name       TEXT PRIMARY KEY,
    seen_until TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- The most members each md array has been seen with. DSM's system arrays
-- always show empty slots, so "a disk went missing" can only be answered
-- against what the array used to have - and that has to survive a restart,
-- or a disk lost while the doctor was down would become the new normal.
CREATE TABLE IF NOT EXISTS raid_members (
    array_name TEXT PRIMARY KEY,
    most       INTEGER NOT NULL,
    seen_at    TEXT NOT NULL
);

-- Each drive's corrected read errors, added up over its arrays, one reading
-- per hour. md's own count restarts at 0 whenever the arrays are assembled,
-- so this is what remembers a failing disk across a reboot - and what tells
-- a count that keeps rising from one that stopped.
CREATE TABLE IF NOT EXISTS drive_errors (
    disk   TEXT NOT NULL,
    bucket TEXT NOT NULL,
    at     TEXT NOT NULL,
    errors INTEGER NOT NULL,
    PRIMARY KEY (disk, bucket)
);

-- Each drive's temperature as SMART reports it, averaged per bucket (the
-- host chart's resolution). Keyed by serial where there is one, so a drive
-- moved to another bay keeps its history.
CREATE TABLE IF NOT EXISTS drive_temps (
    drive   TEXT NOT NULL,
    bucket  TEXT NOT NULL,
    at      TEXT NOT NULL,
    polls   INTEGER NOT NULL,
    celsius REAL NOT NULL,
    PRIMARY KEY (drive, bucket)
);

-- The dashboard briefing, one row per section plus "overall", each with the
-- fingerprint of the facts it was written from. Kept in SQLite so a restart
-- does not pay to rewrite words that are still true.
CREATE TABLE IF NOT EXISTS briefing (
    key          TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    level        TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id             TEXT PRIMARY KEY,
    incident_id    TEXT NOT NULL,
    container_name TEXT NOT NULL,
    action         TEXT NOT NULL,
    status         TEXT NOT NULL,
    decided_by     TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    result         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    executed_at    TEXT,
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE INDEX IF NOT EXISTS idx_actions_incident ON actions(incident_id);
CREATE INDEX IF NOT EXISTS idx_actions_recent ON actions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_actions_container ON actions(container_name, created_at DESC);

CREATE TABLE IF NOT EXISTS diagnosis_cache (
    fingerprint    TEXT PRIMARY KEY,
    diagnosis_json TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    hits           INTEGER NOT NULL DEFAULT 0
);

-- The last state seen per container name, so a change made while the doctor
-- was down is still caught - and a restart of the doctor itself is not taken
-- for thirty recreated containers.
CREATE TABLE IF NOT EXISTS container_state (
    name          TEXT PRIMARY KEY,
    container_id  TEXT NOT NULL,
    image         TEXT NOT NULL DEFAULT '',
    image_id      TEXT NOT NULL DEFAULT '',
    created       TEXT NOT NULL DEFAULT '',
    started_at    TEXT NOT NULL DEFAULT '',
    restart_count INTEGER NOT NULL DEFAULT 0,
    state         TEXT NOT NULL DEFAULT '',
    seen_at       TEXT NOT NULL,
    stack         TEXT NOT NULL DEFAULT '',
    service       TEXT NOT NULL DEFAULT '',
    config_json   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS container_changes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    container_name TEXT NOT NULL,
    stack          TEXT NOT NULL DEFAULT '',
    at             TEXT NOT NULL,
    kind           TEXT NOT NULL,
    summary        TEXT NOT NULL,
    detail_json    TEXT NOT NULL DEFAULT '{}',
    count          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_changes_container ON container_changes(container_name, at DESC);
CREATE INDEX IF NOT EXISTS idx_changes_recent ON container_changes(at DESC);

-- Memory per container per hour: the peak and the mean. The detail view's
-- minute-by-minute history lives in memory and forgets a restart; a limit is
-- worth suggesting only from weeks, peaks included.
CREATE TABLE IF NOT EXISTS mem_samples (
    name        TEXT NOT NULL,
    bucket      TEXT NOT NULL,
    max_bytes   INTEGER NOT NULL,
    sum_bytes   INTEGER NOT NULL,
    n           INTEGER NOT NULL,
    PRIMARY KEY (name, bucket)
);

-- Every request the doctor sent to a model, as it was sent: what the operator
-- would want to see before trusting a vendor with their logs. Kept for
-- DOCTOR_LLM_LOG_DAYS; the daily totals in llm_usage outlive it.
CREATE TABLE IF NOT EXISTS llm_calls (
    id            TEXT PRIMARY KEY,
    at            TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    ref           TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    endpoint      TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    latency_ms    INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0.0,
    request_json  TEXT NOT NULL DEFAULT '{}',
    response_text TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_recent ON llm_calls(at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_purpose ON llm_calls(purpose, at DESC);
-- Covers the spend chart's per-day sums, so it reads this small index instead
-- of every logged row - each one carrying its request body - from disk.
CREATE INDEX IF NOT EXISTS idx_llm_calls_spend
    ON llm_calls(at, purpose, cost_usd, input_tokens, output_tokens);

-- Every verdict the classifier gave, which doubles as its cache: the same
-- signal within DOCTOR_CLASSIFIER_CACHE_HOURS reuses the last verdict.
CREATE TABLE IF NOT EXISTS classifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT NOT NULL,
    cache_key      TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    container_name TEXT NOT NULL,
    kind           TEXT NOT NULL,
    rule_severity  TEXT NOT NULL,
    verdict_json   TEXT NOT NULL,
    action         TEXT NOT NULL,
    applied        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_classifications_key ON classifications(cache_key, at DESC);
CREATE INDEX IF NOT EXISTS idx_classifications_recent ON classifications(at DESC);

-- The classifier's own meter, so it and the diagnosis cannot eat each
-- other's daily allowance. Its spend is also added to llm_usage's dollars.
CREATE TABLE IF NOT EXISTS classifier_usage (
    day      TEXT PRIMARY KEY,
    calls    INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL    NOT NULL DEFAULT 0.0
);

-- Settings changed from the Settings page, by variable name: only the ones
-- someone changed, so a better default in a new release still reaches every
-- setting left alone. A variable set in the environment overrides its row.
CREATE TABLE IF NOT EXISTS settings (
    env        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);

-- Every change to `settings`, so a setting that behaves differently can be
-- traced to who changed it and when. An empty new_value is a reset.
CREATE TABLE IF NOT EXISTS setting_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    env        TEXT NOT NULL,
    old_value  TEXT NOT NULL DEFAULT '',
    new_value  TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL DEFAULT ''
);

-- One row per OIDC sign-in: the provider's refresh token, encrypted, and when
-- the access token it came with runs out. The cookie carries only the id, so
-- the token never reaches the browser. Losing a row ends that session.
CREATE TABLE IF NOT EXISTS oidc_sessions (
    id            TEXT PRIMARY KEY,
    subject       TEXT NOT NULL,
    refresh_token TEXT NOT NULL DEFAULT '',
    expires_at    REAL NOT NULL,
    retry_at      REAL NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    -- For the profile page: what the session is, and when it was last used.
    -- Times as Unix seconds; claims as the provider last described the user.
    renewed_at    REAL NOT NULL DEFAULT 0,
    last_seen_at  REAL NOT NULL DEFAULT 0,
    ip            TEXT NOT NULL DEFAULT '',
    user_agent    TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    platform      TEXT NOT NULL DEFAULT '',
    label         TEXT NOT NULL DEFAULT '',
    claims_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS llm_usage (
    day           TEXT PRIMARY KEY,
    calls         INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0.0
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Sightings kept per incident. The count on the incident is never capped;
# only the detail of the oldest sightings is let go.
OCCURRENCE_HISTORY_LIMIT = 200


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Store:
    """One writer connection behind a lock, and a read connection per thread.

    In WAL mode readers do not wait for the writer, but only on connections
    of their own: on the one shared connection every dashboard read queued
    behind every poll write - and a slow commit or a prune on a busy disk
    held them all, one after another. Every write commits before it lets go
    of the lock, so a read that follows it, on any thread, sees it.
    """

    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._local = threading.local()
        # Every read connection opened, to close them all with the store.
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        # An in-memory database is private to its connection: there is no
        # second connection that would see it, so reads share the writer's.
        self._shared_reads = path == ":memory:" or path.startswith("file::memory:")
        self._closed = False
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # In WAL mode NORMAL is still crash-safe; it skips the fsync on
            # every commit, which on a saturated disk held the lock - and
            # every request queued behind it - for seconds at a time.
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns to a database created by an earlier version.

        `CREATE TABLE IF NOT EXISTS` silently leaves an existing table alone,
        so new columns have to be added by hand or an upgrade breaks on a
        database that already has incidents in it.
        """
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(incidents)")}
        for column, ddl in (
            ("last_seen_at", "ALTER TABLE incidents ADD COLUMN last_seen_at TEXT"),
            ("occurrences", "ALTER TABLE incidents ADD COLUMN occurrences INTEGER NOT NULL DEFAULT 1"),
            ("rule_severity", "ALTER TABLE incidents ADD COLUMN rule_severity TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in have:
                self._conn.execute(ddl)
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(container_state)")}
        for column in ("stack", "service", "config_json"):
            if column not in have:
                self._conn.execute(
                    f"ALTER TABLE container_state ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(host_samples)")}
        if "temp_c" not in have:
            self._conn.execute("ALTER TABLE host_samples ADD COLUMN temp_c REAL")
        # NULL on rows from before it was kept: those fall back to total_bytes.
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(disk_samples)")}
        if "free_bytes" not in have:
            self._conn.execute("ALTER TABLE disk_samples ADD COLUMN free_bytes INTEGER")
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(oidc_sessions)")}
        for column, ddl in (
            ("renewed_at", "REAL NOT NULL DEFAULT 0"),
            ("last_seen_at", "REAL NOT NULL DEFAULT 0"),
            ("ip", "TEXT NOT NULL DEFAULT ''"),
            ("user_agent", "TEXT NOT NULL DEFAULT ''"),
            ("model", "TEXT NOT NULL DEFAULT ''"),
            ("platform", "TEXT NOT NULL DEFAULT ''"),
            ("label", "TEXT NOT NULL DEFAULT ''"),
            ("claims_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if column not in have:
                self._conn.execute(f"ALTER TABLE oidc_sessions ADD COLUMN {column} {ddl}")
        self._conn.execute(
            "UPDATE incidents SET last_seen_at = opened_at WHERE last_seen_at IS NULL"
        )
        # Backfill from `severity`: wrong where a diagnosis already revised it,
        # but it self-corrects on the next sighting and beats a blank.
        self._conn.execute(
            "UPDATE incidents SET rule_severity = severity WHERE rule_severity = ''"
        )
        self._rename_host()

    # The host's own incidents and changes were filed under host:nas.
    OLD_HOST_ID, HOST_ID = "host:nas", "host:system"

    def _rename_host(self) -> None:
        """host:nas -> host:system wherever a container name is kept. A host
        incident's fingerprint hashes the name, so it is worked out again for
        the ones the host rules raise - or the next sighting of an open one
        would open a second incident beside it."""
        old, new = self.OLD_HOST_ID, self.HOST_ID
        for row in self._conn.execute(
                "SELECT id, kind, fingerprint FROM incidents WHERE container_name = ?",
                (old,)).fetchall():
            # The host rules sign with their kind and nothing else (detector.check_host).
            if row["fingerprint"] == fingerprint(old, "", row["kind"], row["kind"]):
                self._conn.execute("UPDATE incidents SET fingerprint = ? WHERE id = ?",
                                   (fingerprint(new, "", row["kind"], row["kind"]), row["id"]))
        tables = [r["name"] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            columns = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if "container_name" in columns:
                self._conn.execute(
                    f"UPDATE {table} SET container_name = ? WHERE container_name = ?", (new, old))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._conn.close()
        with self._readers_lock:
            for conn in self._readers:
                conn.close()
            self._readers.clear()

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """A connection for reads only: this thread's own, never waiting on writes."""
        if self._shared_reads:
            with self._lock:
                yield self._conn
            return
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            # Opened in this thread and only used in it; close() may run in
            # another, hence check_same_thread off.
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA query_only=ON")
            with self._readers_lock:
                self._readers.append(conn)
            self._local.conn = conn
        yield conn

    # -- settings saved from the Settings page ------------------------------

    def saved_settings(self) -> dict[str, str]:
        with self._read() as conn:
            rows = conn.execute("SELECT env, value FROM settings").fetchall()
        return {row["env"]: row["value"] for row in rows}

    def save_setting(self, env: str, value: str, *, by: str = "") -> bool:
        """Save a setting's value as typed; empty resets it to its default.
        Records the change. False when nothing changed."""
        return bool(self.save_settings({env: value}, by=by))

    def save_settings(self, values: dict[str, str], *, by: str = "") -> list[str]:
        """Save several settings in one transaction - all of them, or, on
        any error, none. Empty resets. Returns the variables that changed."""
        now = utcnow()
        changed: list[str] = []
        with self._lock:
            try:
                for env, value in values.items():
                    row = self._conn.execute(
                        "SELECT value FROM settings WHERE env = ?", (env,)).fetchone()
                    old = row["value"] if row else ""
                    if old == value:
                        continue
                    if value:
                        self._conn.execute(
                            "INSERT INTO settings (env, value, updated_at, updated_by) "
                            "VALUES (?, ?, ?, ?) ON CONFLICT(env) DO UPDATE SET "
                            "value = excluded.value, updated_at = excluded.updated_at, "
                            "updated_by = excluded.updated_by", (env, value, now, by))
                    else:
                        self._conn.execute("DELETE FROM settings WHERE env = ?", (env,))
                    self._conn.execute(
                        "INSERT INTO setting_changes "
                        "(env, old_value, new_value, changed_at, changed_by) "
                        "VALUES (?, ?, ?, ?, ?)", (env, old, value, now, by))
                    changed.append(env)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return changed

    def setting_changes(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT env, old_value, new_value, changed_at, changed_by FROM setting_changes "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # -- incidents ----------------------------------------------------------

    def open_incident(
        self,
        *,
        container_id: str,
        container_name: str,
        image: str,
        kind: str,
        fingerprint: str,
        severity: str,
        summary: str,
        evidence: dict[str, Any],
        mode: str,
        rule_severity: str | None = None,
        lines: list[str] | None = None,
    ) -> str:
        """`rule_severity` is what the rule said, when a second opinion moved
        `severity`: recurrences and escalations are judged against the rule.
        `lines` are the log lines this sighting fired on, already redacted."""
        incident_id = new_id()
        now = utcnow()
        with self._lock:
            self._conn.execute(
                """INSERT INTO incidents
                   (id, container_id, container_name, image, kind, fingerprint, severity,
                    rule_severity, status, summary, opened_at, updated_at, last_seen_at,
                    evidence_json, mode_at_open)
                   VALUES (?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?)""",
                (
                    incident_id,
                    container_id,
                    container_name,
                    image,
                    kind,
                    fingerprint,
                    severity,
                    rule_severity or severity,
                    summary,
                    now,
                    now,
                    now,
                    json.dumps(evidence, default=str),
                    mode,
                ),
            )
            self._record_occurrence(incident_id, now, summary, lines)
            self._conn.commit()
        return incident_id

    def find_open_by_fingerprint(self, fingerprint: str, within_hours: int = 24) -> dict | None:
        """An unresolved incident for the same failure, still recently seen.

        Matched on the fingerprint rather than (container, kind) so that two
        different failures of one kind stay two incidents, and bounded by when
        it was last seen rather than when it opened - a condition that keeps
        recurring stays one incident for as long as it lasts, instead of
        turning into a new one every time the window rolls over.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat(timespec="seconds")
        with self._read() as conn:
            row = conn.execute(
                """SELECT * FROM incidents
                   WHERE fingerprint=? AND status NOT IN ('resolved','dismissed')
                     AND COALESCE(last_seen_at, opened_at) >= ?
                   ORDER BY opened_at DESC LIMIT 1""",
                (fingerprint, cutoff),
            ).fetchone()
        return dict(row) if row else None

    def touch_incident(
        self, incident_id: str, *, summary: str = "", lines: list[str] | None = None,
    ) -> int:
        """Record another sighting of an incident. Returns the new count."""
        now = utcnow()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE incidents
                   SET occurrences = occurrences + 1, last_seen_at = ?, updated_at = ?
                   WHERE id = ?""",
                (now, now, incident_id),
            )
            if cur.rowcount:
                self._record_occurrence(incident_id, now, summary, lines)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT occurrences FROM incidents WHERE id=?", (incident_id,)
            ).fetchone()
        return int(row["occurrences"]) if row else 0

    def keep_incident_open(self, incident_id: str) -> None:
        """Note that an incident's condition still holds, without counting a
        sighting: a disk whose error count has not moved is not news each poll,
        but it must not look silent enough to be folded away or auto-resolved."""
        with self._lock:
            self._conn.execute(
                "UPDATE incidents SET last_seen_at=? WHERE id=?", (utcnow(), incident_id))
            self._conn.commit()

    def was_dismissed(self, fingerprint: str, within_days: int = 30) -> bool:
        """Whether someone dismissed this failure lately."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat(timespec="seconds")
        with self._read() as conn:
            row = conn.execute(
                """SELECT 1 FROM incidents WHERE fingerprint=? AND status='dismissed'
                     AND updated_at >= ? LIMIT 1""",
                (fingerprint, cutoff),
            ).fetchone()
        return row is not None

    def open_incidents_of_kind(self, kind: str) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM incidents WHERE kind=? AND status NOT IN ('resolved','dismissed')",
                (kind,),
            ).fetchall()
        return [_hydrate_incident(dict(r)) for r in rows]

    def _record_occurrence(
        self, incident_id: str, at: str, summary: str, lines: list[str] | None,
    ) -> None:
        """Caller holds the lock and commits."""
        self._conn.execute(
            "INSERT INTO incident_occurrences (incident_id, at, summary, lines_json)"
            " VALUES (?,?,?,?)",
            (incident_id, at, summary or "", json.dumps([str(line) for line in lines or []])),
        )
        self._conn.execute(
            """DELETE FROM incident_occurrences
               WHERE incident_id = ? AND id NOT IN (
                   SELECT id FROM incident_occurrences WHERE incident_id = ?
                   ORDER BY id DESC LIMIT ?)""",
            (incident_id, incident_id, OCCURRENCE_HISTORY_LIMIT),
        )

    def list_occurrences(
        self, incident_id: str, *, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        """Sightings of one incident, newest first, and how many are kept.

        Incidents from before this table existed have no rows for their
        earlier sightings; `occurrences` on the incident still counts them.
        """
        with self._read() as conn:
            rows = conn.execute(
                """SELECT at, summary, lines_json FROM incident_occurrences
                   WHERE incident_id = ? ORDER BY id DESC LIMIT ? OFFSET ?""",
                (incident_id, limit, offset),
            ).fetchall()
            kept = conn.execute(
                "SELECT COUNT(*) FROM incident_occurrences WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()[0]
        out = []
        for row in rows:
            try:
                lines = json.loads(row["lines_json"] or "[]")
            except json.JSONDecodeError:
                lines = []
            out.append({"at": row["at"], "summary": row["summary"], "lines": lines})
        return {"occurrences": out, "kept": int(kept), "limit": OCCURRENCE_HISTORY_LIMIT}

    def set_incident_status(self, incident_id: str, status: str, *, resolved: bool = False) -> None:
        now = utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE incidents SET status=?, updated_at=?, resolved_at=? WHERE id=?",
                (status, now, now if resolved else None, incident_id),
            )
            self._conn.commit()

    def auto_resolve_stale(self, hours: int, exclude_kinds: tuple[str, ...] = ()) -> int:
        """Resolve incidents whose condition has not been seen for `hours`.

        A rule that is still true re-fires on every poll, which bumps
        last_seen_at - so silence for hours means the condition is over. Only
        safe for kinds that behave that way, hence `exclude_kinds`: host disk
        pressure suppresses its own re-alerts until usage climbs another 2%, so
        a quiet disk incident means the disk is *steadily* full, not fixed.

        Incidents with an action still awaiting approval are left alone: the
        decision is a human's to make, not a timeout's.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        now = utcnow()
        placeholders = ",".join("?" for _ in exclude_kinds)
        clauses = [
            "status NOT IN ('resolved','dismissed')",
            "COALESCE(last_seen_at, opened_at) < ?",
            "id NOT IN (SELECT incident_id FROM actions WHERE status='pending')",
        ]
        params: list[Any] = [now, now, cutoff]
        if exclude_kinds:
            clauses.append(f"kind NOT IN ({placeholders})")
            params.extend(exclude_kinds)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE incidents SET status='resolved', resolved_at=?, updated_at=? "
                f"WHERE {' AND '.join(clauses)}",
                params,
            )
            self._conn.commit()
        return cur.rowcount

    def dismiss_all(self, *, kind: str | None = None, container_name: str | None = None) -> int:
        """Dismiss every incident still open. Returns how many were closed.

        For clearing out a run of false alarms after the rule that raised them
        has been fixed - one at a time is not an option at a few hundred rows.
        Dismissing rather than deleting: the rows age out on their own at
        DOCTOR_RETENTION_DAYS, and a deleted incident takes its audit trail of
        actions with it.
        """
        clauses = ["status NOT IN ('resolved','dismissed')", "kind != ?"]
        params: list[Any] = [self.MANUAL]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if container_name:
            clauses.append("container_name = ?")
            params.append(container_name)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE incidents SET status='dismissed', updated_at=? "
                f"WHERE {' AND '.join(clauses)}",
                [utcnow(), *params],
            )
            self._conn.commit()
        return cur.rowcount

    def set_diagnosis(self, incident_id: str, diagnosis: dict[str, Any], severity: str | None = None) -> None:
        with self._lock:
            if severity:
                self._conn.execute(
                    "UPDATE incidents SET diagnosis_json=?, severity=?, updated_at=? WHERE id=?",
                    (json.dumps(diagnosis, default=str), severity, utcnow(), incident_id),
                )
            else:
                self._conn.execute(
                    "UPDATE incidents SET diagnosis_json=?, updated_at=? WHERE id=?",
                    (json.dumps(diagnosis, default=str), utcnow(), incident_id),
                )
            self._conn.commit()

    def get_incident(self, incident_id: str) -> dict | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not row:
            return None
        return _hydrate_incident(dict(row))

    ACTIVE = "active"
    CLOSED_STATUSES = ("resolved", "dismissed")
    # What an operator asked for from the dashboard. The row exists only
    # because every action hangs off an incident; nothing went wrong, so it is
    # never listed or counted as one - the action shows under Activity.
    MANUAL = "manual"

    @classmethod
    def _status_clause(cls, status: str | None) -> tuple[str, list[Any]]:
        """`active` is every status that still wants a human.

        Matching `open` exactly hid anything awaiting approval - which is the
        one thing you would least want filtered out of the list.
        """
        clause, params = " WHERE kind != ?", [cls.MANUAL]
        if not status:
            return clause, params
        if status == cls.ACTIVE:
            placeholders = ",".join("?" for _ in cls.CLOSED_STATUSES)
            return (f"{clause} AND status NOT IN ({placeholders})",
                    [*params, *cls.CLOSED_STATUSES])
        return f"{clause} AND status=?", [*params, status]

    def list_incidents(
        self, *, limit: int = 100, status: str | None = None, offset: int = 0,
        container_name: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM incidents"
        clause, params = self._status_clause(status)
        query += clause
        if container_name:
            query += " AND container_name=?"
            params.append(container_name)
        query += " ORDER BY opened_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_hydrate_incident(dict(r)) for r in rows]

    def count_incidents(self, status: str | None = None) -> int:
        clause, params = self._status_clause(status)
        with self._read() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM incidents{clause}", params
            ).fetchone()
        return int(row["n"])

    def record_disk_sample(
        self, path: str, used_bytes: int, total_bytes: int, *, free_bytes: int | None = None,
        bucket_minutes: int = 60,
    ) -> None:
        """Keep the latest reading in each time bucket."""
        now = datetime.now(timezone.utc)
        minutes = (now.hour * 60 + now.minute) // max(1, bucket_minutes)
        bucket = f"{now:%Y-%m-%d}T{minutes:04d}"
        with self._lock:
            self._conn.execute(
                """INSERT INTO disk_samples (path, bucket, at, used_bytes, total_bytes, free_bytes)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(path, bucket) DO UPDATE SET
                     at=excluded.at, used_bytes=excluded.used_bytes,
                     total_bytes=excluded.total_bytes, free_bytes=excluded.free_bytes""",
                (path, bucket, now.isoformat(timespec="seconds"), int(used_bytes),
                 int(total_bytes), None if free_bytes is None else int(free_bytes)),
            )
            self._conn.commit()

    def disk_samples(self, path: str, since: datetime) -> list[tuple[datetime, int]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT at, used_bytes FROM disk_samples WHERE path=? AND at >= ? "
                "ORDER BY at",
                (path, since.isoformat(timespec="seconds")),
            ).fetchall()
        out: list[tuple[datetime, int]] = []
        for row in rows:
            when = _parse_iso(row["at"])
            if when is not None:
                out.append((when, int(row["used_bytes"])))
        return out

    def raid_most_members(self, array_name: str, active: int) -> int:
        """Record a sighting and return the most members this array has had."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO raid_members (array_name, most, seen_at) VALUES (?,?,?)
                   ON CONFLICT(array_name) DO UPDATE SET
                     most=MAX(most, excluded.most), seen_at=excluded.seen_at""",
                (array_name, int(active), utcnow()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT most FROM raid_members WHERE array_name=?", (array_name,)
            ).fetchone()
        return int(row["most"]) if row else int(active)

    def drive_error_samples(self, disk: str, since: datetime) -> list[tuple[datetime, int]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT at, errors FROM drive_errors WHERE disk=? AND at >= ? ORDER BY at",
                (disk, since.isoformat(timespec="seconds")),
            ).fetchall()
        out: list[tuple[datetime, int]] = []
        for row in rows:
            when = _parse_iso(row["at"])
            if when is not None:
                out.append((when, int(row["errors"])))
        return out

    def record_drive_errors(self, disk: str, errors: int) -> None:
        """Keep the latest reading in each hour."""
        now = datetime.now(timezone.utc)
        with self._lock:
            self._conn.execute(
                """INSERT INTO drive_errors (disk, bucket, at, errors) VALUES (?,?,?,?)
                   ON CONFLICT(disk, bucket) DO UPDATE SET
                     at=excluded.at, errors=excluded.errors""",
                (disk, f"{now:%Y-%m-%dT%H}", now.isoformat(timespec="seconds"), int(errors)),
            )
            self._conn.commit()

    def record_host_sample(self, reading: dict[str, Any], *, bucket_minutes: int = 5) -> None:
        """Fold one reading into its bucket's running averages."""
        now = datetime.now(timezone.utc)
        bucket = _bucket(now, bucket_minutes)
        load = reading.get("load") or [None]
        sensors = reading.get("temperatures") or []
        values = {
            "cpu_pct": reading.get("cpu_pct"),
            "iowait_pct": reading.get("iowait_pct"),
            "mem_used_pct": reading.get("mem_used_pct"),
            "swap_used_pct": reading.get("swap_used_pct"),
            "load1": load[0],
            # The hottest sensor, as the tile shows it.
            "temp_c": max((t["celsius"] for t in sensors), default=None),
        }
        averaged = ", ".join(
            f"{k} = CASE WHEN excluded.{k} IS NULL THEN {k} WHEN {k} IS NULL THEN excluded.{k} "
            f"ELSE ({k} * polls + excluded.{k}) / (polls + 1) END" for k in values)
        with self._lock:
            self._conn.execute(
                f"""INSERT INTO host_samples (bucket, at, polls, cpu_peak, {", ".join(values)})
                    VALUES (?, ?, 1, ?, {", ".join("?" for _ in values)})
                    ON CONFLICT(bucket) DO UPDATE SET {averaged},
                      cpu_peak = MAX(COALESCE(cpu_peak, 0), COALESCE(excluded.cpu_peak, 0)),
                      at = excluded.at, polls = polls + 1""",
                (bucket, now.isoformat(timespec="seconds"), values["cpu_pct"], *values.values()),
            )
            self._conn.commit()

    def host_history(self, since: datetime) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT at, cpu_pct, cpu_peak, iowait_pct, mem_used_pct, swap_used_pct, load1, temp_c "
                "FROM host_samples WHERE at >= ? ORDER BY at",
                (since.isoformat(timespec="seconds"),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_drive_temps(self, temps: dict[str, float], *, bucket_minutes: int = 5) -> None:
        """Fold each drive's temperature into its bucket's running average."""
        if not temps:
            return
        now = datetime.now(timezone.utc)
        bucket, at = _bucket(now, bucket_minutes), now.isoformat(timespec="seconds")
        with self._lock:
            self._conn.executemany(
                """INSERT INTO drive_temps (drive, bucket, at, polls, celsius) VALUES (?,?,?,1,?)
                   ON CONFLICT(drive, bucket) DO UPDATE SET
                     celsius = (celsius * polls + excluded.celsius) / (polls + 1),
                     polls = polls + 1, at = excluded.at""",
                [(drive, bucket, at, float(c)) for drive, c in temps.items()],
            )
            self._conn.commit()

    def drive_temp_history(self, drive: str, since: datetime) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT at, celsius FROM drive_temps WHERE drive=? AND at >= ? ORDER BY at",
                (drive, since.isoformat(timespec="seconds")),
            ).fetchall()
        return [dict(row) for row in rows]

    def host_state(self, key: str) -> str | None:
        with self._read() as conn:
            row = conn.execute("SELECT value FROM host_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_host_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO host_state (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._conn.commit()

    def log_marks(self) -> dict[str, str]:
        with self._read() as conn:
            rows = conn.execute("SELECT name, seen_until FROM log_marks").fetchall()
        return {row["name"]: row["seen_until"] for row in rows}

    def save_log_marks(self, marks: dict[str, str]) -> None:
        """Keep the marks that moved; drop those of containers gone a month."""
        if not marks:
            return
        now = utcnow()
        month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        with self._lock:
            self._conn.executemany(
                "INSERT INTO log_marks (name, seen_until, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET seen_until=excluded.seen_until, "
                "updated_at=excluded.updated_at",
                [(name, mark, now) for name, mark in marks.items()])
            self._conn.execute("DELETE FROM log_marks WHERE updated_at < ?", (month_ago,))
            self._conn.commit()

    def briefing_entries(self) -> dict[str, dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM briefing").fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                content = json.loads(row["content_json"])
            except json.JSONDecodeError:
                continue
            out[row["key"]] = {"fingerprint": row["fingerprint"], "level": row["level"],
                               "content": content, "created_at": row["created_at"]}
        return out

    def save_briefing(self, key: str, fingerprint: str, level: str,
                      content: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO briefing (key, fingerprint, level, content_json, created_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET fingerprint=excluded.fingerprint,
                     level=excluded.level, content_json=excluded.content_json,
                     created_at=excluded.created_at""",
                (key, fingerprint, level, json.dumps(content), utcnow()),
            )
            self._conn.commit()

    def disk_history(self, path: str, since: datetime) -> list[dict[str, Any]]:
        """Samples for the dashboard's chart, oldest first."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT at, used_bytes, total_bytes, free_bytes FROM disk_samples "
                "WHERE path=? AND at >= ? ORDER BY at",
                (path, since.isoformat(timespec="seconds")),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_incidents_since(self, since: datetime) -> int:
        with self._read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM incidents WHERE opened_at >= ? AND kind != ?",
                (since.isoformat(timespec="seconds"), self.MANUAL),
            ).fetchone()
        return int(row["c"])

    # -- actions ------------------------------------------------------------

    def record_action(
        self,
        *,
        incident_id: str,
        container_name: str,
        action: str,
        status: str,
        decided_by: str,
        reason: str = "",
    ) -> str:
        action_id = new_id()
        with self._lock:
            self._conn.execute(
                """INSERT INTO actions
                   (id, incident_id, container_name, action, status, decided_by, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (action_id, incident_id, container_name, action, status, decided_by, reason, utcnow()),
            )
            self._conn.commit()
        return action_id

    def finish_action(self, action_id: str, status: str, result: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE actions SET status=?, result=?, executed_at=? WHERE id=?",
                (status, result[:2000], utcnow(), action_id),
            )
            self._conn.commit()

    def get_action(self, action_id: str) -> dict | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        return dict(row) if row else None

    def action_limit_reason(
        self, container_name: str, fingerprint: str, action: str, *,
        per_container_cap: int, global_cap: int, cooldown_minutes: int,
    ) -> str | None:
        with self._lock:
            return self._action_limit_reason(
                container_name, fingerprint, action,
                per_container_cap, global_cap, cooldown_minutes,
            )

    def claim_action(
        self, action_id: str, *, per_container_cap: int,
        global_cap: int, cooldown_minutes: int,
    ) -> str | None:
        """Check limits and reserve execution atomically; return a denial reason.

        BEGIN IMMEDIATE also serializes claims from separate Store connections.
        Executing reservations count against subsequent claims before Docker is
        called. A denied action stays pending with the current limit reason.
        """
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                """SELECT a.*, i.fingerprint FROM actions a
                   JOIN incidents i ON i.id=a.incident_id WHERE a.id=?""",
                (action_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return "action is no longer pending"
            reason = self._action_limit_reason(
                row["container_name"], row["fingerprint"], row["action"],
                per_container_cap, global_cap, cooldown_minutes,
            )
            if reason:
                self._conn.execute("UPDATE actions SET reason=? WHERE id=?", (reason, action_id))
            else:
                self._conn.execute(
                    "UPDATE actions SET status='executing', executed_at=? WHERE id=?",
                    (utcnow(), action_id),
                )
            return reason

    def _action_limit_reason(
        self, container_name: str, fingerprint: str, action: str,
        per_container_cap: int, global_cap: int, cooldown_minutes: int,
    ) -> str | None:
        """Caller holds the connection lock (and a transaction when claiming)."""
        now = datetime.now(timezone.utc)
        hour_ago = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        counts = self._conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(container_name=?), 0) AS container_total
               FROM actions WHERE created_at >= ?
                 AND status IN ('executing','succeeded','failed')""",
            (container_name, hour_ago),
        ).fetchone()
        if counts["container_total"] >= per_container_cap:
            return (
                f"circuit breaker: already ran {counts['container_total']} action(s) on "
                f"{container_name} in the last hour (cap {per_container_cap}). "
                "Repeated failure needs a human, not another restart."
            )
        if counts["total"] >= global_cap:
            return (
                f"circuit breaker: {counts['total']} actions across the host in the last "
                f"hour (cap {global_cap}). Something bigger is wrong."
            )
        cutoff = (now - timedelta(minutes=cooldown_minutes)).isoformat(timespec="seconds")
        if fingerprint and self._conn.execute(
            """SELECT 1 FROM actions a JOIN incidents i ON i.id=a.incident_id
               WHERE i.fingerprint=? AND a.action=? AND a.created_at >= ?
                 AND a.status IN ('executing','succeeded','failed') LIMIT 1""",
            (fingerprint, action, cutoff),
        ).fetchone():
            return (
                f"cooldown: '{action}' was already tried for this exact failure within "
                f"the last {cooldown_minutes} minutes and it came back"
            )
        return None

    def list_actions(
        self, *, limit: int = 100, incident_id: str | None = None, offset: int = 0,
        container_name: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM actions"
        params: list[Any] = []
        if incident_id:
            query += " WHERE incident_id=?"
            params.append(incident_id)
        elif container_name:
            query += " WHERE container_name=?"
            params.append(container_name)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def count_actions(self) -> int:
        with self._read() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM actions").fetchone()
        return int(row["n"])

    def actions_for_incidents(self, incident_ids: list[str]) -> dict[str, list[dict]]:
        """Every incident's actions in one query rather than one query each."""
        if not incident_ids:
            return {}
        grouped: dict[str, list[dict]] = {}
        with self._read() as conn:
            for chunk in range(0, len(incident_ids), 500):
                ids = incident_ids[chunk:chunk + 500]
                rows = conn.execute(
                    f"SELECT * FROM actions WHERE incident_id IN ({','.join('?' for _ in ids)}) "
                    f"ORDER BY created_at DESC",
                    ids,
                ).fetchall()
                for row in rows:
                    grouped.setdefault(row["incident_id"], []).append(dict(row))
        return grouped

    def count_executed_actions(self, *, since: datetime, container_name: str | None = None) -> int:
        """Count actions that actually touched Docker (for the circuit breaker)."""
        query = (
            "SELECT COUNT(*) AS c FROM actions "
            "WHERE created_at >= ? AND status IN ('executing','succeeded','failed')"
        )
        params: list[Any] = [since.isoformat(timespec="seconds")]
        if container_name:
            query += " AND container_name=?"
            params.append(container_name)
        with self._read() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["c"])

    def recent_action_for_fingerprint(self, fingerprint: str, action: str, since: datetime) -> dict | None:
        with self._read() as conn:
            row = conn.execute(
                """SELECT a.* FROM actions a
                   JOIN incidents i ON i.id = a.incident_id
                   WHERE i.fingerprint=? AND a.action=? AND a.created_at >= ?
                     AND a.status IN ('executing','succeeded','failed')
                   ORDER BY a.created_at DESC LIMIT 1""",
                (fingerprint, action, since.isoformat(timespec="seconds")),
            ).fetchone()
        return dict(row) if row else None

    def pending_actions(self) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM actions WHERE status='pending' ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # -- diagnosis cache ----------------------------------------------------

    def cached_diagnosis(self, fingerprint: str, max_age_hours: int) -> dict | None:
        if max_age_hours <= 0:
            return None  # DOCTOR_DIAGNOSIS_CACHE_HOURS=0 disables reuse entirely
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
        with self._lock:
            row = self._conn.execute(
                "SELECT diagnosis_json FROM diagnosis_cache WHERE fingerprint=? AND created_at >= ?",
                (fingerprint, cutoff),
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE diagnosis_cache SET hits = hits + 1 WHERE fingerprint=?", (fingerprint,)
                )
                self._conn.commit()
        if not row:
            return None
        try:
            return json.loads(row["diagnosis_json"])
        except json.JSONDecodeError:
            return None

    def cache_diagnosis(self, fingerprint: str, diagnosis: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO diagnosis_cache (fingerprint, diagnosis_json, created_at)
                   VALUES (?,?,?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                     diagnosis_json=excluded.diagnosis_json, created_at=excluded.created_at""",
                (fingerprint, json.dumps(diagnosis, default=str), utcnow()),
            )
            self._conn.commit()

    # -- spend --------------------------------------------------------------

    def reserve_call(self, daily_call_limit: int) -> bool:
        """Atomically claim one call against today's quota.

        Claiming the slot *before* the request is what makes the cap exact:
        incidents are processed concurrently, so a check-then-call would let
        several requests through on the same reading. A reserved slot is not
        returned if the request then fails - most vendors count a failed
        request against the quota too, and over-counting is the safe direction.
        """
        day = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT calls FROM llm_usage WHERE day=?", (day,)
            ).fetchone()
            used = int(row["calls"]) if row else 0
            if daily_call_limit > 0 and used >= daily_call_limit:
                return False
            self._conn.execute(
                """INSERT INTO llm_usage (day, calls) VALUES (?, 1)
                   ON CONFLICT(day) DO UPDATE SET calls = calls + 1""",
                (day,),
            )
            self._conn.commit()
        return True

    def release_call(self) -> None:
        """Give a reserved slot back - used when the call never left the host."""
        day = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE llm_usage SET calls = MAX(calls - 1, 0) WHERE day=?", (day,)
            )
            self._conn.commit()

    def add_usage(
        self, *, input_tokens: int, output_tokens: int, cached_tokens: int, cost_usd: float
    ) -> None:
        """Record the tokens and cost of a call already reserved by reserve_call."""
        day = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO llm_usage (day, input_tokens, output_tokens, cached_tokens, cost_usd)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(day) DO UPDATE SET
                     input_tokens = input_tokens + excluded.input_tokens,
                     output_tokens = output_tokens + excluded.output_tokens,
                     cached_tokens = cached_tokens + excluded.cached_tokens,
                     cost_usd = cost_usd + excluded.cost_usd""",
                (day, input_tokens, output_tokens, cached_tokens, cost_usd),
            )
            self._conn.commit()

    def usage_today(self) -> dict[str, Any]:
        day = datetime.now(timezone.utc).date().isoformat()
        with self._read() as conn:
            row = conn.execute("SELECT * FROM llm_usage WHERE day=?", (day,)).fetchone()
        if not row:
            return {"day": day, "calls": 0, "input_tokens": 0, "output_tokens": 0,
                    "cached_tokens": 0, "cost_usd": 0.0}
        return dict(row)

    def usage_history(self, days: int = 14) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_usage ORDER BY day DESC LIMIT ?", (days,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- what changed -------------------------------------------------------

    def container_states(self) -> dict[str, dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM container_state").fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            try:
                item["config"] = json.loads(item.pop("config_json") or "null")
            except json.JSONDecodeError:
                item["config"] = None
            out[row["name"]] = item
        return out

    def save_container_states(self, states: dict[str, dict[str, Any]], gone: list[str]) -> None:
        """Replace the stored state of every container seen this poll."""
        now = utcnow()
        with self._lock:
            self._conn.executemany(
                """INSERT INTO container_state
                     (name, container_id, image, image_id, created, started_at,
                      restart_count, state, seen_at, stack, service, config_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     container_id=excluded.container_id, image=excluded.image,
                     image_id=excluded.image_id, created=excluded.created,
                     started_at=excluded.started_at, restart_count=excluded.restart_count,
                     state=excluded.state, seen_at=excluded.seen_at,
                     stack=excluded.stack, service=excluded.service,
                     config_json=excluded.config_json""",
                [(name, s["container_id"], s["image"], s["image_id"], s["created"],
                  s["started_at"], s["restart_count"], s["state"], now,
                  s.get("stack", ""), s.get("service", ""),
                  json.dumps(s["config"], sort_keys=True) if s.get("config") else "")
                 for name, s in states.items()],
            )
            self._conn.executemany("DELETE FROM container_state WHERE name=?",
                                   [(name,) for name in gone])
            self._conn.commit()

    def record_change(self, container_name: str, stack: str, kind: str, at: str,
                      summary: str, detail: dict[str, Any], *,
                      fold_minutes: int = 0) -> None:
        """Write one change - or fold it into the last one of the same kind.

        A crash loop restarts a container every few seconds; forty identical
        rows would bury the one change that explains it. Within `fold_minutes`
        a repeat of the same kind counts up the last row instead.
        """
        at = at or utcnow()
        with self._lock:
            if fold_minutes > 0:
                cutoff = (_parse_iso(at) or datetime.now(timezone.utc)) - timedelta(minutes=fold_minutes)
                row = self._conn.execute(
                    """SELECT id, at FROM container_changes
                       WHERE container_name=? AND kind=? ORDER BY at DESC LIMIT 1""",
                    (container_name, kind),
                ).fetchone()
                last = _parse_iso(row["at"]) if row else None
                if row and last and last >= cutoff:
                    self._conn.execute(
                        """UPDATE container_changes SET count = count + 1, at = ?,
                             summary = ?, detail_json = ? WHERE id = ?""",
                        (at, summary, json.dumps(detail, default=str), row["id"]),
                    )
                    self._conn.commit()
                    return
            self._conn.execute(
                """INSERT INTO container_changes
                     (container_name, stack, at, kind, summary, detail_json)
                   VALUES (?,?,?,?,?,?)""",
                (container_name, stack, at, kind, summary, json.dumps(detail, default=str)),
            )
            self._conn.commit()

    def list_changes(self, *, container_name: str | None = None, since: datetime | None = None,
                     limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        clauses, params = [], []
        if container_name:
            clauses.append("container_name = ?")
            params.append(container_name)
        if since is not None:
            clauses.append("at >= ?")
            params.append(since.isoformat(timespec="seconds"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM container_changes {where} ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json") or "{}")
            except json.JSONDecodeError:
                item["detail"] = {}
            out.append(item)
        return out

    def count_changes(self) -> int:
        with self._read() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM container_changes").fetchone()[0])

    # -- memory per hour ----------------------------------------------------

    def record_mem_samples(self, samples: list[tuple[str, int]]) -> None:
        """One reading per container into its hour: the peak and a running mean."""
        if not samples:
            return
        bucket = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00+00:00")
        with self._lock:
            self._conn.executemany(
                """INSERT INTO mem_samples (name, bucket, max_bytes, sum_bytes, n)
                   VALUES (?,?,?,?,1)
                   ON CONFLICT(name, bucket) DO UPDATE SET
                     max_bytes = MAX(max_bytes, excluded.max_bytes),
                     sum_bytes = sum_bytes + excluded.sum_bytes,
                     n = n + 1""",
                [(name, bucket, used, used) for name, used in samples],
            )
            self._conn.commit()

    def mem_history(self, since: datetime, name: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Every container's hourly peaks and means since `since`, oldest first."""
        clause, params = ("AND name = ?", [name]) if name else ("", [])
        with self._read() as conn:
            rows = conn.execute(
                f"""SELECT name, bucket, max_bytes, sum_bytes, n FROM mem_samples
                    WHERE bucket >= ? {clause} ORDER BY name, bucket""",
                (since.isoformat(timespec="seconds"), *params),
            ).fetchall()
        out: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(row["name"], []).append({
                "bucket": row["bucket"], "max": int(row["max_bytes"]),
                "mean": int(row["sum_bytes"] / row["n"]) if row["n"] else 0,
            })
        return out

    # -- the classifier -----------------------------------------------------

    def reserve_classifier_call(self, daily_limit: int) -> bool:
        """Claim one classifier call against today's cap, before making it."""
        day = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT calls FROM classifier_usage WHERE day=?", (day,)).fetchone()
            if daily_limit > 0 and row and int(row["calls"]) >= daily_limit:
                return False
            self._conn.execute(
                """INSERT INTO classifier_usage (day, calls) VALUES (?, 1)
                   ON CONFLICT(day) DO UPDATE SET calls = calls + 1""", (day,))
            self._conn.commit()
        return True

    def add_classifier_usage(self, cost_usd: float, input_tokens: int, output_tokens: int) -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO classifier_usage (day, cost_usd) VALUES (?, ?)
                   ON CONFLICT(day) DO UPDATE SET cost_usd = cost_usd + excluded.cost_usd""",
                (day, cost_usd))
            self._conn.commit()
        # The dollars count toward the day's spend like any other call; the
        # call itself does not count against the diagnosis's call cap.
        self.add_usage(input_tokens=input_tokens, output_tokens=output_tokens,
                       cached_tokens=0, cost_usd=cost_usd)

    def classifier_usage_today(self) -> dict[str, Any]:
        day = datetime.now(timezone.utc).date().isoformat()
        with self._read() as conn:
            row = conn.execute("SELECT * FROM classifier_usage WHERE day=?", (day,)).fetchone()
        return dict(row) if row else {"day": day, "calls": 0, "cost_usd": 0.0}

    def record_classification(self, key: str, fingerprint: str, container_name: str,
                              kind: str, rule_severity: str, verdict: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO classifications
                     (at, cache_key, fingerprint, container_name, kind, rule_severity,
                      verdict_json, action, applied)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (utcnow(), key, fingerprint, container_name, kind, rule_severity,
                 json.dumps(verdict, default=str), verdict.get("action", "open"),
                 1 if verdict.get("applied") else 0))
            self._conn.commit()

    def cached_classification(self, key: str, max_age_hours: int) -> dict[str, Any] | None:
        if max_age_hours <= 0:
            return None
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
        with self._read() as conn:
            row = conn.execute(
                """SELECT verdict_json FROM classifications
                   WHERE cache_key=? AND at >= ? ORDER BY at DESC LIMIT 1""",
                (key, cutoff)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["verdict_json"])
        except json.JSONDecodeError:
            return None

    def list_classifications(self, *, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM classifications ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["verdict"] = json.loads(item.pop("verdict_json") or "{}")
            except json.JSONDecodeError:
                item["verdict"] = {}
            out.append(item)
        return out

    def classification_counts(self, since: datetime) -> dict[str, Any]:
        """How often each action was reached since `since`, for the summary."""
        with self._read() as conn:
            rows = conn.execute(
                """SELECT action, applied, COUNT(*) AS n FROM classifications
                   WHERE at >= ? GROUP BY action, applied""",
                (since.isoformat(timespec="seconds"),)).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
        counts: dict[str, int] = {}
        applied = 0
        for row in rows:
            counts[row["action"]] = counts.get(row["action"], 0) + int(row["n"])
            applied += int(row["n"]) if row["applied"] else 0
        return {"by_action": counts, "applied": applied, "total": int(total)}

    # -- the call log -------------------------------------------------------

    LLM_CALL_COLUMNS = ("id", "at", "purpose", "ref", "provider", "model", "endpoint",
                        "status", "latency_ms", "input_tokens", "output_tokens",
                        "cached_tokens", "cost_usd", "error")

    def record_llm_call(self, entry: dict[str, Any]) -> str:
        call_id = new_id()
        with self._lock:
            self._conn.execute(
                """INSERT INTO llm_calls
                     (id, at, purpose, ref, provider, model, endpoint, status, latency_ms,
                      input_tokens, output_tokens, cached_tokens, cost_usd,
                      request_json, response_text, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (call_id, entry.get("at") or utcnow(), entry.get("purpose", ""),
                 entry.get("ref", ""), entry.get("provider", ""), entry.get("model", ""),
                 entry.get("endpoint", ""), entry.get("status", "ok"),
                 int(entry.get("latency_ms") or 0), int(entry.get("input_tokens") or 0),
                 int(entry.get("output_tokens") or 0), int(entry.get("cached_tokens") or 0),
                 float(entry.get("cost_usd") or 0.0),
                 json.dumps(entry.get("request") or {}, default=str),
                 str(entry.get("response") or ""), str(entry.get("error") or "")[:4000]),
            )
            self._conn.commit()
        return call_id

    @staticmethod
    def _purpose_clause(purpose: str | None) -> tuple[str, list[Any]]:
        """`briefing` matches every briefing_* purpose; anything else exactly."""
        if not purpose:
            return "", []
        return "WHERE purpose = ? OR purpose LIKE ? ESCAPE '!'", [purpose, f"{purpose}!_%"]

    def list_llm_calls(self, *, limit: int = 50, offset: int = 0,
                       purpose: str | None = None) -> list[dict[str, Any]]:
        """The log without the bodies - those are one call's worth each."""
        where, params = self._purpose_clause(purpose)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(self.LLM_CALL_COLUMNS)} FROM llm_calls {where} "
                "ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_llm_calls(self, purpose: str | None = None) -> int:
        where, params = self._purpose_clause(purpose)
        with self._read() as conn:
            return int(conn.execute(
                f"SELECT COUNT(*) FROM llm_calls {where}", params).fetchone()[0])

    def get_llm_call(self, call_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM llm_calls WHERE id=?", (call_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["request"] = json.loads(item.pop("request_json") or "{}")
        except json.JSONDecodeError:
            item["request"] = {}
        return item

    def llm_spend_by_purpose(self, since: datetime) -> list[dict[str, Any]]:
        """Per day and purpose, from the log: what the daily totals are made of."""
        with self._read() as conn:
            rows = conn.execute(
                """SELECT substr(at, 1, 10) AS day, purpose, COUNT(*) AS calls,
                          SUM(cost_usd) AS cost_usd, SUM(input_tokens) AS input_tokens,
                          SUM(output_tokens) AS output_tokens
                   FROM llm_calls WHERE at >= ? GROUP BY day, purpose ORDER BY day""",
                (since.isoformat(timespec="seconds"),),
            ).fetchall()
        return [dict(r) for r in rows]

    def usage_since(self, since: datetime) -> list[dict[str, Any]]:
        """Daily totals from `since` on, oldest first."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_usage WHERE day >= ? ORDER BY day",
                (since.date().isoformat(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def prune_llm_calls(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute("DELETE FROM llm_calls WHERE at < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount or 0

    # -- OIDC sessions ------------------------------------------------------

    def save_oidc_session(self, session_id: str, subject: str, refresh_token: str,
                          expires_at: float, *, ip: str = "", user_agent: str = "",
                          claims: dict[str, Any] | None = None) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO oidc_sessions (id, subject, refresh_token, expires_at, created_at,"
                " renewed_at, last_seen_at, ip, user_agent, claims_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, subject, refresh_token, expires_at, utcnow(), now, now,
                 ip, user_agent, json.dumps(claims or {})),
            )
            self._conn.commit()

    def oidc_session(self, session_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM oidc_sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def oidc_sessions_of(self, subject: str) -> list[dict[str, Any]]:
        """Every session one person has, most recently used first."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM oidc_sessions WHERE subject = ? ORDER BY last_seen_at DESC",
                (subject,)).fetchall()
        return [dict(row) for row in rows]

    def renew_oidc_session(self, session_id: str, refresh_token: str, expires_at: float,
                           claims: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE oidc_sessions SET refresh_token = ?, expires_at = ?, retry_at = 0,"
                " renewed_at = ? WHERE id = ?",
                (refresh_token, expires_at, time.time(), session_id),
            )
            if claims:
                self._conn.execute(
                    "UPDATE oidc_sessions SET claims_json = ? WHERE id = ?",
                    (json.dumps(claims), session_id))
            self._conn.commit()

    def defer_oidc_session(self, session_id: str, retry_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE oidc_sessions SET retry_at = ? WHERE id = ?", (retry_at, session_id))
            self._conn.commit()

    def touch_oidc_session(self, session_id: str, ip: str, user_agent: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE oidc_sessions SET last_seen_at = ?, ip = ?,"
                " user_agent = CASE WHEN ? = '' THEN user_agent ELSE ? END WHERE id = ?",
                (time.time(), ip, user_agent, user_agent, session_id))
            self._conn.commit()

    def describe_oidc_session(self, session_id: str, *, model: str | None = None,
                              platform: str | None = None, label: str | None = None) -> None:
        """Set what the page reported about the device, or the name typed for it."""
        with self._lock:
            for column, value in (("model", model), ("platform", platform), ("label", label)):
                if value is not None:
                    self._conn.execute(
                        f"UPDATE oidc_sessions SET {column} = ? WHERE id = ?", (value, session_id))
            self._conn.commit()

    def delete_oidc_session(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM oidc_sessions WHERE id = ?", (session_id,))
            self._conn.commit()

    def prune_oidc_sessions(self, before: float) -> int:
        """Drop sessions nobody has used since `before`: their cookie has run out."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM oidc_sessions WHERE MAX(last_seen_at, expires_at) < ?", (before,))
            self._conn.commit()
        return cur.rowcount or 0

    # -- housekeeping -------------------------------------------------------

    def prune(self, retention_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM actions WHERE incident_id IN (SELECT id FROM incidents WHERE opened_at < ?)",
                (cutoff,),
            )
            removed = cur.rowcount or 0
            self._conn.execute(
                "DELETE FROM incident_occurrences WHERE incident_id IN"
                " (SELECT id FROM incidents WHERE opened_at < ?)",
                (cutoff,),
            )
            cur = self._conn.execute("DELETE FROM incidents WHERE opened_at < ?", (cutoff,))
            removed += cur.rowcount or 0
            self._conn.execute("DELETE FROM diagnosis_cache WHERE created_at < ?", (cutoff,))
            self._conn.execute("DELETE FROM disk_samples WHERE at < ?", (cutoff,))
            self._conn.execute("DELETE FROM host_samples WHERE at < ?", (cutoff,))
            self._conn.execute("DELETE FROM drive_errors WHERE at < ?", (cutoff,))
            self._conn.execute("DELETE FROM drive_temps WHERE at < ?", (cutoff,))
            self._conn.execute("DELETE FROM container_changes WHERE at < ?", (cutoff,))
            self._conn.execute("DELETE FROM mem_samples WHERE bucket < ?", (cutoff,))
            self._conn.execute("DELETE FROM classifications WHERE at < ?", (cutoff,))
            self._conn.commit()
        return removed


def _bucket(now: datetime, minutes: int) -> str:
    """The start of the `minutes`-long slot of the day `now` falls in."""
    step = max(1, minutes)
    start = (now.hour * 60 + now.minute) // step * step
    return f"{now:%Y-%m-%d}T{start // 60:02d}:{start % 60:02d}"


def _parse_iso(value: str | None) -> datetime | None:
    """Timestamps are written by us, but a hand-edited row should not crash a poll."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _hydrate_incident(row: dict) -> dict:
    for key, target in (("evidence_json", "evidence"), ("diagnosis_json", "diagnosis")):
        raw = row.pop(key, None)
        try:
            row[target] = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            row[target] = None
    return row
