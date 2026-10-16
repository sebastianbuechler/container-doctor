"""A boolean setting that cannot be read should say so, not quietly default.

Reported from a running instance: `DOCTOR_WEB_PASSWORD_FALLBACK` was set and the
warning saying it was not still appeared every restart. The parser accepted
`true` but not `"true"`, and an unrecognised value fell back to the default
without a word - so a config mistake became a silent change in who can sign in.
"""

from __future__ import annotations

import importlib
import os
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

import app.config as config_module  # noqa: E402

BASE = {
    "DOCTOR_OIDC_ISSUER": "https://id.example",
    "DOCTOR_OIDC_CLIENT_ID": "client",
    "DOCTOR_PUBLIC_URL": "https://doctor.example",
    "DOCTOR_WEB_PASSWORD": "hunter2",
}


def settings_with(fallback: str | None):
    """A fresh Settings with only the variables under test set."""
    previous = dict(os.environ)
    os.environ.clear()
    os.environ["DOCTOR_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.update(BASE)
    if fallback is not None:
        os.environ["DOCTOR_WEB_PASSWORD_FALLBACK"] = fallback
    try:
        importlib.reload(config_module)
        settings = config_module.Settings()
        return settings, settings.validate()
    finally:
        os.environ.clear()
        os.environ.update(previous)
        importlib.reload(config_module)


class ReadingAYesNoValue(unittest.TestCase):
    TRUTHY = ["true", "True", "TRUE", " true ", '"true"', "'true'", "yes", "y",
              "on", "1", "enabled"]
    FALSEY = ["false", "False", '"false"', "no", "n", "off", "0", "disabled"]

    def test_every_reasonable_spelling_of_yes_is_understood(self):
        for value in self.TRUTHY:
            with self.subTest(value):
                settings, _ = settings_with(value)
                self.assertTrue(settings.password_fallback)

    def test_every_reasonable_spelling_of_no_is_understood(self):
        for value in self.FALSEY:
            with self.subTest(value):
                settings, _ = settings_with(value)
                self.assertFalse(settings.password_fallback)

    def test_quoted_values_are_not_read_as_their_opposite(self):
        """The reported bug: `"true"` kept its quotes and read as false."""
        settings, warnings = settings_with('"true"')
        self.assertTrue(settings.password_fallback)
        self.assertTrue(settings.password_login_allowed)
        self.assertFalse([w for w in warnings if "but ignored while OIDC" in w])


class WhenTheValueMakesNoSense(unittest.TestCase):
    def test_it_is_reported_rather_than_silently_defaulted(self):
        _, warnings = settings_with("maybe")
        complaint = [w for w in warnings if "not a yes/no value" in w]
        self.assertEqual(len(complaint), 1)
        self.assertIn("DOCTOR_WEB_PASSWORD_FALLBACK", complaint[0])
        self.assertIn("maybe", complaint[0])

    def test_the_default_still_applies(self):
        settings, _ = settings_with("maybe")
        self.assertFalse(settings.password_fallback)

    def test_an_unset_variable_is_not_a_complaint(self):
        _, warnings = settings_with(None)
        self.assertFalse([w for w in warnings if "not a yes/no value" in w])


class TheWarningDiagnosesItself(unittest.TestCase):
    """Reading the log should be enough to tell what went wrong."""

    def test_it_reports_what_it_actually_read(self):
        _, warnings = settings_with("nope")
        ignored = [w for w in warnings if "but ignored while OIDC" in w]
        self.assertEqual(len(ignored), 1)
        self.assertIn("'nope'", ignored[0])

    def test_it_says_unset_when_there_is_nothing_to_read(self):
        _, warnings = settings_with(None)
        ignored = [w for w in warnings if "but ignored while OIDC" in w]
        self.assertIn("unset", ignored[0])

    def test_it_stays_quiet_once_the_fallback_is_on(self):
        _, warnings = settings_with("true")
        self.assertFalse([w for w in warnings if "but ignored while OIDC" in w])


if __name__ == "__main__":
    unittest.main()
