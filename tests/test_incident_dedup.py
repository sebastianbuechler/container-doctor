"""One ongoing condition is one incident.

A disk that stays full re-fires the rule on every poll. Opening a fresh
incident each time buries the dashboard in identical rows - and, before the
diagnosis cache catches it, pays for the same answer twice. It should instead
count the sightings against the incident that is already open.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.store import Store  # noqa: E402


def make_store() -> Store:
    return Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))


def open_disk_incident(store: Store, fingerprint: str = "fp-disk") -> str:
    return store.open_incident(
        container_id="", container_name="host:/data", image="", kind="host_disk_pressure",
        fingerprint=fingerprint, severity="warning",
        summary="Host path /data is 92% full", evidence={"used_pct": 92.0}, mode="observe",
    )


class Deduplication(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)

    def test_a_fresh_incident_starts_at_one(self):
        incident = self.store.get_incident(open_disk_incident(self.store))
        self.assertEqual(incident["occurrences"], 1)
        self.assertEqual(incident["last_seen_at"], incident["opened_at"])

    def test_the_same_failure_is_found_and_counted(self):
        incident_id = open_disk_incident(self.store)
        found = self.store.find_open_by_fingerprint("fp-disk")
        self.assertEqual(found["id"], incident_id)
        self.assertEqual(self.store.touch_incident(incident_id), 2)
        self.assertEqual(self.store.touch_incident(incident_id), 3)
        self.assertEqual(self.store.get_incident(incident_id)["occurrences"], 3)

    def test_a_different_failure_is_not_folded_in(self):
        open_disk_incident(self.store, "fp-disk")
        self.assertIsNone(self.store.find_open_by_fingerprint("fp-oom"))

    def test_a_dismissed_incident_does_not_absorb_the_next_one(self):
        incident_id = open_disk_incident(self.store)
        self.store.set_incident_status(incident_id, "dismissed")
        self.assertIsNone(self.store.find_open_by_fingerprint("fp-disk"))

    def test_an_incident_last_seen_long_ago_does_not_absorb_it(self):
        incident_id = open_disk_incident(self.store)
        stale = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
        self.store._conn.execute(
            "UPDATE incidents SET last_seen_at=?, opened_at=? WHERE id=?",
            (stale, stale, incident_id),
        )
        self.store._conn.commit()
        self.assertIsNone(self.store.find_open_by_fingerprint("fp-disk", within_hours=24))

    def test_a_condition_that_keeps_firing_stays_one_incident(self):
        """Sightings keep it current, so the window never rolls over on it."""
        incident_id = open_disk_incident(self.store)
        opened = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        self.store._conn.execute(
            "UPDATE incidents SET opened_at=? WHERE id=?", (opened, incident_id)
        )
        self.store._conn.commit()
        self.store.touch_incident(incident_id)
        self.assertEqual(self.store.find_open_by_fingerprint("fp-disk")["id"], incident_id)


class ClearingOutFalseAlarms(unittest.TestCase):
    """A fixed rule leaves its incidents behind - 239 of them, in practice."""

    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)

    def open_one(self, kind="log_fatal", name="stirling-pdf", fingerprint="fp"):
        return self.store.open_incident(
            container_id="c", container_name=name, image="img", kind=kind,
            fingerprint=fingerprint, severity="critical", summary="boom",
            evidence={}, mode="observe",
        )

    def test_dismisses_everything_still_open(self):
        ids = [self.open_one(fingerprint=f"fp{n}") for n in range(5)]
        self.assertEqual(self.store.dismiss_all(), 5)
        for incident_id in ids:
            self.assertEqual(self.store.get_incident(incident_id)["status"], "dismissed")

    def test_leaves_resolved_and_dismissed_alone(self):
        already = self.open_one(fingerprint="fp1")
        self.store.set_incident_status(already, "resolved", resolved=True)
        self.open_one(fingerprint="fp2")
        self.assertEqual(self.store.dismiss_all(), 1)
        self.assertEqual(self.store.get_incident(already)["status"], "resolved")

    def test_can_be_narrowed_to_one_kind(self):
        self.open_one(kind="log_fatal", fingerprint="fp1")
        other = self.open_one(kind="host_disk_pressure", fingerprint="fp2")
        self.assertEqual(self.store.dismiss_all(kind="log_fatal"), 1)
        self.assertEqual(self.store.get_incident(other)["status"], "open")

    def test_can_be_narrowed_to_one_container(self):
        self.open_one(name="stirling-pdf", fingerprint="fp1")
        other = self.open_one(name="Docmost-DB", fingerprint="fp2")
        self.assertEqual(self.store.dismiss_all(container_name="stirling-pdf"), 1)
        self.assertEqual(self.store.get_incident(other)["status"], "open")

    def test_nothing_open_is_not_an_error(self):
        self.assertEqual(self.store.dismiss_all(), 0)


class UpgradingAnExistingDatabase(unittest.TestCase):
    """`CREATE TABLE IF NOT EXISTS` will not add a column to a live database."""

    OLD_SCHEMA = """
    CREATE TABLE incidents (
        id TEXT PRIMARY KEY, container_id TEXT NOT NULL, container_name TEXT NOT NULL,
        image TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'warning', status TEXT NOT NULL DEFAULT 'open',
        summary TEXT NOT NULL DEFAULT '', opened_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        resolved_at TEXT, evidence_json TEXT NOT NULL DEFAULT '{}', diagnosis_json TEXT,
        mode_at_open TEXT NOT NULL DEFAULT 'observe'
    );
    """

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "doctor.db")
        old = sqlite3.connect(self.path)
        old.executescript(self.OLD_SCHEMA)
        old.execute(
            """INSERT INTO incidents
               (id, container_id, container_name, kind, fingerprint, summary,
                opened_at, updated_at)
               VALUES ('old-1','','host:/data','host_disk_pressure','fp-disk',
                       '/data is 92% full','2026-09-24T15:00:00','2026-09-24T15:00:00')"""
        )
        old.commit()
        old.close()

    def test_the_new_columns_are_added_and_backfilled(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        incident = store.get_incident("old-1")
        self.assertEqual(incident["occurrences"], 1)
        self.assertEqual(incident["last_seen_at"], "2026-09-24T15:00:00")
        self.assertEqual(incident["rule_severity"], incident["severity"])

    def test_upgrading_from_the_version_in_between(self):
        """The deployed database already has last_seen_at and occurrences."""
        store = Store(self.path)
        store.close()  # first upgrade: adds all three columns
        conn = sqlite3.connect(self.path)
        conn.execute("ALTER TABLE incidents RENAME TO incidents_new")
        conn.execute("""CREATE TABLE incidents AS
                        SELECT id, container_id, container_name, image, kind, fingerprint,
                               severity, status, summary, opened_at, updated_at, last_seen_at,
                               occurrences, resolved_at, evidence_json, diagnosis_json,
                               mode_at_open
                        FROM incidents_new""")
        conn.execute("DROP TABLE incidents_new")
        conn.commit()
        conn.close()

        store = Store(self.path)
        self.addCleanup(store.close)
        incident = store.get_incident("old-1")
        self.assertEqual(incident["rule_severity"], incident["severity"])
        self.assertEqual(incident["occurrences"], 1)

    def test_an_incident_from_before_the_upgrade_still_deduplicates(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        store._conn.execute(
            "UPDATE incidents SET last_seen_at=? WHERE id='old-1'",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
        )
        store._conn.commit()
        self.assertEqual(store.find_open_by_fingerprint("fp-disk")["id"], "old-1")
        self.assertEqual(store.touch_incident("old-1"), 2)

    def test_opening_the_store_twice_is_safe(self):
        Store(self.path).close()
        store = Store(self.path)
        self.addCleanup(store.close)
        self.assertEqual(store.get_incident("old-1")["occurrences"], 1)


class ThroughTheSupervisor(unittest.TestCase):
    """The rule re-fires on every poll while the disk stays full."""

    @classmethod
    def setUpClass(cls):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class FakeDaemon(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = {"Version": "27.0", "ApiVersion": "1.45"} \
                    if self.path.startswith("/version") else []
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        cls.server = HTTPServer(("127.0.0.1", 0), FakeDaemon)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def supervisor(self):
        from app.config import Settings
        from app.supervisor import Supervisor

        previous = dict(os.environ)
        os.environ.update(
            DOCTOR_DATA_DIR=tempfile.mkdtemp(),
            DOCTOR_MODE="observe",
            ANTHROPIC_API_KEY="",
            DOCKER_HOST=f"tcp://127.0.0.1:{self.port}",
        )
        try:
            supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(supervisor.store.close)
        return supervisor

    @staticmethod
    def disk_signal(pct: float = 92.0):
        from app.detector import Signal

        return Signal(
            kind="host_disk_pressure", severity="warning",
            summary=f"Host path /data is {pct:.0f}% full (2.8 TiB free)",
            container={"id": "", "name": "host:/data", "image": "", "labels": {}},
            details={"path": "/data", "used_pct": pct}, signature_parts=["/data"],
        )

    def test_a_worsening_condition_opens_its_own_incident(self):
        """92% is a warning; 96% is critical and must not be folded away."""
        import asyncio

        supervisor = self.supervisor()
        warning = self.disk_signal(92.0)
        critical = self.disk_signal(92.0)
        critical.severity = "critical"

        async def run():
            await supervisor._process_signal_inner(warning)
            await supervisor._process_signal_inner(critical)

        asyncio.run(run())
        rows = supervisor.store.list_incidents(limit=50)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["severity"] for r in rows}, {"warning", "critical"})

    def test_a_less_severe_repeat_is_still_folded_in(self):
        import asyncio

        supervisor = self.supervisor()
        critical = self.disk_signal(92.0)
        critical.severity = "critical"

        async def run():
            await supervisor._process_signal_inner(critical)
            await supervisor._process_signal_inner(self.disk_signal(92.0))

        asyncio.run(run())
        rows = supervisor.store.list_incidents(limit=50)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurrences"], 2)

    @staticmethod
    def drive_signal(errors: int = 40, severity: str = "warning", grew: bool = True):
        from app.detector import drive_signal

        return drive_signal({
            "disk": "sata7", "label": "Drive 7", "errors": errors,
            "by_array": {"md2": errors}, "severity": severity, "grew": grew,
            "summary": f"Drive 7 has had {errors} read errors corrected in md2",
        })

    def test_a_drive_count_that_did_not_move_is_not_a_sighting(self):
        """707 sightings of the same 32 errors said nothing: count changes only."""
        import asyncio

        supervisor = self.supervisor()

        async def run():
            await supervisor._process_signal_inner(self.drive_signal(40))
            for _ in range(5):
                await supervisor._process_signal_inner(self.drive_signal(40, grew=False))
            await supervisor._process_signal_inner(self.drive_signal(41))

        asyncio.run(run())
        [row] = supervisor.store.list_incidents(limit=50)
        self.assertEqual(row["occurrences"], 2)
        self.assertEqual(row["container_name"], "host:Drive 7")

    def test_a_dismissed_drive_stays_dismissed_until_it_gets_worse(self):
        import asyncio

        supervisor = self.supervisor()

        async def run():
            await supervisor._process_signal_inner(self.drive_signal(40))
            [row] = supervisor.store.list_incidents(limit=50)
            supervisor.store.set_incident_status(row["id"], "dismissed")
            await supervisor._process_signal_inner(self.drive_signal(40, grew=False))
            self.assertEqual(len(supervisor.store.list_incidents(limit=50)), 1)
            await supervisor._process_signal_inner(self.drive_signal(41))

        asyncio.run(run())
        rows = supervisor.store.list_incidents(limit=50)
        self.assertEqual(sorted(r["status"] for r in rows), ["dismissed", "open"])

    def test_a_drive_turning_critical_replaces_its_warning(self):
        import asyncio

        supervisor = self.supervisor()

        async def run():
            await supervisor._process_signal_inner(self.drive_signal(40))
            await supervisor._process_signal_inner(self.drive_signal(52, "critical"))

        asyncio.run(run())
        rows = supervisor.store.list_incidents(limit=50)
        self.assertEqual({(r["severity"], r["status"] == "resolved") for r in rows},
                         {("warning", True), ("critical", False)})

    def test_raid_warnings_nothing_fires_any_more_are_resolved(self):
        """The per-array incidents from before drives were counted as one."""
        import asyncio

        supervisor = self.supervisor()
        old = supervisor.store.open_incident(
            container_id="", container_name="host:md2", image="", kind="raid_errors",
            fingerprint="per-array", severity="warning",
            summary="Drive 7 has had 32 read errors corrected in md2", evidence={},
            mode="observe",
        )
        signal = self.drive_signal(40)
        supervisor.raid = {"arrays": [{"name": "md2", "operation": "",
                                       "drives": [{"disk": "sata7"}]}]}

        async def run():
            await supervisor._process_signal_inner(signal)
            await supervisor._resolve_cleared_raid_warnings([signal])

        asyncio.run(run())
        self.assertEqual(supervisor.store.get_incident(old)["status"], "resolved")
        self.assertEqual(len(supervisor.store.open_incidents_of_kind("raid_errors")), 1)

    def test_nothing_is_resolved_while_sys_cannot_be_read(self):
        import asyncio

        supervisor = self.supervisor()
        old = supervisor.store.open_incident(
            container_id="", container_name="host:Drive 7", image="", kind="raid_errors",
            fingerprint="drive", severity="warning", summary="Drive 7", evidence={},
            mode="observe",
        )
        supervisor.raid = {"arrays": [{"name": "md2", "operation": "", "drives": []}]}
        asyncio.run(supervisor._resolve_cleared_raid_warnings([]))
        self.assertEqual(supervisor.store.get_incident(old)["status"], "open")

    def test_a_diagnosis_downgrade_does_not_reopen_the_incident(self):
        """The loop that spammed the dashboard.

        The rule calls a `log_fatal` critical; the model reads the lines and
        says they are not fatal, which rewrites the incident's severity to
        info. Comparing the next poll's critical against *that* made every
        sighting look like an escalation, so the same failure opened a fresh
        incident every 30 seconds, forever.
        """
        import asyncio

        supervisor = self.supervisor()
        signal = self.fatal_signal()

        async def run():
            for _ in range(4):
                await supervisor._process_signal_inner(signal)
                newest = supervisor.store.list_incidents(limit=1)[0]
                supervisor.store.set_diagnosis(
                    newest["id"],
                    {"summary": "Not fatal - historical events.", "severity": "info"},
                    "info",
                )

        asyncio.run(run())
        rows = supervisor.store.list_incidents(limit=50)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurrences"], 4)

    def test_the_rules_severity_survives_a_diagnosis(self):
        incident_id = open_disk_incident(self.plain_store())
        store = self._last_store
        store.set_diagnosis(incident_id, {"summary": "mild"}, "info")
        incident = store.get_incident(incident_id)
        self.assertEqual(incident["severity"], "info")
        self.assertEqual(incident["rule_severity"], "warning")

    def plain_store(self):
        self._last_store = make_store()
        self.addCleanup(self._last_store.close)
        return self._last_store

    @staticmethod
    def fatal_signal():
        from app.detector import Signal

        return Signal(
            kind="log_fatal", severity="critical",
            summary="Docmost-DB logged a fatal error",
            container={"id": "d1", "name": "Docmost-DB", "image": "postgres:16", "labels": {}},
            details={"fatal_lines": ["2025-11-26 ... FATAL:  starting up"]},
            signature_parts=["2025-11-26 ... FATAL:  starting up"],
        )

    def test_four_identical_signals_make_one_incident(self):
        import asyncio

        supervisor = self.supervisor()

        async def run():
            for _ in range(4):
                await supervisor._process_signal_inner(self.disk_signal())

        asyncio.run(run())
        rows = supervisor.store.list_incidents(limit=50)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurrences"], 4)


if __name__ == "__main__":
    unittest.main()
