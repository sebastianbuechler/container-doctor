"""The host itself: parsing the kernel's counters, the host rules, and noticing a reboot."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import host  # noqa: E402
from app.config import Settings  # noqa: E402
from app.detector import Detector  # noqa: E402
from app.remediator import Policy  # noqa: E402
from app.store import Store  # noqa: E402

STAT = """cpu  100 0 50 800 50 0 0 0 0 0
cpu0 50 0 25 400 25 0 0 0 0 0
cpu1 50 0 25 400 25 0 0 0 0 0
intr 1 2 3
"""
STAT_LATER = """cpu  160 0 80 840 120 0 0 0 0 0
cpu0 80 0 40 420 60 0 0 0 0 0
cpu1 80 0 40 420 60 0 0 0 0 0
"""
MEMINFO = """MemTotal:        8000000 kB
MemFree:          200000 kB
MemAvailable:    2000000 kB
Cached:          1500000 kB
SwapTotal:       4000000 kB
SwapFree:        3000000 kB
"""
DISKSTATS = """   8       0 sata1 100 0 2000 0 50 0 4000 0 0 1000 0 0 0 0 0
   8       1 sata1p1 100 0 2000 0 50 0 4000 0 0 1000 0 0 0 0 0
   9       0 md2 100 0 2000 0 50 0 4000 0 0 1000 0 0 0 0 0
 253       0 dm-0 100 0 2000 0 50 0 4000 0 0 1000 0 0 0 0 0
   8      16 sdb 10 0 200 0 5 0 400 0 0 100 0 0 0 0 0
"""
PRESSURE = """some avg10=1.00 avg60=2.50 avg300=3.00 total=1
full avg10=0.50 avg60=1.25 avg300=1.00 total=1
"""


class Parsing(unittest.TestCase):
    def test_cpu_busy_and_iowait_between_two_readings(self):
        before, cores = host.parse_stat(STAT)
        after, _ = host.parse_stat(STAT_LATER)
        self.assertEqual(cores, 2)
        # 60 user + 30 system + 40 idle + 70 iowait = 200 ticks; busy 90, iowait 70.
        self.assertEqual(host.cpu_usage(before, after), (45.0, 35.0))

    def test_no_rate_from_a_single_reading(self):
        self.assertIsNone(host.cpu_usage({}, host.parse_stat(STAT)[0]))

    def test_memory_counts_available_not_free(self):
        figures = host.memory_figures(host.parse_meminfo(MEMINFO))
        self.assertEqual(figures["mem_available_pct"], 25.0)
        self.assertEqual(figures["mem_used_pct"], 75.0)
        self.assertEqual(figures["swap_used_pct"], 25.0)

    def test_whole_disks_only(self):
        self.assertEqual(sorted(host.parse_diskstats(DISKSTATS)), ["sata1", "sdb"])

    def test_disk_busy_share(self):
        before = host.parse_diskstats(DISKSTATS)
        after = {name: {**d, "busy_ms": d["busy_ms"] + 15000, "read": d["read"] + 2048}
                 for name, d in before.items()}
        [sata1, _] = host.disk_rates(before, after, 30.0)
        self.assertEqual(sata1["busy_pct"], 50.0)
        self.assertEqual(sata1["read_bps"], round(2048 * 512 / 30))

    def test_pressure(self):
        self.assertEqual(host.parse_pressure(PRESSURE), {"some": 2.5, "full": 1.25})

    def test_temperatures_from_hwmon_and_thermal(self):
        with tempfile.TemporaryDirectory() as sysfs:
            chip = Path(sysfs, "class", "hwmon", "hwmon0")
            chip.mkdir(parents=True)
            (chip / "name").write_text("coretemp\n")
            (chip / "temp1_input").write_text("54000\n")
            (chip / "temp1_label").write_text("Package id 0\n")
            (chip / "temp1_crit").write_text("100000\n")
            (chip / "temp2_input").write_text("-273000\n")  # a sensor reporting nonsense
            zone = Path(sysfs, "class", "thermal", "thermal_zone0")
            zone.mkdir(parents=True)
            (zone / "type").write_text("x86_pkg_temp\n")
            (zone / "temp").write_text("55000\n")
            sensors = host.read_temperatures(sysfs)
        self.assertEqual([(s["name"], s["celsius"]) for s in sensors],
                         [("coretemp", 54.0), ("x86_pkg_temp", 55.0)])
        self.assertEqual(sensors[0]["critical"], 100.0)

    def test_reader_gives_rates_from_the_second_reading(self):
        with tempfile.TemporaryDirectory() as proc:
            Path(proc, "stat").write_text(STAT)
            Path(proc, "meminfo").write_text(MEMINFO)
            Path(proc, "loadavg").write_text("1.00 4.00 3.00 2/300 1234\n")
            Path(proc, "uptime").write_text("3600.5 100.0\n")
            Path(proc, "sys", "kernel").mkdir(parents=True)
            Path(proc, "sys", "kernel", "osrelease").write_text("4.4.302+\n")
            reader = host.HostReader(proc, "")
            first = reader.read()
            Path(proc, "stat").write_text(STAT_LATER)
            second = reader.read()
        self.assertIsNone(first["cpu_pct"])
        self.assertEqual(second["cpu_pct"], 45.0)
        self.assertEqual(second["load_per_core"], 2.0)  # the 5-minute figure over 2 CPUs
        self.assertEqual(second["uptime_seconds"], 3600)
        self.assertIsNone(second["pressure"])  # missing, not zero
        self.assertEqual(second["kernel"], "4.4.302+")  # says why pressure is missing

    def test_no_proc_is_no_reading(self):
        self.assertIsNone(host.HostReader("/nonexistent", "").read())


def reading(**overrides):
    base = {"cores": 4, "cpu_pct": 10.0, "iowait_pct": 1.0, "load": [0.5, 0.5, 0.5],
            "load_per_core": 0.1, "mem_total": 8 << 30, "mem_available": 4 << 30,
            "mem_available_pct": 50.0, "swap_in_per_s": 0.0, "disks": [], "temperatures": []}
    return {**base, **overrides}


class Rules(unittest.TestCase):
    def setUp(self):
        self.detector = Detector(Settings(host_breach_polls=3))

    def poll(self, value, times):
        out = []
        for _ in range(times):
            out.append(self.detector.check_host(value))
        return out

    def test_fires_once_when_it_has_lasted_then_keeps_it_open_quietly(self):
        low = reading(mem_available_pct=3.0, mem_available=240 << 20)
        results = self.poll(low, 5)
        self.assertEqual([len(s) for s, _ in results[:2]], [0, 0])
        fired = results[2][0][0]
        self.assertEqual((fired.kind, fired.severity, fired.unchanged), ("host_memory", "warning", False))
        self.assertEqual(fired.container["name"], "host:system")
        self.assertTrue(all(s[0].unchanged for s, _ in results[3:]))

    def test_very_low_memory_is_critical(self):
        signals, _ = self.poll(reading(mem_available_pct=2.0), 3)[-1]
        self.assertEqual(signals[0].severity, "critical")

    def test_clears_after_lasting_as_long_back_to_normal(self):
        self.poll(reading(iowait_pct=60.0), 3)
        results = self.poll(reading(), 3)
        self.assertEqual([("host_iowait" in c) for _, c in results], [False, False, True])

    def test_a_blip_does_not_fire(self):
        self.poll(reading(load_per_core=3.0, load=[12, 12, 12]), 2)
        self.poll(reading(), 1)
        signals, _ = self.poll(reading(load_per_core=3.0, load=[12, 12, 12]), 2)[-1]
        self.assertEqual(signals, [])

    def test_swap_in_use_is_fine_swap_read_back_is_not(self):
        self.assertNotIn("host_swap", self.detector.host_conditions(reading(swap_used_pct=80.0)))
        self.assertIn("host_swap", self.detector.host_conditions(reading(swap_in_per_s=900.0)))

    def test_temperature_uses_the_sensor_critical_point(self):
        hot = reading(temperatures=[{"name": "coretemp", "label": "Package", "celsius": 92.0,
                                     "critical": 90.0}])
        severity, summary, _ = self.detector.host_conditions(hot)["host_temperature"]
        self.assertEqual(severity, "critical")
        self.assertIn("92", summary)

    def test_host_incidents_never_lead_to_an_action(self):
        policy = Policy(Settings(mode="auto"), store=None)
        decision = policy.decide(
            incident={"container_name": "host:system", "kind": "host_memory", "fingerprint": "f"},
            diagnosis={"recommended_action": "restart_container", "confidence": 0.99},
            container_policy=SimpleNamespace(enabled=True, mode=None, actions=None,
                                             max_actions_per_hour=None),
        )
        self.assertEqual((decision.action, decision.status), ("notify_only", "skipped"))


class History(unittest.TestCase):
    def test_a_bucket_averages_its_polls_and_keeps_the_peak(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.record_host_sample(reading(cpu_pct=10.0, mem_used_pct=40.0), bucket_minutes=60)
        store.record_host_sample(reading(cpu_pct=30.0, mem_used_pct=None), bucket_minutes=60)
        [row] = store.host_history(datetime.now(timezone.utc) - timedelta(hours=2))
        self.assertEqual(row["cpu_pct"], 20.0)
        self.assertEqual(row["cpu_peak"], 30.0)
        self.assertEqual(row["mem_used_pct"], 40.0)  # a missing reading does not drag it down

    def test_the_hottest_sensor_is_kept_per_bucket(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        sensors = [{"name": "k10temp", "label": "temp1", "celsius": 60.0, "critical": None},
                   {"name": "acpitz", "label": "zone0", "celsius": 40.0, "critical": None}]
        store.record_host_sample(reading(temperatures=sensors), bucket_minutes=60)
        sensors[0]["celsius"] = 70.0
        store.record_host_sample(reading(temperatures=sensors), bucket_minutes=60)
        store.record_host_sample(reading(temperatures=[]), bucket_minutes=60)
        [row] = store.host_history(datetime.now(timezone.utc) - timedelta(hours=2))
        self.assertEqual(row["temp_c"], 65.0)

    def test_drive_temperatures_average_per_drive(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.record_drive_temps({"WD-1": 38.0, "WD-2": 41.0}, bucket_minutes=60)
        store.record_drive_temps({"WD-1": 40.0}, bucket_minutes=60)
        since = datetime.now(timezone.utc) - timedelta(hours=2)
        self.assertEqual([r["celsius"] for r in store.drive_temp_history("WD-1", since)], [39.0])
        self.assertEqual([r["celsius"] for r in store.drive_temp_history("WD-2", since)], [41.0])
        self.assertEqual(store.drive_temp_history("WD-3", since), [])

    def test_an_older_database_gains_the_temperature_column(self):
        import sqlite3
        path = Path(tempfile.mkdtemp(), "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE host_samples (bucket TEXT PRIMARY KEY, at TEXT NOT NULL, "
                     "polls INTEGER NOT NULL, cpu_pct REAL, cpu_peak REAL, iowait_pct REAL, "
                     "mem_used_pct REAL, swap_used_pct REAL, load1 REAL)")
        conn.commit()
        conn.close()
        store = Store(str(path))
        self.addCleanup(store.close)
        store.record_host_sample(reading(temperatures=[
            {"name": "k10temp", "label": "temp1", "celsius": 66.0, "critical": None}]))
        [row] = store.host_history(datetime.now(timezone.utc) - timedelta(hours=1))
        self.assertEqual(row["temp_c"], 66.0)


class Reboot(unittest.TestCase):
    def supervisor(self, store):
        from app.supervisor import Supervisor
        sup = Supervisor.__new__(Supervisor)
        sup.settings = Settings(public_url="")
        sup.store = store
        sup.broadcast = lambda *args: None
        sup.notifier = SimpleNamespace(notify_recovered=AsyncMock())
        return sup

    def test_a_later_boot_time_is_a_restart_said_once(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        sup = self.supervisor(store)
        boot = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)

        def seen(booted, at):
            return {"boot_time": booted.isoformat(), "at": at.isoformat()}

        asyncio.run(sup._notice_reboot(seen(boot, boot + timedelta(days=2))))
        # The same boot read again, a second off: not a restart.
        asyncio.run(sup._notice_reboot(seen(boot + timedelta(seconds=1), boot + timedelta(days=2, minutes=1))))
        sup.notifier.notify_recovered.assert_not_awaited()

        rebooted = boot + timedelta(days=2, minutes=10)
        asyncio.run(sup._notice_reboot(seen(rebooted, rebooted + timedelta(minutes=2))))
        sup.notifier.notify_recovered.assert_awaited_once()
        [change] = store.list_changes(container_name="host:system")
        self.assertEqual(change["kind"], "rebooted")
        self.assertIn("after 2d 0h up", change["summary"])

        asyncio.run(sup._notice_reboot(seen(rebooted, rebooted + timedelta(minutes=3))))
        self.assertEqual(sup.notifier.notify_recovered.await_count, 1)


if __name__ == "__main__":
    unittest.main()
