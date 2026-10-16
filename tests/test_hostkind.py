"""The host is any Linux; a Synology is told apart by its kernel.

What depends on it is DSM's own layout - md0 and md1 always showing empty
bays - so a Synology must still be recognised without any setting, and any
other host must have its md0 judged like every other array.
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import hostkind, raid  # noqa: E402
from app.redact import fingerprint  # noqa: E402
from app.store import Store  # noqa: E402

# md0 with one of its two members gone - DSM's normal, anyone else's outage.
HALF_MD0 = """Personalities : [raid1]
md0 : active raid1 sda1[0]
      2490176 blocks super 1.2 [2/1] [U_]

unused devices: <none>
"""


def proc(**files):
    root = tempfile.mkdtemp()
    for rel, text in files.items():
        path = os.path.join(root, *rel.split("__"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    return root


class Detecting(unittest.TestCase):
    def test_a_synology_names_its_model(self):
        found = hostkind.detect(proc(sys__kernel__syno_hw_version="DS920+\n"))
        self.assertEqual((found.kind, found.model, found.detected), ("synology", "DS920+", True))

    def test_any_other_kernel_is_generic(self):
        found = hostkind.detect(proc(version="Linux version 6.8.0-45-generic (buildd@lcy02)"))
        self.assertEqual((found.kind, found.model), ("generic", ""))

    def test_no_proc_at_all_is_generic(self):
        self.assertEqual(hostkind.detect("/nonexistent").kind, "generic")

    def test_the_setting_overrides_what_was_found(self):
        generic = hostkind.detect("/nonexistent")
        forced = hostkind.resolve("synology", generic)
        self.assertEqual((forced.kind, forced.detected), ("synology", False))
        self.assertIs(hostkind.resolve("auto", generic), generic)

    def test_how_the_host_is_described_and_called(self):
        syno = hostkind.Host("synology", "DS920+", True)
        self.assertEqual(hostkind.describe(syno, "tower"), "a Synology NAS (DS920+) called tower")
        self.assertEqual(hostkind.describe(hostkind.Host("generic", "", True)), "a Linux host")
        self.assertEqual(hostkind.called("", start=True), "The host")
        self.assertEqual(hostkind.called("tower", start=True), "tower")


class Md0(unittest.TestCase):
    def test_on_a_synology_an_empty_bay_in_md0_is_normal(self):
        [array] = raid.parse(HALF_MD0)
        self.assertTrue(raid.judge(array, most_seen=1).healthy)

    def test_anywhere_else_md0_is_degraded_like_any_array(self):
        [array] = raid.parse(HALF_MD0, frozenset())
        verdict = raid.judge(array, most_seen=1)
        self.assertFalse(verdict.healthy)
        self.assertIn("degraded", verdict.summary)


class RenamingTheHost(unittest.TestCase):
    def test_host_nas_rows_become_host_system_and_keep_folding(self):
        path = os.path.join(tempfile.mkdtemp(), "doctor.db")
        store = Store(path)
        incident_id = store.open_incident(
            container_id="", container_name="host:nas", image="", kind="host_memory",
            fingerprint=fingerprint("host:nas", "", "host_memory", "host_memory"),
            severity="warning", summary="The NAS has 3% of its memory available",
            evidence={}, mode="observe")
        store.record_change("host:nas", "", "rebooted", "2026-10-01T08:00:00+00:00",
                            "The NAS restarted", {})
        store.close()

        store = Store(path)
        self.addCleanup(store.close)
        incident = store.get_incident(incident_id)
        self.assertEqual(incident["container_name"], "host:system")
        self.assertEqual(incident["fingerprint"],
                         fingerprint("host:system", "", "host_memory", "host_memory"))
        self.assertEqual(len(store.list_changes(container_name="host:system")), 1)
        self.assertEqual(store.list_changes(container_name="host:nas"), [])


if __name__ == "__main__":
    unittest.main()
