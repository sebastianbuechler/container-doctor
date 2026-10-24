"""The longer histories: each drive's reads and writes, its SMART counters
hour by hour, and each container's CPU per hour - stored, summed up, pruned."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import smart  # noqa: E402
from app.config import Settings  # noqa: E402
from app.store import Store  # noqa: E402


def supervisor():
    from app.supervisor import Supervisor
    sup = Supervisor.__new__(Supervisor)
    sup.settings = Settings(public_url="")
    sup.store = Store(":memory:")
    sup._drive_ids = {}
    return sup


def hour(days_ago: float = 0, hours_ago: float = 0) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)).replace(
        minute=0, second=0, microsecond=0)


class DriveIo(unittest.TestCase):
    def setUp(self):
        self.sup = supervisor()
        self.addCleanup(self.sup.store.close)

    def test_rates_average_per_bucket_and_add_up_over_the_range(self):
        store = self.sup.store
        store.record_drive_io({"WD-1": {"read_bps": 1000, "write_bps": 0, "busy_pct": 10}},
                              bucket_minutes=60)
        store.record_drive_io({"WD-1": {"read_bps": 3000, "write_bps": 400, "busy_pct": 30}},
                              bucket_minutes=60)
        history = self.sup.drive_io_history("WD-1", 24)
        [[_, read, written, busy]] = history["samples"]
        self.assertEqual((read, written, busy), (2000, 200, 20.0))
        # Five-minute buckets by default: the average rate times the bucket.
        self.assertEqual((history["read_bytes"], history["written_bytes"]), (2000 * 300, 200 * 300))
        self.assertEqual(self.sup.drive_io_history("WD-2", 24)["samples"], [])

    def test_a_disk_is_filed_under_the_serial_smart_last_saw_in_it(self):
        self.sup._drive_ids = {"sata1": {"serial": "WD-1"}}
        self.assertEqual(self.sup._drive_history_key("sata1"), "WD-1")
        # No SMART for it, or not read yet: its device name.
        self.assertEqual(self.sup._drive_history_key("sata2"), "sata2")


class DriveCounters(unittest.TestCase):
    def setUp(self):
        self.sup = supervisor()
        self.addCleanup(self.sup.store.close)

    def put(self, at: datetime, counters: dict[str, int], drive: str = "WD-1"):
        import json
        self.sup.store._conn.execute(
            "INSERT INTO drive_counters (drive, bucket, at, counters) VALUES (?,?,?,?)",
            (drive, f"{at:%Y-%m-%dT%H}", at.isoformat(timespec="seconds"), json.dumps(counters)))

    def test_the_last_reading_in_an_hour_is_kept(self):
        store = self.sup.store
        store.record_drive_counters({"WD-1": {"pending": 0}})
        store.record_drive_counters({"WD-1": {"pending": 2}})
        [row] = store.drive_counter_history("WD-1", hour(hours_ago=2))
        self.assertEqual(row["counters"], {"pending": 2})

    def test_a_counter_that_rose_says_when_and_how_often(self):
        self.put(hour(days_ago=3), {"pending": 0, "reallocated": 4, "spin_ups": 100})
        self.put(hour(days_ago=2), {"pending": 8, "reallocated": 4, "spin_ups": 106})
        self.put(hour(days_ago=1), {"pending": 0, "reallocated": 12, "spin_ups": 112})
        self.put(hour(), {"pending": 0, "reallocated": 12, "spin_ups": 118})
        counters = {c["key"]: c for c in self.sup.drive_counter_history("WD-1", 30)["counters"]}
        self.assertEqual(list(counters), ["reallocated", "pending", "spin_ups"])
        reallocated = counters["reallocated"]
        self.assertEqual((reallocated["first"], reallocated["last"], reallocated["rises"]), (4, 12, 1))
        self.assertEqual(reallocated["rose_at"], hour(days_ago=1).isoformat(timespec="seconds"))
        self.assertEqual(len(reallocated["series"]), 4)
        # Pending sectors remapped: back where it was, but it did rise once.
        self.assertEqual((counters["pending"]["first"], counters["pending"]["last"],
                          counters["pending"]["rises"]), (0, 0, 1))
        self.assertEqual(counters["spin_ups"]["last"] - counters["spin_ups"]["first"], 18)

    def test_a_steady_counter_carries_no_series(self):
        self.put(hour(days_ago=1), {"crc_errors": 3})
        self.put(hour(), {"crc_errors": 3})
        [crc] = self.sup.drive_counter_history("WD-1", 30)["counters"]
        self.assertEqual((crc["rises"], crc["series"], crc["rose_at"]), (0, [], None))

    def test_counters_read_from_a_smart_reading(self):
        drive = {"counters": {"Reallocated_Sector_Ct": 24, "Current_Pending_Sector": 8},
                 "crc_errors": 3, "spin_ups": 1200, "nvme": {}}
        self.assertEqual(smart.counters(drive), {"reallocated": 24, "pending": 8,
                                                 "crc_errors": 3, "spin_ups": 1200})
        nvme = {"counters": {}, "crc_errors": None, "spin_ups": None, "nvme": {"media_errors": 0.0}}
        self.assertEqual(smart.counters(nvme), {"media_errors": 0})


class ContainerCpu(unittest.TestCase):
    def setUp(self):
        self.sup = supervisor()
        self.addCleanup(self.sup.store.close)

    def put(self, name: str, at: datetime, readings: list[float]):
        self.sup.store._conn.execute(
            "INSERT INTO cpu_samples (name, bucket, max_pct, sum_pct, n) VALUES (?,?,?,?,?)",
            (name, at.isoformat(timespec="seconds"), max(readings), sum(readings), len(readings)))

    def test_peak_and_mean_per_hour(self):
        store = self.sup.store
        for pct in (2.0, 10.0, 6.0):
            store.record_cpu_samples([("plex", pct)])
        [row] = store.cpu_history(hour(hours_ago=2))["plex"]
        self.assertEqual((row["max"], row["mean"]), (10.0, 6.0))

    def test_the_busiest_containers_first_with_their_busiest_hour(self):
        self.put("plex", hour(hours_ago=3), [20.0, 40.0])
        self.put("plex", hour(hours_ago=2), [2.0])
        self.put("sonarr", hour(hours_ago=3), [5.0])
        self.put("idle", hour(hours_ago=3), [0.0])
        self.put("gone", hour(days_ago=3), [90.0])  # outside the range
        top = self.sup.cpu_by_container(hour(days_ago=1))
        self.assertEqual([r["name"] for r in top], ["plex", "sonarr"])
        plex = top[0]
        self.assertEqual((plex["mean"], plex["peak"], plex["busiest_mean"]), (16.0, 40.0, 30.0))
        self.assertEqual(plex["busiest_hour"], hour(hours_ago=3).isoformat(timespec="seconds"))


class Pruning(unittest.TestCase):
    def test_old_rows_go_with_the_rest_of_the_history(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        old = datetime.now(timezone.utc) - timedelta(days=40)
        stamp = old.isoformat(timespec="seconds")
        store._conn.execute("INSERT INTO drive_io VALUES ('WD-1', ?, ?, 1, 1, 1, 1)",
                            (f"{old:%Y-%m-%dT%H:%M}", stamp))
        store._conn.execute("INSERT INTO drive_counters VALUES ('WD-1', ?, ?, '{}')",
                            (f"{old:%Y-%m-%dT%H}", stamp))
        store._conn.execute("INSERT INTO cpu_samples VALUES ('plex', ?, 1, 1, 1)", (stamp,))
        store.prune(30)
        for table in ("drive_io", "drive_counters", "cpu_samples"):
            self.assertEqual(store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
