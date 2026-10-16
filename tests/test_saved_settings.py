"""Settings saved from the page sit between the code's defaults and the
environment: they override a default, and the stack environment overrides them."""

from __future__ import annotations

import base64
import os
import tempfile
import time
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import SETTINGS, load_settings, resolve, source, storable  # noqa: E402
from app.store import Store  # noqa: E402
from app.web.api import describe_rows  # noqa: E402
from app.config import default_values  # noqa: E402
from tests.example_compose import passed  # noqa: E402


class Env:
    """Only the given setting variables are set, for the duration."""

    def __init__(self, **env):
        self.env = env

    def __enter__(self):
        self.saved = dict(os.environ)
        for name in SETTINGS:
            if name != "DOCTOR_DATA_DIR":
                os.environ.pop(name, None)
        os.environ.update(self.env)

    def __exit__(self, *_):
        os.environ.clear()
        os.environ.update(self.saved)


class Precedence(unittest.TestCase):
    def test_saved_overrides_the_default(self):
        with Env():
            self.assertEqual(resolve({"DOCTOR_MODE": "suggest"}).mode, "suggest")

    def test_the_environment_overrides_what_is_saved(self):
        with Env(DOCTOR_MODE="observe"):
            self.assertEqual(resolve({"DOCTOR_MODE": "auto"}).mode, "observe")

    def test_empty_in_the_environment_is_unset(self):
        """A compose file passes every setting, empty when the stack sets none."""
        with Env(DOCTOR_POLL_INTERVAL=""):
            self.assertEqual(resolve({"DOCTOR_POLL_INTERVAL": "45"}).poll_interval_seconds, 45)

    def test_a_saved_value_is_read_like_the_environments(self):
        with Env():
            settings = resolve({"DOCTOR_EFFORT": "High", "DOCTOR_BRIEFING": "off",
                                "DOCTOR_NET_DNS_NAMES": "none", "DOCTOR_MDSTAT_PATH": "off"})
        self.assertEqual((settings.effort, settings.briefing), ("high", False))
        self.assertEqual((settings.net_dns_names, settings.mdstat_path), ([], ""))

    def test_secrets_and_startup_settings_are_never_read_from_the_database(self):
        with Env():
            settings = resolve({"DOCTOR_WEB_PASSWORD": "pw", "DOCTOR_DATA_DIR": "/elsewhere",
                                "DOCTOR_WEB_PORT": "1", "DOCTOR_NOT_A_SETTING": "x"})
        self.assertEqual(settings.web_password, "")
        self.assertNotEqual(settings.data_dir, "/elsewhere")
        self.assertEqual(settings.web_port, 8080)
        self.assertEqual(storable({"DOCTOR_WEB_PASSWORD": "pw", "DOCTOR_NOT_A_SETTING": "x",
                                   "DOCTOR_MODE": "auto"}), {"DOCTOR_MODE": "auto"})

    def test_it_leaves_nothing_behind(self):
        with Env():
            resolve({"DOCTOR_MODE": "auto"})
            self.assertEqual(resolve({}).mode, "observe")


class Source(unittest.TestCase):
    def test_each_source(self):
        saved = {"DOCTOR_MODE": "auto", "DOCTOR_WEB_PASSWORD": "pw"}
        with Env(DOCKER_HOST="tcp://proxy:2375", DOCTOR_MODEL="x"):
            self.assertEqual(source("DOCTOR_MODE", saved), "saved")
            self.assertEqual(source("DOCTOR_MODEL", saved), "env")
            self.assertEqual(source("DOCKER_HOST", saved), "env")
            self.assertEqual(source("DOCTOR_EFFORT", saved), "default")
            self.assertEqual(source("DOCTOR_WEB_PASSWORD", saved), "default")
        with Env(DOCTOR_MODE="observe"):
            self.assertEqual(source("DOCTOR_MODE", saved), "env")

    def test_the_page_says_where_a_row_comes_from(self):
        with Env(DOCTOR_MODEL="claude-x"):
            rows = {(s["title"], i["label"]): i for s in describe_rows(
                resolve({"DOCTOR_MODE": "suggest"}).overview(), passed(),
                default_values(), {"DOCTOR_MODE": "suggest"}) for i in s["items"]}
        self.assertEqual(rows[("Build and mode", "Mode")]["source"], "saved")
        self.assertEqual(rows[("Build and mode", "Mode")]["value"], "suggest")
        self.assertEqual(rows[("Model", "Model")]["source"], "env")
        self.assertEqual(rows[("Model", "Reasoning effort")]["source"], "default")
        self.assertIsNone(rows[("Build and mode", "Build")]["source"])


class TheStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "doctor.db"))

    def tearDown(self):
        self.store.close()

    def test_save_change_and_reset(self):
        self.assertTrue(self.store.save_setting("DOCTOR_MODE", "suggest", by="ana"))
        self.assertFalse(self.store.save_setting("DOCTOR_MODE", "suggest", by="ana"))
        self.store.save_setting("DOCTOR_MODE", "auto", by="ben")
        self.assertEqual(self.store.saved_settings(), {"DOCTOR_MODE": "auto"})
        self.store.save_setting("DOCTOR_MODE", "", by="ana")
        self.assertEqual(self.store.saved_settings(), {})
        history = [(c["old_value"], c["new_value"], c["changed_by"])
                   for c in self.store.setting_changes()]
        self.assertEqual(history, [("auto", "", "ana"), ("suggest", "auto", "ben"),
                                   ("", "suggest", "ana")])

    def test_resetting_what_was_never_saved_records_nothing(self):
        self.assertFalse(self.store.save_setting("DOCTOR_MODE", ""))
        self.assertEqual(self.store.setting_changes(), [])

    def test_the_running_settings_read_it(self):
        self.store.save_setting("DOCTOR_MODE", "suggest")
        with Env(DOCTOR_DATA_DIR=self.dir):
            self.assertEqual(load_settings().mode, "suggest")

    def test_without_a_database_the_environment_alone(self):
        with Env(DOCTOR_DATA_DIR=os.path.join(self.dir, "nothing-here")):
            self.assertEqual(load_settings().mode, "observe")
            self.assertFalse(os.path.exists(os.path.join(self.dir, "nothing-here")))


if __name__ == "__main__":
    unittest.main()


class ThroughTheApi(unittest.TestCase):
    """Saving from the page: checked, recorded, and only from the page itself."""

    def client(self, restarted=None, refresh=None, broadcasts=None, **env):
        from fastapi.testclient import TestClient

        from app.config import Settings
        from app.web.api import build_app

        with Env(DOCTOR_WEB_PASSWORD="pw", DOCTOR_WEB_USER="ana", **env):
            settings = Settings()
            store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))

            class Stub:
                def __init__(self):
                    self.settings = settings
                    self.store = store
                    self.docker_connected = False

                _github = {"checked_at": None}

                def broadcast(self, *a, **k):
                    if broadcasts is not None:
                        broadcasts.append(a)

                async def _refresh_deploy_stacks(self):
                    if refresh:
                        await refresh(self)

                def apply_settings(self, new):
                    return {"applied": [], "restart": []}

                def github_status(self):
                    return []

            client = TestClient(build_app(Stub(), settings,
                                          restart=lambda: restarted.append(1) if restarted is not None else None))
            client.headers["authorization"] = "Basic " + base64.b64encode(b"ana:pw").decode()
        self.addCleanup(store.close)
        return client, store

    def put(self, client, env, value, **headers):
        return client.put(f"/api/settings/{env}", json={"value": value}, headers=headers)

    def test_a_setting_is_saved_and_shows_as_pending(self):
        client, store = self.client()
        with Env(DOCTOR_WEB_PASSWORD="pw"):
            self.assertEqual(self.put(client, "DOCTOR_POLL_INTERVAL", "45").status_code, 200)
            answer = client.get("/api/settings").json()
        self.assertEqual(store.saved_settings(), {"DOCTOR_POLL_INTERVAL": "45"})
        self.assertEqual(store.setting_changes()[0]["changed_by"], "ana")
        rows = {(s["title"], i["label"]): i for s in answer["sections"] for i in s["items"]}
        row = rows[("Build and mode", "Poll interval")]
        self.assertEqual((row["value"], row["pending"], row["source"]), ("30s", "45s", "saved"))
        self.assertTrue(answer["pending"])
        self.assertEqual([(c["env"], c["old_value"], c["new_value"], c["changed_by"])
                          for c in answer["changes"]], [("DOCTOR_POLL_INTERVAL", "", "45", "ana")])
        variable = row["env"][0]
        self.assertEqual((variable["editable"], variable["current"], variable["type"]),
                         (True, "45", "int"))

    def test_the_page_does_not_wait_for_github(self):
        import asyncio
        import threading

        checked = threading.Event()
        broadcasts = []

        async def slow_github(stub):
            await asyncio.sleep(0.5)
            stub._github = {"checked_at": "now"}
            checked.set()

        client, _ = self.client(refresh=slow_github, broadcasts=broadcasts)
        with Env(DOCTOR_WEB_PASSWORD="pw"), client:
            started = time.monotonic()
            self.assertEqual(client.get("/api/settings").status_code, 200)
            self.assertLess(time.monotonic() - started, 0.4)
            # The open page hears about the answer once it is in.
            self.assertTrue(checked.wait(5))
            time.sleep(0.05)
        self.assertIn(("settings", {"github": True}), broadcasts)

    def test_a_row_about_a_secret_offers_no_editor(self):
        client, _ = self.client()
        with Env(DOCTOR_WEB_PASSWORD="pw"):
            answer = client.get("/api/settings").json()
        rows = {(s["title"], i["label"]): i for s in answer["sections"] for i in s["items"]}
        self.assertFalse(any(e["editable"] for e in rows[("Model", "Credentials")]["env"]))
        self.assertNotIn("current", str(rows[("Model", "Credentials")]))

    def test_a_value_it_cannot_read_is_refused(self):
        client, store = self.client()
        with Env(DOCTOR_WEB_PASSWORD="pw"):
            response = self.put(client, "DOCTOR_POLL_INTERVAL", "soon")
        self.assertEqual(response.status_code, 422)
        self.assertIn("whole number", response.json()["detail"])
        self.assertEqual(store.saved_settings(), {})

    def test_a_secret_or_a_variable_set_in_the_stack_environment_is_refused(self):
        client, _ = self.client()
        with Env(DOCTOR_WEB_PASSWORD="pw", DOCTOR_MODE="observe"):
            self.assertEqual(self.put(client, "DOCTOR_WEB_PASSWORD", "x").status_code, 422)
            response = self.put(client, "DOCTOR_MODE", "auto")
        self.assertEqual(response.status_code, 422)
        self.assertIn("stack environment", response.json()["detail"])

    def test_empty_resets(self):
        client, store = self.client()
        with Env(DOCTOR_WEB_PASSWORD="pw"):
            self.put(client, "DOCTOR_EFFORT", "high")
            self.put(client, "DOCTOR_EFFORT", "")
        self.assertEqual(store.saved_settings(), {})

    def test_another_site_cannot_save(self):
        client, store = self.client()
        with Env(DOCTOR_WEB_PASSWORD="pw"):
            response = self.put(client, "DOCTOR_EFFORT", "high", origin="https://evil.example")
            same = self.put(client, "DOCTOR_EFFORT", "high", origin="http://testserver")
        self.assertEqual((response.status_code, same.status_code), (403, 200))

    def test_without_a_login_nothing_is_saved(self):
        client, store = self.client()
        del client.headers["authorization"]
        with Env(DOCTOR_WEB_PASSWORD="pw"):
            self.assertEqual(self.put(client, "DOCTOR_EFFORT", "high").status_code, 401)
        self.assertEqual(store.saved_settings(), {})

    def test_the_page_can_be_made_read_only(self):
        client, store = self.client(DOCTOR_SETTINGS_EDITABLE="false")
        with Env(DOCTOR_WEB_PASSWORD="pw"):
            self.assertEqual(self.put(client, "DOCTOR_EFFORT", "high").status_code, 403)
            answer = client.get("/api/settings").json()
        self.assertFalse(answer["editable"])
        self.assertFalse(any(e["editable"] for s in answer["sections"]
                             for i in s["items"] for e in i["env"]))

    def test_restart_answers_then_stops(self):
        restarted = []
        client, _ = self.client(restarted=restarted)
        with Env(DOCTOR_WEB_PASSWORD="pw"), client:  # keeps its event loop running
            self.assertEqual(client.post("/api/settings/restart").status_code, 200)
            self.assertEqual(restarted, [])
            time.sleep(0.7)
        self.assertEqual(restarted, [1])
