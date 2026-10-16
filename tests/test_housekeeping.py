"""Incidents that stop recurring resolve themselves; some must not.

A rule that is still true re-fires on every poll, which bumps `last_seen_at`,
so silence for hours means the condition is over. That reasoning does not hold
for every kind - host disk pressure suppresses its own re-alerts until usage
climbs another 2%, so a quiet disk incident means a steadily full disk, not a
fixed one.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.store import Store  # noqa: E402
from app.supervisor import AUTO_RESOLVE_EXCLUDED_KINDS  # noqa: E402


def make_store() -> Store:
    return Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))


class AutoResolve(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)

    def open_one(self, kind="log_errors", fingerprint="fp", name="app") -> str:
        return self.store.open_incident(
            container_id="c", container_name=name, image="img", kind=kind,
            fingerprint=fingerprint, severity="warning", summary="boom",
            evidence={}, mode="observe",
        )

    def age(self, incident_id: str, hours: float) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        self.store._conn.execute(
            "UPDATE incidents SET last_seen_at=?, opened_at=? WHERE id=?",
            (old, old, incident_id),
        )
        self.store._conn.commit()

    def test_a_condition_that_went_quiet_resolves(self):
        incident_id = self.open_one()
        self.age(incident_id, 20)
        self.assertEqual(self.store.auto_resolve_stale(12), 1)
        row = self.store.get_incident(incident_id)
        self.assertEqual(row["status"], "resolved")
        self.assertIsNotNone(row["resolved_at"])

    def test_one_still_recurring_is_left_open(self):
        incident_id = self.open_one()
        self.age(incident_id, 20)
        self.store.touch_incident(incident_id)  # seen just now
        self.assertEqual(self.store.auto_resolve_stale(12), 0)
        self.assertEqual(self.store.get_incident(incident_id)["status"], "open")

    def test_disk_pressure_is_excluded(self):
        quiet = self.open_one(kind="host_disk_pressure", fingerprint="fp-disk")
        self.age(quiet, 48)
        self.assertEqual(
            self.store.auto_resolve_stale(12, AUTO_RESOLVE_EXCLUDED_KINDS), 0
        )
        self.assertEqual(self.store.get_incident(quiet)["status"], "open")

    def test_the_exclusion_list_is_the_one_the_supervisor_uses(self):
        self.assertIn("host_disk_pressure", AUTO_RESOLVE_EXCLUDED_KINDS)

    def test_an_incident_awaiting_approval_is_left_alone(self):
        incident_id = self.open_one()
        self.age(incident_id, 48)
        self.store.record_action(
            incident_id=incident_id, container_name="app", action="restart_container",
            status="pending", decided_by="policy", reason="needs approval",
        )
        self.assertEqual(self.store.auto_resolve_stale(12), 0)
        self.assertEqual(self.store.get_incident(incident_id)["status"], "open")

    def test_already_dismissed_stays_dismissed(self):
        incident_id = self.open_one()
        self.age(incident_id, 48)
        self.store.set_incident_status(incident_id, "dismissed")
        self.assertEqual(self.store.auto_resolve_stale(12), 0)
        self.assertEqual(self.store.get_incident(incident_id)["status"], "dismissed")


class Paging(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)
        self.ids = [
            self.store.open_incident(
                container_id="c", container_name="app", image="img", kind="log_errors",
                fingerprint=f"fp{n}", severity="warning", summary=f"boom {n}",
                evidence={}, mode="observe",
            )
            for n in range(120)
        ]

    def test_counts_everything_not_just_the_page(self):
        self.assertEqual(self.store.count_incidents(), 120)
        self.assertEqual(len(self.store.list_incidents(limit=50)), 50)

    def test_offset_walks_the_list_without_repeating(self):
        first = self.store.list_incidents(limit=50, offset=0)
        second = self.store.list_incidents(limit=50, offset=50)
        third = self.store.list_incidents(limit=50, offset=100)
        seen = [row["id"] for row in first + second + third]
        self.assertEqual(len(seen), 120)
        self.assertEqual(len(set(seen)), 120)

    def test_counting_respects_the_status_filter(self):
        self.store.set_incident_status(self.ids[0], "dismissed")
        self.assertEqual(self.store.count_incidents("dismissed"), 1)
        self.assertEqual(self.store.count_incidents(), 120)


class ActionPaging(unittest.TestCase):
    """The action log shows ten at a time; the total is what the pager reads."""

    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)
        self.ids = [
            self.store.record_action(
                incident_id="i", container_name="app", action="notify_only",
                status="skipped", decided_by="policy", reason=f"reason {n}",
            )
            for n in range(25)
        ]

    def test_counts_everything_not_just_the_page(self):
        self.assertEqual(self.store.count_actions(), 25)
        self.assertEqual(len(self.store.list_actions(limit=10)), 10)

    def test_offset_walks_the_log_without_repeating(self):
        pages = [self.store.list_actions(limit=10, offset=n) for n in (0, 10, 20)]
        seen = [row["id"] for page in pages for row in page]
        self.assertEqual(len(seen), 25)
        self.assertEqual(set(seen), set(self.ids))


class DiskThresholds(unittest.TestCase):
    """A NAS that is simply well used sits high for years."""

    def detector(self, **env):
        previous = dict(os.environ)
        os.environ.update({"DOCTOR_DATA_DIR": tempfile.mkdtemp(), **env})
        try:
            from app.config import Settings
            from app.detector import Detector

            return Detector(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def check(self, detector, pct):
        return detector.check_disk([{
            "path": "/data", "used_pct": pct, "free_bytes": 3_000_000_000_000,
            "total_bytes": 40_000_000_000_000, "used_bytes": 37_000_000_000_000,
        }])

    def test_a_disk_below_the_threshold_is_not_an_incident(self):
        """92% with terabytes free was pestering every poll."""
        self.assertEqual(self.check(self.detector(), 92.0), [])

    def test_the_default_bar_is_95(self):
        detector = self.detector()
        self.assertEqual(self.check(detector, 94.9), [])
        self.assertEqual(len(self.check(detector, 95.0)), 1)

    def test_critical_has_its_own_setting_rather_than_a_hard_coded_95(self):
        detector = self.detector(DOCTOR_DISK_THRESHOLD="80", DOCTOR_DISK_CRITICAL="98")
        self.assertEqual(self.check(detector, 85.0)[0].severity, "warning")
        # a fresh detector: the same path only re-alerts once it worsens by 2%
        self.assertEqual(
            self.detector(DOCTOR_DISK_THRESHOLD="80", DOCTOR_DISK_CRITICAL="98")
            .check_disk([{"path": "/data", "used_pct": 99.0, "free_bytes": 1,
                          "total_bytes": 2, "used_bytes": 1}])[0].severity,
            "critical",
        )


class TheActiveFilter(unittest.TestCase):
    """What the dashboard's default view asks for."""

    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)
        self.ids = {}
        for kind, status in (
            ("a", "open"), ("b", "awaiting_approval"),
            ("c", "resolved"), ("d", "dismissed"),
        ):
            incident_id = self.store.open_incident(
                container_id="c", container_name="x", image="i", kind=kind,
                fingerprint=kind, severity="warning", summary="s",
                evidence={}, mode="observe",
            )
            if status != "open":
                self.store.set_incident_status(incident_id, status)
            self.ids[status] = incident_id

    def test_active_covers_awaiting_approval_too(self):
        """Filtering on `open` alone hid the ones waiting on a human."""
        rows = self.store.list_incidents(status=self.store.ACTIVE)
        self.assertEqual(
            {row["status"] for row in rows}, {"open", "awaiting_approval"}
        )

    def test_active_excludes_what_has_been_dealt_with(self):
        rows = self.store.list_incidents(status=self.store.ACTIVE)
        ids = {row["id"] for row in rows}
        self.assertNotIn(self.ids["dismissed"], ids)
        self.assertNotIn(self.ids["resolved"], ids)

    def test_the_count_agrees_with_the_rows(self):
        for status in (None, self.store.ACTIVE, "open", "dismissed", "resolved"):
            with self.subTest(status=status):
                self.assertEqual(
                    self.store.count_incidents(status),
                    len(self.store.list_incidents(status=status, limit=500)),
                )

    def test_dismissing_everything_empties_the_default_view(self):
        """The reported surprise: dismissed, but still on screen."""
        self.store.dismiss_all()
        self.assertEqual(self.store.list_incidents(status=self.store.ACTIVE), [])
        self.assertEqual(self.store.count_incidents(self.store.ACTIVE), 0)
        # still reachable under its own filter, and under "all"
        self.assertEqual(self.store.count_incidents("dismissed"), 3)
        self.assertEqual(self.store.count_incidents(), 4)


class BatchedActions(unittest.TestCase):
    """One query for every incident's actions, not one query per incident."""

    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)

    def test_groups_actions_by_incident(self):
        first = self.store.open_incident(
            container_id="c", container_name="app", image="i", kind="log_errors",
            fingerprint="a", severity="warning", summary="s", evidence={}, mode="observe")
        second = self.store.open_incident(
            container_id="c", container_name="app", image="i", kind="log_errors",
            fingerprint="b", severity="warning", summary="s", evidence={}, mode="observe")
        self.store.record_action(
            incident_id=first, container_name="app", action="restart_container",
            status="pending", decided_by="policy", reason="r")
        grouped = self.store.actions_for_incidents([first, second])
        self.assertEqual(len(grouped.get(first, [])), 1)
        self.assertEqual(grouped.get(second, []), [])

    def test_no_ids_is_not_a_query(self):
        self.assertEqual(self.store.actions_for_incidents([]), {})


if __name__ == "__main__":
    unittest.main()


class ManualRequests(unittest.TestCase):
    """A restart or redeploy asked for from the dashboard is not an incident.

    The row exists only so the action has something to hang off; the outcome
    is shown under Activity, and a failed redeploy must not sit in the
    Incidents list or its badge as if something had broken.
    """

    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)

    def open_one(self, kind: str) -> str:
        return self.store.open_incident(
            container_id="c", container_name="radarr", image="img", kind=kind,
            fingerprint=f"{kind}:radarr", severity="info", summary="s",
            evidence={}, mode="observe",
        )

    def test_never_listed_or_counted(self):
        manual = self.open_one(Store.MANUAL)
        self.store.set_incident_status(manual, "failed")
        real = self.open_one("unhealthy")
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        for status in (None, Store.ACTIVE, "failed"):
            ids = [row["id"] for row in self.store.list_incidents(status=status)]
            self.assertNotIn(manual, ids)
        self.assertEqual([r["id"] for r in self.store.list_incidents(container_name="radarr")], [real])
        self.assertEqual(self.store.count_incidents(Store.ACTIVE), 1)
        self.assertEqual(self.store.count_incidents_since(since), 1)

    def test_still_anchors_its_action(self):
        manual = self.open_one(Store.MANUAL)
        self.store.record_action(incident_id=manual, container_name="radarr",
                                 action="redeploy_stack", status="failed",
                                 decided_by="user:me")
        self.assertEqual(len(self.store.list_actions()), 1)
        self.assertIsNotNone(self.store.get_incident(manual))

    def test_dismiss_all_leaves_one_in_flight_alone(self):
        manual = self.open_one(Store.MANUAL)
        self.store.set_incident_status(manual, "acting")
        self.open_one("unhealthy")
        self.assertEqual(self.store.dismiss_all(), 1)
        self.assertEqual(self.store.get_incident(manual)["status"], "acting")
