"""Polls are quick, keep to their interval, and can be asked for.

`docker stats` waits a second for its CPU sample. Read one container at a time,
33 of them made a poll run over a minute, and with the interval counted from
the end of a poll the dashboard called it overdue most of the day.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import Settings  # noqa: E402
from app.supervisor import Supervisor  # noqa: E402


class SlowDocker:
    """Answers like Docker, with `stats` taking as long as the real thing."""

    def __init__(self, count: int, stats_seconds: float) -> None:
        self.count = count
        self.stats_seconds = stats_seconds
        self.in_flight = 0
        self.most_in_flight = 0

    async def list_containers(self):
        return [{"id": f"id{i}", "name": f"app{i}", "state": "running",
                 "health": "none", "labels": {}} for i in range(self.count)]

    async def networks(self):
        return None  # as behind a proxy with NETWORKS=0

    async def stats(self, container_id):
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.stats_seconds)
        finally:
            self.in_flight -= 1
        return {"cpu_pct": 1.0, "cpu_host_pct": 0.25, "online_cpus": 4,
                "mem_usage_bytes": 1, "mem_limit_bytes": 10, "mem_pct": 10.0,
                "for": container_id}

    async def logs(self, container_id, lines, since=None):
        return f"log of {container_id}"


def supervisor(docker, **settings) -> Supervisor:
    sup = Supervisor.__new__(Supervisor)
    sup.settings = Settings(disk_paths=[], mdstat_path="", proc_path="", smart_url="", **settings)
    sup.docker = docker
    sup.policy = SimpleNamespace(effective_mode=lambda policy: "observe",
                                 protected=lambda name: "")
    sup.settling = SimpleNamespace(strip=lambda text: (text, 0), active=lambda: False,
                                   requested_stop=lambda *args: False)
    sup.detector = MagicMock()
    sup.detector.from_poll.return_value = []
    sup.store = MagicMock()
    sup.containers = {}
    sup.history = {}
    sup.disk = []
    sup.raid = {"arrays": [], "pools": [], "reason": "", "drives": []}
    sup.host = None
    sup.net = None
    sup.smart = None
    sup.image_updates = {"checked_at": None, "images": []}
    sup._image_checking = False
    sup._image_progress = None
    sup._image_partial = []
    sup._redeploying = {}
    sup._deploy_stacks = None
    sup._github = {"checked_at": None, "error": "", "workflow": None}
    sup.last_poll_at = None
    sup.last_poll_seconds = None
    sup._polling = False
    sup._poll_progress = None
    sup._poll_now = asyncio.Event()
    sup.docker_connected = False
    sup._track_changes = lambda snapshots: None
    sup.events = []
    sup.broadcast = lambda kind, payload: sup.events.append((kind, payload))
    return sup


class PollOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_containers_are_read_several_at_a_time(self):
        docker = SlowDocker(count=12, stats_seconds=0.2)
        sup = supervisor(docker, poll_concurrency=4)
        started = time.monotonic()
        await sup._poll_once()
        took = time.monotonic() - started
        # One at a time this is 2.4s; four at a time, three rounds of 0.2s.
        self.assertLess(took, 1.2)
        self.assertEqual(docker.most_in_flight, 4)
        self.assertIsNotNone(sup.last_poll_seconds)

    async def test_each_container_keeps_its_own_stats_and_logs(self):
        sup = supervisor(SlowDocker(count=5, stats_seconds=0.01), poll_concurrency=3)
        await sup._poll_once()
        for container in sup.containers.values():
            self.assertEqual(container["stats"]["for"], container["id"])
        seen = {call.args[0]["id"]: call.args[2] for call in sup.detector.from_poll.call_args_list}
        self.assertEqual(seen, {f"id{i}": f"log of id{i}" for i in range(5)})

    async def test_the_finished_poll_is_published_as_no_longer_polling(self):
        sup = supervisor(SlowDocker(count=2, stats_seconds=0.01))
        sup._polling = True
        await sup._poll_once()
        kind, payload = sup.events[-1]
        self.assertEqual(kind, "state")
        self.assertFalse(payload["polling"])
        self.assertEqual(payload["poll_interval_seconds"], sup.settings.poll_interval_seconds)
        self.assertIsNone(payload["poll_progress"])

    async def test_progress_counts_the_reads_and_names_the_ones_in_flight(self):
        """The dashboard counts along instead of watching a bare spinner."""
        sup = supervisor(SlowDocker(count=5, stats_seconds=0.01), poll_concurrency=2)
        await sup._poll_once()
        steps = [payload["progress"] for kind, payload in sup.events if kind == "poll"]
        self.assertEqual(steps[0], {"done": 0, "total": 5, "reading": []})
        self.assertEqual(steps[-1], {"done": 5, "total": 5, "reading": []})
        self.assertTrue(all(len(step["reading"]) <= 2 for step in steps))
        self.assertTrue(any(len(step["reading"]) == 2 for step in steps))
        done = [step["done"] for step in steps]
        self.assertEqual(done, sorted(done))


class PollLoopTests(unittest.IsolatedAsyncioTestCase):
    async def run_loop(self, sup, seconds):
        task = asyncio.create_task(sup._poll_loop())
        await asyncio.sleep(seconds)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_the_interval_runs_from_the_start_of_each_poll(self):
        sup = supervisor(SlowDocker(count=1, stats_seconds=0), poll_interval_seconds=1)
        starts = deque()

        async def poll():
            starts.append(time.monotonic())
            await asyncio.sleep(0.6)

        sup._poll_once = poll
        await self.run_loop(sup, 2.3)
        gaps = [b - a for a, b in zip(starts, list(starts)[1:])]
        self.assertTrue(gaps)
        for gap in gaps:
            self.assertAlmostEqual(gap, 1.0, delta=0.15)

    async def test_a_requested_poll_does_not_wait_for_the_interval(self):
        sup = supervisor(SlowDocker(count=1, stats_seconds=0), poll_interval_seconds=60)
        polls = []

        async def poll():
            polls.append(time.monotonic())

        sup._poll_once = poll
        task = asyncio.create_task(sup._poll_loop())
        await asyncio.sleep(0.05)
        self.assertEqual(len(polls), 1)
        self.assertEqual(sup.request_poll(), {"started": True, "polling": True})
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(polls), 2)

    async def test_a_poll_already_running_is_not_asked_for_again(self):
        sup = supervisor(SlowDocker(count=1, stats_seconds=0))
        sup._polling = True
        self.assertEqual(sup.request_poll(), {"started": False, "polling": True})
        self.assertFalse(sup._poll_now.is_set())

    async def test_a_poll_that_could_not_reach_docker_still_ends(self):
        sup = supervisor(SlowDocker(count=1, stats_seconds=0), poll_interval_seconds=60)

        async def unreachable():
            return None  # returns before publishing a snapshot

        sup._poll_once = unreachable
        await self.run_loop(sup, 0.05)
        self.assertFalse(sup._polling)
        self.assertEqual(sup.events[-1], ("poll", {"polling": False}))


if __name__ == "__main__":
    unittest.main()
