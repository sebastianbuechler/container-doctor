"""The incident-scoped log views.

Deliberately not a general log browser - that is Dozzle's job, and a live
unredacted feed would undo the redaction the rest of this design rests on. What
is tested here is the boundary: redaction still applies, excluded containers
stay excluded, and live log content is never readable without a configured
login (unlike the read-only endpoints, which fall through when no password is
set).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import Settings  # noqa: E402
from app.supervisor import _parse_iso  # noqa: E402

try:  # noqa: E402
    from fastapi.testclient import TestClient

    from app.web.api import build_app
except ImportError:  # pragma: no cover - a partial local environment
    TestClient = None
    build_app = None

NEEDS_APP = unittest.skipIf(
    TestClient is None,
    "needs the app's dependencies: pip install -r requirements.txt",
)

SECRET_LOG = (
    "2026-09-24T09:13:59Z ERROR failed to open database: connection refused\n"
    "2026-09-24T09:14:00Z FATAL giving up, token=sk-ant-supersecretvalue1234\n"
    "2026-09-24T09:14:01Z ERROR password=hunter2hunter2 retrying\n"
)


def make_settings(**overrides: str) -> Settings:
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        return Settings()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class StubSupervisor:
    """Only what the log endpoints touch."""

    def __init__(self):
        self.last_call: tuple = ()
        self.docker_connected = True
        self.docker_access: list = []

    async def container_logs(self, name, lines=200, since=None, until=None):
        self.last_call = (name, lines, since, until)
        if name == "excluded":
            return {"ok": False, "error": "container is excluded via doctor.enable=false"}
        if name == "missing":
            return {"ok": False, "error": "unknown container"}
        from app.redact import redact

        return {"ok": True, "container": name, "lines": lines,
                "since": None, "until": None, "logs": redact(SECRET_LOG)}

    async def incident_context(self, incident_id, minutes=5):
        self.last_call = (incident_id, minutes)
        if incident_id == "missing":
            return {"ok": False, "error": "unknown incident"}
        return {"ok": True, "window_minutes": minutes, "since": "", "until": "",
                "logs": "", "neighbours": [], "nearby_incidents": []}

    def snapshot(self):
        return {}

    def status(self):
        return {}


def client_for(settings: Settings) -> tuple[TestClient, StubSupervisor]:
    supervisor = StubSupervisor()
    return TestClient(build_app(supervisor, settings)), supervisor


@NEEDS_APP
class AuthBoundaryTests(unittest.TestCase):
    LOGS = "/api/containers/memos/logs"
    CONTEXT = "/api/incidents/abc/context"

    def test_live_logs_are_refused_outright_when_no_password_is_configured(self):
        # The read-only endpoints fall through without auth; live log content
        # must not, or an unprotected dashboard streams every container's logs.
        client, _ = client_for(make_settings(DOCTOR_WEB_PASSWORD=""))
        for path in (self.LOGS, self.CONTEXT):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 403)

    def test_credentials_are_required_when_a_password_is_set(self):
        client, _ = client_for(make_settings(DOCTOR_WEB_PASSWORD="s3cret"))
        for path in (self.LOGS, self.CONTEXT):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 401)

    def test_correct_credentials_are_accepted(self):
        client, _ = client_for(make_settings(DOCTOR_WEB_PASSWORD="s3cret"))
        self.assertEqual(client.get(self.LOGS, auth=("doctor", "s3cret")).status_code, 200)

    def test_wrong_password_is_rejected(self):
        client, _ = client_for(make_settings(DOCTOR_WEB_PASSWORD="s3cret"))
        self.assertEqual(client.get(self.LOGS, auth=("doctor", "nope")).status_code, 401)


@NEEDS_APP
class LogEndpointTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(DOCTOR_WEB_PASSWORD="s3cret")
        self.client, self.supervisor = client_for(self.settings)
        self.auth = ("doctor", "s3cret")

    def test_secrets_are_redacted_before_they_reach_the_page(self):
        body = self.client.get("/api/containers/memos/logs", auth=self.auth).json()
        self.assertNotIn("sk-ant-supersecretvalue1234", body["logs"])
        self.assertNotIn("hunter2hunter2", body["logs"])
        self.assertIn("[redacted]", body["logs"])
        # The useful context survives.
        self.assertIn("connection refused", body["logs"])

    def test_an_excluded_container_stays_excluded_on_request(self):
        response = self.client.get("/api/containers/excluded/logs", auth=self.auth)
        self.assertEqual(response.status_code, 404)
        self.assertIn("doctor.enable=false", response.json()["detail"])

    def test_unknown_container_is_a_404(self):
        self.assertEqual(
            self.client.get("/api/containers/missing/logs", auth=self.auth).status_code, 404
        )

    def test_line_count_is_clamped(self):
        self.client.get("/api/containers/memos/logs?lines=999999", auth=self.auth)
        self.assertEqual(self.supervisor.last_call[1], 2000)
        self.client.get("/api/containers/memos/logs?lines=0", auth=self.auth)
        self.assertEqual(self.supervisor.last_call[1], 1)

    def test_context_window_is_clamped(self):
        self.client.get("/api/incidents/abc/context?minutes=9999", auth=self.auth)
        self.assertEqual(self.supervisor.last_call[1], 60)
        self.client.get("/api/incidents/abc/context?minutes=-5", auth=self.auth)
        self.assertEqual(self.supervisor.last_call[1], 1)

    def test_unknown_incident_is_a_404(self):
        self.assertEqual(
            self.client.get("/api/incidents/missing/context", auth=self.auth).status_code, 404
        )


class TimestampParsingTests(unittest.TestCase):
    def test_stored_utc_timestamps_parse(self):
        parsed = _parse_iso("2026-09-24T09:14:03+00:00")
        self.assertEqual(parsed, datetime(2026, 9, 24, 9, 14, 3, tzinfo=timezone.utc))

    def test_a_naive_timestamp_is_treated_as_utc(self):
        # Comparing a naive datetime against an aware one raises; the window
        # maths must not blow up on a row written by an older build.
        parsed = _parse_iso("2026-09-24T09:14:03")
        self.assertIsNotNone(parsed.tzinfo)
        self.assertLess(parsed - timedelta(minutes=5), parsed)

    def test_garbage_is_none_rather_than_an_exception(self):
        for value in ("", None, "not a date", "2026-13-45T99:99:99"):
            with self.subTest(value=value):
                self.assertIsNone(_parse_iso(value))


class RedactionReachesEveryFieldTests(unittest.TestCase):
    def test_correlated_samples_are_redacted_at_source(self):
        import re

        from app.redact import error_lines

        hits = error_lines(SECRET_LOG, re.compile("(?i)fatal|error"), limit=5)
        joined = "\n".join(hits)
        self.assertNotIn("sk-ant-supersecretvalue1234", joined)
        self.assertNotIn("hunter2hunter2", joined)


if __name__ == "__main__":
    unittest.main()
