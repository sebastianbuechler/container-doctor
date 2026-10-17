"""Settings saved on the page apply while the doctor runs, unless they are
read only when it starts.

The guard at the top keeps that honest: a setting read where the doctor is
built - a constructor, start(), build_app() - is read once, so it must be
marked `restart`, or be rebuilt by Supervisor.apply_settings. Without it, a
setting saved on the page could report "applied" and change nothing.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import os
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import FIELD_SPECS, SETTINGS, Settings, resolve  # noqa: E402
from tests.test_classifier import _VersionOnly  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app"

# Where the doctor is built: whatever these read is read once.
BUILT_IN = {"__init__", "__post_init__", "start", "build_app", "create_app",
            "_decision_model", "docker_pool_size", "plan"}

# Read where the doctor is built, but built again by apply_settings.
REBUILT = {"log_error_pattern", "deploy_settle_minutes", "briefing", "briefing_model",
           "briefing_refresh_minutes", "model", "llm_log_days", "classifier_mode",
           "classifier_provider", "classifier_model", "classifier_url", "classifier_api_key",
           "classifier_timeout_seconds", "classifier_price_per_mtok", "net_tcp_targets",
           "net_dns_names", "net_dns_servers", "net_http_urls", "openai_api_key",
           "openai_base_url"}

# Read where the doctor is built only to be written to the log.
LOGGED = {"mode"}


def property_fields() -> dict[str, set[str]]:
    """Each Settings property, and the fields it reads - through other
    properties too."""
    tree = ast.parse((APP / "config.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Settings")
    direct: dict[str, set[str]] = {}
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and any(
                isinstance(d, ast.Name) and d.id == "property" for d in node.decorator_list):
            direct[node.name] = {a.attr for a in ast.walk(node)
                                 if isinstance(a, ast.Attribute)
                                 and isinstance(a.value, ast.Name) and a.value.id == "self"}
    fields = set(FIELD_SPECS)

    def expand(name: str, seen: frozenset = frozenset()) -> set[str]:
        out: set[str] = set()
        for attr in direct.get(name, ()):
            if attr in fields:
                out.add(attr)
            elif attr in direct and attr not in seen:
                out |= expand(attr, seen | {name})
        return out

    return {name: expand(name) for name in direct}


def read_where_built() -> dict[str, set[str]]:
    """Every Settings field read where the doctor is built, and where."""
    props = property_fields()
    found: dict[str, set[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and fn.name in BUILT_IN):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Attribute):
                    continue
                base = ast.unparse(node.value)
                # supervisor.settings is read when used: what apply_settings swaps.
                if base.startswith("supervisor.") or not (
                        base in ("s", "settings") or base.endswith(".settings")):
                    continue
                names = props.get(node.attr, {node.attr} & set(FIELD_SPECS))
                for name in names:
                    found.setdefault(name, set()).add(f"{path.name}:{fn.name}")
    return found


class TheGuard(unittest.TestCase):
    def test_what_is_read_once_waits_for_a_restart_or_is_rebuilt(self):
        unaccounted = {
            name: sorted(where) for name, where in read_where_built().items()
            if not (FIELD_SPECS[name].restart or FIELD_SPECS[name].env_only
                    or name in REBUILT or name in LOGGED)}
        self.assertEqual(unaccounted, {}, "mark these restart=True, or rebuild them in "
                                          "Supervisor.apply_settings and list them in REBUILT")

    def test_it_finds_what_it_should(self):
        """The guard would pass on nothing found; make sure it finds things."""
        found = read_where_built()
        self.assertIn("poll_concurrency", found)  # docker_pool_size
        self.assertIn("docker_host", found)
        self.assertIn("web_password", found)  # through password_login_allowed

    def test_rebuilt_is_not_stale(self):
        self.assertEqual(sorted(REBUILT - set(FIELD_SPECS)), [])


def a_supervisor(**env):
    from app.supervisor import Supervisor

    server = HTTPServer(("127.0.0.1", 0), _VersionOnly)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    previous = dict(os.environ)
    for name in SETTINGS:
        os.environ.pop(name, None)
    env = {"DOCTOR_DATA_DIR": tempfile.mkdtemp(),
           "DOCKER_HOST": f"tcp://127.0.0.1:{server.server_address[1]}", **env}
    os.environ.update(env)
    try:
        supervisor = Supervisor(Settings())
    finally:
        os.environ.clear()
        os.environ.update(previous)
    supervisor.test_env = env
    return supervisor, server


class Applying(unittest.TestCase):
    def setUp(self):
        self.supervisor, server = a_supervisor()
        self.addCleanup(server.shutdown)
        self.addCleanup(self.supervisor.store.close)

    def apply(self, **changes):
        return self.supervisor.apply_settings(
            dataclasses.replace(self.supervisor.settings, **changes))

    def test_a_live_setting_reaches_every_part(self):
        old = self.supervisor.settings
        holders = self.supervisor._settings_holders()
        self.assertGreater(len(holders), 3)
        result = self.apply(cpu_threshold_pct=50.0, mode="suggest")
        self.assertEqual(sorted(result["applied"]), ["cpu_threshold_pct", "mode"])
        new = self.supervisor.settings
        self.assertIsNot(new, old)
        self.assertEqual((new.cpu_threshold_pct, new.mode), (50.0, "suggest"))
        for obj in holders:
            self.assertIs(obj.settings, new, type(obj).__name__)

    def test_nothing_still_holds_the_old_settings(self):
        old = self.supervisor.settings
        self.apply(min_confidence=0.9)
        stale = [name for name, obj in vars(self.supervisor).items()
                 if getattr(obj, "settings", None) is old]
        self.assertEqual(stale, [])

    def test_a_restart_setting_keeps_its_running_value(self):
        result = self.apply(poll_concurrency=12, cpu_threshold_pct=60.0)
        self.assertEqual((result["applied"], result["restart"]),
                         (["cpu_threshold_pct"], ["poll_concurrency"]))
        self.assertEqual(self.supervisor.settings.poll_concurrency, 6)

    def test_only_restart_settings_changes_nothing(self):
        old = self.supervisor.settings
        result = self.apply(poll_concurrency=2)
        self.assertEqual(result, {"applied": [], "restart": ["poll_concurrency"]})
        self.assertIs(self.supervisor.settings, old)

    def test_what_was_built_from_them_is_built_again(self):
        s = self.supervisor
        self.apply(log_error_pattern=r"(?i)\bkaboom\b", deploy_settle_minutes=9.0,
                   briefing_refresh_minutes=45, llm_log_days=0,
                   net_tcp_targets=["10.0.0.1:443"])
        self.assertTrue(s.detector._error_regex.search("a KABOOM here"))
        self.assertEqual(s.settling.settle.total_seconds(), 540)
        self.assertEqual(s.briefing.refresh.total_seconds(), 2700)
        self.assertIsNone(s.diagnoser.recorder)
        if s.net is not None:
            self.assertIn("10.0.0.1:443", [c.target for c in s.net.checks])

    def test_the_classifier_gets_its_new_backend(self):
        """The key is a secret, from the environment; switching it on is the page's."""
        s, server = a_supervisor(DOCTOR_CLASSIFIER_API_KEY="sk-or-x")
        self.addCleanup(server.shutdown)
        self.addCleanup(s.store.close)
        self.assertIsNone(s.classifier.model)
        s.apply_settings(dataclasses.replace(s.settings, classifier_mode="shadow"))
        self.assertIsNotNone(s.classifier.model)
        self.assertEqual(s.classifier.model.api_key, "sk-or-x")
        s.apply_settings(dataclasses.replace(s.settings, classifier_mode="off"))
        self.assertIsNone(s.classifier.model)

    def test_the_warnings_follow(self):
        self.apply(min_confidence=3.0)
        self.assertIn("DOCTOR_MIN_CONFIDENCE must be between 0 and 1.",
                      self.supervisor.startup_warnings)

    def test_a_failure_leaves_it_as_it_was(self):
        s = self.supervisor
        old = s.settings

        def broken():
            raise RuntimeError("no backend")

        s._decision_model = broken
        with self.assertRaises(RuntimeError):
            self.apply(cpu_threshold_pct=10.0)
        self.assertIs(s.settings, old)
        self.assertTrue(all(obj.settings is old for obj in s._settings_holders()))


class ThroughTheApi(unittest.TestCase):
    """Saving and importing apply at once what needs no restart."""

    def setUp(self):
        from fastapi.testclient import TestClient

        from app.web.api import build_app

        self.env = {"DOCTOR_WEB_PASSWORD": "pw", "DOCTOR_WEB_USER": "ana"}
        self.supervisor, server = a_supervisor(**self.env)
        self.env = self.supervisor.test_env
        self.addCleanup(server.shutdown)
        self.addCleanup(self.supervisor.store.close)
        self.restarted = []
        self.patch_env()
        self.client = TestClient(build_app(self.supervisor, self.supervisor.settings,
                                           restart=lambda: self.restarted.append(1)))
        self.client.headers["authorization"] = "Basic " + base64.b64encode(b"ana:pw").decode()

    def patch_env(self, **extra):
        """The environment the doctor runs with, for the length of the test."""
        previous = dict(os.environ)
        for name in SETTINGS:
            if name != "DOCTOR_DATA_DIR":
                os.environ.pop(name, None)
        os.environ.update(self.env, **extra)

        def restore():
            os.environ.clear()
            os.environ.update(previous)

        self.addCleanup(restore)

    def put(self, env, value):
        return self.client.put(f"/api/settings/{env}", json={"value": value})

    def test_a_live_setting_applies_at_once(self):
        answer = self.put("DOCTOR_CPU_THRESHOLD", "55").json()
        self.assertEqual((answer["applied"], answer["restart"]), (["DOCTOR_CPU_THRESHOLD"], []))
        self.assertEqual(self.supervisor.settings.cpu_threshold_pct, 55.0)
        page = self.client.get("/api/settings").json()
        self.assertFalse(page["pending"])
        rows = {(s["title"], i["label"]): i for s in page["sections"] for i in s["items"]}
        self.assertEqual(rows[("Detection", "CPU threshold")]["value"], "55% of the host")

    def test_a_restart_setting_waits(self):
        answer = self.put("DOCTOR_POLL_CONCURRENCY", "12").json()
        self.assertEqual((answer["applied"], answer["restart"]), ([], ["DOCTOR_POLL_CONCURRENCY"]))
        self.assertEqual(self.supervisor.settings.poll_concurrency, 6)
        page = self.client.get("/api/settings").json()
        self.assertTrue(page["pending"])

    def test_a_reset_applies_too(self):
        self.put("DOCTOR_CPU_THRESHOLD", "55")
        self.put("DOCTOR_CPU_THRESHOLD", "")
        self.assertEqual(self.supervisor.settings.cpu_threshold_pct, 85.0)

    def test_export_holds_what_was_saved(self):
        self.put("DOCTOR_CPU_THRESHOLD", "55")
        self.put("DOCTOR_EFFORT", "high")
        response = self.client.get("/api/settings/export")
        self.assertIn("attachment", response.headers["content-disposition"])
        body = response.json()
        self.assertEqual((body["format"], body["version"]), ("container-doctor-settings", 1))
        self.assertEqual(body["settings"], {"DOCTOR_CPU_THRESHOLD": "55", "DOCTOR_EFFORT": "high"})

    def importing(self, settings, **options):
        return self.client.post("/api/settings/import", json={
            "format": "container-doctor-settings", "version": 1, "settings": settings,
            **options})

    def test_a_dry_run_reports_and_changes_nothing(self):
        answer = self.importing({"DOCTOR_CPU_THRESHOLD": "40"}, dry_run=True).json()
        self.assertEqual(answer["changes"], [{"env": "DOCTOR_CPU_THRESHOLD", "old_value": "",
                                              "new_value": "40"}])
        self.assertEqual(self.supervisor.store.saved_settings(), {})
        self.assertEqual(self.supervisor.settings.cpu_threshold_pct, 85.0)

    def test_an_import_saves_and_applies(self):
        answer = self.importing({"DOCTOR_CPU_THRESHOLD": "40",
                                 "DOCTOR_POLL_CONCURRENCY": "8"}).json()
        self.assertEqual(self.supervisor.store.saved_settings(),
                         {"DOCTOR_CPU_THRESHOLD": "40", "DOCTOR_POLL_CONCURRENCY": "8"})
        self.assertEqual(answer["applied"], ["DOCTOR_CPU_THRESHOLD"])
        self.assertEqual(answer["restart"], ["DOCTOR_POLL_CONCURRENCY"])
        self.assertEqual(self.supervisor.settings.cpu_threshold_pct, 40.0)

    def test_a_hand_edited_file_with_numbers_is_read(self):
        self.importing({"DOCTOR_CPU_THRESHOLD": 70, "DOCTOR_BRIEFING": False})
        self.assertEqual(self.supervisor.store.saved_settings(),
                         {"DOCTOR_CPU_THRESHOLD": "70", "DOCTOR_BRIEFING": "off"})

    def test_replace_resets_what_the_file_leaves_out(self):
        self.put("DOCTOR_EFFORT", "high")
        self.importing({"DOCTOR_CPU_THRESHOLD": "40"}, replace=True)
        self.assertEqual(self.supervisor.store.saved_settings(), {"DOCTOR_CPU_THRESHOLD": "40"})

    def test_what_cannot_be_saved_here_is_skipped_and_said(self):
        self.patch_env(DOCTOR_MODEL="from-portainer")
        answer = self.importing({"DOCTOR_CPU_THRESHOLD": "40", "DOCTOR_MODEL": "x",
                                 "DOCTOR_WEB_PASSWORD": "pw2", "DOCTOR_FROM_THE_FUTURE": "1"}).json()
        self.assertEqual({s["env"] for s in answer["skipped"]},
                         {"DOCTOR_MODEL", "DOCTOR_WEB_PASSWORD", "DOCTOR_FROM_THE_FUTURE"})
        self.assertEqual(self.supervisor.store.saved_settings(), {"DOCTOR_CPU_THRESHOLD": "40"})

    def test_one_bad_value_refuses_the_whole_file(self):
        response = self.importing({"DOCTOR_CPU_THRESHOLD": "40", "DOCTOR_POLL_INTERVAL": "soon"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.supervisor.store.saved_settings(), {})
        self.assertEqual(self.supervisor.settings.cpu_threshold_pct, 85.0)

    def test_a_value_that_adds_a_problem_is_refused(self):
        """A confidence no diagnosis can reach: the file is refused, whole."""
        response = self.importing({"DOCTOR_MIN_CONFIDENCE": "2", "DOCTOR_CPU_THRESHOLD": "40"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("DOCTOR_MIN_CONFIDENCE", response.json()["detail"])
        self.assertEqual(self.supervisor.store.saved_settings(), {})

    def test_another_file_is_refused(self):
        response = self.client.post("/api/settings/import",
                                    json={"format": "something-else", "settings": {}})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/settings/import", json={
            "format": "container-doctor-settings", "version": 2, "settings": {}})
        self.assertEqual(response.status_code, 422)

    def test_another_site_cannot_import(self):
        response = self.client.post("/api/settings/import", headers={"origin": "https://evil.example"},
                                    json={"settings": {"DOCTOR_CPU_THRESHOLD": "1"}})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.supervisor.store.saved_settings(), {})


class TheStore(unittest.TestCase):
    def test_a_batch_is_all_or_nothing(self):
        from app.store import Store

        store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.addCleanup(store.close)
        with self.assertRaises(Exception):
            store.save_settings({"DOCTOR_CPU_THRESHOLD": "40", "DOCTOR_EFFORT": object()})
        self.assertEqual(store.saved_settings(), {})
        self.assertEqual(store.setting_changes(), [])


class Resolving(unittest.TestCase):
    def test_resolve_is_what_apply_receives(self):
        """apply_settings is given resolve(saved): the environment still wins."""
        previous = dict(os.environ)
        try:
            os.environ["DOCTOR_MODE"] = "observe"
            self.assertEqual(resolve({"DOCTOR_MODE": "auto"}).mode, "observe")
        finally:
            os.environ.clear()
            os.environ.update(previous)


if __name__ == "__main__":
    unittest.main()
