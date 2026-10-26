"""The SMART sidecar's entrypoint, run for real: which drives each of its two
readers is given. The exporter is a stand-in that writes down its arguments,
and the drives are files in a temporary /dev - the script's own paths are
moved there, the same way as the ones it is given."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def entrypoint() -> str:
    service = yaml.safe_load((ROOT / "compose.example.yaml").read_text())["services"]["doctor-smart"]
    return service["entrypoint"][-1]


@unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
class Readers(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "dev").mkdir()
        self.calls = self.root / "calls"
        exporter = self.root / "smartctl_exporter"
        exporter.write_text(f'#!/bin/sh\necho "$@" >> {self.calls}\n')
        exporter.chmod(0o755)
        script = (entrypoint().replace("$$", "$")       # Compose's escaping
                  .replace("/dev/", f"{self.root}/dev/")
                  .replace("[ -b ", "[ -e ")             # files, not block devices
                  .replace("/bin/smartctl_exporter", str(exporter))
                  .replace("sleep 60", "break"))         # one round of the restart loop
        self.script = self.root / "run.sh"
        self.script.write_text(script)

    def drives(self, *names):
        for name in names:
            (self.root / "dev" / name).touch()

    def run_with(self, **env):
        env = {k: v.replace("/dev/", f"{self.root}/dev/") for k, v in env.items()}
        subprocess.run(["sh", str(self.script)], env={"PATH": os.environ["PATH"], **env},
                       check=True, timeout=10)
        # The cold reader runs in the background; give it its moment.
        for _ in range(50):
            if self.calls.exists() and len(self.calls.read_text().splitlines()) >= self.expected:
                break
            time.sleep(0.02)
        calls = self.calls.read_text().replace(f"{self.root}", "").splitlines()
        main = next(c for c in calls if "--smartctl.interval=5m" in c)
        cold = next((c for c in calls if ":9634" in c), None)
        return main, cold

    def test_found_drives_minus_the_cold_ones(self):
        self.drives("sata1", "sata2", "sata5", "sata6", "nvme0n1")
        self.expected = 2
        main, cold = self.run_with(SMART_COLD_DEVICES="/dev/sata5,/dev/sata6")
        self.assertEqual(main, "--smartctl.device=/dev/sata1;sat --smartctl.device=/dev/sata2;sat "
                               "--smartctl.device=/dev/nvme0n1 --smartctl.interval=5m "
                               "--smartctl.powermode-check=standby")
        self.assertEqual(cold, "--smartctl.device=/dev/sata5;sat --smartctl.device=/dev/sata6;sat "
                               "--web.listen-address=:9634 --smartctl.interval=24h "
                               "--smartctl.powermode-check=never")

    def test_no_cold_drives_no_second_reader(self):
        self.drives("sata1", "sata5")
        self.expected = 1
        main, cold = self.run_with()
        self.assertIn("/dev/sata5;sat", main)
        self.assertIsNone(cold)

    def test_listed_drives_with_their_type_and_another_interval(self):
        self.expected = 2
        main, cold = self.run_with(SMART_DEVICES="/dev/sata1;sat,/dev/sata5;sat",
                                   SMART_COLD_DEVICES="/dev/sata5;sat", SMART_COLD_HOURS="6")
        self.assertEqual(main, "--smartctl.device=/dev/sata1;sat --smartctl.interval=5m "
                               "--smartctl.powermode-check=standby")
        self.assertIn("--smartctl.device=/dev/sata5;sat --web.listen-address=:9634 "
                      "--smartctl.interval=6h", cold)

    def test_all_cold_the_everyday_reader_scans_for_nothing(self):
        # Left without a drive, the exporter scans - and would read them all.
        self.drives("sata5")
        self.expected = 2
        main, cold = self.run_with(SMART_COLD_DEVICES="/dev/sata5")
        self.assertTrue(main.startswith("--smartctl.device-include=^$ "))
        self.assertIn("/dev/sata5;sat", cold)

    def test_nothing_found_nothing_cold_scans(self):
        self.expected = 1
        main, cold = self.run_with()
        self.assertTrue(main.startswith("--smartctl.scan "))
        self.assertIsNone(cold)


if __name__ == "__main__":
    unittest.main()
