"""Checking the registries on demand: one check at a time, not more than once a minute."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


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


class Refresh(unittest.TestCase):
    def setUp(self):
        from app.config import Settings
        from app.supervisor import Supervisor

        server = HTTPServer(("127.0.0.1", 0), _VersionOnly)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        previous = dict(os.environ)
        os.environ.update(DOCTOR_DATA_DIR=tempfile.mkdtemp(), ANTHROPIC_API_KEY="",
                          DOCKER_HOST=f"tcp://127.0.0.1:{server.server_address[1]}")
        try:
            self.supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(self.supervisor.store.close)
        self.supervisor.containers = {"app": {"name": "app", "image": "app:1"}}
        self.checks = 0

        async def check():
            self.checks += 1
            await asyncio.sleep(0.05)
            self.supervisor.image_updates = {"checked_at": datetime.now(timezone.utc).isoformat(),
                                             "images": []}
        self.supervisor._check_image_updates = check

    def test_one_check_at_a_time_and_the_flag_says_so(self):
        async def run():
            first = self.supervisor.refresh_images()
            await asyncio.sleep(0.01)
            during = self.supervisor.snapshot()["image_updates"]["checking"]
            second = self.supervisor.refresh_images()
            await asyncio.sleep(0.1)
            return first, during, second, self.supervisor.snapshot()["image_updates"]["checking"]

        first, during, second, after = asyncio.run(run())
        self.assertTrue(first["started"])
        self.assertTrue(during)
        self.assertEqual(second, {"started": False, "checking": True})
        self.assertFalse(after)
        self.assertIsNone(self.supervisor.snapshot()["image_updates"]["progress"])
        self.assertEqual(self.checks, 1)

    def test_not_again_within_a_minute(self):
        self.supervisor.image_updates = {
            "checked_at": (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()}
        result = self.supervisor.refresh_images()
        self.assertFalse(result["started"])
        self.assertIn("minute", result["reason"])

    def test_a_failed_check_still_stops_the_spinner(self):
        async def broken():
            raise RuntimeError("registry down")
        self.supervisor._check_image_updates = broken

        async def run():
            self.supervisor.refresh_images()
            await asyncio.sleep(0.05)
        from unittest import mock
        from app import supervisor as module

        with mock.patch.object(module.log, "exception") as logged:
            asyncio.run(run())
        logged.assert_called_once()
        self.assertFalse(self.supervisor.snapshot()["image_updates"]["checking"])


if __name__ == "__main__":
    unittest.main()
