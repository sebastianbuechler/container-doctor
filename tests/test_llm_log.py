"""The call log: every request as it went out, answered or not, and the spend
chart built from it."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.store import Store


class _Vendor(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint that answers, or fails, on request."""

    fail = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if _Vendor.fail:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"upstream down")
            return
        body = json.dumps({
            "choices": [{"message": {"content": json.dumps({"summary": "fine"})}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 80},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CallLog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Vendor)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        from app.config import Settings
        from app.diagnoser import Diagnoser

        previous = dict(os.environ)
        os.environ.update(
            DOCTOR_LLM_PROVIDER="openai", DOCTOR_MODEL="cheap-model",
            DOCTOR_OPENAI_BASE_URL=f"http://127.0.0.1:{self.server.server_address[1]}/v1",
            DOCTOR_LLM_MAX_RETRIES="0", DOCTOR_PRICE_IN_PER_MTOK="1", DOCTOR_PRICE_OUT_PER_MTOK="2",
        )
        try:
            self.diagnoser = Diagnoser(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.addCleanup(self.store.close)
        self.diagnoser.recorder = self.store.record_llm_call
        _Vendor.fail = False

    def ask(self, **kwargs):
        return asyncio.run(self.diagnoser.structured(
            "the system prompt", "the evidence", {"type": "object"}, "container_diagnosis",
            **kwargs))

    def test_an_answered_call_is_logged_as_it_was_sent(self):
        self.ask(ref="incident-1")
        [row] = self.store.list_llm_calls()
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["ref"], "incident-1")
        self.assertEqual(row["input_tokens"], 1200)
        self.assertAlmostEqual(row["cost_usd"], 1200 / 1e6 + 80 * 2 / 1e6)
        self.assertTrue(row["endpoint"].endswith("/v1/chat/completions"))
        full = self.store.get_llm_call(row["id"])
        self.assertEqual(full["request"]["system"], "the system prompt")
        self.assertEqual(full["request"]["prompt"], "the evidence")
        self.assertEqual(full["request"]["sent_as"], "json_schema + temperature")
        self.assertIn('"summary": "fine"', full["response_text"])

    def test_a_failed_call_is_logged_and_still_raises(self):
        _Vendor.fail = True
        with self.assertRaises(RuntimeError):
            self.ask()
        [row] = self.store.list_llm_calls()
        self.assertEqual(row["status"], "error")
        self.assertIn("HTTP 500", row["error"])

    def test_a_broken_log_never_costs_the_call(self):
        def explode(_entry):
            raise OSError("disk full")
        self.diagnoser.recorder = explode
        with self.assertLogs("app.diagnoser", level="ERROR"):
            result, _ = self.ask()
        self.assertEqual(result, {"summary": "fine"})


class Filters(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.addCleanup(self.store.close)
        for purpose in ("container_diagnosis", "briefing_section", "briefing_overall",
                        "classifier_signal"):
            self.store.record_llm_call({"purpose": purpose, "status": "ok", "cost_usd": 0.01})

    def test_a_group_matches_its_purposes(self):
        self.assertEqual(self.store.count_llm_calls("briefing"), 2)
        self.assertEqual(self.store.count_llm_calls("container_diagnosis"), 1)
        self.assertEqual(self.store.count_llm_calls(), 4)

    def test_an_underscore_is_not_a_wildcard(self):
        self.store.record_llm_call({"purpose": "briefingX", "status": "ok"})
        self.assertEqual(self.store.count_llm_calls("briefing"), 2)


class Spend(unittest.TestCase):
    """The chart's days: totals from llm_usage, split from the log."""

    def test_a_day_older_than_the_log_keeps_its_total(self):
        from types import SimpleNamespace
        from app.supervisor import Supervisor

        store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.addCleanup(store.close)
        today = datetime.now(timezone.utc)
        old_day = (today - timedelta(days=3)).date().isoformat()
        with store._lock:
            store._conn.execute(
                "INSERT INTO llm_usage (day, calls, cost_usd) VALUES (?, 4, 0.5)", (old_day,))
            store._conn.commit()
        store.reserve_call(0)
        store.add_usage(input_tokens=10, output_tokens=5, cached_tokens=0, cost_usd=0.03)
        store.record_llm_call({"purpose": "container_diagnosis", "status": "ok", "cost_usd": 0.02})
        store.record_llm_call({"purpose": "briefing_section", "status": "ok", "cost_usd": 0.01})

        fake = SimpleNamespace(store=store, settings=SimpleNamespace(
            daily_usd_budget=1.0, daily_call_limit=0, llm_log_days=14))
        spend = Supervisor.llm_spend(fake, 7)
        days = {d["day"]: d for d in spend["days"]}
        self.assertEqual(len(spend["days"]), 7)
        self.assertEqual(days[old_day]["by_purpose"], {"earlier": {"calls": 0, "cost_usd": 0.5}})
        now = days[today.date().isoformat()]
        self.assertAlmostEqual(now["cost_usd"], 0.03)
        self.assertEqual(set(now["by_purpose"]), {"diagnosis", "briefing"})


if __name__ == "__main__":
    unittest.main()
