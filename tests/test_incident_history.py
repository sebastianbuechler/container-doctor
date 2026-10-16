"""A deduplicated incident keeps the detail of every sighting.

Folding sightings into one incident fixed the wall of duplicates, but the
count alone loses what each one saw: which movies this particular scan could
not probe. The history keeps each sighting's time and lines, on demand.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import store as store_module  # noqa: E402
from app.detector import Signal  # noqa: E402
from app.store import Store  # noqa: E402
from app.supervisor import _sighting_lines  # noqa: E402

try:  # noqa: E402
    from fastapi.testclient import TestClient

    from app.web.api import build_app
    from tests.test_incident_logs import make_settings
except ImportError:  # pragma: no cover - a partial local environment
    TestClient = None

NEEDS_APP = unittest.skipIf(TestClient is None, "needs the app's dependencies")


def make_store() -> Store:
    return Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))


def open_probe_incident(store: Store, lines=None) -> str:
    return store.open_incident(
        container_id="c", container_name="jellyfin", image="jellyfin/jellyfin", kind="log_errors",
        fingerprint="fp-probe", severity="warning", summary="jellyfin logged 12 lines",
        evidence={}, mode="observe", lines=lines or ["Error probing /media/movies/Heat.mkv"],
    )


class Recording(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)

    def test_the_first_sighting_is_the_opening(self):
        incident_id = open_probe_incident(self.store)
        history = self.store.list_occurrences(incident_id)
        self.assertEqual(history["kept"], 1)
        self.assertEqual(history["occurrences"][0]["lines"], ["Error probing /media/movies/Heat.mkv"])
        self.assertEqual(history["occurrences"][0]["at"],
                         self.store.get_incident(incident_id)["opened_at"])

    def test_each_sighting_keeps_its_own_lines_newest_first(self):
        incident_id = open_probe_incident(self.store)
        self.store.touch_incident(incident_id, summary="second",
                                  lines=["Error probing /media/movies/Alien.mkv"])
        self.store.touch_incident(incident_id, summary="third",
                                  lines=["Error probing /media/movies/Brazil.mkv"])
        rows = self.store.list_occurrences(incident_id)["occurrences"]
        self.assertEqual([r["summary"] for r in rows], ["third", "second", "jellyfin logged 12 lines"])
        self.assertEqual(rows[0]["lines"], ["Error probing /media/movies/Brazil.mkv"])

    def test_a_sighting_without_lines_still_has_its_time(self):
        incident_id = open_probe_incident(self.store)
        self.store.touch_incident(incident_id)
        self.assertEqual(self.store.list_occurrences(incident_id)["occurrences"][0]["lines"], [])

    def test_touching_an_unknown_incident_records_nothing(self):
        self.assertEqual(self.store.touch_incident("nope", lines=["x"]), 0)
        self.assertEqual(self.store.list_occurrences("nope")["kept"], 0)

    def test_the_detail_is_capped_but_the_count_is_not(self):
        incident_id = open_probe_incident(self.store)
        limit = store_module.OCCURRENCE_HISTORY_LIMIT
        for n in range(limit + 5):
            self.store.touch_incident(incident_id, summary=f"s{n}")
        history = self.store.list_occurrences(incident_id, limit=1)
        self.assertEqual(history["kept"], limit)
        self.assertEqual(history["occurrences"][0]["summary"], f"s{limit + 4}")
        self.assertEqual(self.store.get_incident(incident_id)["occurrences"], limit + 6)

    def test_pruning_an_incident_takes_its_history(self):
        incident_id = open_probe_incident(self.store)
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
        self.store._conn.execute("UPDATE incidents SET opened_at=? WHERE id=?", (old, incident_id))
        self.store._conn.commit()
        self.store.prune(30)
        left = self.store._conn.execute("SELECT COUNT(*) FROM incident_occurrences").fetchone()[0]
        self.assertEqual(left, 0)


class WhatASightingKeeps(unittest.TestCase):
    def signal(self, **details):
        return Signal(kind="log_errors", severity="warning", summary="s",
                      container={"name": "jellyfin"}, details=details)

    def test_the_matched_lines(self):
        self.assertEqual(_sighting_lines(self.signal(matched_lines=["a", "b"])), ["a", "b"])

    def test_a_fatal_keeps_its_fatal_lines(self):
        self.assertEqual(_sighting_lines(self.signal(fatal_lines=["boom"])), ["boom"])

    def test_nothing_for_a_rule_without_lines(self):
        self.assertEqual(_sighting_lines(self.signal(event="oom")), [])

    def test_secrets_are_redacted_even_if_a_rule_forgot(self):
        lines = _sighting_lines(self.signal(matched_lines=["ERROR token=sk-ant-supersecretvalue1234"]))
        self.assertNotIn("sk-ant-supersecretvalue1234", lines[0])


class StubSupervisor:
    def __init__(self, store):
        self.store = store
        self.docker_connected = True
        self.docker_access: list = []

    def snapshot(self):
        return {}

    def status(self):
        return {}


@NEEDS_APP
class TheEndpoint(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)
        self.client = TestClient(build_app(StubSupervisor(self.store),
                                           make_settings(DOCTOR_WEB_PASSWORD="")))

    def test_it_serves_the_history_with_the_total(self):
        incident_id = open_probe_incident(self.store)
        self.store.touch_incident(incident_id, lines=["Error probing /media/movies/Up.mkv"])
        body = self.client.get(f"/api/incidents/{incident_id}/occurrences").json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["kept"], 2)
        self.assertEqual(body["occurrences"][0]["lines"], ["Error probing /media/movies/Up.mkv"])

    def test_an_unknown_incident_is_a_404(self):
        self.assertEqual(self.client.get("/api/incidents/nope/occurrences").status_code, 404)


if __name__ == "__main__":
    unittest.main()
