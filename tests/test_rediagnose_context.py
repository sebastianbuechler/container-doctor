"""Re-diagnosing with what the operator knows.

The logs cannot say "I am migrating the database right now"; the person who
runs the NAS can. These cover the note reaching the model, being redacted on
the way, being kept with the diagnosis it produced, and not being handed on to
next week's identical failure by the cache.
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

from app.diagnoser import SYSTEM_PROMPT, _render_incident  # noqa: E402
from tests.test_incident_logs import NEEDS_APP, StubSupervisor, make_settings  # noqa: E402

DIAGNOSIS = {
    "summary": "memos cannot reach its database", "severity": "warning",
    "root_cause": "planned migration", "recommended_action": "notify_only",
    "action_rationale": "wait", "confidence": 0.8, "is_transient": True,
    "human_fix_steps": [], "evidence_used": [],
}


class ThePrompt(unittest.TestCase):
    EVIDENCE = {"kind": "unhealthy", "summary": "memos is unhealthy", "container": {"name": "memos"},
                "logs": "ERROR connection refused"}

    def test_the_note_is_in_the_prompt_before_the_logs(self):
        prompt = _render_incident({**self.EVIDENCE, "operator_context": "Migrating postgres until 15:00"})
        self.assertIn("## Context from the operator\nMigrating postgres until 15:00", prompt)
        self.assertLess(prompt.index("Context from the operator"), prompt.index("log tail"))

    def test_no_note_no_section(self):
        self.assertNotIn("Context from the operator", _render_incident(self.EVIDENCE))

    def test_the_model_is_told_it_informs_but_does_not_widen(self):
        self.assertIn("context from the operator", SYSTEM_PROMPT)
        self.assertIn("does not widen what you may propose", SYSTEM_PROMPT)


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
        cls.server.server_close()

    def setUp(self):
        from app.config import Settings
        from app.supervisor import Supervisor

        previous = dict(os.environ)
        os.environ.update(DOCTOR_DATA_DIR=tempfile.mkdtemp(), ANTHROPIC_API_KEY="sk-ant-test",
                          DOCKER_HOST=f"tcp://127.0.0.1:{self.server.server_address[1]}")
        try:
            self.supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(self.supervisor.store.close)
        self.seen: list[dict] = []

        async def diagnose(evidence, ref=""):
            self.seen.append(evidence)
            return dict(DIAGNOSIS), {"input_tokens": 1, "output_tokens": 1,
                                     "cached_tokens": 0, "cost_usd": 0.0}

        self.supervisor.diagnoser.diagnose = diagnose
        self.incident = self.supervisor.store.open_incident(
            container_id="c1", container_name="memos", image="memos:1", kind="unhealthy",
            fingerprint="fp-memos", severity="warning", summary="memos is unhealthy",
            evidence={"kind": "unhealthy", "summary": "memos is unhealthy"}, mode="suggest")

    def rediagnose(self, context="", actor="alice"):
        return asyncio.run(self.supervisor.rediagnose(self.incident, context=context, actor=actor))

    def stored(self):
        return self.supervisor.store.get_incident(self.incident)["diagnosis"]

    def test_the_note_reaches_the_model_and_stays_with_the_diagnosis(self):
        result = self.rediagnose("  Migrating postgres until 15:00  ")
        self.assertTrue(result["ok"])
        self.assertEqual(self.seen[0]["operator_context"], "Migrating postgres until 15:00")
        note = self.stored()["operator_context"]
        self.assertEqual((note["text"], note["by"]), ("Migrating postgres until 15:00", "alice"))
        self.assertTrue(note["at"])

    def test_a_pasted_secret_is_redacted_before_it_goes_anywhere(self):
        self.rediagnose("it logs token=sk-ant-supersecretvalue1234 on start")
        self.assertNotIn("supersecretvalue", self.seen[0]["operator_context"])
        self.assertNotIn("supersecretvalue", self.stored()["operator_context"]["text"])

    def test_a_long_note_is_cut_to_size(self):
        from app.supervisor import OPERATOR_CONTEXT_CHARS

        self.rediagnose("x" * 5000)
        self.assertEqual(len(self.seen[0]["operator_context"]), OPERATOR_CONTEXT_CHARS)

    def test_the_stored_evidence_is_left_as_the_rule_captured_it(self):
        self.rediagnose("a note")
        evidence = self.supervisor.store.get_incident(self.incident)["evidence"]
        self.assertNotIn("operator_context", evidence)

    def test_a_diagnosis_resting_on_a_note_is_not_cached(self):
        self.rediagnose("I am restoring a backup right now")
        self.assertIsNone(self.supervisor.store.cached_diagnosis("fp-memos", 24))

    def test_without_a_note_it_is_the_old_behaviour(self):
        self.rediagnose("")
        self.assertNotIn("operator_context", self.seen[0])
        self.assertNotIn("operator_context", self.stored())
        self.assertIsNotNone(self.supervisor.store.cached_diagnosis("fp-memos", 24))


class ContextSupervisor(StubSupervisor):
    async def rediagnose(self, incident_id, context="", actor=""):
        self.last_call = (incident_id, context, actor)
        return {"ok": True, "diagnosis": {}}


@NEEDS_APP
class TheEndpoint(unittest.TestCase):
    def client(self):
        from fastapi.testclient import TestClient

        from app.web.api import build_app

        supervisor = ContextSupervisor()
        settings = make_settings(DOCTOR_WEB_PASSWORD="pw", DOCTOR_WEB_USER="doctor",
                                 DOCTOR_OIDC_ISSUER="")
        return TestClient(build_app(supervisor, settings)), supervisor

    def test_context_and_the_signed_in_user_are_passed_on(self):
        client, supervisor = self.client()
        response = client.post("/api/incidents/abc/diagnose", json={"context": "a note"},
                               auth=("doctor", "pw"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(supervisor.last_call, ("abc", "a note", "doctor"))

    def test_no_body_still_works(self):
        client, supervisor = self.client()
        response = client.post("/api/incidents/abc/diagnose", auth=("doctor", "pw"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(supervisor.last_call, ("abc", "", "doctor"))

    def test_it_still_needs_a_login(self):
        client, _ = self.client()
        self.assertEqual(client.post("/api/incidents/abc/diagnose", json={"context": "x"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
