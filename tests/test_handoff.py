"""An incident handed over, as Markdown, to an LLM agent the operator runs.

The doctor's diagnosis is one shot with no follow-up questions. These cover the
brief that lets a conversation pick up from it: that it carries everything the
doctor had, exactly as its model saw it, and nothing it did not.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import handoff  # noqa: E402
from app.diagnoser import SYSTEM_PROMPT, _render_incident  # noqa: E402
from tests.test_incident_logs import NEEDS_APP, StubSupervisor, make_settings  # noqa: E402

EVIDENCE = {
    "kind": "unhealthy", "summary": "memos is unhealthy", "detected_at": "2026-09-25T10:00:00+00:00",
    "container": {"name": "memos", "image": "neosmemo/memos:0.31.0", "state": "running"},
    "logs": "```\nERROR failed to open database: connection refused\n```",
}
DIAGNOSIS = {
    "summary": "memos cannot reach its database", "root_cause": "postgres is down",
    "severity": "warning", "confidence": 0.82, "is_transient": True,
    "recommended_action": "restart_container", "action_rationale": "the pool is wedged",
    "human_fix_steps": ["Check postgres", "Restart memos"], "evidence_used": ["connection refused"],
}
INCIDENT = {
    "id": "abc123", "container_name": "memos", "image": "neosmemo/memos:0.31.0",
    "kind": "unhealthy", "summary": "memos is unhealthy", "severity": "warning",
    "rule_severity": "warning", "status": "awaiting_approval", "opened_at": "2026-09-25T10:00:00+00:00",
    "last_seen_at": "2026-09-25T10:30:00+00:00", "occurrences": 4,
    "evidence": EVIDENCE, "diagnosis": DIAGNOSIS,
}
ACTIONS = [
    {"created_at": "2026-09-25T10:01:00+00:00", "action": "restart_container",
     "status": "pending", "decided_by": "policy", "reason": "suggest mode", "result": ""},
]


def render(**overrides):
    incident = {**INCIDENT, **overrides.pop("incident", {})}
    return handoff.render(incident, overrides.pop("actions", ACTIONS), **overrides)


class TheBrief(unittest.TestCase):
    def test_it_opens_with_what_to_do_and_a_warning(self):
        text = render()
        self.assertTrue(text.startswith("# Incident: memos cannot reach its database"))
        self.assertIn("Treat it as a hypothesis to test", text)
        self.assertIn("read it through before pasting", text)

    def test_it_asks_for_evidence_only_when_needed(self):
        text = render()
        self.assertIn("When it already settles the question", text)
        self.assertIn("Gather what you can yourself", text)
        self.assertIn("rarely more than two", text)
        self.assertNotIn("Ask for specific, read-only evidence before concluding", text)

    def test_the_diagnosis_is_all_there(self):
        text = render(model="claude-opus-5", provider="anthropic")
        for expected in ("postgres is down", "82%", "restart the container", "the pool is wedged",
                         "1. Check postgres", "2. Restart memos", "connection refused",
                         "anthropic claude-opus-5"):
            with self.subTest(expected):
                self.assertIn(expected, text)

    def test_the_evidence_is_the_prompt_the_model_was_sent(self):
        self.assertIn(_render_incident(EVIDENCE).rstrip(), render())

    def test_the_fence_outlasts_the_backticks_inside_it(self):
        """The log tail brings its own ``` fence; the outer one must survive it."""
        text = render()
        section = text.split("## The evidence, exactly as the doctor's model was shown it")[1]
        opening = re.search(r"^(`{3,})markdown$", section, re.MULTILINE).group(1)
        self.assertGreater(len(opening), 3)
        self.assertIn(f"\n{opening}\n", section)

    def test_what_was_done_is_listed(self):
        self.assertIn("**restart the container** - pending, decided by policy. suggest mode", render())

    def test_the_operator_note_is_shown_and_back_in_the_prompt(self):
        note = {"text": "Migrating postgres", "by": "alice", "at": "2026-09-25T10:20:00+00:00"}
        text = render(incident={"diagnosis": {**DIAGNOSIS, "operator_context": note}})
        self.assertIn("## What the operator told the doctor", text)
        self.assertIn("> Migrating postgres", text)
        self.assertIn("## Context from the operator\nMigrating postgres", text)

    def test_no_diagnosis_says_so(self):
        text = render(incident={"diagnosis": None})
        self.assertIn("No diagnosis was recorded", text)
        self.assertIn("## The evidence", text)

    def test_the_container_now_and_its_image(self):
        text = render(current={"state": "running", "health": "unhealthy", "restart_count": 3,
                               "stats": {"cpu_host_pct": 2.5}, "stack": "memos", "service": "app"},
                      image={"state": "update", "newer": {"tag": "0.32.0", "level": "minor",
                                                          "notes_url": "https://github.com/x/releases",
                                                          "breaking": "config moved"}})
        for expected in ("- **Health:** unhealthy", "2.5% of the host", "`memos`, service `app`",
                         "newer build of this tag", "`0.32.0` (minor)", "config moved"):
            with self.subTest(expected):
                self.assertIn(expected, text)

    def test_the_doctors_own_instructions_close_it(self):
        text = render()
        self.assertIn(SYSTEM_PROMPT.splitlines()[0], text)
        self.assertIn("They do not apply to you.", text)

    def test_the_named_manager_and_method_reach_both_prompts(self):
        text = render(manager="Coolify", method="a push to main; Coolify watches the branch")
        self.assertIn("Compose stacks managed by Coolify, deployed by: a push to main", text)
        self.assertIn("rather than in Coolify or on the host directly", text)
        self.assertIn("not in Coolify or on the host directly", text)

    def test_unnamed_it_speaks_of_compose_stacks_alone(self):
        text = render()
        self.assertIn("as Compose stacks, so", text)
        self.assertNotIn("Portainer", text)

    def test_a_dashboard_link_when_there_is_one(self):
        self.assertIn("https://doctor.example/#incident/abc123",
                      render(link="https://doctor.example/#incident/abc123"))


class ThroughTheSupervisor(unittest.TestCase):
    def test_a_stored_incident_becomes_a_brief(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from app.config import Settings
        from app.supervisor import Supervisor

        class Daemon(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps({"Version": "27.0", "ApiVersion": "1.45"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Daemon)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        previous = dict(os.environ)
        os.environ.update(DOCTOR_DATA_DIR=tempfile.mkdtemp(), ANTHROPIC_API_KEY="",
                          DOCTOR_PUBLIC_URL="https://doctor.example",
                          DOCKER_HOST=f"tcp://127.0.0.1:{server.server_address[1]}")
        try:
            supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(supervisor.store.close)
        store = supervisor.store
        incident = store.open_incident(
            container_id="c1", container_name="memos", image="memos:1", kind="unhealthy",
            fingerprint="fp", severity="warning", summary="memos is unhealthy",
            evidence=EVIDENCE, mode="suggest")
        store.set_diagnosis(incident, DIAGNOSIS)
        store.record_action(incident_id=incident, container_name="memos",
                            action="notify_only", status="skipped", decided_by="policy",
                            reason="observe mode")
        text = supervisor.incident_handoff(incident)
        self.assertIn("postgres is down", text)
        self.assertIn("**notify only** - skipped", text)
        self.assertIn(f"https://doctor.example/#incident/{incident}", text)
        self.assertIsNone(supervisor.incident_handoff("nope"))


class HandoffSupervisor(StubSupervisor):
    def incident_handoff(self, incident_id):
        return None if incident_id == "missing" else f"# Incident: {incident_id}\n"


@NEEDS_APP
class TheEndpoint(unittest.TestCase):
    def client(self, **env):
        from fastapi.testclient import TestClient

        from app.web.api import build_app

        settings = make_settings(DOCTOR_OIDC_ISSUER="", **env)
        return TestClient(build_app(HandoffSupervisor(), settings))

    def test_it_is_markdown(self):
        response = self.client().get("/api/incidents/abc/handoff")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/markdown"))
        self.assertEqual(response.text, "# Incident: abc\n")
        self.assertNotIn("content-disposition", response.headers)

    def test_download_names_the_file(self):
        response = self.client().get("/api/incidents/abc/handoff?download=1")
        self.assertEqual(response.headers["content-disposition"],
                         'attachment; filename="incident-abc.md"')

    def test_unknown_is_404(self):
        self.assertEqual(self.client().get("/api/incidents/missing/handoff").status_code, 404)

    def test_it_is_checked_like_the_incident_itself(self):
        client = self.client(DOCTOR_WEB_PASSWORD="pw", DOCTOR_WEB_USER="doctor")
        self.assertEqual(client.get("/api/incidents/abc/handoff").status_code, 401)
        self.assertEqual(client.get("/api/incidents/abc/handoff", auth=("doctor", "pw")).status_code, 200)


if __name__ == "__main__":
    unittest.main()
