"""The demo host still fits the doctor.

demo/ stands in for Docker, /proc and /sys, the SMART exporter and the
history the doctor would have gathered; the docs' screenshots and live demo
are taken from it (python -m demo.record). These read each stand-in through
the doctor's own code, so a change there that the demo no longer satisfies
fails here, not at the next recording.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import docker_client, host, raid, smart  # noqa: E402
from app.store import Store  # noqa: E402
from demo import fakes, seed, world  # noqa: E402


class TheFakeDocker(unittest.TestCase):
    def setUp(self):
        self.client = docker_client.DockerClient.__new__(docker_client.DockerClient)
        self.client._client = fakes.FakeDockerPy()
        self.client._env_key = b""

    def test_every_container_is_listed_with_its_stack(self):
        listed = {c["name"]: c for c in self.client._list_containers_sync()}
        self.assertEqual(set(listed), {b.name for b in world.BOXES})
        self.assertEqual(listed["paperless-db"]["stack"], "paperless")
        self.assertEqual(listed["paperless-db"]["health"], "unhealthy")

    def test_stats_and_logs_read(self):
        box = next(b for b in world.BOXES if b.name == "sonarr")
        stats = self.client._stats_sync(box.id)
        self.assertGreater(stats["mem_usage_bytes"], 0)
        self.assertIn("401 Unauthorized", self.client._logs_sync(box.id, 120))


class TheFakeHost(unittest.TestCase):
    def test_the_host_raid_and_drives_read(self):
        root = Path(tempfile.mkdtemp())
        fake = fakes.FakeHost(root)
        reader = host.HostReader(str(fake.proc), str(fake.sys))
        reader.read()
        fake.tick()
        reading = reader.read()
        self.assertIsNotNone(reading["cpu_pct"])
        self.assertTrue(reading["temperatures"])

        arrays, reason = raid.read(str(fake.mdstat), frozenset())
        self.assertEqual(reason, "")
        pools = raid.inspect(arrays, str(fake.sys), synology=False)
        self.assertEqual(pools[0]["arrays"], ["md0"])

        drives = smart.drives(smart.parse_metrics(fakes.smart_metrics()))
        self.assertEqual(sorted(d["device"] for d in drives), ["nvme0n1", "sda", "sdb"])


class TheSeed(unittest.TestCase):
    def test_two_weeks_of_history_go_in(self):
        path = os.path.join(tempfile.mkdtemp(), "doctor.db")
        seed.seed(path)
        store = Store(path)
        try:
            self.assertEqual(store.count_incidents(), len(seed.PAST))
            self.assertGreater(store.count_changes(), 10)
            self.assertGreater(store.count_llm_calls(), len(seed.PAST))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
