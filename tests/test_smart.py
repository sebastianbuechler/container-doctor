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

    def test_a_drive_asleep_is_not_a_failure(self):
        [drive] = smart.drives(smart.parse_metrics(
            'smartctl_device_smartctl_exit_status{device="sata3"} 2\n'))
        self.assertTrue(drive["asleep"])
        self.assertEqual(assess(drive), ("", []))

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


class Supervision(unittest.TestCase):
    """The supervisor reading the sidecar: signals, CRC baselines, recovery."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Sidecar)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

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
        sup.broadcast = lambda *args: None
        sup.notifier = SimpleNamespace(notify_recovered=AsyncMock())
        return sup

    def read(self, sup):
        sup.smart = None  # past the once-a-minute limit
        return asyncio.run(sup._check_smart())

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


if __name__ == "__main__":
    unittest.main()
