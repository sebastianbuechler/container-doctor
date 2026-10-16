"""A notification should land on the incident it is about, not the dashboard's
top, and say what is waiting there."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

import httpx  # noqa: E402

from app.config import Settings  # noqa: E402
from app.notifier import Notifier  # noqa: E402

URL = "https://doctor.example/#incident/abc123"
INCIDENT = {"container_name": "sonarr", "kind": "unhealthy", "summary": "sonarr is unhealthy"}


class Links(unittest.TestCase):
    def publish(self, call):
        previous = dict(os.environ)
        os.environ.update(DOCTOR_NTFY_URL="https://ntfy.example", DOCTOR_NTFY_TOPIC="doctor")
        try:
            notifier = Notifier(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        sent: list[httpx.Request] = []

        def handler(request):
            sent.append(request)
            return httpx.Response(200)

        notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        asyncio.run(call(notifier))
        return sent[0].headers

    def test_an_incident_opens_itself(self):
        headers = self.publish(lambda n: n.notify_incident(INCIDENT, None, "", dashboard_url=URL))
        self.assertEqual(headers["Click"], URL)
        self.assertEqual(headers["Actions"], f"view, Open incident, {URL}, clear=true")

    def test_an_approval_says_so_on_its_button(self):
        headers = self.publish(lambda n: n.notify_incident(
            INCIDENT, None, "Proposed: restart", dashboard_url=URL, awaiting_approval=True))
        self.assertIn("Review and approve", headers["Actions"])

    def test_without_a_public_url_there_is_no_button_to_nowhere(self):
        headers = self.publish(lambda n: n.notify_incident(INCIDENT, None, ""))
        self.assertNotIn("Click", headers)
        self.assertNotIn("Actions", headers)


if __name__ == "__main__":
    unittest.main()
