"""Settings tells a value the stack environment can change from one it cannot.

A variable the compose file does not pass never reaches the container: setting
it in the stack environment does nothing at all. The doctor reads which ones
are passed from its own environment (config.passed_env); these tests keep
compose.example.yaml, .env.example and the Settings page telling the same story.
"""

from __future__ import annotations

import dataclasses
import os
import re
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import (CHOICES_FOR, ENV_FOR, SECRET_ENV, SETTINGS, Effort,  # noqa: E402
                        Mode, Settings, default_values, passed_env)
from app.web.api import describe_rows  # noqa: E402
from tests.example_compose import doctor_env, passed  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def read_by_settings() -> set[str]:
    """Every variable a Settings field reads."""
    return set(SETTINGS)


def overview_rows(**overrides) -> set[tuple[str, str]]:
    return {(section["title"], label)
            for section in Settings(**overrides).overview()
            for label, _, _ in section["items"]}


class TheMapping(unittest.TestCase):
    def test_every_row_but_the_build_names_its_variables(self):
        rows = overview_rows(github_token="x")
        self.assertEqual(sorted(rows - set(ENV_FOR)), [("Build and mode", "Build")])

    def test_no_entry_for_a_row_that_does_not_exist(self):
        self.assertEqual([key for key in ENV_FOR if key not in overview_rows(github_token="x")], [])

    def test_every_variable_named_is_one_settings_reads(self):
        named = {name for env in ENV_FOR.values() for name in env}
        self.assertEqual(sorted(named - read_by_settings()), [])

    def test_secrets_are_all_known_variables(self):
        self.assertEqual(sorted(SECRET_ENV - read_by_settings()), [])


class WhatIsPassed(unittest.TestCase):
    def test_a_variable_passed_empty_marks_a_pass_through_deployment(self):
        found = passed_env({"DOCTOR_MODE": "", "DOCTOR_MODEL": "x", "PATH": "/bin"})
        self.assertEqual(found, {"DOCTOR_MODE", "DOCTOR_MODEL"})

    def test_without_one_it_is_unknown_not_none(self):
        self.assertIsNone(passed_env({"DOCTOR_MODEL": "x"}))
        self.assertIsNone(passed_env({}))


class TheExampleComposeFile(unittest.TestCase):
    def test_it_is_read_with_the_stack_variable_behind_each(self):
        env = doctor_env()
        self.assertEqual(env["DOCTOR_MODE"], ("DOCTOR_MODE", ""))
        self.assertEqual(env["DOCKER_HOST"][0], "DOCTOR_DOCKER_HOST")
        # Only the doctor's own service: the proxy's ALLOW_RESTARTS is not ours.
        self.assertNotIn("ALLOW_RESTARTS", env)

    def test_every_documented_doctor_variable_reaches_the_container(self):
        """What .env.example tells you to set must be passed on."""
        passed_on = {stack for stack, _ in doctor_env().values()}
        documented = set(re.findall(r"^#?\s*(DOCTOR_[A-Z0-9_]+|ANTHROPIC_API_KEY)=",
                                    (ROOT / ".env.example").read_text(), re.M))
        # Read by the compose file itself, for other services, the image tag,
        # the port or the data directory.
        elsewhere = {"DOCTOR_IMAGE_TAG", "DOCTOR_PROXY_ALLOW_POST", "DOCTOR_PROXY_ALLOW_IMAGES",
                     "DOCTOR_SMART_DEVICES", "DOCTOR_DATA_VOLUME", "DOCTOR_UID", "DOCTOR_GID",
                     "DOCTOR_PORT"}
        self.assertEqual(sorted(documented - passed_on - elsewhere), [])

    def test_defaults_are_the_codes_but_for_the_deployments_own(self):
        """A default here would read as set in the stack environment, and hide the code's."""
        wiring = {"DOCKER_HOST", "DOCTOR_DISK_PATHS"}
        with_default = {name for name, (_, default) in doctor_env().items()
                        if default and name in SETTINGS}
        self.assertEqual(sorted(with_default - wiring), [])

    def test_a_container_started_from_it_reads_as_passing_through(self):
        self.assertIn("DOCTOR_MODE", passed())


class TheRows(unittest.TestCase):
    def rows(self, compose, **overrides):
        described = describe_rows(Settings(**overrides).overview(), compose,
                                  default_values())
        return {(section["title"], item["label"]): item
                for section in described for item in section["items"]}

    def test_defaults_are_the_codes_and_changes_are_marked(self):
        compose = passed()
        rows = self.rows(compose, poll_interval_seconds=45,
                         min_confidence=SETTINGS["DOCTOR_MIN_CONFIDENCE"].default)
        interval = rows[("Build and mode", "Poll interval")]
        self.assertEqual((interval["kind"], interval["changed"]), ("setting", True))
        self.assertEqual(interval["default"], f"{SETTINGS['DOCTOR_POLL_INTERVAL'].default}s")
        self.assertEqual([(e["name"], e["settable"]) for e in interval["env"]],
                         [("DOCTOR_POLL_INTERVAL", True)])
        confidence = rows[("Remediation", "Minimum confidence")]
        self.assertFalse(confidence["changed"])

    def test_a_variable_the_compose_file_does_not_pass_is_fixed(self):
        compose = passed() - {"DOCTOR_RETENTION_DAYS"}
        item = self.rows(compose)[("Incident lifecycle", "Keep history for")]
        self.assertEqual(item["kind"], "fixed")

    def test_the_variable_named_is_the_containers(self):
        item = self.rows(passed())[("Build and mode", "Docker endpoint")]
        self.assertEqual(item["env"][0]["name"], "DOCKER_HOST")

    def test_the_build_is_a_fact(self):
        item = self.rows(passed())[("Build and mode", "Build")]
        self.assertEqual((item["kind"], item["default"], item["changed"]), ("status", None, False))

    def test_when_it_is_unknown_nothing_is_called_fixed(self):
        kinds = {item["kind"] for item in self.rows(None).values()}
        self.assertNotIn("fixed", kinds)

    def test_a_secret_shows_as_set_and_its_default_as_not_set(self):
        item = self.rows(passed(), github_token="github_pat_CANARY")[("GitHub", "Token")]
        self.assertEqual((item["value"], item["default"]), ("set", None))  # no section by default
        item = self.rows(passed(), ntfy_token="CANARY")[("Notifications", "Token")]
        self.assertEqual((item["value"], item["default"], item["changed"]), ("set", "not set", True))
        self.assertNotIn("CANARY", str(self.rows(passed(), ntfy_token="CANARY")))


class Reading(unittest.TestCase):
    """How a variable's value becomes a setting."""

    def read(self, **env):
        saved = dict(os.environ)
        try:
            for name in SETTINGS:
                os.environ.pop(name, None)
            os.environ.update(env)
            return Settings()
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_every_field_is_declared_as_a_setting(self):
        self.assertEqual(len(SETTINGS), len(dataclasses.fields(Settings)))

    def test_empty_is_unset(self):
        """A compose file passes every variable, empty when the stack sets none."""
        settings = self.read(DOCTOR_MODEL="", DOCTOR_POLL_INTERVAL="", DOCTOR_BRIEFING="",
                             DOCTOR_NET_DNS_NAMES="", DOCTOR_MDSTAT_PATH="")
        self.assertEqual(settings.model, SETTINGS["DOCTOR_MODEL"].default)
        self.assertEqual(settings.poll_interval_seconds, 30)
        self.assertTrue(settings.briefing)
        self.assertEqual(settings.net_dns_names, ["cloudflare.com", "github.com"])
        self.assertEqual(settings.mdstat_path, "/proc/mdstat")

    def test_off_turns_off_what_is_on_by_default(self):
        settings = self.read(DOCTOR_MDSTAT_PATH="off", DOCTOR_SMART_URL="None")
        self.assertEqual((settings.mdstat_path, settings.smart_url), ("", ""))

    def test_none_empties_a_list(self):
        self.assertEqual(self.read(DOCTOR_NET_TCP_TARGETS="none").net_tcp_targets, [])

    def test_a_choice_is_read_lowercased(self):
        settings = self.read(DOCTOR_EFFORT="High", DOCTOR_CLASSIFIER="Shadow")
        self.assertEqual((settings.effort, settings.classifier_mode), ("high", "shadow"))

    def test_secrets_are_the_ones_declared(self):
        self.assertIn("DOCTOR_GITHUB_TOKEN", SECRET_ENV)
        self.assertNotIn("DOCTOR_MODEL", SECRET_ENV)


class TheChoices(unittest.TestCase):
    """A closed-set row shows every value its enum has, and which is in use."""

    def rows(self, **overrides):
        described = describe_rows(Settings(**overrides).overview(), passed(),
                                  default_values())
        return {(section["title"], item["label"]): item
                for section in described for item in section["items"]}

    def test_no_entry_for_a_row_that_does_not_exist(self):
        self.assertEqual([key for key in CHOICES_FOR if key not in overview_rows()], [])

    def test_every_default_is_one_of_its_choices(self):
        """Catches a row whose wording drifted from its enum's values."""
        for key, item in self.rows().items():
            if item["choices"]:
                self.assertEqual(item["choices"]["unknown"], [], key)

    def test_the_options_are_the_enums_members(self):
        item = self.rows()[("Model", "Reasoning effort")]
        self.assertEqual(item["choices"]["options"], [e.value for e in Effort])
        self.assertEqual(item["choices"]["selected"], [item["value"]])

    def test_the_value_in_use_is_selected(self):
        item = self.rows(mode=Mode.AUTO)[("Build and mode", "Mode")]
        self.assertEqual((item["choices"]["selected"], item["value"]), (["auto"], "auto"))

    def test_a_multiple_choice_row_selects_each(self):
        item = self.rows(auto_actions=["restart_container", "prune_images"])[
            ("Remediation", "Auto-applies")]
        self.assertTrue(item["choices"]["multiple"])
        self.assertEqual(item["choices"]["selected"], ["restart_container", "prune_images"])
        empty = self.rows(auto_actions=[])[("Remediation", "Auto-applies")]
        self.assertEqual((empty["choices"]["selected"], empty["choices"]["unknown"]), ([], []))

    def test_a_value_that_is_no_choice_is_still_shown(self):
        item = self.rows(effort="extreme")[("Model", "Reasoning effort")]
        self.assertEqual((item["choices"]["selected"], item["choices"]["unknown"]),
                         ([], ["extreme"]))

    def test_a_free_form_row_has_none(self):
        self.assertIsNone(self.rows()[("Build and mode", "Poll interval")]["choices"])


class BuildingTheDefaults(unittest.TestCase):
    def test_the_environment_is_left_as_it_was(self):
        before = dict(os.environ)
        os.environ["DOCTOR_POLL_INTERVAL"] = "77"
        try:
            defaults = default_values()
            self.assertEqual(os.environ["DOCTOR_POLL_INTERVAL"], "77")
            self.assertEqual(defaults[("Build and mode", "Poll interval")], "30s")
        finally:
            os.environ.clear()
            os.environ.update(before)


if __name__ == "__main__":
    unittest.main()
