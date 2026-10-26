"""/api/widget: a few counts for Homepage and the like, read with a token.

DOCTOR_WIDGET_TOKEN sits in another app's config, so it must open this one
endpoint and nothing else, and the endpoint must say nothing a tile does not
need - no names, logs or settings.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import Settings  # noqa: E402
from app.store import Store  # noqa: E402
from app.supervisor import Supervisor  # noqa: E402
from tests.test_incident_logs import NEEDS_APP, StubSupervisor, make_settings  # noqa: E402

try:
    from fastapi.testclient import TestClient

    from app.web.api import build_app
except ImportError:  # pragma: no cover - reported by NEEDS_APP
    TestClient = None

TOKEN = "widget-token-0123456789abcdef"


class WidgetStub(StubSupervisor):
    def __init__(self):
        super().__init__()
        # Only OIDC's session table reads it.
        self.store = Store(os.path.join(tempfile.mkdtemp(), "d.db"))

    def widget_summary(self):
        return {"status": "ok", "open_incidents": 0}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@NEEDS_APP
class WidgetAccess(unittest.TestCase):
    def client(self, **settings):
        values = dict(DOCTOR_WEB_PASSWORD="a-long-password", DOCTOR_OIDC_ISSUER="",
                      DOCTOR_WIDGET_TOKEN=TOKEN)
        values.update(settings)
        stub = WidgetStub()
        self.addCleanup(stub.store.close)
        return TestClient(build_app(stub, make_settings(**values)))

    def test_the_token_reads_the_widget(self):
        response = self.client().get("/api/widget", headers=bearer(TOKEN))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_the_token_opens_nothing_else(self):
        client = self.client()
        for path in ("/api/status", "/api/state", "/api/incidents", "/api/settings"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path, headers=bearer(TOKEN)).status_code, 401)

    def test_a_wrong_token_is_refused(self):
        response = self.client().get("/api/widget", headers=bearer("not-the-token"))
        self.assertEqual(response.status_code, 401)

    def test_no_token_configured_means_bearer_is_refused(self):
        response = self.client(DOCTOR_WIDGET_TOKEN="").get("/api/widget", headers=bearer(TOKEN))
        self.assertEqual(response.status_code, 401)

    def test_without_credentials_it_is_refused(self):
        self.assertEqual(self.client().get("/api/widget").status_code, 401)

    def test_a_signed_in_user_reads_it_too(self):
        response = self.client().get("/api/widget", auth=("doctor", "a-long-password"))
        self.assertEqual(response.status_code, 200)

    def test_the_token_works_with_oidc_as_the_only_sign_in(self):
        client = self.client(DOCTOR_WEB_PASSWORD="", DOCTOR_OIDC_ISSUER="https://id.example",
                             DOCTOR_OIDC_CLIENT_ID="doctor",
                             DOCTOR_PUBLIC_URL="https://doctor.example")
        self.assertEqual(client.get("/api/widget", headers=bearer(TOKEN)).status_code, 200)

    def test_guessing_is_rate_limited(self):
        client = self.client()
        codes = [client.get("/api/widget", headers=bearer(f"guess-{n}")).status_code
                 for n in range(12)]
        self.assertIn(429, codes)


class WidgetSummary(unittest.TestCase):
    def setUp(self):
        sup = Supervisor.__new__(Supervisor)
        sup.settings = Settings()
        sup.store = Store(os.path.join(tempfile.mkdtemp(), "d.db"))
        self.addCleanup(sup.store.close)
        sup.docker_connected = True
        sup.image_updates = {"available": 2}
        sup.last_poll_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        sup.containers = {
            "a": {"name": "a", "state": "running", "health": "healthy", "watched": True},
            "b": {"name": "b", "state": "running", "health": "unhealthy", "watched": True},
            "c": {"name": "c", "state": "exited", "watched": True},
            "d": {"name": "d", "state": "exited", "watched": False},
        }
        self.sup = sup

    def test_counts_watched_containers_only(self):
        summary = self.sup.widget_summary()
        self.assertEqual(
            (summary["containers_watched"], summary["containers_running"],
             summary["containers_unhealthy"], summary["containers_stopped"]),
            (3, 2, 1, 1))
        self.assertEqual(summary["image_updates"], 2)
        self.assertEqual(summary["status"], "ok")
        self.assertGreaterEqual(summary["last_poll_seconds_ago"], 30)

    def test_an_open_incident_needs_attention(self):
        self.sup.store.open_incident(
            container_id="c1", container_name="b", image="b:1", kind="unhealthy",
            fingerprint="fp-b", severity="warning", summary="b is unhealthy",
            evidence={"kind": "unhealthy"}, mode="observe")
        summary = self.sup.widget_summary()
        self.assertEqual((summary["status"], summary["open_incidents"]), ("attention", 1))

    def test_docker_down_says_so(self):
        self.sup.docker_connected = False
        self.assertEqual(self.sup.widget_summary()["status"], "docker unreachable")

    def test_says_no_names(self):
        # Counts only: the token lives in another app's config.
        self.assertNotIn('"b"', repr(self.sup.widget_summary()))

    def test_before_the_first_poll(self):
        self.sup.last_poll_at = None
        self.sup.image_updates = {}
        summary = self.sup.widget_summary()
        self.assertIsNone(summary["last_poll_seconds_ago"])
        self.assertEqual(summary["image_updates"], 0)


if __name__ == "__main__":
    unittest.main()
