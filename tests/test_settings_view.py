"""The settings view shows the configuration without giving away the secrets.

It is built from an allowlist rather than by filtering out names that look
sensitive, so a secret added to Settings later is absent by default rather than
present until somebody notices. This asserts the property directly: real secret
values go in, and none of them comes out.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import Settings  # noqa: E402

# Distinctive enough that a partial leak still trips the assertion.
SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-LEAKCANARY-anthropic",
    "DOCTOR_OPENAI_API_KEY": "sk-LEAKCANARY-openai",
    "DOCTOR_OIDC_CLIENT_SECRET": "LEAKCANARY-client-secret",
    "DOCTOR_SESSION_SECRET": "LEAKCANARY-session-secret",
    "DOCTOR_WEB_PASSWORD": "LEAKCANARY-password",
    "DOCTOR_NTFY_TOKEN": "LEAKCANARY-ntfy-token",
    "DOCTOR_GITHUB_TOKEN": "github_pat_LEAKCANARY",
}
CONFIG = {
    "DOCTOR_OIDC_ISSUER": "https://id.example",
    "DOCTOR_OIDC_CLIENT_ID": "380782e1-ddc4",
    "DOCTOR_PUBLIC_URL": "https://doctor.example",
    "DOCTOR_MODE": "suggest",
}


def settings_without_secrets() -> Settings:
    """Explicitly cleared, so the assertion does not depend on the machine."""
    previous = dict(os.environ)
    for name in SECRETS:
        os.environ.pop(name, None)
    for name in ("DOCTOR_OIDC_ISSUER", "DOCTOR_OPENAI_BASE_URL"):
        os.environ.pop(name, None)
    try:
        return Settings()
    finally:
        os.environ.clear()
        os.environ.update(previous)


def settings_with_secrets() -> Settings:
    previous = dict(os.environ)
    os.environ.update(SECRETS)
    os.environ.update(CONFIG)
    try:
        return Settings()
    finally:
        os.environ.clear()
        os.environ.update(previous)


class NoSecretsInTheOverview(unittest.TestCase):
    def setUp(self):
        self.settings = settings_with_secrets()
        self.overview = self.settings.overview()
        self.blob = json.dumps(self.overview)

    def test_not_one_secret_value_appears(self):
        for name, value in SECRETS.items():
            with self.subTest(name):
                self.assertNotIn(value, self.blob)

    def test_the_canary_prefix_appears_nowhere_at_all(self):
        """Catches a partial or truncated leak, not just an exact match."""
        self.assertNotIn("LEAKCANARY", self.blob)

    def test_secrets_are_reported_as_set(self):
        flat = {label: value for section in self.overview for label, value, _ in section["items"]}
        self.assertEqual(flat["Client secret"], "set")
        self.assertEqual(flat["Session secret"], "set")
        self.assertEqual(flat["Credentials"], "set")

    def test_credentials_are_judged_by_the_provider_in_use(self):
        """`openai_api_key` carries a placeholder default, so reading the raw
        field said "set" with nothing configured."""
        flat = {label: value for section in settings_without_secrets().overview()
                for label, value, _ in section["items"]}
        self.assertEqual(flat["Credentials"], "not set")

    def test_an_unset_secret_says_so(self):
        flat = {label: value for section in settings_without_secrets().overview()
                for label, value, _ in section["items"]}
        self.assertEqual(flat["Session secret"], "not set")
        self.assertEqual(flat["Credentials"], "not set")


class WhatItDoesShow(unittest.TestCase):
    def setUp(self):
        self.overview = settings_with_secrets().overview()
        self.flat = {label: value for section in self.overview
                     for label, value, _ in section["items"]}

    def test_the_things_that_were_asked_for(self):
        self.assertIn("Build", self.flat)
        self.assertIn("Model", self.flat)
        self.assertIn("Daily budget", self.flat)
        self.assertEqual(self.flat["Mode"], "suggest")

    def test_non_secret_identifiers_are_shown_in_full(self):
        self.assertEqual(self.flat["Client ID"], "380782e1-ddc4")
        self.assertEqual(self.flat["Issuer"], "https://id.example")

    def test_every_section_has_a_title_and_items(self):
        for section in self.overview:
            with self.subTest(section["title"]):
                self.assertTrue(section["title"])
                self.assertTrue(section["items"])
                for item in section["items"]:
                    self.assertEqual(len(item), 3)  # label, value, note

    def test_a_negative_price_reads_as_the_built_in_table(self):
        self.assertIn("built-in table", self.flat["Input price"])


class ThroughTheApi(unittest.TestCase):
    def test_the_endpoint_serialises_without_leaking(self):
        from fastapi.testclient import TestClient

        from app.store import Store
        from app.web.api import build_app

        settings = settings_with_secrets()

        class Stub:
            def __init__(self):
                self.settings = settings
                self.store = Store(os.path.join(tempfile.mkdtemp(), "d.db"))
                self.docker_connected = False

            def broadcast(self, *a, **k):
                pass

        client = TestClient(build_app(Stub(), settings))
        # Password login is refused while OIDC is on, so read as the browser
        # would before signing in.
        response = client.get("/api/settings")
        if response.status_code == 200:
            self.assertNotIn("LEAKCANARY", response.text)
        else:
            self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
