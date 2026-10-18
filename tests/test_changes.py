"""The change timeline: what counts as a change, and what does not."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from app import changes
from app.store import Store


def seen(**overrides):
    base = {"id": "c1", "image": "linuxserver/sonarr:4.0.20", "image_id": "sha256:aaaa1111bbbb2222",
            "created": "2026-09-20T10:00:00.123456789Z", "started_at": "2026-09-20T10:00:05.5Z",
            "finished_at": "0001-01-01T00:00:00Z", "restart_count": 0, "state": "running"}
    base.update(overrides)
    return changes.observe(base)


class Diffs(unittest.TestCase):
    def test_nothing_changed_is_nothing(self):
        self.assertEqual(changes.diff("sonarr", seen(), seen(), first_run=False), [])

    def test_the_first_run_records_no_history(self):
        self.assertEqual(changes.diff("sonarr", None, seen(), first_run=True), [])

    def test_a_new_name_after_the_first_run_appeared(self):
        [change] = changes.diff("sonarr", None, seen(), first_run=False)
        self.assertEqual(change.kind, "appeared")

    def test_a_retagged_build_says_so(self):
        """The Sonarr surprise: same tag, new image, recreated by a redeploy."""
        after = seen(id="c2", image_id="sha256:cccc3333dddd4444",
                     created="2026-09-26T07:40:00Z", started_at="2026-09-26T07:40:02Z")
        [change] = changes.diff("sonarr", seen(), after, first_run=False)
        self.assertEqual(change.kind, "recreated")
        self.assertEqual(change.detail["change"], "build")
        self.assertIn("new build", change.summary)
        self.assertIn("aaaa1111bbbb", change.summary)
        self.assertEqual(change.at, "2026-09-26T07:40:00+00:00")

    def test_a_version_bump_names_both_versions(self):
        after = seen(id="c2", image="linuxserver/sonarr:4.0.21", image_id="sha256:ee")
        [change] = changes.diff("sonarr", seen(), after, first_run=False)
        self.assertEqual(change.detail["change"], "version")
        self.assertIn("4.0.21", change.summary)
        self.assertIn("4.0.20", change.summary)

    def test_a_digest_pin_on_the_same_build_is_not_a_new_image(self):
        """authentik: server:2026.8.3 pinned to its digest, same build - the
        diagnosis was told the image changed and proposed rolling it back."""
        after = seen(id="c2", image="linuxserver/sonarr:4.0.20@sha256:ab9b4e8c")
        [change] = changes.diff("sonarr", seen(), after, first_run=False)
        self.assertEqual(change.detail["change"], "same")
        self.assertIn("same image", change.summary)
        self.assertIn("4.0.20@sha256:ab9b4e8c", change.summary)

    def test_a_stored_pin_from_before_is_read_as_the_same_image(self):
        detail = {"change": "version", "from_build": "aaaa1111bbbb", "to_build": "aaaa1111bbbb"}
        self.assertEqual(changes.image_moved(detail), "same")
        detail["to_build"] = "cccc3333dddd"
        self.assertEqual(changes.image_moved(detail), "version")
        self.assertEqual(changes.image_moved({"change": "version"}), "version")

    def test_a_restart_policy_restart_is_attributed(self):
        after = seen(started_at="2026-09-26T08:00:00Z", restart_count=1)
        [change] = changes.diff("sonarr", seen(), after, first_run=False)
        self.assertEqual(change.kind, "restarted")
        self.assertEqual(change.detail["by"], "docker")

    def test_a_stop_is_timed_by_when_it_finished(self):
        after = seen(state="exited", finished_at="2026-09-26T08:10:00.9Z")
        [change] = changes.diff("sonarr", seen(), after, first_run=False)
        self.assertEqual(change.kind, "stopped")
        self.assertEqual(change.at, "2026-09-26T08:10:00+00:00")

    def test_a_started_stopped_container(self):
        before = seen(state="exited")
        after = seen(started_at="2026-09-26T09:00:00Z")
        [change] = changes.diff("sonarr", before, after, first_run=False)
        self.assertEqual(change.kind, "started")

    def test_a_stack_redeploy_is_grouped(self):
        recreate = lambda name: changes.Change("recreated", "t", f"{name} was recreated", {})
        found = [("sonarr", "arr-stack", recreate("sonarr")),
                 ("radarr", "arr-stack", recreate("radarr")),
                 ("memos", "tools", recreate("memos"))]
        changes.group_redeploys(found)
        self.assertEqual(found[0][2].detail["redeploy"]["containers"], ["radarr", "sonarr"])
        self.assertIn("1 other container of stack arr-stack", found[0][2].summary)
        self.assertNotIn("redeploy", found[2][2].detail)


class Renames(unittest.TestCase):
    """The sockpuppetbrowser case: a compose rename is not a new container."""

    def state(self, name, stack="tools", service=None, image_id="sha256:5eed", **extra):
        labels = {"com.docker.compose.project": stack,
                  "com.docker.compose.service": service or name}
        return changes.observe(dict({"id": f"{name}-1", "image": "sockpuppetbrowser:latest",
                                     "image_id": image_id, "created": "2026-09-28T13:48:41Z",
                                     "state": "running", "labels": labels}, **extra))

    def pair(self, before, now):
        found = [(name, now[name]["stack"], change)
                 for name in now
                 for change in changes.diff(name, before.get(name), now[name], first_run=False)]
        return found, changes.pair_renames(found, before, now)

    def test_a_new_name_on_the_same_image_in_the_same_stack_is_a_rename(self):
        before = {"ChangeDetection-SOCK": self.state("ChangeDetection-SOCK", service="sockpuppetbrowser")}
        now = {"changedetection-sockpuppetbrowser": self.state("changedetection-sockpuppetbrowser")}
        [(name, _, change)], consumed = self.pair(before, now)
        self.assertEqual(change.kind, "renamed")
        self.assertEqual(change.detail["from_name"], "ChangeDetection-SOCK")
        self.assertEqual(change.detail["change"], "same")
        self.assertIn("replaced ChangeDetection-SOCK", change.summary)
        self.assertIn("on the same image", change.summary)
        self.assertEqual(consumed, ["ChangeDetection-SOCK"])

    def test_a_same_service_under_a_new_name_and_a_new_version_is_a_rename(self):
        before = {"old": self.state("old", service="app", image="app:1", image_id="sha256:1")}
        now = {"new": self.state("new", service="app", image="app:2", image_id="sha256:2")}
        [(_, _, change)], _ = self.pair(before, now)
        self.assertEqual(change.kind, "renamed")
        self.assertEqual(change.detail["change"], "version")

    def test_another_stack_is_not_a_rename(self):
        before = {"old": self.state("old", stack="arr-stack")}
        now = {"new": self.state("new")}
        [(_, _, change)], consumed = self.pair(before, now)
        self.assertEqual(change.kind, "appeared")
        self.assertEqual(consumed, [])

    def test_two_candidates_stay_appeared(self):
        before = {"a": self.state("a"), "b": self.state("b")}
        now = {"new": self.state("new")}
        [(_, _, change)], _ = self.pair(before, now)
        self.assertEqual(change.kind, "appeared")

    def test_a_different_service_on_a_different_image_is_new(self):
        before = {"old": self.state("old", image_id="sha256:1")}
        now = {"new": self.state("new", image_id="sha256:2")}
        [(_, _, change)], _ = self.pair(before, now)
        self.assertEqual(change.kind, "appeared")


class PromptHints(unittest.TestCase):
    """The line under the timeline follows what changed, not a fixed guess."""

    def render(self, *entries):
        from app.diagnoser import _render_incident
        return _render_incident({"kind": "log_errors", "detected_at": "2026-09-29T06:07:51+00:00",
                                 "container": {"name": "x"},
                                 "changes": [dict({"at": "2026-09-28T13:48:41+00:00", "count": 1,
                                                   "summary": "s"}, **e) for e in entries]})

    def test_a_moved_image_asks_for_the_previous_one(self):
        prompt = self.render({"kind": "recreated", "image_change": "build"})
        self.assertIn("name the previous image", prompt)

    def test_a_rename_on_the_same_image_rules_out_a_rollback(self):
        prompt = self.render({"kind": "renamed", "image_change": "same"})
        self.assertNotIn("name the previous image", prompt)
        self.assertIn("no earlier image to go back to", prompt)

    def test_an_appearance_does_not_invent_a_previous_image(self):
        prompt = self.render({"kind": "appeared", "image_change": ""})
        self.assertNotIn("name the previous image", prompt)
        self.assertIn("do not propose rolling back", prompt)


class Upgrade(unittest.TestCase):
    def test_a_database_from_before_stack_and_config_gains_them(self):
        import sqlite3
        path = os.path.join(tempfile.mkdtemp(), "doctor.db")
        old = sqlite3.connect(path)
        old.execute("""CREATE TABLE container_state (name TEXT PRIMARY KEY,
            container_id TEXT NOT NULL, image TEXT NOT NULL DEFAULT '',
            image_id TEXT NOT NULL DEFAULT '', created TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '', restart_count INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT '', seen_at TEXT NOT NULL)""")
        old.execute("INSERT INTO container_state (name, container_id, seen_at) VALUES ('a','c','t')")
        old.commit()
        old.close()
        store = Store(path)
        self.addCleanup(store.close)
        row = store.container_states()["a"]
        self.assertEqual((row["stack"], row["service"], row["config"]), ("", "", None))


class Folding(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.addCleanup(self.store.close)

    def test_a_crash_loop_is_one_row_counting_up(self):
        start = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)
        for i in range(5):
            at = (start + timedelta(seconds=20 * i)).isoformat()
            self.store.record_change("app", "", "restarted", at, "app restarted", {},
                                     fold_minutes=30)
        [row] = self.store.list_changes(container_name="app")
        self.assertEqual(row["count"], 5)

    def test_restarts_far_apart_stay_separate(self):
        self.store.record_change("app", "", "restarted", "2026-09-26T08:00:00+00:00", "r", {},
                                 fold_minutes=30)
        self.store.record_change("app", "", "restarted", "2026-09-26T12:00:00+00:00", "r", {},
                                 fold_minutes=30)
        self.assertEqual(len(self.store.list_changes(container_name="app")), 2)


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


class Tracking(unittest.TestCase):
    """The supervisor's pass over a poll, against the real store."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _VersionOnly)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        from app.config import Settings
        from app.supervisor import Supervisor

        previous = dict(os.environ)
        os.environ.update(DOCTOR_DATA_DIR=tempfile.mkdtemp(), ANTHROPIC_API_KEY="",
                          DOCKER_HOST=f"tcp://127.0.0.1:{self.server.server_address[1]}")
        try:
            self.supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(self.supervisor.store.close)

    def poll(self, *containers):
        snapshots = {c["name"]: dict(c, watched=True) for c in containers}
        self.supervisor._track_changes(snapshots)

    def box(self, name, **extra):
        base = {"id": f"{name}-1", "name": name, "image": f"{name}:1", "image_id": "sha256:1",
                "created": "2026-09-20T10:00:00Z", "started_at": "2026-09-20T10:00:01Z",
                "restart_count": 0, "state": "running",
                "stats": {"mem_usage_bytes": 100 * 1024 * 1024},
                "labels": {"com.docker.compose.project": "arr-stack",
                           "com.docker.compose.service": name}}
        base.update(extra)
        return base

    def test_a_restart_of_the_doctor_is_not_thirty_recreates(self):
        self.poll(self.box("sonarr"), self.box("radarr"))
        self.poll(self.box("sonarr"), self.box("radarr"))
        self.assertEqual(self.supervisor.store.list_changes(), [])

    def test_a_redeploy_between_polls_is_a_recreate_not_remove_and_appear(self):
        self.poll(self.box("sonarr"))
        self.poll()  # mid-redeploy: the old one is gone, the new one not there yet
        self.poll(self.box("sonarr", id="sonarr-2", image_id="sha256:2",
                           created="2026-09-26T07:40:00Z"))
        kinds = [c["kind"] for c in self.supervisor.store.list_changes()]
        self.assertEqual(kinds, ["recreated"])

    def test_a_rename_between_polls_is_one_rename_and_the_old_name_goes(self):
        self.poll(self.box("ChangeDetection-SOCK",
                           labels={"com.docker.compose.project": "tools",
                                   "com.docker.compose.service": "sockpuppetbrowser"}))
        self.poll(self.box("changedetection-sockpuppetbrowser", image_id="sha256:1",
                           created="2026-09-28T13:48:41Z",
                           labels={"com.docker.compose.project": "tools",
                                   "com.docker.compose.service": "changedetection-sockpuppetbrowser"}))
        rows = {c["container_name"]: c for c in self.supervisor.store.list_changes()}
        self.assertEqual(rows["changedetection-sockpuppetbrowser"]["kind"], "renamed")
        self.assertEqual(rows["ChangeDetection-SOCK"]["kind"], "removed")
        self.assertIn("renamed to changedetection-sockpuppetbrowser",
                      rows["ChangeDetection-SOCK"]["summary"])
        self.assertNotIn("ChangeDetection-SOCK", self.supervisor.store.container_states())

    def test_a_config_change_survives_the_store_and_is_named(self):
        from app import runconfig
        attrs = lambda memory: {"Config": {"Env": ["TZ=UTC"], "Labels": {}},
                                "HostConfig": {"Memory": memory}}
        self.poll(self.box("sonarr", run_config=runconfig.snapshot(attrs(1024 ** 3), b"k" * 32)))
        self.poll(self.box("sonarr", id="sonarr-2", created="2026-09-26T07:40:00Z",
                           run_config=runconfig.snapshot(attrs(2 * 1024 ** 3), b"k" * 32)))
        [row] = self.supervisor.store.list_changes()
        self.assertIn("memory limit 1g → 2g", row["summary"])
        self.assertEqual(row["detail"]["config"], ["memory limit 1g → 2g"])

    def test_the_hour_of_memory_is_recorded(self):
        self.poll(self.box("sonarr"))
        history = self.supervisor.store.mem_history(datetime.now(timezone.utc) - timedelta(hours=2))
        self.assertEqual(history["sonarr"][0]["max"], 100 * 1024 * 1024)

    def test_changes_reach_the_diagnosis_prompt(self):
        from app.diagnoser import _render_incident

        prompt = _render_incident({
            "kind": "log_errors", "detected_at": "2026-09-26T07:50:00+00:00",
            "container": {"name": "sonarr"},
            "changes": [{"at": "2026-09-26T07:40:00+00:00", "kind": "recreated",
                         "summary": "sonarr was recreated on a new build", "count": 1}],
        })
        self.assertIn("## Recent changes to this container", prompt)
        self.assertIn("(10 min before detection)", prompt)


if __name__ == "__main__":
    unittest.main()
