"""Tests for the parts that must not be wrong.

Stdlib only - run from the `container-doctor` directory with:

    python -m unittest discover -s tests -v

These cover the safety-critical behaviour: secrets never leaving the host,
failure fingerprints staying stable, and the policy engine refusing to act.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import ContainerPolicy, Settings, VALID_ACTIONS  # noqa: E402
from app.detector import Detector  # noqa: E402
from app.diagnoser import _coerce, _cost, _loads_lenient, _retry_after  # noqa: E402
from app.docker_client import _parse_stats  # noqa: E402
from app.preflight import PROBES, Result, http_base, summarize  # noqa: E402
from app.redact import error_lines, fingerprint, redact, redact_env, redact_obj  # noqa: E402
from app.remediator import Policy  # noqa: E402
from app.store import Store  # noqa: E402


# Sentinel so a test can pass diagnosis=None and mean it.
_DEFAULT = object()

# The coercion test deliberately feeds the policy a bad action; don't print it.
logging.disable(logging.WARNING)


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


# Every store a test makes, closed when the module's tests are done: most
# tests here make one in passing and would otherwise leave it open.
_STORES: list[Store] = []


def make_store() -> Store:
    store = Store(os.path.join(tempfile.mkdtemp(), "test.db"))
    _STORES.append(store)
    return store


def tearDownModule() -> None:
    for store in _STORES:
        store.close()


class RedactionTests(unittest.TestCase):
    def test_common_secret_shapes(self):
        cases = [
            "token=sk-ant-api03-abcdefghijklmnop",
            "password=hunter2hunter2",
            "Authorization: Bearer abcdefghijklmnopqrst",
            "postgres://user:s3cr3tpw@db:5432/app",
            'API_KEY": "abcdefghijklmnop"',
            "ghp_abcdefghijklmnopqrstuvwxyz0123",
            "AKIAIOSFODNN7EXAMPLE",
            "eyJhbGciOi.eyJzdWIiOiI.SflKxwRJSMe",
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertIn("[redacted]", redact(case))

    def test_useful_context_survives(self):
        line = "ERROR failed to open database: connection refused (db:5432)"
        self.assertEqual(redact(line), line)

    def test_redact_obj_walks_nested_structures(self):
        payload = {
            "lines": ["FATAL token=sk-ant-supersecretvalue1234"],
            "nested": {"dsn": "postgres://u:p4ssw0rd123@db/x"},
            "count": 5,
            "flag": True,
        }
        out = redact_obj(payload)
        self.assertNotIn("supersecret", str(out))
        self.assertNotIn("p4ssw0rd123", str(out))
        # Non-string values pass through untouched.
        self.assertEqual(out["count"], 5)
        self.assertIs(out["flag"], True)

    def test_error_lines_are_redacted_at_source(self):
        pattern = re.compile("(?i)fatal")
        lines = error_lines("FATAL boom api_key=abcdefghijklmn\n", pattern)
        self.assertNotIn("abcdefghijklmn", lines[0])

    def test_env_allowlist(self):
        out = redact_env(["TZ=CET", "MEMOS_PORT=5230", "APP_KEY=verysecretvalue"])
        self.assertIn("TZ=CET", out)
        self.assertIn("MEMOS_PORT=5230", out)
        self.assertIn("APP_KEY=[redacted]", out)


class FingerprintTests(unittest.TestCase):
    def test_stable_across_timestamps_and_ids(self):
        a = fingerprint("memos", "log_errors", "2026-09-18T11:00:02Z ERROR refused req=a1b2c3d4e5f6")
        b = fingerprint("memos", "log_errors", "2026-09-19T04:31:59Z ERROR refused req=0f9e8d7c6b5a")
        self.assertEqual(a, b)

    def test_stable_across_rotated_secrets(self):
        a = fingerprint("memos", "log_errors", redact("FATAL token=AAAAAAAAAAAAAAAA"))
        b = fingerprint("memos", "log_errors", redact("FATAL token=BBBBBBBBBBBBBBBB"))
        self.assertEqual(a, b)

    def test_different_failures_differ(self):
        self.assertNotEqual(
            fingerprint("memos", "log_errors", "ERROR connection refused"),
            fingerprint("memos", "log_errors", "ERROR permission denied"),
        )


class DiagnosisCoercionTests(unittest.TestCase):
    def test_action_outside_the_enum_is_neutered(self):
        out = _coerce({"recommended_action": "rm -rf /", "confidence": 0.9})
        self.assertEqual(out["recommended_action"], "notify_only")

    def test_every_valid_action_survives(self):
        for action in VALID_ACTIONS:
            self.assertEqual(_coerce({"recommended_action": action})["recommended_action"], action)

    def test_confidence_is_clamped(self):
        self.assertEqual(_coerce({"confidence": 7.5})["confidence"], 1.0)
        self.assertEqual(_coerce({"confidence": -3})["confidence"], 0.0)
        self.assertEqual(_coerce({"confidence": "nonsense"})["confidence"], 0.0)

    def test_missing_fields_get_safe_defaults(self):
        out = _coerce({})
        self.assertEqual(out["recommended_action"], "notify_only")
        self.assertEqual(out["severity"], "warning")
        self.assertEqual(out["human_fix_steps"], [])


class PricingTests(unittest.TestCase):
    def test_known_anthropic_model_is_priced_from_the_table(self):
        # 1M in + 1M out on Opus 5 at $5/$25 per MTok.
        self.assertAlmostEqual(_cost("claude-opus-5", 1_000_000, 1_000_000, 0), 30.0)

    def test_unknown_model_is_free_rather_than_guessed(self):
        self.assertEqual(_cost("some-vendor/some-model", 1_000_000, 1_000_000, 0), 0.0)

    def test_operator_override_prices_any_vendor(self):
        cost = _cost("gemini-3-flash", 1_000_000, 1_000_000, 0, override=(0.30, 2.50))
        self.assertAlmostEqual(cost, 2.80)

    def test_override_beats_the_builtin_table(self):
        # e.g. a discounted or batch rate the operator actually pays.
        self.assertAlmostEqual(
            _cost("claude-opus-5", 1_000_000, 0, 0, override=(1.0, 1.0)), 1.0
        )

    def test_cached_tokens_bill_at_a_tenth(self):
        self.assertAlmostEqual(_cost("claude-opus-5", 0, 0, 1_000_000), 0.5)

    def test_custom_pricing_is_off_unless_configured(self):
        self.assertIsNone(Settings().custom_pricing)
        configured = make_settings(DOCTOR_PRICE_IN_PER_MTOK="0.3", DOCTOR_PRICE_OUT_PER_MTOK="2.5")
        self.assertEqual(configured.custom_pricing, (0.3, 2.5))

    def test_a_free_model_can_be_priced_at_zero_explicitly(self):
        free = make_settings(DOCTOR_PRICE_IN_PER_MTOK="0", DOCTOR_PRICE_OUT_PER_MTOK="0")
        self.assertEqual(free.custom_pricing, (0.0, 0.0))


class RequestDegradationTests(unittest.TestCase):
    """Vendors reject different parameters; the request shape adapts one at a time."""

    def _shapes(self, reject) -> list[tuple[bool, bool]]:
        """Replay the ladder against a predicate saying which shapes 400."""
        import asyncio as _asyncio

        from app.diagnoser import Diagnoser

        seen: list[tuple[bool, bool]] = []

        class FakeResponse:
            def __init__(self, bad):
                self.status_code = 400 if bad else 200
                self.text = "unsupported parameter"

            def json(self):
                return {
                    "choices": [{"message": {"content": '{"summary":"s","root_cause":"r",'
                                                        '"confidence":0.8,"severity":"warning",'
                                                        '"is_transient":true,'
                                                        '"recommended_action":"notify_only",'
                                                        '"action_rationale":"a",'
                                                        '"human_fix_steps":[],"evidence_used":[]}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }

        settings = make_settings(
            DOCTOR_LLM_PROVIDER="openai",
            DOCTOR_OPENAI_BASE_URL="http://stub/v1",
            DOCTOR_MODEL="stub",
        )
        diagnoser = Diagnoser(settings)

        async def fake_post(client, url, payload, headers):
            shape = ("temperature" in payload,
                     payload.get("response_format", {}).get("type") == "json_schema")
            seen.append(shape)
            return FakeResponse(reject(*shape))

        diagnoser._post_with_retries = fake_post  # type: ignore[assignment]
        _asyncio.run(diagnoser.diagnose({"kind": "unhealthy", "container": {}}))
        return seen

    def test_a_vendor_that_takes_everything_asks_once(self):
        shapes = self._shapes(lambda temp, schema: False)
        self.assertEqual(shapes, [(True, True)])

    def test_rejecting_temperature_keeps_schema_enforcement(self):
        # GPT-6 Luna refuses `temperature` at any reasoning effort above none,
        # but does support structured outputs - dropping the schema too would
        # give away the guarantee for no reason.
        shapes = self._shapes(lambda temp, schema: temp)
        self.assertEqual(shapes, [(True, True), (False, True)])
        self.assertTrue(shapes[-1][1], "the accepted request must still carry the schema")

    def test_rejecting_the_schema_walks_all_the_way_down(self):
        shapes = self._shapes(lambda temp, schema: schema)
        self.assertEqual(shapes, [(True, True), (False, True), (False, False)])

    def test_rejecting_both_still_lands_on_a_working_shape(self):
        shapes = self._shapes(lambda temp, schema: temp or schema)
        self.assertEqual(shapes[-1], (False, False))

    def test_temperature_can_be_switched_off_up_front(self):
        settings = make_settings(DOCTOR_LLM_TEMPERATURE="-1")
        self.assertLess(settings.llm_temperature, 0)


class VendorQuirkTests(unittest.TestCase):
    """Things real OpenAI-compatible endpoints do that the spec does not."""

    def test_plain_json_parses(self):
        self.assertEqual(_loads_lenient('{"a": 1}'), {"a": 1})

    def test_markdown_fenced_json_parses(self):
        content = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps!'
        self.assertEqual(_loads_lenient(content), {"a": 1})

    def test_json_with_surrounding_prose_parses(self):
        self.assertEqual(_loads_lenient('Sure! {"a": 1} Let me know.'), {"a": 1})

    def test_non_json_raises_rather_than_returning_junk(self):
        with self.assertRaises(ValueError):
            _loads_lenient("I cannot help with that.")

    def test_retry_after_seconds_is_honoured(self):
        self.assertEqual(_retry_after(_response({"retry-after": "7"})), 7.0)

    def test_retry_after_is_capped(self):
        self.assertEqual(_retry_after(_response({"retry-after": "99999"})), 120.0)

    def test_missing_or_unparseable_retry_after_falls_back_to_backoff(self):
        self.assertIsNone(_retry_after(_response({})))
        self.assertIsNone(_retry_after(_response({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})))


def _response(headers: dict[str, str]):
    import httpx

    return httpx.Response(429, headers=headers)


class FreeTierGuardTests(unittest.TestCase):
    def test_openai_provider_without_a_call_cap_warns(self):
        settings = make_settings(
            DOCTOR_LLM_PROVIDER="openai",
            DOCTOR_OPENAI_BASE_URL="http://ollama:11434/v1",
            DOCTOR_DAILY_CALL_LIMIT="0",
        )
        self.assertTrue(any("DOCTOR_DAILY_CALL_LIMIT" in w for w in settings.validate()))

    def test_a_configured_call_cap_clears_the_warning(self):
        settings = make_settings(
            DOCTOR_LLM_PROVIDER="openai",
            DOCTOR_OPENAI_BASE_URL="http://ollama:11434/v1",
            DOCTOR_DAILY_CALL_LIMIT="200",
        )
        self.assertFalse(any("DOCTOR_DAILY_CALL_LIMIT" in w for w in settings.validate()))

    def test_reserving_a_slot_counts_it_immediately(self):
        store = make_store()
        self.assertTrue(store.reserve_call(3))
        # The count moves before the request goes out, not after it returns.
        self.assertEqual(store.usage_today()["calls"], 1)

    def test_the_cap_is_exact_not_approximate(self):
        store = make_store()
        granted = [store.reserve_call(2) for _ in range(5)]
        self.assertEqual(granted, [True, True, False, False, False])
        self.assertEqual(store.usage_today()["calls"], 2)

    def test_concurrent_reservations_cannot_overshoot(self):
        import threading

        store = make_store()
        results: list[bool] = []
        lock = threading.Lock()

        def claim():
            granted = store.reserve_call(5)
            with lock:
                results.append(granted)

        threads = [threading.Thread(target=claim) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 5, "exactly five callers should win the quota")
        self.assertEqual(store.usage_today()["calls"], 5)

    def test_zero_limit_means_unlimited(self):
        store = make_store()
        self.assertTrue(all(store.reserve_call(0) for _ in range(50)))

    def test_a_released_slot_is_reusable(self):
        store = make_store()
        self.assertTrue(store.reserve_call(1))
        self.assertFalse(store.reserve_call(1))
        store.release_call()
        self.assertTrue(store.reserve_call(1))

    def test_tokens_are_recorded_without_double_counting_the_call(self):
        store = make_store()
        store.reserve_call(0)
        store.add_usage(input_tokens=100, output_tokens=10, cached_tokens=0, cost_usd=0.0)
        today = store.usage_today()
        self.assertEqual(today["calls"], 1, "add_usage must not increment the call count again")
        self.assertEqual(today["input_tokens"], 100)
        # A free tier reports zero cost, so only the call count can stop it.
        self.assertEqual(today["cost_usd"], 0.0)


class DockerEndpointTests(unittest.TestCase):
    """The doctor may be pointed at a proxy someone else configured."""

    HOST = "tcp://doctor-socket-proxy:2375"

    @staticmethod
    def results(**allowed: bool) -> list[Result]:
        """Build a probe result set; anything unnamed takes its safe default."""
        defaults = {
            "ping": True, "version": True, "containers": True, "events": True,
            "write": True, "images": False, "networks": True,
            "exec": False, "volumes": False, "secrets": False,
            "swarm": False, "info": False,
        }
        defaults.update(allowed)
        return [
            Result(probe, defaults[probe.name], f"stub {defaults[probe.name]}")
            for probe in PROBES
        ]

    def test_docker_host_forms_are_understood(self):
        self.assertEqual(http_base("tcp://proxy:2375"), ("http://proxy:2375", None))
        self.assertEqual(http_base("http://proxy:2375"), ("http://proxy:2375", None))
        self.assertEqual(
            http_base("unix:///var/run/docker.sock"),
            ("http://localhost", "/var/run/docker.sock"),
        )

    def test_the_bundled_proxy_is_exactly_right(self):
        problems, warnings = summarize(self.HOST, self.results(), mode="auto")
        self.assertEqual(problems, [])
        self.assertEqual(warnings, [])

    def test_a_proxy_missing_the_event_stream_is_a_problem(self):
        problems, _ = summarize(self.HOST, self.results(events=False), mode="observe")
        self.assertTrue(any("events" in p for p in problems))

    def test_a_read_only_proxy_is_fine_for_observe_but_not_for_auto(self):
        observe_problems, observe_warnings = summarize(
            self.HOST, self.results(write=False), mode="observe"
        )
        self.assertEqual(observe_problems, [])
        self.assertTrue(any("read-only" in w for w in observe_warnings))

        auto_problems, _ = summarize(self.HOST, self.results(write=False), mode="auto")
        self.assertTrue(any("auto" in p for p in auto_problems))

    def test_an_over_wide_proxy_is_flagged_but_not_blocking(self):
        problems, warnings = summarize(
            self.HOST, self.results(exec=True, volumes=True, secrets=True), mode="observe"
        )
        self.assertEqual(problems, [], "a wide proxy still works; it is a warning, not a stop")
        joined = " ".join(warnings)
        for section in ("exec", "volumes", "secrets"):
            self.assertIn(section, joined)

    def test_a_raw_socket_is_called_out(self):
        _, warnings = summarize("unix:///var/run/docker.sock", self.results(), mode="observe")
        self.assertTrue(any("raw Docker socket" in w for w in warnings))

    def test_an_unreachable_endpoint_reports_once_and_clearly(self):
        dead = [Result(probe, False, "unreachable: ConnectError") for probe in PROBES]
        problems, warnings = summarize(self.HOST, dead, mode="observe")
        self.assertEqual(len(problems), 1)
        self.assertIn("unreachable", problems[0])
        self.assertEqual(warnings, [], "no point listing capabilities of something that is down")

    def test_the_dangerous_sections_are_actually_checked(self):
        checked = {p.name for p in PROBES if p.need == "should_be_blocked"}
        self.assertTrue({"exec", "volumes", "secrets", "swarm"} <= checked)

    def test_write_probe_cannot_affect_a_real_container(self):
        write = next(p for p in PROBES if p.name == "write")
        self.assertEqual(write.method, "POST")
        # A 64-zero id is well-formed and can never match a real container.
        self.assertIn("0" * 64, write.path)

    def test_read_probes_never_use_a_mutating_method(self):
        for probe in PROBES:
            if probe.name != "write":
                self.assertEqual(probe.method, "GET", f"{probe.name} must be a read")


class PromptEvidenceTests(unittest.TestCase):
    """The lines that fired the rule have to survive into the prompt.

    They used to go through `json.dumps(details)[:2000]`, so ten access-log
    lines were escaped and then cut off mid-array. The model said so itself:
    "the incident excerpt does not show the 10 lines that triggered the rule".
    """

    def render(self, lines):
        from app.diagnoser import _render_incident

        return _render_incident({
            "kind": "log_errors",
            "summary": f"traefik logged {len(lines)} lines matching the error pattern",
            "container": {"name": "traefik"},
            "details": {"matched_lines": lines, "match_count": len(lines), "source": "keyword"},
        })

    def test_every_triggering_line_is_present_in_full(self):
        lines = [f"2026-09-24 16:0{n}:00 ERROR upstream {n} timed out after 30s" for n in range(10)]
        prompt = self.render(lines)
        for line in lines:
            self.assertIn(line, prompt)

    def test_long_lines_are_not_dropped_by_the_json_cap(self):
        lines = [f"{n} " + "x" * 300 + " connection refused" for n in range(10)]
        prompt = self.render(lines)
        self.assertEqual(prompt.count("connection refused"), 10)

    def test_the_rest_of_the_details_still_render(self):
        prompt = self.render(["boom"])
        self.assertIn("match_count", prompt)
        self.assertIn("The 1 line that triggered the rule", prompt)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(
            DOCTOR_MODE="auto",
            DOCTOR_MIN_CONFIDENCE="0.75",
            DOCTOR_AUTO_ACTIONS="restart_container",
            DOCTOR_MAX_ACTIONS_PER_CONTAINER_PER_HOUR="2",
            DOCTOR_MAX_ACTIONS_GLOBAL_PER_HOUR="3",
            DOCTOR_ACTION_COOLDOWN_MINUTES="30",
        )
        self.store = make_store()
        self.policy = Policy(self.settings, self.store)

    def incident(self, name="memos", fingerprint_="fp1"):
        incident_id = self.store.open_incident(
            container_id="cid", container_name=name, image="img", kind="unhealthy",
            fingerprint=fingerprint_, severity="warning", summary="s", evidence={}, mode="auto",
        )
        return self.store.get_incident(incident_id)

    @staticmethod
    def diagnosis(action="restart_container", confidence=0.9):
        return {"recommended_action": action, "confidence": confidence, "action_rationale": "r"}

    #  `diagnosis` defaults to a confident restart proposal; pass None
    #  explicitly via the _NO_DIAGNOSIS sentinel to test the undiagnosed path.
    def decide(self, incident=None, diagnosis=_DEFAULT, policy=None):
        return self.policy.decide(
            incident=incident or self.incident(),
            diagnosis=self.diagnosis() if diagnosis is _DEFAULT else diagnosis,
            container_policy=policy or ContainerPolicy(),
        )

    def test_confident_allowed_action_is_approved(self):
        self.assertEqual(self.decide().status, "approved")

    def test_low_confidence_needs_a_human(self):
        self.assertEqual(self.decide(diagnosis=self.diagnosis(confidence=0.5)).status, "pending")

    def test_action_outside_the_allowlist_is_skipped(self):
        self.assertEqual(self.decide(diagnosis=self.diagnosis(action="stop_container")).status, "skipped")

    def test_no_diagnosis_means_no_action(self):
        self.assertEqual(self.decide(diagnosis=None).status, "skipped")

    def test_disabled_container_is_never_touched(self):
        self.assertEqual(self.decide(policy=ContainerPolicy(enabled=False)).status, "skipped")

    def test_container_can_pin_itself_to_observe(self):
        self.assertEqual(self.decide(policy=ContainerPolicy(mode="observe")).status, "skipped")

    def test_suggest_mode_waits_for_approval(self):
        self.assertEqual(self.decide(policy=ContainerPolicy(mode="suggest")).status, "pending")

    def test_doctor_never_acts_on_itself_or_its_socket_proxy(self):
        for name in ("container-doctor", "container-doctor-socket-proxy"):
            with self.subTest(name=name):
                decision = self.decide(incident=self.incident(name=name))
                self.assertEqual(decision.status, "skipped")

    def test_per_container_circuit_breaker(self):
        incident = self.incident()
        for _ in range(2):  # the configured cap
            self.store.record_action(
                incident_id=incident["id"], container_name="memos",
                action="restart_container", status="succeeded", decided_by="policy",
            )
        decision = self.decide(incident=incident)
        self.assertEqual(decision.status, "pending")
        self.assertIn("circuit breaker", decision.reason)

    def test_global_circuit_breaker(self):
        incident = self.incident()
        for name in ("a", "b", "c"):  # three across the host, the global cap
            self.store.record_action(
                incident_id=incident["id"], container_name=name,
                action="restart_container", status="succeeded", decided_by="policy",
            )
        decision = self.decide(incident=self.incident(name="d"))
        self.assertEqual(decision.status, "pending")
        self.assertIn("circuit breaker", decision.reason)

    def test_same_failure_is_not_retried_inside_the_cooldown(self):
        first = self.incident(fingerprint_="samefp")
        self.store.record_action(
            incident_id=first["id"], container_name="memos",
            action="restart_container", status="failed", decided_by="policy",
        )
        decision = self.decide(incident=self.incident(fingerprint_="samefp"))
        self.assertEqual(decision.status, "pending")
        self.assertIn("cooldown", decision.reason)


class StoreTests(unittest.TestCase):
    def test_diagnosis_cache_ttl(self):
        store = make_store()
        store.cache_diagnosis("fp", {"summary": "cached"})
        self.assertEqual(store.cached_diagnosis("fp", 6)["summary"], "cached")
        self.assertIsNone(store.cached_diagnosis("fp", 0), "a zero TTL disables the cache")
        self.assertIsNone(store.cached_diagnosis("unknown", 6))

    def test_only_executed_actions_count_against_the_breaker(self):
        store = make_store()
        from datetime import datetime, timedelta, timezone

        incident_id = store.open_incident(
            container_id="c", container_name="memos", image="i", kind="unhealthy",
            fingerprint="f", severity="warning", summary="s", evidence={}, mode="auto",
        )
        for status in ("skipped", "rejected", "pending"):
            store.record_action(incident_id=incident_id, container_name="memos",
                                action="restart_container", status=status, decided_by="policy")
        hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        self.assertEqual(store.count_executed_actions(since=hour_ago), 0)
        store.record_action(incident_id=incident_id, container_name="memos",
                            action="restart_container", status="succeeded", decided_by="policy")
        self.assertEqual(store.count_executed_actions(since=hour_ago), 1)


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(DOCTOR_RESOURCE_BREACH_POLLS="2", DOCTOR_CPU_THRESHOLD="85")
        self.detector = Detector(self.settings)

    @staticmethod
    def container(**overrides):
        base = {
            "id": "cid", "name": "memos", "image": "img", "state": "running",
            "health": "none", "exit_code": 0, "restart_count": 0, "labels": {},
        }
        base.update(overrides)
        return base

    def test_clean_exit_is_not_an_incident(self):
        event = {"Action": "die", "Actor": {"Attributes": {"name": "memos", "exitCode": "0"}}}
        self.assertIsNone(self.detector.from_event(event, self.container(state="exited")))

    def test_failed_exit_is_an_incident(self):
        event = {"Action": "die", "Actor": {"Attributes": {"name": "memos", "exitCode": "1"}}}
        signal = self.detector.from_event(event, self.container(state="exited"))
        self.assertEqual(signal.kind, "exit_nonzero")
        self.assertEqual(signal.severity, "critical")

    def test_sigterm_exit_found_on_poll_is_treated_as_intentional(self):
        container = self.container(state="exited", exit_code=143)
        self.assertEqual(self.detector.from_poll(container, None, ""), [])

    def test_stopped_container_reports_once_only(self):
        container = self.container(state="exited", exit_code=137)
        first = self.detector.from_poll(container, None, "")
        second = self.detector.from_poll(container, None, "")
        self.assertEqual([s.kind for s in first], ["exit_nonzero"])
        self.assertEqual(second, [])

    def test_unhealthy_needs_two_consecutive_polls(self):
        container = self.container(health="unhealthy")
        self.assertEqual(self.detector.from_poll(container, None, ""), [])
        signals = self.detector.from_poll(container, None, "")
        self.assertEqual([s.kind for s in signals], ["unhealthy"])

    def test_cpu_threshold_is_a_share_of_the_host_not_of_one_core(self):
        # 200% in `docker stats` terms on a 4-core host is 50% of the machine,
        # which must not trip an 85%-of-host threshold.
        stats = {"cpu_pct": 200.0, "cpu_host_pct": 50.0, "online_cpus": 4,
                 "mem_usage_bytes": 1, "mem_limit_bytes": 0, "mem_pct": 0.0}
        container = self.container()
        for _ in range(4):
            self.assertEqual(self.detector.from_poll(container, stats, ""), [])

    def test_sustained_host_cpu_breach_fires_once(self):
        stats = {"cpu_pct": 360.0, "cpu_host_pct": 90.0, "online_cpus": 4,
                 "mem_usage_bytes": 1, "mem_limit_bytes": 0, "mem_pct": 0.0}
        container = self.container()
        kinds = [s.kind for _ in range(4) for s in self.detector.from_poll(container, stats, "")]
        self.assertEqual(kinds, ["high_cpu"])


class StatsParsingTests(unittest.TestCase):
    def test_cpu_and_memory_are_derived_correctly(self):
        raw = {
            "cpu_stats": {"cpu_usage": {"total_usage": 200_000_000},
                          "system_cpu_usage": 1_000_000_000, "online_cpus": 4},
            "precpu_stats": {"cpu_usage": {"total_usage": 100_000_000},
                             "system_cpu_usage": 900_000_000},
            "memory_stats": {"usage": 900_000_000, "limit": 1_000_000_000,
                             "stats": {"inactive_file": 100_000_000}},
        }
        stats = _parse_stats(raw)
        # (100M / 100M) * 4 cores * 100 = 400% of one core = 100% of the host.
        self.assertAlmostEqual(stats["cpu_pct"], 400.0)
        self.assertAlmostEqual(stats["cpu_host_pct"], 100.0)
        # Page cache is excluded, matching what `docker stats` shows.
        self.assertEqual(stats["mem_usage_bytes"], 800_000_000)
        self.assertAlmostEqual(stats["mem_pct"], 80.0)

    def test_garbage_stats_do_not_raise(self):
        stats = _parse_stats({})
        self.assertEqual(stats["cpu_pct"], 0.0)
        self.assertEqual(stats["mem_pct"], 0.0)


class LabelPolicyTests(unittest.TestCase):
    def test_labels_are_parsed(self):
        policy = ContainerPolicy.from_labels({
            "doctor.enable": "false",
            "doctor.mode": "auto",
            "doctor.actions": "restart_container, stop_container",
            "doctor.max_actions_per_hour": "1",
            "doctor.critical": "true",
        })
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.mode, "auto")
        self.assertEqual(policy.actions, ["restart_container", "stop_container"])
        self.assertEqual(policy.max_actions_per_hour, 1)
        self.assertTrue(policy.critical)

    def test_absent_labels_mean_defaults(self):
        policy = ContainerPolicy.from_labels({})
        self.assertTrue(policy.enabled)
        self.assertIsNone(policy.mode)
        self.assertIsNone(policy.actions)

    def test_a_nonsense_mode_label_is_ignored(self):
        self.assertIsNone(ContainerPolicy.from_labels({"doctor.mode": "yolo"}).mode)


if __name__ == "__main__":
    unittest.main()
