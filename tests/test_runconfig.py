"""The run-config snapshot: what counts as a configuration change, and what
never leaves the container's inspect in readable form."""

from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import unittest

from app import changes, runconfig

KEY = b"k" * 32


def inspect(**overrides):
    attrs = {
        "Config": {
            "Env": ["TZ=Europe/Zurich", "DB_PASSWORD=hunter2hunter2", "MAX_WORKERS=5"],
            "Cmd": ["serve"], "Entrypoint": None, "User": "",
            "Labels": {"com.docker.compose.project": "tools",
                       "com.docker.compose.service": "app",
                       "com.docker.compose.config-hash": "abc",
                       "com.docker.compose.container-number": "1",
                       "org.opencontainers.image.version": "1.0",
                       "traefik.http.routers.app.rule": "Host(`app.example`)",
                       "traefik.http.middlewares.app.basicauth.users": "me:$apr1$salt$hash"},
            "Healthcheck": {"Test": ["CMD", "true"]},
        },
        "HostConfig": {"Memory": 1024 ** 3, "NanoCpus": 0, "CapAdd": ["SYS_ADMIN"],
                       "RestartPolicy": {"Name": "unless-stopped"}, "NetworkMode": "tools_default",
                       "PortBindings": {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]}},
        "Mounts": [{"Source": "/srv/docker/app", "Destination": "/data", "RW": True}],
        "NetworkSettings": {"Networks": {"tools_default": {}}},
    }
    for path, value in overrides.items():
        section, _, field = path.partition("__")
        attrs[section][field] = value
    return attrs


def snap(**overrides):
    return runconfig.snapshot(inspect(**overrides), KEY)


class Snapshots(unittest.TestCase):
    def test_a_secret_env_value_is_never_stored_readably(self):
        stored = json.dumps(snap())
        self.assertNotIn("hunter2", stored)
        self.assertNotIn("$apr1$", stored)

    def test_an_allowlisted_env_value_stays_readable(self):
        self.assertEqual(snap()["env"]["TZ"], "v:Europe/Zurich")

    def test_the_hash_depends_on_the_install_key(self):
        other = runconfig.snapshot(inspect(), b"o" * 32)
        self.assertNotEqual(snap()["env"]["DB_PASSWORD"], other["env"]["DB_PASSWORD"])

    def test_deploy_noise_and_image_labels_are_left_out(self):
        labels = snap()["labels"]
        self.assertNotIn("com.docker.compose.container-number", labels)
        self.assertNotIn("org.opencontainers.image.version", labels)
        self.assertIn("traefik.http.routers.app.rule", labels)


class Diffs(unittest.TestCase):
    def test_nothing_changed_is_an_empty_list_not_unknown(self):
        self.assertEqual(runconfig.diff(snap(), snap()), [])

    def test_no_earlier_snapshot_is_unknown(self):
        self.assertIsNone(runconfig.diff(None, snap()))

    def test_a_secret_that_changed_says_so_without_the_value(self):
        after = snap(Config__Env=["TZ=Europe/Zurich", "DB_PASSWORD=other", "MAX_WORKERS=5"])
        [line] = runconfig.diff(snap(), after)
        self.assertEqual(line, "env DB_PASSWORD changed (value not shown)")

    def test_a_readable_value_that_changed_shows_both(self):
        after = snap(Config__Env=["TZ=UTC", "DB_PASSWORD=hunter2hunter2", "MAX_WORKERS=5"])
        self.assertEqual(runconfig.diff(snap(), after), ["env TZ Europe/Zurich → UTC"])

    def test_limits_caps_mounts_and_ports(self):
        before = snap()
        after = copy.deepcopy(before)
        after.update(mem_limit=2 * 1024 ** 3, cap_add=[], mounts=["/mnt/disk2/app:/data:rw"],
                     ports=["8081->80/tcp"])
        found = runconfig.diff(before, after)
        self.assertIn("memory limit 1g → 2g", found)
        self.assertIn("cap add - SYS_ADMIN", found)
        self.assertIn("mounts + /mnt/disk2/app:/data:rw", found)
        self.assertIn("ports - 8080->80/tcp", found)

    def test_a_basic_auth_label_change_hides_the_hash(self):
        labels = dict(inspect()["Config"]["Labels"])
        labels["traefik.http.middlewares.app.basicauth.users"] = "me:$apr1$new$hash"
        [line] = runconfig.diff(snap(), snap(Config__Labels=labels))
        self.assertIn("changed (value not shown)", line)

    def test_summaries(self):
        self.assertEqual(runconfig.summarize(None, None), "")
        self.assertEqual(runconfig.summarize([], True), "; configuration unchanged")
        self.assertIn("compose definition changed", runconfig.summarize([], False))
        self.assertEqual(runconfig.summarize(list("abcde"), None), "; config: a, b, c (+2 more)")


class Keys(unittest.TestCase):
    def test_the_key_is_made_once_and_private(self):
        folder = tempfile.mkdtemp()
        first = runconfig.load_key(folder)
        self.assertEqual(runconfig.load_key(folder), first)
        mode = stat.S_IMODE(os.stat(os.path.join(folder, runconfig.KEY_FILE)).st_mode)
        self.assertEqual(mode, 0o600)


class OnTheTimeline(unittest.TestCase):
    def seen(self, **overrides):
        base = {"id": "c1", "image": "app:1", "image_id": "sha256:1",
                "created": "2026-09-20T10:00:00Z", "state": "running",
                "run_config": snap()}
        base.update(overrides)
        return changes.observe(base)

    def test_a_recreate_names_what_changed_in_its_configuration(self):
        after_config = snap(HostConfig__Memory=512 * 1024 ** 2)
        [change] = changes.diff("app", self.seen(), self.seen(id="c2", run_config=after_config),
                                first_run=False)
        self.assertIn("config: memory limit 1g → 512m", change.summary)
        self.assertEqual(change.detail["config"], ["memory limit 1g → 512m"])

    def test_a_recreate_with_nothing_changed_says_so(self):
        [change] = changes.diff("app", self.seen(), self.seen(id="c2"), first_run=False)
        self.assertIn("on the same image; configuration unchanged", change.summary)
        self.assertEqual(change.detail["config"], [])
        self.assertEqual(change.detail["compose_definition"], "same")

    def test_a_container_from_before_snapshots_claims_nothing(self):
        [change] = changes.diff("app", self.seen(run_config=None), self.seen(id="c2"),
                                first_run=False)
        self.assertNotIn("config", change.detail)
        self.assertNotIn("configuration", change.summary)


class InThePrompt(unittest.TestCase):
    def render(self, **change):
        from app.diagnoser import _render_incident
        return _render_incident({"kind": "log_errors", "detected_at": "2026-09-29T06:07:51+00:00",
                                 "container": {"name": "app"},
                                 "changes": [dict({"at": "2026-09-29T06:00:00+00:00", "count": 1,
                                                   "kind": "recreated", "image_change": "same",
                                                   "summary": "s"}, **change)]})

    def test_config_differences_are_listed_and_pointed_at(self):
        prompt = self.render(config_changes=["memory limit 1g → 512m"])
        self.assertIn("    - memory limit 1g → 512m", prompt)
        self.assertIn("configuration it runs with changed", prompt)

    def test_a_compared_unchanged_config_narrows_it_to_the_workload(self):
        prompt = self.render(config_changes=[])
        self.assertIn("did not change either", prompt)

    def test_an_uncompared_config_claims_nothing(self):
        prompt = self.render(config_changes=None)
        self.assertNotIn("did not change either", prompt)


if __name__ == "__main__":
    unittest.main()
