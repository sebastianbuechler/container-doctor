"""A degraded array loses data on the next failure, and runs perfectly until then.

Nothing else the doctor watches can see it - every container carries on - so
these cover reading /proc/mdstat the way DSM writes it, and telling a missing
disk apart from the empty slots DSM's own system arrays always show.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import raid  # noqa: E402

# A two-disk SHR volume in a four-bay unit, as DSM builds it: md0 and md1 are
# sized for four bays and so always show two empty slots.
HEALTHY = """Personalities : [raid1] [linear] [raid0] [raid10] [raid6] [raid5] [raid4]
md2 : active raid1 sata1p5[0] sata2p5[1]
      3902196544 blocks super 1.2 [2/2] [UU]

md1 : active raid1 sata1p2[0] sata2p2[1]
      2097088 blocks [4/2] [UU__]

md0 : active raid1 sata1p1[0] sata2p1[1]
      2490176 blocks [4/2] [UU__]

unused devices: <none>
"""

DEGRADED = """Personalities : [raid1]
md2 : active raid1 sata1p5[0]
      3902196544 blocks super 1.2 [2/1] [U_]

md1 : active raid1 sata1p2[0]
      2097088 blocks [4/1] [U___]

md0 : active raid1 sata1p1[0]
      2490176 blocks [4/1] [U___]

unused devices: <none>
"""

FAILED = """Personalities : [raid5]
md2 : active raid5 sata1p5[0] sata4p5[3] sata3p5[2](F) sata2p5[1]
      8776306368 blocks super 1.2 level 5, 64k chunk, algorithm 2 [4/3] [UU_U]

unused devices: <none>
"""

REBUILDING = """Personalities : [raid1]
md2 : active raid1 sata2p5[2] sata1p5[0]
      3902196544 blocks super 1.2 [2/1] [U_]
      [=>...................]  recovery =  8.5% (331686400/3902196544) finish=312.4min speed=190424K/sec

md1 : active raid1 sata1p2[0] sata2p2[1]
      2097088 blocks [4/2] [UU__]

md0 : active raid1 sata1p1[0] sata2p1[1]
      2490176 blocks [4/2] [UU__]

unused devices: <none>
"""

SCRUBBING = """Personalities : [raid5]
md2 : active raid5 sata1p5[0] sata3p5[2] sata2p5[1]
      7804392448 blocks super 1.2 level 5, 64k chunk, algorithm 2 [3/3] [UUU]
      [====>................]  check = 22.1% (863157248/3902196224) finish=280.0min speed=180840K/sec

unused devices: <none>
"""

QUEUED = """Personalities : [raid1]
md3 : active raid1 sata4p5[2] sata3p5[0]
      1948779648 blocks super 1.2 [2/1] [U_]
        resync=DELAYED

unused devices: <none>
"""

INACTIVE = """Personalities :
md4 : inactive sata3p5[0](S)
      1948779648 blocks super 1.2

unused devices: <none>
"""


def by_name(text: str) -> dict[str, raid.Array]:
    return {array.name: array for array in raid.parse(text)}


class ReadingMdstat(unittest.TestCase):
    def test_every_array_is_found(self):
        self.assertEqual(sorted(by_name(HEALTHY)), ["md0", "md1", "md2"])

    def test_members_and_status(self):
        md2 = by_name(HEALTHY)["md2"]
        self.assertEqual((md2.level, md2.state), ("raid1", "active"))
        self.assertEqual(md2.members, ["sata1p5", "sata2p5"])
        self.assertEqual((md2.slots, md2.active, md2.map), (2, 2, "UU"))

    def test_a_failed_member_is_set_apart(self):
        md2 = by_name(FAILED)["md2"]
        self.assertEqual(md2.failed, ["sata3p5"])
        self.assertNotIn("sata3p5", md2.members)

    def test_a_rebuild_reads_its_progress_and_eta(self):
        md2 = by_name(REBUILDING)["md2"]
        self.assertEqual(md2.operation, "recovery")
        self.assertAlmostEqual(md2.progress, 8.5)
        self.assertAlmostEqual(md2.finish_minutes, 312.4)

    def test_a_scrub_is_an_operation_too(self):
        self.assertEqual(by_name(SCRUBBING)["md2"].operation, "check")

    def test_a_queued_resync_has_no_progress(self):
        md3 = by_name(QUEUED)["md3"]
        self.assertEqual((md3.operation, md3.progress), ("resync", None))

    def test_a_spare_on_an_inactive_array(self):
        md4 = by_name(INACTIVE)["md4"]
        self.assertEqual((md4.state, md4.spares), ("inactive", ["sata3p5"]))

    def test_nonsense_is_not_an_error(self):
        self.assertEqual(raid.parse("garbage\n\n: [x]\n"), [])
        self.assertEqual(raid.parse(""), [])


class JudgingAnArray(unittest.TestCase):
    def verdict(self, text, name, most_seen=0):
        return raid.judge(by_name(text)[name], most_seen)

    def test_a_healthy_volume_is_healthy(self):
        for name in ("md0", "md1", "md2"):
            with self.subTest(name):
                self.assertTrue(self.verdict(HEALTHY, name, most_seen=2).healthy)

    def test_system_arrays_are_not_degraded_by_their_empty_bays(self):
        """[4/2] [UU__] is every two-disk four-bay Synology, permanently."""
        self.assertTrue(self.verdict(HEALTHY, "md0", most_seen=2).healthy)

    def test_a_data_array_missing_a_disk_is_critical(self):
        verdict = self.verdict(DEGRADED, "md2", most_seen=2)
        self.assertFalse(verdict.healthy)
        self.assertEqual(verdict.severity, "critical")
        self.assertIn("1 of 2", verdict.summary)

    def test_a_system_array_that_lost_a_disk_is_critical(self):
        verdict = self.verdict(DEGRADED, "md0", most_seen=2)
        self.assertEqual(verdict.severity, "critical")
        self.assertIn("1 of the 2", verdict.summary)

    def test_a_system_array_seen_for_the_first_time_is_given_the_benefit(self):
        """No history means no way to tell an empty bay from a lost disk -
        md2 carries the alarm in that case."""
        self.assertTrue(self.verdict(DEGRADED, "md0", most_seen=1).healthy)

    def test_a_failed_member_is_critical_whatever_the_counts(self):
        verdict = self.verdict(FAILED, "md2", most_seen=4)
        self.assertEqual(verdict.severity, "critical")
        self.assertIn("sata3p5", verdict.summary)

    def test_a_rebuild_is_a_warning_with_its_eta(self):
        verdict = self.verdict(REBUILDING, "md2", most_seen=2)
        self.assertEqual(verdict.severity, "warning")
        self.assertIn("8.5% done", verdict.summary)
        self.assertIn("5 h", verdict.summary)

    def test_a_scrub_is_routine(self):
        self.assertTrue(self.verdict(SCRUBBING, "md2", most_seen=3).healthy)

    def test_a_queued_resync_of_a_degraded_array_is_still_degraded(self):
        self.assertFalse(self.verdict(QUEUED, "md3", most_seen=2).healthy)

    def test_an_inactive_array_is_critical(self):
        self.assertEqual(self.verdict(INACTIVE, "md4").severity, "critical")


class ReadingTheFile(unittest.TestCase):
    def test_no_file_is_a_reason(self):
        arrays, reason = raid.read("/nonexistent/mdstat")
        self.assertEqual(arrays, [])
        self.assertIn("does not exist", reason)

    def test_off_is_off(self):
        self.assertEqual(raid.read(""), ([], "off"))


class VersionOnlyDaemon(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"Version": "27.0", "ApiVersion": "1.45"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThroughTheSupervisor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), VersionOnlyDaemon)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.mdstat = os.path.join(self.data, "mdstat")

    def write(self, text):
        with open(self.mdstat, "w", encoding="utf-8") as handle:
            handle.write(text)

    def supervisor(self):
        from app.config import Settings
        from app.supervisor import Supervisor

        previous = dict(os.environ)
        os.environ.update(
            DOCTOR_DATA_DIR=self.data, DOCTOR_MODE="auto", ANTHROPIC_API_KEY="",
            DOCTOR_MDSTAT_PATH=self.mdstat,
            # The fixtures are a Synology's, md0 and md1 included.
            DOCTOR_HOST_KIND="synology",
            DOCTOR_PUBLIC_URL="https://doctor.example",
            DOCKER_HOST=f"tcp://127.0.0.1:{self.server.server_address[1]}",
        )
        try:
            supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(supervisor.store.close)
        self.notices: list[tuple[str, str]] = []

        async def recovered(title, message, dashboard_url=""):
            self.notices.append((title, dashboard_url))

        supervisor.notifier.notify_recovered = recovered
        return supervisor

    def poll(self, supervisor):
        """The RAID part of a poll, and the incidents it leads to."""
        async def go():
            supervisor.raid = await asyncio.to_thread(supervisor._read_raid)
            for signal in supervisor.detector.check_raid(supervisor.raid["arrays"]):
                await supervisor._process_signal_inner(signal)
            await supervisor._resolve_recovered_arrays(supervisor.raid["arrays"])
        asyncio.run(go())

    def open_raid_incidents(self, supervisor):
        return [i for i in supervisor.store.list_incidents(limit=50, status="active")
                if i["kind"] == "raid_degraded"]

    def test_a_lost_disk_opens_one_incident_and_recovery_closes_it(self):
        supervisor = self.supervisor()
        self.write(HEALTHY)
        self.poll(supervisor)
        self.assertEqual(self.open_raid_incidents(supervisor), [])

        self.write(DEGRADED)
        self.poll(supervisor)
        self.poll(supervisor)  # still degraded: the same incident, seen again
        opened = self.open_raid_incidents(supervisor)
        # md2 and both system arrays each lost a disk
        self.assertEqual(sorted(i["container_name"] for i in opened),
                         ["host:md0", "host:md1", "host:md2"])
        self.assertTrue(all(i["occurrences"] == 2 for i in opened))

        self.write(REBUILDING)
        self.poll(supervisor)
        self.assertEqual([i["container_name"] for i in self.open_raid_incidents(supervisor)],
                         ["host:md2"])  # md0 and md1 came back first

        self.write(HEALTHY)
        self.poll(supervisor)
        self.assertEqual(self.open_raid_incidents(supervisor), [])
        self.assertIn("md2: healthy again", [title for title, _ in self.notices])
        self.assertTrue(all(url.startswith("https://doctor.example/#incident/")
                            for _, url in self.notices))

    def test_the_doctor_never_acts_on_an_array(self):
        supervisor = self.supervisor()
        self.write(HEALTHY)
        self.poll(supervisor)
        self.write(DEGRADED)
        self.poll(supervisor)
        for incident in self.open_raid_incidents(supervisor):
            actions = supervisor.store.list_actions(incident_id=incident["id"])
            self.assertTrue(actions)
            self.assertTrue(all(a["status"] == "skipped" for a in actions))

    def test_the_most_members_seen_survives_a_restart(self):
        """A disk lost while the doctor was down must not become the new normal."""
        first = self.supervisor()
        self.write(HEALTHY)
        self.poll(first)
        first.store.close()

        second = self.supervisor()
        self.write(DEGRADED)
        self.poll(second)
        self.assertIn("host:md0",
                      [i["container_name"] for i in self.open_raid_incidents(second)])

    def test_the_arrays_ride_along_in_the_snapshot(self):
        supervisor = self.supervisor()
        self.write(REBUILDING)
        self.poll(supervisor)
        arrays = {a["name"]: a for a in supervisor.snapshot()["raid"]["arrays"]}
        self.assertFalse(arrays["md2"]["healthy"])
        self.assertEqual(arrays["md2"]["operation"], "recovery")
        self.assertTrue(arrays["md1"]["system"])


if __name__ == "__main__":
    unittest.main()
