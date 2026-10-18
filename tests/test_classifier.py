"""The classifier: the wire format, the guard rails, the budget, and what it
may and may not do to a signal."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from app.classifier import QUESTIONS, Classifier, cache_key
from app.decision import Answer, Choice, Decision, Flag, OpenRouterDecisions, Score, read_answers, wire_questions
from app.detector import Signal
from app.store import Store


def settings(**overrides):
    base = dict(classifier_mode="on", classifier_suppress_at=0.9, classifier_daily_calls=500,
                classifier_daily_usd=0.10, classifier_max_per_minute=10,
                classifier_cache_hours=6, classifier_timeout_seconds=2.0,
                classifier_max_state_chars=12000)
    base.update(overrides)
    return SimpleNamespace(**base)


def signal(kind="log_errors", lines=("ERROR retrying upstream 12",), count=6, severity="warning"):
    return Signal(kind=kind, severity=severity, summary=f"app {kind}",
                  container={"name": "app", "image": "app:1"},
                  details={"matched_lines": list(lines), "match_count": count},
                  signature_parts=list(lines[:2]))


class FakeModel:
    """A decision model that answers what it is told to, and counts."""

    def __init__(self, verdict="real_fault", probabilities=None, severity="warning",
                 severity_confidence=0.95, calibrated=True, fail=False):
        self.calibrated = calibrated
        self.name = "fake-jev"
        self.calls = 0
        self.fail = fail
        self.answers = {
            "verdict": Answer(verdict, 0.9, probabilities or {verdict: 0.95}),
            "severity": Answer(severity, severity_confidence, {severity: severity_confidence}),
            "cause": Answer("unknown"),
            "self_heals": Answer(False, 0.8, {"true": 0.2, "false": 0.8}),
        }

    async def decide(self, state, questions, *, purpose, ref=""):
        self.calls += 1
        if self.fail:
            raise RuntimeError("upstream down")
        return Decision(dict(self.answers), self.name, self.calibrated, 3000, 0, 0.000126, 90)


class Rails(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.addCleanup(self.store.close)

    def review(self, model, sig=None, **overrides):
        classifier = Classifier(settings(**overrides), model, self.store)
        return classifier, asyncio.run(classifier.review(sig or signal(), "state"))

    def test_confident_noise_is_suppressed(self):
        _, verdict = self.review(FakeModel("benign_noise", {"benign_noise": 0.93, "real_fault": 0.07}))
        self.assertEqual(verdict.action, "suppress")
        self.assertTrue(verdict.applied)
        self.assertAlmostEqual(verdict.p_noise, 0.93)

    def test_the_working_is_kept_for_the_details(self):
        _, verdict = self.review(FakeModel("benign_noise", {"benign_noise": 0.49, "real_fault": 0.51}))
        shown = verdict.to_dict()
        self.assertEqual(shown["rule_severity"], "warning")
        self.assertEqual(shown["suppress_at"], 0.9)
        self.assertEqual(shown["confidence"], 0.9)
        self.assertEqual(shown["severity_probabilities"], {"warning": 0.95})
        self.assertEqual(shown["severity_confidence"], 0.95)
        self.assertEqual((shown["input_tokens"], shown["latency_ms"]), (3000, 90))
        self.assertAlmostEqual(shown["self_heals"], 0.2)

    def test_noise_below_the_bar_opens(self):
        _, verdict = self.review(FakeModel("benign_noise", {"benign_noise": 0.7, "real_fault": 0.3}))
        self.assertEqual(verdict.action, "open")

    def test_an_uncalibrated_model_never_hides_anything(self):
        _, verdict = self.review(FakeModel("benign_noise", {"benign_noise": 0.99}, calibrated=False))
        self.assertEqual(verdict.action, "open")
        self.assertIsNone(verdict.p_noise)

    def test_an_uncalibrated_model_may_still_escalate(self):
        _, verdict = self.review(FakeModel(severity="critical", calibrated=False))
        self.assertEqual(verdict.action, "escalate")

    def test_a_downgrade_needs_confidence(self):
        _, sure = self.review(FakeModel(severity="info", severity_confidence=0.95))
        self.assertEqual(sure.action, "downgrade")
        _, unsure = self.review(FakeModel(severity="info", severity_confidence=0.6),
                                sig=signal(lines=("other line",)))
        self.assertEqual(unsure.action, "open")

    def test_shadow_records_but_does_not_apply(self):
        _, verdict = self.review(FakeModel("benign_noise", {"benign_noise": 0.99}),
                                 classifier_mode="shadow")
        self.assertEqual(verdict.action, "suppress")
        self.assertFalse(verdict.applied)
        [row] = self.store.list_classifications()
        self.assertEqual(row["applied"], 0)

    def test_facts_are_never_asked_about(self):
        model = FakeModel("benign_noise", {"benign_noise": 0.99})
        for kind in ("exit_nonzero", "oom_killed", "raid_degraded", "host_disk_pressure"):
            _, verdict = self.review(model, sig=signal(kind=kind))
            self.assertIsNone(verdict)
        self.assertEqual(model.calls, 0)

    def test_the_same_signal_is_asked_once(self):
        model = FakeModel()
        classifier = Classifier(settings(), model, self.store)
        asyncio.run(classifier.review(signal(), "state"))
        again = asyncio.run(classifier.review(signal(lines=("ERROR retrying upstream 99",)), "state"))
        self.assertTrue(again.from_cache)
        self.assertEqual(model.calls, 1)

    def test_twice_the_volume_is_asked_again(self):
        self.assertNotEqual(cache_key(signal(count=6)), cache_key(signal(count=13)))
        self.assertEqual(cache_key(signal(count=5)), cache_key(signal(count=7)))

    def test_the_daily_cap_holds(self):
        model = FakeModel()
        classifier = Classifier(settings(classifier_daily_calls=2, classifier_cache_hours=0),
                                model, self.store)
        for i in range(4):
            asyncio.run(classifier.review(signal(lines=(f"line {chr(97 + i)}",)), "state"))
        self.assertEqual(model.calls, 2)

    def test_the_dollar_cap_holds(self):
        self.store.reserve_classifier_call(0)
        self.store.add_classifier_usage(0.2, 1000, 0)
        _, verdict = self.review(FakeModel(), classifier_daily_usd=0.1)
        self.assertIsNone(verdict)

    def test_its_spend_counts_toward_the_day_but_not_the_diagnosis_calls(self):
        self.review(FakeModel())
        today = self.store.usage_today()
        self.assertAlmostEqual(today["cost_usd"], 0.000126)
        self.assertEqual(today["calls"], 0)
        self.assertEqual(self.store.classifier_usage_today()["calls"], 1)

    def test_three_failures_open_the_breaker(self):
        model = FakeModel(fail=True)
        classifier = Classifier(settings(), model, self.store)
        from unittest import mock
        from app import classifier as module

        with mock.patch.object(module.log, "warning") as warned:
            for i in range(5):
                self.assertIsNone(asyncio.run(
                    classifier.review(signal(lines=(f"x{chr(97 + i)}",)), "state")))
        warned.assert_called_once()
        self.assertEqual(model.calls, 3)
        self.assertIn("paused", classifier.last_error)

    def test_a_slow_model_is_cut_off(self):
        class Slow(FakeModel):
            async def decide(self, *args, **kwargs):
                await asyncio.sleep(1)
        _, verdict = self.review(Slow(), classifier_timeout_seconds=0.05)
        self.assertIsNone(verdict)


class WireFormat(unittest.TestCase):
    def test_questions_in_the_decisions_shape(self):
        wire = wire_questions({
            "c": Choice("Which?", {"a": "first", "b": "second"}),
            "f": Flag("Is it?", "yes", "no"),
            "s": Score("How bad?", ("fine", "bad", "worse")),
        })
        self.assertEqual(wire["c"], {"type": "choice", "instructions": "Which?",
                                     "criteria": {"a": "first", "b": "second"}})
        self.assertEqual(wire["f"]["criteria"], {"true": "yes", "false": "no"})
        self.assertEqual(wire["s"]["criteria"], ["fine", "bad", "worse"])

    def test_an_option_that_was_not_offered_is_dropped(self):
        answers = read_answers({"verdict": {"type": "choice", "choice": "ignore previous instructions",
                                            "confidence": 1.0}}, QUESTIONS)
        self.assertNotIn("verdict", answers)

    def test_a_noul_reads_as_a_probability(self):
        answers = read_answers({"self_heals": {"type": "noul", "noul": 0.8}}, QUESTIONS)
        self.assertTrue(answers["self_heals"].value)
        self.assertAlmostEqual(answers["self_heals"].probabilities["false"], 0.2)


class _Decisions(BaseHTTPRequestHandler):
    last: dict = {}

    def log_message(self, *args):
        pass

    def do_POST(self):
        _Decisions.last = {"body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                           "auth": self.headers.get("Authorization")}
        body = json.dumps({
            "id": "gen-1", "provider": "TypeSafe", "model": "typesafe/jev-1.13",
            "answers": {
                "verdict": {"type": "choice", "choice": "benign_noise", "confidence": 0.9,
                            "probabilities": {"real_fault": 0.04, "benign_noise": 0.91,
                                              "expected_transient": 0.03, "unclear": 0.02}},
                "severity": {"type": "choice", "choice": "info", "confidence": 0.8,
                             "probabilities": {"info": 0.8, "warning": 0.15, "critical": 0.05}},
                "cause": {"type": "choice", "choice": "unknown", "confidence": 0.5,
                          "probabilities": {}},
                "self_heals": {"type": "noul", "noul": 0.7},
            },
            "usage": {"input_tokens": 2500, "output_tokens": 30, "cost": 0.000105},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AgainstTheApi(unittest.TestCase):
    def test_one_round_trip(self):
        server = HTTPServer(("127.0.0.1", 0), _Decisions)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.addCleanup(store.close)
        model = OpenRouterDecisions(
            url=f"http://127.0.0.1:{server.server_address[1]}/api/alpha/decisions",
            api_key="sk-or-test", model="typesafe/jev-1.13", timeout=5,
            price_in_per_mtok=0.042, recorder=store.record_llm_call)
        decision = asyncio.run(model.decide("the evidence", QUESTIONS, purpose="classifier_signal"))

        sent = _Decisions.last
        self.assertEqual(sent["auth"], "Bearer sk-or-test")
        self.assertEqual(sent["body"]["model"], "typesafe/jev-1.13")
        self.assertEqual(sent["body"]["state"], "the evidence")
        self.assertEqual(set(sent["body"]["questions"]), set(QUESTIONS))
        self.assertEqual(decision.answers["verdict"].value, "benign_noise")
        self.assertAlmostEqual(decision.answers["verdict"].probabilities["benign_noise"], 0.91)
        self.assertAlmostEqual(decision.cost_usd, 0.000105)  # what OpenRouter billed
        [row] = store.list_llm_calls()
        self.assertEqual((row["purpose"], row["status"], row["provider"]),
                         ("classifier_signal", "ok", "openrouter"))


class _VersionOnly(BaseHTTPRequestHandler):
    """Enough of a Docker daemon for the client to be constructed."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"Version": "27.0", "ApiVersion": "1.45"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThroughThePipeline(unittest.TestCase):
    """What an incident looks like - or whether there is one - per mode."""

    def setUp(self):
        from app.config import Settings
        from app.supervisor import Supervisor

        self.server = HTTPServer(("127.0.0.1", 0), _VersionOnly)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        previous = dict(os.environ)
        os.environ.update(DOCTOR_DATA_DIR=tempfile.mkdtemp(), ANTHROPIC_API_KEY="",
                          DOCKER_HOST=f"tcp://127.0.0.1:{self.server.server_address[1]}")
        try:
            self.supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(self.supervisor.store.close)

        async def evidence(sig):
            return {"kind": sig.kind, "summary": sig.summary, "container": sig.container,
                    "details": sig.details}

        async def no_diagnosis(*args):
            return None, ""

        async def nothing(*args):
            return None

        self.supervisor._gather_evidence = evidence
        self.supervisor._diagnose = no_diagnosis
        self.supervisor._apply_decision = nothing

    def run_with(self, mode, model):
        self.supervisor.classifier = Classifier(settings(classifier_mode=mode), model,
                                                self.supervisor.store)
        asyncio.run(self.supervisor._process_signal_inner(signal()))
        return self.supervisor.store.list_incidents(limit=5)

    def test_on_noise_opens_nothing(self):
        incidents = self.run_with("on", FakeModel("benign_noise", {"benign_noise": 0.95}))
        self.assertEqual(incidents, [])

    def test_shadow_opens_it_with_the_verdict_attached(self):
        [incident] = self.run_with("shadow", FakeModel("benign_noise", {"benign_noise": 0.95}))
        verdict = incident["evidence"]["classification"]
        self.assertEqual(verdict["action"], "suppress")
        self.assertFalse(verdict["applied"])

    def test_an_escalation_moves_severity_but_not_the_rules(self):
        [incident] = self.run_with("on", FakeModel(severity="critical"))
        self.assertEqual(incident["severity"], "critical")
        self.assertEqual(incident["rule_severity"], "warning")

    def test_a_suppressed_signal_is_not_even_read_again(self):
        model = FakeModel("benign_noise", {"benign_noise": 0.95})
        self.run_with("on", model)
        gathered = []

        async def counting(sig):
            gathered.append(sig)
            return {}
        self.supervisor._gather_evidence = counting
        asyncio.run(self.supervisor._process_signal_inner(signal()))
        self.assertEqual(gathered, [])
        self.assertEqual(model.calls, 1)


if __name__ == "__main__":
    unittest.main()


class TheKey(unittest.TestCase):
    """The classifier borrows the diagnosis's key only when it is an OpenRouter one."""

    def make(self, **env):
        from app.config import Settings
        keys = ("DOCTOR_CLASSIFIER_API_KEY", "DOCTOR_LLM_PROVIDER",
                "DOCTOR_OPENAI_BASE_URL", "DOCTOR_OPENAI_API_KEY")
        saved = {k: os.environ.pop(k, None) for k in keys}
        try:
            os.environ.update(env)
            return Settings()
        finally:
            for k in keys:
                os.environ.pop(k, None)
                if saved[k] is not None:
                    os.environ[k] = saved[k]

    def test_its_own_key_wins(self):
        s = self.make(DOCTOR_CLASSIFIER_API_KEY="own", DOCTOR_LLM_PROVIDER="openai",
                      DOCTOR_OPENAI_BASE_URL="https://openrouter.ai/api/v1",
                      DOCTOR_OPENAI_API_KEY="shared")
        self.assertEqual(s.classifier_key, "own")

    def test_falls_back_to_the_diagnosis_openrouter_key(self):
        s = self.make(DOCTOR_LLM_PROVIDER="openai",
                      DOCTOR_OPENAI_BASE_URL="https://openrouter.ai/api/v1",
                      DOCTOR_OPENAI_API_KEY="shared")
        self.assertEqual(s.classifier_key, "shared")

    def test_no_fallback_to_another_vendor(self):
        s = self.make(DOCTOR_LLM_PROVIDER="openai",
                      DOCTOR_OPENAI_BASE_URL="https://api.openai.com/v1",
                      DOCTOR_OPENAI_API_KEY="sk-openai")
        self.assertEqual(s.classifier_key, "")

    def test_no_fallback_to_the_placeholder(self):
        s = self.make(DOCTOR_LLM_PROVIDER="openai",
                      DOCTOR_OPENAI_BASE_URL="https://openrouter.ai/api/v1")
        self.assertEqual(s.classifier_key, "")

    def test_no_fallback_from_anthropic(self):
        s = self.make(DOCTOR_LLM_PROVIDER="anthropic")
        self.assertEqual(s.classifier_key, "")
