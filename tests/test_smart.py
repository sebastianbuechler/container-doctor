"""Drive health from the SMART sidecar.

The fixture is real output of prometheuscommunity/smartctl-exporter v0.14.0,
fed smartctl JSON for a healthy WD Red (sata1), a worn one with reallocated
and pending sectors and CRC errors (sata2), and a heavily written NVMe drive.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import smart  # noqa: E402
from app.config import Settings  # noqa: E402
from app.remediator import Policy  # noqa: E402
from app.store import Store  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixtures" / "smartctl_exporter.prom").read_text()


def assess(drive, crc_before=0):
    return smart.assess(drive, crc_before=crc_before, temp_limit=55, nvme_temp_limit=70,
                        wear_limit=90)


class Parsing(unittest.TestCase):
    def setUp(self):
        self.drives = {d["device"]: d for d in smart.drives(smart.parse_metrics(FIXTURE))}

    def test_every_drive_with_its_identity(self):
        self.assertEqual(sorted(self.drives), ["nvme0n1", "sata1", "sata2"])
        sata2 = self.drives["sata2"]
        self.assertEqual((sata2["label"], sata2["model"], sata2["serial"]),
                         ("Drive 2", "WDC WD40EFZX-68AWUN0", "WD-WX67890"))
        self.assertEqual(sata2["family"], "Western Digital Red Plus")
        self.assertEqual(self.drives["nvme0n1"]["family"], "")  # "unknown" is not a family

    def test_raw_counters_not_normalised_values(self):
        sata2 = self.drives["sata2"]
        self.assertEqual(sata2["counters"]["Reallocated_Sector_Ct"], 24)
        self.assertEqual(sata2["counters"]["Current_Pending_Sector"], 8)
        self.assertEqual(sata2["crc_errors"], 3)

    def test_health_temperature_and_age(self):
        sata1 = self.drives["sata1"]
        self.assertTrue(sata1["passed"])
        self.assertEqual(sata1["temperature"], 34)
        self.assertEqual(sata1["power_on_hours"], 30120)
        self.assertFalse(sata1["asleep"])

    def test_nvme_health_log(self):
        nvme = self.drives["nvme0n1"]["nvme"]
        self.assertEqual((nvme["percentage_used"], nvme["available_spare"], nvme["media_errors"]),
                         (93, 100, 0))

    def test_what_a_hard_disk_has_been_through(self):
        sata1 = self.drives["sata1"]
        self.assertEqual((sata1["rotation_rpm"], sata1["link_bps"], sata1["error_log"]),
                         (5400, 6e9, 0))
        # No lifetime data from a WD Red: the exporter's bytes_* are NVMe's.
        self.assertEqual((sata1["bytes_written"], sata1["wear_pct"]), (None, None))

    def test_nvme_lifetime_data_and_wear(self):
        nvme = self.drives["nvme0n1"]
        self.assertEqual((nvme["bytes_written"], nvme["bytes_read"], nvme["wear_pct"],
                          nvme["error_log"]), (1200987136000, 632098304000, 93, 2))

    def test_a_sata_ssd_from_its_vendor_attributes(self):
        attribute = ('smartctl_device_attribute{{attribute_name="{0}",attribute_value_type="{1}",'
                     'device="sata7"}} {2}\n')
        [drive] = smart.drives(smart.parse_metrics(
            'smartctl_device{device="sata7",model_name="Samsung SSD 870 EVO 4TB",protocol="ATA"} 1\n'
            'smartctl_device_rotation_rate{device="sata7"} 0\n'
            'smartctl_device_bytes_written{device="sata7"} 0\n'
            'smartctl_device_block_size{blocks_type="logical",device="sata7"} 512\n'
            + attribute.format("Total_LBAs_Written", "raw", "2.5e+10")
            + attribute.format("Total_LBAs_Read", "raw", "1e+10")
            + attribute.format("Wear_Leveling_Count", "raw", "41")
            + attribute.format("Wear_Leveling_Count", "value", "97")
            + attribute.format("Load_Cycle_Count", "raw", "0")))
        self.assertEqual((drive["bytes_written"], drive["bytes_read"], drive["wear_pct"],
                          drive["rotation_rpm"]), (25e9 * 512, 10e9 * 512, 3, 0))

    def test_writes_counted_in_the_unit_the_attribute_names(self):
        [drive] = smart.drives(smart.parse_metrics(
            'smartctl_device_attribute{attribute_name="Host_Writes_32MiB",'
            'attribute_value_type="raw",device="sda"} 1000\n'))
        self.assertEqual(drive["bytes_written"], 1000 * 32 * 1024 ** 2)

    def test_a_drive_asleep_is_not_a_failure(self):
        [drive] = smart.drives(smart.parse_metrics(
            'smartctl_device_smartctl_exit_status{device="sata3"} 2\n'))
        self.assertTrue(drive["asleep"])
        self.assertEqual(assess(drive), ("", []))

    def test_how_often_a_hard_disk_spun_up(self):
        [drive] = smart.drives(smart.parse_metrics(
            'smartctl_device_attribute{attribute_name="Start_Stop_Count",'
            'attribute_value_type="raw",device="sata3"} 1234\n'
            'smartctl_device_attribute{attribute_name="Start_Stop_Count",'
            'attribute_value_type="value",device="sata3"} 99\n'))
        self.assertEqual(drive["spin_ups"], 1234)

    def test_only_spinning_disks_can_sleep(self):
        self.assertTrue(smart.can_sleep(self.drives["sata1"]))
        self.assertFalse(smart.can_sleep(self.drives["nvme0n1"]))
        self.assertFalse(smart.can_sleep({**self.drives["sata1"], "rotation_rpm": 0}))
        # Asleep, a drive says nothing about its rotation - but it is a disk.
        [asleep] = smart.drives(smart.parse_metrics(
            'smartctl_device_smartctl_exit_status{device="sata3"} 2\n'))
        self.assertTrue(smart.can_sleep(asleep))

    def test_a_sata_drive_read_as_scsi_has_no_data_not_0_degrees(self):
        # sata7 on the NAS before the sidecar forced `;sat`: a WD Red that
        # smartctl took for a SCSI disk from vendor "WDC".
        [drive] = smart.drives(smart.parse_metrics(
            'smartctl_device{device="sata7",interface="scsi",model_family="unknown",'
            'model_name="WDC WD60EFRX-68L0BN1",protocol="SCSI",scsi_vendor="WDC",'
            'serial_number="WD-X"} 1\n'
            'smartctl_device_smartctl_exit_status{device="sata7"} 0\n'
            'smartctl_device_temperature{device="sata7",temperature_type="current"} 0\n'
            'smartctl_device_temperature{device="sata7",temperature_type="drive_trip"} 0\n'))
        self.assertEqual((drive["passed"], drive["temperature"], drive["counters"], drive["asleep"]),
                         (None, None, {}, False))
        self.assertEqual(assess(drive), ("", []))

    def test_escaped_label_values(self):
        [(_, labels, value)] = smart.parse_metrics('m{a="x \\"y\\"",b="z"} 1.5e+02\n')
        self.assertEqual((labels["a"], value), ('x "y"', 150.0))


class Assessment(unittest.TestCase):
    def setUp(self):
        self.drives = {d["device"]: d for d in smart.drives(smart.parse_metrics(FIXTURE))}

    def test_healthy_drive(self):
        self.assertEqual(assess(self.drives["sata1"]), ("", []))

    def test_bad_sectors_are_a_warning(self):
        severity, problems = assess(self.drives["sata2"], crc_before=3)
        self.assertEqual(severity, "warning")
        self.assertEqual(problems, ["24 reallocated, 8 pending sectors"])

    def test_only_new_crc_errors_count(self):
        _, old = assess(self.drives["sata2"], crc_before=3)
        _, new = assess(self.drives["sata2"], crc_before=1)
        self.assertFalse(any("CRC" in p for p in old))
        self.assertIn("2 new interface CRC errors", new[-1])

    def test_ssd_wear(self):
        self.assertEqual(assess(self.drives["nvme0n1"]),
                         ("warning", ["93% of its rated write endurance used"]))

    def test_a_worn_sata_ssd_is_a_warning_too(self):
        drive = {**self.drives["sata1"], "rotation_rpm": 0, "wear_pct": 92.0}
        self.assertEqual(assess(drive), ("warning", ["92% of its rated write endurance used"]))

    def test_failing_verdict_is_critical(self):
        drive = {**self.drives["sata1"], "passed": False}
        self.assertEqual(assess(drive)[0], "critical")

    def test_spare_below_threshold_is_critical(self):
        nvme = self.drives["nvme0n1"]
        drive = {**nvme, "nvme": {**nvme["nvme"], "available_spare": 8, "percentage_used": 10}}
        severity, problems = assess(drive)
        self.assertEqual(severity, "critical")
        self.assertIn("spare capacity down to 8%", problems[0])

    def test_hot_drive(self):
        drive = {**self.drives["sata1"], "temperature": 58}
        self.assertEqual(assess(drive), ("warning", ["at 58 °C"]))

    def test_smart_incidents_never_lead_to_an_action(self):
        decision = Policy(Settings(mode="auto"), store=None).decide(
            incident={"container_name": "host:Drive 2", "kind": "smart_health", "fingerprint": "f"},
            diagnosis={"recommended_action": "restart_container", "confidence": 0.99},
            container_policy=SimpleNamespace(enabled=True, mode=None, actions=None,
                                             max_actions_per_hour=None),
        )
        self.assertEqual((decision.action, decision.status), ("notify_only", "skipped"))


class Sidecar(BaseHTTPRequestHandler):
    body = FIXTURE

    def log_message(self, *args):
        pass

    def do_GET(self):
        data = self.body.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class SidecarCase(unittest.TestCase):
    """A supervisor reading the fixture from a sidecar on a local port."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Sidecar)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def supervisor(self):
        from app.supervisor import Supervisor
        sup = Supervisor.__new__(Supervisor)
        sup.settings = Settings(
            smart_url=f"http://127.0.0.1:{self.server.server_address[1]}/metrics", public_url="")
        sup.store = Store(":memory:")
        self.addCleanup(sup.store.close)
        sup.smart = None
        sup._smart_read_at = 0.0
        sup._smart_said = {}
        sup._drive_ids = {}
        sup._cold_seen = {}
        sup.broadcast = lambda *args: None
        sup.notifier = SimpleNamespace(notify_recovered=AsyncMock())
        return sup

    def read(self, sup):
        sup.smart = None  # past the once-a-minute limit
        return asyncio.run(sup._check_smart())


class Supervision(SidecarCase):
    """The supervisor reading the sidecar: signals, CRC baselines, recovery."""

    def test_signals_for_the_drives_with_problems(self):
        sup = self.supervisor()
        signals = self.read(sup)
        self.assertEqual(sorted(s.container["name"] for s in signals),
                         ["host:Drive 2", "host:nvme0n1"])
        drive2 = next(s for s in signals if s.container["name"] == "host:Drive 2")
        self.assertEqual(drive2.kind, "smart_health")
        self.assertIn("24 reallocated, 8 pending sectors", drive2.summary)
        # The CRC count seen first is the baseline, not a problem.
        self.assertNotIn("CRC", drive2.summary)
        self.assertFalse(drive2.unchanged)
        self.assertTrue(all(s.unchanged for s in self.read(sup)))

    def test_a_sidecar_that_is_not_there_is_a_note_not_a_failure(self):
        sup = self.supervisor()
        sup.settings = Settings(smart_url="http://127.0.0.1:9/metrics")
        self.assertEqual(asyncio.run(sup._check_smart()), [])
        self.assertTrue(sup.smart["error"])

    def test_a_drive_that_recovers_closes_its_incident(self):
        sup = self.supervisor()
        incident_id = sup.store.open_incident(
            container_id="", container_name="host:Drive 1", image="", kind="smart_health",
            fingerprint="f1", severity="warning", summary="Drive 1: 2 pending sectors",
            evidence={}, mode="observe", rule_severity="warning", lines=[])
        self.read(sup)
        self.assertEqual(sup.store.get_incident(incident_id)["status"], "resolved")
        sup.notifier.notify_recovered.assert_awaited_once()


ASLEEP = 'smartctl_device_smartctl_exit_status{device="sata1"} 2\n'


class Sleep(SidecarCase):
    """Whether each hard disk slept, kept per bucket, and read back as a bar."""

    def asleep(self):
        Sidecar.body = ASLEEP
        self.addCleanup(setattr, Sidecar, "body", FIXTURE)

    def since(self):
        return datetime.now(timezone.utc) - timedelta(hours=1)

    def test_hard_disks_are_recorded_ssds_are_not(self):
        sup = self.supervisor()
        self.read(sup)
        self.assertEqual(sup.store.drive_sleep_history("WD-WX12345", self.since())[0]["asleep"], 0)
        self.assertEqual(len(sup.store.drive_sleep_history("WD-WX67890", self.since())), 1)
        nvme = next(d for d in sup.smart["drives"] if d["device"] == "nvme0n1")
        self.assertEqual(sup.store.drive_sleep_history(nvme["serial"], self.since()), [])

    def test_a_drive_asleep_keeps_its_name_and_its_history(self):
        sup = self.supervisor()
        self.read(sup)
        self.asleep()
        self.read(sup)
        [drive] = sup.smart["drives"]
        self.assertTrue(drive["asleep"])
        self.assertEqual((drive["serial"], drive["model"], drive["label"]),
                         ("WD-WX12345", "WDC WD40EFZX-68AWUN0", "Drive 1"))
        [row] = sup.store.drive_sleep_history("WD-WX12345", self.since())
        self.assertEqual((row["polls"], row["asleep"]), (2, 1))
        # Not its temperature: asleep, it has none.
        self.assertEqual(len(sup.store.drive_temp_history("WD-WX12345", self.since())), 1)

    def test_counters_are_kept_from_drives_awake_only(self):
        sup = self.supervisor()
        self.read(sup)
        [row] = sup.store.drive_counter_history("WD-WX67890", self.since())
        self.assertEqual((row["counters"]["reallocated"], row["counters"]["pending"]), (24, 8))
        self.asleep()
        self.read(sup)
        # The hour's reading is still the one taken awake.
        [row] = sup.store.drive_counter_history("WD-WX12345", self.since())
        self.assertEqual(row["counters"]["reallocated"], 0)

    def test_who_a_drive_is_survives_a_restart(self):
        sup = self.supervisor()
        self.read(sup)
        sup._drive_ids = {}  # what a restart forgets; the store keeps it
        self.asleep()
        self.read(sup)
        self.assertEqual(sup.smart["drives"][0]["serial"], "WD-WX12345")

    def test_a_drive_never_seen_awake_is_filed_under_its_device(self):
        sup = self.supervisor()
        self.asleep()
        self.read(sup)
        self.assertEqual(sup.smart["drives"][0]["serial"], "")
        self.assertEqual(len(sup.store.drive_sleep_history("sata1", self.since())), 1)

    def test_how_long_it_slept_and_how_often_it_woke(self):
        sup = self.supervisor()
        start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=2)
        # Asleep, woken, asleep again, then a gap - the doctor down - before
        # it is next seen awake: one wake, not two.
        pattern = [(4, 4), (4, 4), (4, 0), (4, 3), (4, 4), None, (4, 0), (4, 0)]
        for i, reads in enumerate(pattern):
            if reads is None:
                continue
            t = start + timedelta(minutes=5 * i)
            sup.store._conn.execute(
                "INSERT INTO drive_sleep (drive, bucket, at, polls, asleep) VALUES (?,?,?,?,?)",
                ("WD-1", f"{t:%Y-%m-%dT%H:%M}", t.isoformat(timespec="seconds"), *reads))
        history = sup.drive_sleep_history("WD-1", 24)
        self.assertEqual((history["wakes"], history["asleep_pct"]), (1, 57))
        self.assertEqual([share for _, share in history["samples"]], [1.0, 1.0, 0.0, 0.75, 1.0, 0.0, 0.0])
        self.assertEqual(sup.drive_sleep_history("WD-2", 24)["asleep_pct"], None)

    def test_old_readings_are_pruned(self):
        sup = self.supervisor()
        old = datetime.now(timezone.utc) - timedelta(days=40)
        sup.store._conn.execute(
            "INSERT INTO drive_sleep (drive, bucket, at, polls, asleep) VALUES ('WD-1', ?, ?, 1, 1)",
            (f"{old:%Y-%m-%dT%H:%M}", old.isoformat(timespec="seconds")))
        sup.store.prune(30)
        self.assertEqual(sup.store.drive_sleep_history("WD-1", old - timedelta(days=1)), [])


# Drive 1 of the fixture, as an SSD in an expansion unit: sata5, its own serial.
COLD = "".join(line.replace('device="sata1"', 'device="sata5"').replace("WD-WX12345", "SSD-COLD-1") + "\n"
               for line in FIXTURE.splitlines() if 'device="sata1"' in line)


class ColdSidecar(Sidecar):
    body = COLD


class ColdDrives(SidecarCase):
    """Drives read by the second, slow reader: merged in, recorded only when
    their reading is new, and never asked about sleep."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cold_server = HTTPServer(("127.0.0.1", 0), ColdSidecar)
        threading.Thread(target=cls.cold_server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.cold_server.shutdown()
        cls.cold_server.server_close()
        super().tearDownClass()

    def supervisor(self, cold_url=None):
        sup = super().supervisor()
        sup.settings = Settings(
            smart_url=sup.settings.smart_url, public_url="",
            smart_cold_devices=["/dev/sata5"], smart_cold_hours=24,
            smart_cold_url=cold_url or f"http://127.0.0.1:{self.cold_server.server_address[1]}/metrics")
        return sup

    def since(self):
        return datetime.now(timezone.utc) - timedelta(hours=1)

    def cold(self, sup):
        return next(d for d in sup.smart["drives"] if d["device"] == "sata5")

    def test_merged_in_order_and_marked_cold(self):
        sup = self.supervisor()
        self.read(sup)
        self.assertEqual([d["device"] for d in sup.smart["drives"]], ["sata1", "sata2", "sata5", "nvme0n1"])
        cold = self.cold(sup)
        self.assertTrue(cold["cold"])
        self.assertTrue(cold["read_at"])
        self.assertFalse(any(d.get("cold") for d in sup.smart["drives"] if d["device"] != "sata5"))
        self.assertEqual(sup.smart["cold"], {"hours": 24, "error": None})

    def test_a_reading_served_again_is_not_recorded_again(self):
        sup = self.supervisor()
        self.read(sup)
        self.read(sup)
        [row] = sup.store.drive_temp_history("SSD-COLD-1", self.since())
        self.assertEqual(row["celsius"], 34)
        # Drive 1 is read every time: two polls in one bucket.
        self.assertEqual(sup.store._conn.execute(
            "SELECT polls FROM drive_temps WHERE drive='SSD-COLD-1'").fetchone()[0], 1)
        self.assertEqual(sup.store._conn.execute(
            "SELECT polls FROM drive_temps WHERE drive='WD-WX12345'").fetchone()[0], 2)

    def test_a_new_reading_is_recorded(self):
        sup = self.supervisor()
        self.read(sup)
        first = self.cold(sup)["read_at"]
        ColdSidecar.body = COLD.replace('temperature_type="current"} 34', 'temperature_type="current"} 36')
        self.addCleanup(setattr, ColdSidecar, "body", COLD)
        self.read(sup)
        self.assertTrue(self.cold(sup)["fresh"])
        self.assertGreaterEqual(self.cold(sup)["read_at"], first)
        self.assertEqual(sup.store._conn.execute(
            "SELECT polls FROM drive_temps WHERE drive='SSD-COLD-1'").fetchone()[0], 2)

    def test_when_it_was_read_survives_a_restart(self):
        sup = self.supervisor()
        self.read(sup)
        first = self.cold(sup)["read_at"]
        sup._cold_seen = {}
        self.read(sup)
        self.assertEqual((self.cold(sup)["fresh"], self.cold(sup)["read_at"]), (False, first))

    def test_its_sleep_is_not_tracked(self):
        sup = self.supervisor()
        self.read(sup)
        self.assertFalse(smart.can_sleep(self.cold(sup)))
        self.assertEqual(sup.store.drive_sleep_history("SSD-COLD-1", self.since()), [])

    def test_a_cold_reader_not_answering_leaves_the_rest(self):
        sup = self.supervisor(cold_url="http://127.0.0.1:9/metrics")
        self.read(sup)
        self.assertEqual([d["device"] for d in sup.smart["drives"]], ["sata1", "sata2", "nvme0n1"])
        self.assertTrue(sup.smart["cold"]["error"])
        self.assertIsNone(sup.smart["error"])

    def test_without_cold_drives_nothing_is_asked(self):
        sup = SidecarCase.supervisor(self)
        self.read(sup)
        self.assertIsNone(sup.smart["cold"])


if __name__ == "__main__":
    unittest.main()
