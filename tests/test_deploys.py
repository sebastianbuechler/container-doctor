"""A redeploy is not an outage: stops that were asked for, containers replaced,
and what the rest of the host logs while it happens."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.deploys import Settling, event_time


def stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class Windows(unittest.TestCase):
    def test_lines_logged_during_a_redeploy_do_not_count(self):
        settling = Settling(3)
        deploy = datetime.now(timezone.utc) - timedelta(minutes=10)
        settling.note_deploy(deploy)
        tail = "\n".join([
            f"{stamp(deploy - timedelta(minutes=5))} ERROR before, real",
            f"{stamp(deploy + timedelta(seconds=20))} ERROR dial tcp 192.168.1.179:1919: connection refused",
            f"{stamp(deploy + timedelta(minutes=2))} ERROR still reconnecting",
            f"{stamp(deploy + timedelta(minutes=5))} ERROR after, real",
            "no timestamp at all ERROR",
        ])
        kept, dropped = settling.strip(tail)
        self.assertEqual(dropped, 2)
        self.assertIn("before, real", kept)
        self.assertIn("after, real", kept)
        self.assertIn("no timestamp at all", kept)  # unreadable: kept, not hidden

    def test_windows_close_together_merge(self):
        settling = Settling(3)
        now = datetime.now(timezone.utc)
        settling.note_deploy(now - timedelta(minutes=2))
        settling.note_deploy(now)
        self.assertEqual(len(settling.windows()), 1)
        self.assertTrue(settling.active(now + timedelta(minutes=2)))
        self.assertFalse(settling.active(now + timedelta(minutes=4)))

    def test_zero_turns_it_off(self):
        settling = Settling(0)
        settling.note_deploy(datetime.now(timezone.utc))
        self.assertFalse(settling.active())

    def test_a_stop_is_requested_only_just_before_the_die(self):
        settling = Settling(3)
        now = datetime.now(timezone.utc)
        settling.note_stop("abc", now)
        self.assertTrue(settling.requested_stop("abc", now + timedelta(seconds=12)))
        self.assertFalse(settling.requested_stop("abc", now + timedelta(minutes=5)))
        self.assertFalse(settling.requested_stop("other", now))
        self.assertTrue(settling.requested_stop("abc"))  # a poll finds it stopped later

    def test_event_times(self):
        self.assertEqual(event_time({"time": 1790000000}).year, 2026)
        self.assertEqual(event_time({"timeNano": 1790000000123456789}).second,
                         datetime.fromtimestamp(1790000000, tz=timezone.utc).second)


class HealthHeld(unittest.TestCase):
    def test_a_failing_check_is_held_while_settling_then_reported_once(self):
        from app.config import Settings
        from app.detector import Detector

        detector = Detector(Settings())
        box = {"name": "app", "state": "running", "health": "unhealthy"}
        self.assertEqual(detector.from_poll(box, None, "", settling=True), [])
        self.assertEqual(detector.from_poll(box, None, "", settling=True), [])
        [signal] = detector.from_poll(box, None, "", settling=False)
        self.assertEqual(signal.kind, "unhealthy")
        self.assertEqual(detector.from_poll(box, None, ""), [])  # once per bout


class _VersionOnly(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"Version": "27.0", "ApiVersion": "1.45"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def event(action, cid, name="sonarr", **attrs):
    return {"Action": action, "id": cid, "time": int(datetime.now(timezone.utc).timestamp()),
            "Actor": {"ID": cid, "Attributes": {"name": name, **attrs}}}


class Events(unittest.TestCase):
    def setUp(self):
        from app.config import Settings
        from app.supervisor import Supervisor

        server = HTTPServer(("127.0.0.1", 0), _VersionOnly)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        previous = dict(os.environ)
        os.environ.update(DOCTOR_DATA_DIR=tempfile.mkdtemp(), ANTHROPIC_API_KEY="",
                          DOCTOR_DIE_GRACE_SECONDS="0",
                          DOCKER_HOST=f"tcp://127.0.0.1:{server.server_address[1]}")
        try:
            self.supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(self.supervisor.store.close)
        self.signals = []

        async def record(signal):
            self.signals.append(signal)
        self.supervisor._process_signal = record
        self.listing = []

        async def listing():
            return [dict(c) for c in self.listing]
        self.supervisor.docker.list_containers = listing
        self.supervisor.containers = {"sonarr": self.box("old")}

    def box(self, cid, **extra):
        return {"id": cid, "name": "sonarr", "image": "linuxserver/sonarr:4", "state": "exited",
                "exit_code": 137, "labels": {}, **extra}

    def run_events(self, *events):
        async def run():
            for item in events:
                await self.supervisor._handle_docker_event(item)
            await asyncio.sleep(0.05)  # let a deferred die settle
        asyncio.run(run())

    def test_a_stop_that_was_asked_for_is_not_a_crash(self):
        self.run_events(event("kill", "old", signal="15"), event("die", "old", exitCode="143"))
        self.assertEqual(self.signals, [])
        self.assertTrue(self.supervisor.settling.active())

    def test_a_container_replaced_meanwhile_was_a_redeploy(self):
        self.listing = [self.box("new", state="running", exit_code=0)]
        self.run_events(event("die", "old", exitCode="137"))
        self.assertEqual(self.signals, [])

    def test_a_real_crash_still_opens_an_incident(self):
        self.listing = [self.box("old")]
        self.run_events(event("die", "old", exitCode="1"))
        [signal] = self.signals
        self.assertEqual(signal.kind, "exit_nonzero")

    def test_the_poll_does_not_report_a_stop_that_was_asked_for(self):
        self.run_events(event("kill", "old", signal="15"))
        found = self.supervisor.detector.from_poll(self.box("old"), None, "")
        self.assertEqual([s.kind for s in found], ["exit_nonzero"])
        self.assertEqual(self.supervisor._unrequested(self.box("old"), found), [])
        self.assertEqual(len(self.supervisor._unrequested(self.box("other"), found)), 1)

if __name__ == "__main__":
    unittest.main()
