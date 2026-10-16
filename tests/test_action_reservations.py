"""Regressions for per-container overrides and concurrent remediation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from app.config import ContainerPolicy, Settings
from app.remediator import Decision, Policy
from app.store import Store
from app.supervisor import Supervisor


def incident(store, name, fingerprint):
    iid = store.open_incident(
        container_id=name, container_name=name, image="test", kind="unhealthy",
        fingerprint=fingerprint, severity="warning", summary="unhealthy",
        evidence={}, mode="auto",
    )
    return store.get_incident(iid)


def pending(store, row):
    return store.record_action(
        incident_id=row["id"], container_name=row["container_name"],
        action="restart_container", status="pending", decided_by="policy",
    )


class ReservationTests(unittest.TestCase):
    def test_label_limit_is_used_in_decision_and_reservation(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        policy = Policy(Settings(mode="auto"), store)
        first = pending(store, incident(store, "app", "old"))
        store.finish_action(first, "succeeded", "done")
        row = incident(store, "app", "new")
        for cap in (0, 1):
            with self.subTest(cap=cap):
                override = ContainerPolicy(max_actions_per_hour=cap)
                decision = policy.decide(
                    incident=row, container_policy=override,
                    diagnosis={"recommended_action": "restart_container", "confidence": 0.9},
                )
                self.assertEqual(decision.status, "pending")
                self.assertIn(f"cap {cap}", policy.claim_action(pending(store, row), override))
        self.assertIsNone(policy.claim_action(pending(store, row), ContainerPolicy()))

    def test_competing_connections_cannot_exceed_limits_or_cooldown(self):
        for guard in ("global", "container", "cooldown"):
            with self.subTest(guard=guard), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "doctor.db")
                stores = [Store(path) for _ in range(8)]
                try:
                    ids = [pending(stores[0], incident(
                        stores[0], f"app{i}" if guard == "global" else "app",
                        "same" if guard == "cooldown" else f"fp{i}",
                    )) for i in range(8)]
                    barrier = threading.Barrier(8)

                    def claim(i):
                        barrier.wait()
                        return stores[i].claim_action(
                            ids[i], per_container_cap=1 if guard == "container" else 100,
                            global_cap=1 if guard == "global" else 100, cooldown_minutes=30,
                        )

                    with ThreadPoolExecutor(max_workers=8) as pool:
                        results = list(pool.map(claim, range(8)))
                    self.assertEqual(results.count(None), 1)
                    self.assertEqual(len(stores[0].pending_actions()), 7)
                    for aid in ids:
                        row = stores[0].get_action(aid)
                        if row["status"] == "pending":
                            self.assertTrue(row["reason"])
                finally:
                    for store in stores:
                        store.close()

    def test_an_action_can_only_be_claimed_once(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        aid = pending(store, incident(store, "app", "fp"))
        limits = dict(per_container_cap=10, global_cap=10, cooldown_minutes=0)
        self.assertIsNone(store.claim_action(aid, **limits))
        self.assertEqual(store.claim_action(aid, **limits), "action is no longer pending")


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_approvals_execute_only_one_action(self):
        supervisor = Supervisor.__new__(Supervisor)
        supervisor.settings = Settings(mode="auto", max_actions_global_per_hour=1)
        supervisor.store = Store(":memory:")
        self.addCleanup(supervisor.store.close)
        supervisor.policy = Policy(supervisor.settings, supervisor.store)
        supervisor.broadcast = lambda *args: None
        supervisor.notifier = SimpleNamespace(
            notify_action=AsyncMock(), notify_incident=AsyncMock(),
        )
        supervisor.executor = SimpleNamespace(
            execute=AsyncMock(return_value="done"),
            verify=AsyncMock(return_value=(True, "healthy")),
        )
        # Both have already passed Policy.decide before either reserves a slot.
        await asyncio.gather(*(
            supervisor._apply_decision(
                incident(supervisor.store, name, name), {},
                Decision("restart_container", "approved", "restart"), ContainerPolicy(),
            ) for name in ("a", "b")
        ))
        self.assertEqual(supervisor.executor.execute.await_count, 1)
        pending_rows = supervisor.store.pending_actions()
        self.assertEqual(len(pending_rows), 1)
        self.assertIn("circuit breaker", pending_rows[0]["reason"])
        waiting = supervisor.store.get_incident(pending_rows[0]["incident_id"])
        self.assertEqual(waiting["status"], "awaiting_approval")
        supervisor.notifier.notify_incident.assert_awaited_once()
