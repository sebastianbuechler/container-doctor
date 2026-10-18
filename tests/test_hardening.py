"""What keeps the dashboard, the stored data and the actions safe to expose.

Each case here is one a pre-publication audit found open: a dashboard that
answered anyone when no sign-in was set, writes another site could make with
a browser's cached password, secrets the redaction let through, command lines
kept as given, a pending action two approvals could run twice, a NaN
confidence that passed every threshold, a stop recorded as a failure, and a
re-diagnosis that ignored the dollar budget.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import runconfig  # noqa: E402
from app.config import ContainerPolicy, Settings  # noqa: E402
from app.diagnoser import _coerce  # noqa: E402
from app.redact import REDACTED, redact, redact_obj  # noqa: E402
from app.remediator import Executor, Policy  # noqa: E402
from app.store import Store  # noqa: E402
from app.supervisor import Supervisor  # noqa: E402
from tests.test_incident_logs import NEEDS_APP, StubSupervisor, make_settings  # noqa: E402

PASSWORD = "a-long-test-password"


class ActingSupervisor(StubSupervisor):
    """The API's writes, recorded instead of done."""

    def __init__(self):
        super().__init__()
        self.approved: list[str] = []
        self.store = SimpleNamespace(dismiss_all=lambda **kwargs: 3)
        self.broadcast = lambda *args: None

    async def approve_action(self, action_id, actor=""):
        self.approved.append(action_id)
        return {"ok": True}


def client(*, signed_in=True, **env):
    from fastapi.testclient import TestClient

    from app.web.api import build_app

    env = {"DOCTOR_OIDC_ISSUER": "", "DOCTOR_WEB_USER": "doctor",
           "DOCTOR_WEB_PASSWORD": PASSWORD, **env}
    supervisor = ActingSupervisor()
    test_client = TestClient(build_app(supervisor, make_settings(**env)))
    if signed_in:
        test_client.auth = ("doctor", env["DOCTOR_WEB_PASSWORD"])
    return test_client, supervisor


@NEEDS_APP
class NoSignInConfigured(unittest.TestCase):
    def setUp(self):
        self.client, _ = client(signed_in=False, DOCTOR_WEB_PASSWORD="")
        self.addCleanup(self.client.close)

    def test_the_api_refuses_reads(self):
        for path in ("/api/status", "/api/incidents", "/api/changes", "/api/llm/calls"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 403)
                self.assertIn("DOCTOR_WEB_PASSWORD", response.json()["detail"])

    def test_the_page_and_the_health_check_still_answer(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
        me = self.client.get("/api/me").json()
        self.assertEqual((me["authenticated"], me["configured"]), (False, False))

    def test_a_placeholder_password_is_no_password(self):
        for word in ("change-me", "password", "doctor"):
            with self.subTest(word=word):
                self.assertEqual(make_settings(DOCTOR_WEB_PASSWORD=word,
                                               DOCTOR_WEB_USER="doctor").web_password, "")
        self.assertEqual(make_settings(DOCTOR_WEB_PASSWORD=PASSWORD).web_password, PASSWORD)


@NEEDS_APP
class CrossSiteWrites(unittest.TestCase):
    WRITES = ("/api/actions/a1/approve", "/api/incidents/bulk-dismiss")

    def setUp(self):
        self.client, self.supervisor = client()
        self.addCleanup(self.client.close)

    def test_another_origin_is_refused_on_every_write(self):
        for origin in ("https://attacker.example", "null", "https://testserver"):
            for path in self.WRITES:
                with self.subTest(origin=origin, path=path):
                    # A form post: no JSON body, so no preflight would stop it.
                    response = self.client.post(path, headers={"origin": origin})
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(self.supervisor.approved, [])

    def test_a_browser_without_origin_is_judged_by_fetch_metadata(self):
        response = self.client.post(self.WRITES[0], headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(response.status_code, 403)

    def test_the_dashboard_itself_and_non_browsers_get_through(self):
        same = self.client.post(self.WRITES[0], headers={"origin": "http://testserver"})
        self.assertEqual(same.status_code, 200)
        self.assertEqual(self.client.post(self.WRITES[1]).status_code, 200)
        self.assertEqual(self.supervisor.approved, ["a1"])


@NEEDS_APP
class Responses(unittest.TestCase):
    def setUp(self):
        self.client, _ = client()
        self.addCleanup(self.client.close)

    def test_every_response_keeps_out_of_frames(self):
        response = self.client.get("/")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_the_offline_page_may_run_its_own_script(self):
        policy = self.client.get("/static/offline.html").headers["content-security-policy"]
        self.assertIn("'sha256-", policy)

    def test_the_script_is_compressed(self):
        response = self.client.get("/static/app.js", headers={"accept-encoding": "gzip"})
        self.assertEqual(response.headers.get("content-encoding"), "gzip")

    def test_the_page_has_no_inline_handlers(self):
        script = self.client.get("/static/app.js").text
        self.assertNotIn("onerror=", script)


@NEEDS_APP
class PasswordGuessing(unittest.TestCase):
    def test_repeated_wrong_passwords_are_refused_for_a_while(self):
        guesser, _ = client(signed_in=False)
        self.addCleanup(guesser.close)
        codes = [guesser.get("/api/status", auth=("doctor", f"guess{i}")).status_code
                 for i in range(11)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10], 429)
        # Even the right password, until the window is over.
        self.assertEqual(guesser.get("/api/status", auth=("doctor", PASSWORD)).status_code, 429)

    def test_any_characters_are_a_wrong_password_not_an_error(self):
        guesser, _ = client(signed_in=False)
        self.addCleanup(guesser.close)
        self.assertEqual(guesser.get("/api/status", auth=("dóctor", "pässwörd")).status_code, 401)


class EnvironmentOnly(unittest.TestCase):
    def test_where_secrets_go_and_who_signs_in_cannot_be_saved_from_the_page(self):
        from app.config import _unusable, editable

        for env in ("DOCTOR_OPENAI_BASE_URL", "DOCTOR_CLASSIFIER_URL", "DOCTOR_NTFY_URL",
                    "DOCTOR_OIDC_ISSUER", "DOCTOR_OIDC_CLIENT_ID", "DOCTOR_OIDC_ALLOWED_GROUPS",
                    "DOCTOR_PUBLIC_URL", "DOCTOR_WEB_USER", "DOCTOR_WEB_PASSWORD_FALLBACK",
                    "DOCKER_HOST"):
            with self.subTest(env=env):
                self.assertFalse(editable(env))
                self.assertIn("environment only", _unusable(env))


class SessionSecret(unittest.TestCase):
    def test_it_is_made_once_and_kept_private(self):
        from app.web.api import session_secret_for

        directory = tempfile.mkdtemp()
        settings = SimpleNamespace(session_secret="", data_dir=directory)
        first = session_secret_for(settings)
        self.assertEqual(session_secret_for(settings), first)
        mode = os.stat(os.path.join(directory, "session_secret")).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)
        self.assertEqual(session_secret_for(SimpleNamespace(session_secret="set",
                                                            data_dir=directory)), "set")


class Redaction(unittest.TestCase):
    SECRET = "audit-canary-private-value"

    def test_assignments_in_every_usual_shape(self):
        for line in (f'PASSWORD="{self.SECRET}"', f"DB_PASSWORD={self.SECRET}",
                     f"MYSQL_ROOT_PASSWORD: {self.SECRET}", f"password: '{self.SECRET}'",
                     f'{{"api_key": "{self.SECRET}"}}', f"--db-password={self.SECRET}",
                     f"--db-password {self.SECRET}", f"redis://:{self.SECRET}@redis:6379"):
            with self.subTest(line=line):
                self.assertNotIn(self.SECRET, redact(line))

    def test_current_token_formats(self):
        for token in ("github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
                      "glpat-abcdefghijklmnopqrstuvwxyz",
                      "sk_live_abcdefghijklmnop1234"):
            with self.subTest(token=token):
                self.assertEqual(redact(f"token is {token}").count(token), 0)

    def test_a_key_named_like_a_secret_hides_its_value(self):
        cleaned = redact_obj({"password": self.SECRET, "nested": {"apiKey": self.SECRET},
                              "input_tokens": 12, "summary": "fine"})
        self.assertEqual(cleaned, {"password": REDACTED, "nested": {"apiKey": REDACTED},
                                   "input_tokens": 12, "summary": "fine"})

    def test_ordinary_lines_are_left_alone(self):
        for line in ('level=error msg="connection refused"', "authentication failed for bob"):
            with self.subTest(line=line):
                self.assertEqual(redact(line), line)


class CommandLines(unittest.TestCase):
    KEY = b"k" * 32
    SECRET = "audit-canary-private-value"

    def inspect(self, cmd, entrypoint=None, test=None):
        return {"Config": {"Cmd": cmd, "Entrypoint": entrypoint,
                           "Healthcheck": {"Test": test} if test else {}}}

    def test_secret_options_keep_their_place_and_lose_their_value(self):
        snap = runconfig.snapshot(self.inspect(
            ["server", "--password", self.SECRET, "--port", "80"],
            entrypoint=["run", f"--api-key={self.SECRET}"],
            test=["CMD-SHELL", f"curl -f http://u:{self.SECRET}@localhost/ || exit 1"]), self.KEY)
        for field in ("cmd", "entrypoint", "healthcheck"):
            with self.subTest(field=field):
                self.assertNotIn(self.SECRET, snap[field])
        self.assertRegex(snap["cmd"], r"^server --password \[hidden [0-9a-f]{8}\] --port 80$")
        self.assertTrue(snap["entrypoint"].startswith("run --api-key=[hidden "))

    def test_a_recreate_says_the_secret_changed_without_saying_it(self):
        before = runconfig.snapshot(self.inspect(["server", "--password", self.SECRET]), self.KEY)
        after = runconfig.snapshot(self.inspect(["server", "--password", "another-one"]), self.KEY)
        lines = runconfig.diff(before, after)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("cmd "))
        self.assertNotIn(self.SECRET, lines[0])
        self.assertNotIn("another-one", lines[0])
        same = runconfig.snapshot(self.inspect(["server", "--password", self.SECRET]), self.KEY)
        self.assertEqual(runconfig.diff(before, same), [])


class StoredData(unittest.TestCase):
    SECRET = "audit-canary-private-value"

    def test_an_older_database_is_scrubbed_once(self):
        path = os.path.join(tempfile.mkdtemp(), "doctor.db")
        store = Store(path)
        config = {"cmd": f"server --password {self.SECRET}", "env": {}}
        with store._lock:
            store._conn.execute(
                "INSERT INTO container_state (name, container_id, seen_at, config_json) "
                "VALUES ('app', 'id', 'now', ?)", (json.dumps(config),))
            store._conn.execute(
                "INSERT INTO container_changes (container_name, at, kind, summary, detail_json) "
                "VALUES ('app', 'now', 'recreated', ?, ?)",
                (f"config: cmd a → server --password {self.SECRET}",
                 json.dumps({"password": self.SECRET})))
            store._conn.execute("PRAGMA user_version = 0")
            store._conn.commit()
        store.close()
        store = Store(path)
        self.addCleanup(store.close)
        with store._lock:
            rows = [tuple(r) for r in store._conn.execute(
                "SELECT config_json FROM container_state UNION ALL "
                "SELECT summary || detail_json FROM container_changes")]
        self.assertTrue(rows)
        for (text,) in rows:
            self.assertNotIn(self.SECRET, text)

    def test_a_change_is_scrubbed_as_it_is_written(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.record_change("app", "", "recreated", "", f"--password {self.SECRET}",
                            {"api_key": self.SECRET})
        with store._lock:
            row = store._conn.execute(
                "SELECT summary, detail_json FROM container_changes").fetchone()
        self.assertNotIn(self.SECRET, row["summary"] + row["detail_json"])


def supervisor_for(settings: Settings) -> Supervisor:
    supervisor = Supervisor.__new__(Supervisor)
    supervisor.settings = settings
    supervisor.store = Store(":memory:")
    supervisor.policy = Policy(settings, supervisor.store)
    supervisor.broadcast = lambda *args: None
    supervisor.notifier = SimpleNamespace(notify_action=AsyncMock(), notify_incident=AsyncMock())
    supervisor.executor = SimpleNamespace(execute=AsyncMock(return_value="done"),
                                          verify=AsyncMock(return_value=(True, "healthy")))
    supervisor.incident_url = lambda incident_id: ""
    supervisor.activity_url = lambda: ""
    return supervisor


def pending_action(store: Store) -> str:
    incident_id = store.open_incident(
        container_id="app", container_name="app", image="test", kind="unhealthy",
        fingerprint="fp", severity="warning", summary="unhealthy", evidence={}, mode="suggest")
    return store.record_action(incident_id=incident_id, container_name="app",
                               action="restart_container", status="pending", decided_by="policy")


class Approvals(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.supervisor = supervisor_for(Settings(mode="suggest"))
        self.addCleanup(self.supervisor.store.close)
        self.action_id = pending_action(self.supervisor.store)

    async def settle(self):
        await asyncio.gather(*self.supervisor.__dict__.get("_action_tasks", ()))

    async def test_two_approvals_run_the_action_once(self):
        results = await asyncio.gather(
            self.supervisor.approve_action(self.action_id, actor="alex"),
            self.supervisor.approve_action(self.action_id, actor="sam"))
        await self.settle()
        self.assertEqual(sorted(r["ok"] for r in results), [False, True])
        self.assertEqual(self.supervisor.executor.execute.await_count, 1)
        row = self.supervisor.store.get_action(self.action_id)
        self.assertEqual(row["status"], "succeeded")
        self.assertIn(row["decided_by"], ("user:alex", "user:sam"))

    async def test_approve_and_reject_at_once_is_one_or_the_other(self):
        approved, rejected = await asyncio.gather(
            self.supervisor.approve_action(self.action_id, actor="alex"),
            self.supervisor.reject_action(self.action_id, actor="sam"))
        await self.settle()
        self.assertNotEqual(approved["ok"], rejected["ok"])
        self.assertEqual(self.supervisor.executor.execute.await_count, int(approved["ok"]))

    async def test_an_unclaimed_action_never_runs(self):
        incident = self.supervisor.store.get_incident(
            self.supervisor.store.get_action(self.action_id)["incident_id"])
        await self.supervisor._run_action(self.action_id, incident, None)
        self.supervisor.executor.execute.assert_not_awaited()


class Confidence(unittest.TestCase):
    def test_a_non_number_is_no_confidence(self):
        for raw in ("NaN", "inf", "-inf", float("nan"), "high", None):
            with self.subTest(raw=raw):
                diagnosis = _coerce({"confidence": raw, "recommended_action": "restart_container"})
                self.assertEqual(diagnosis["confidence"], 0.0)

    def test_the_policy_does_not_trust_nan_either(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        policy = Policy(Settings(mode="auto"), store)
        row = store.get_incident(store.open_incident(
            container_id="app", container_name="app", image="test", kind="unhealthy",
            fingerprint="fp", severity="warning", summary="unhealthy", evidence={}, mode="auto"))
        decision = policy.decide(
            incident=row, container_policy=ContainerPolicy(),
            diagnosis={"recommended_action": "restart_container", "confidence": float("nan")})
        self.assertNotEqual(decision.status, "approved")


class StopVerification(unittest.IsolatedAsyncioTestCase):
    async def verify(self, state, action):
        docker = SimpleNamespace(list_containers=AsyncMock(
            return_value=[{"id": "c1", "state": state, "health": None}]))
        executor = Executor(SimpleNamespace(verify_delay_seconds=0), docker, None)
        return (await executor.verify("c1", "app", action))[0]

    async def test_a_stopped_container_is_what_a_stop_wanted(self):
        self.assertTrue(await self.verify("exited", "stop_container"))
        self.assertFalse(await self.verify("running", "stop_container"))

    async def test_a_restart_still_wants_it_running(self):
        self.assertFalse(await self.verify("exited", "restart_container"))
        self.assertTrue(await self.verify("running", "restart_container"))


class RediagnosisBudget(unittest.IsolatedAsyncioTestCase):
    async def test_it_counts_against_the_dollar_budget(self):
        supervisor = supervisor_for(Settings(daily_usd_budget=1.0))
        self.addCleanup(supervisor.store.close)
        supervisor.diagnoser = SimpleNamespace(enabled=True, diagnose=AsyncMock())
        incident_id = supervisor.store.open_incident(
            container_id="app", container_name="app", image="test", kind="unhealthy",
            fingerprint="fp", severity="warning", summary="unhealthy", evidence={}, mode="observe")
        supervisor.store.add_usage(input_tokens=0, output_tokens=0, cached_tokens=0,
                                   cost_usd=100.0)
        result = await supervisor.rediagnose(incident_id)
        self.assertFalse(result["ok"])
        self.assertIn("budget", result["error"])
        supervisor.diagnoser.diagnose.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
