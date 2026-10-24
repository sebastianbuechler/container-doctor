"""A network note that is meant can be dismissed, and stays dismissed.

The notes are recomputed every poll, so a dismissal is kept by the note's key
- its kind and what it is about - rather than by anything in one poll.
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.store import Store  # noqa: E402

try:  # noqa: E402
    from fastapi.testclient import TestClient

    from app.web.api import build_app
    from tests.test_incident_logs import make_settings
except ImportError:  # pragma: no cover - a partial local environment
    TestClient = None

NEEDS_APP = unittest.skipIf(TestClient is None, "needs the app's dependencies")


def make_store() -> Store:
    return Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))


class Storing(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)

    def test_dismiss_and_restore(self):
        self.store.dismiss_finding("published_and_proxied:portainer", "admin")
        kept = self.store.dismissed_findings()["published_and_proxied:portainer"]
        self.assertEqual(kept["dismissed_by"], "admin")
        self.assertTrue(kept["dismissed_at"])
        self.assertTrue(self.store.restore_finding("published_and_proxied:portainer"))
        self.assertEqual(self.store.dismissed_findings(), {})

    def test_dismissing_twice_keeps_the_first(self):
        self.store.dismiss_finding("default_bridge:old", "admin")
        self.store.dismiss_finding("default_bridge:old", "someone")
        self.assertEqual(self.store.dismissed_findings()["default_bridge:old"]["dismissed_by"], "admin")

    def test_restoring_what_is_not_dismissed(self):
        self.assertFalse(self.store.restore_finding("nope"))


class StubSupervisor:
    def __init__(self, store):
        self.store = store
        self.docker_connected = True
        self.docker_access: list = []

    def snapshot(self):
        return {}

    def status(self):
        return {}


@NEEDS_APP
class TheEndpoints(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)
        self.client = TestClient(build_app(StubSupervisor(self.store),
                                           make_settings(DOCTOR_WEB_PASSWORD="pw",
                                                         DOCTOR_WEB_USER="doctor")))
        self.addCleanup(self.client.close)

    def test_a_login_is_needed(self):
        response = self.client.post("/api/networks/findings/dismiss", json={"key": "x:y"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.store.dismissed_findings(), {})

    def test_dismiss_then_restore(self):
        auth = ("doctor", "pw")
        response = self.client.post("/api/networks/findings/dismiss",
                                    json={"key": "lan:x"}, auth=auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.dismissed_findings()["lan:x"]["dismissed_by"], "doctor")
        response = self.client.post("/api/networks/findings/restore",
                                    json={"key": "lan:x"}, auth=auth)
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/api/networks/findings/restore",
                                    json={"key": "lan:x"}, auth=auth)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
