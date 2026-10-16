"""Password-only sign-in: the dashboard has to be able to ask for it.

/api/me never challenges - the page needs an answer to draw a sign-in button -
so with DOCTOR_WEB_PASSWORD and no OIDC it reported everyone as signed out,
and the button pointed at an OIDC route that does not exist without OIDC.
"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from tests.test_incident_logs import NEEDS_APP, client_for, make_settings  # noqa: E402


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@NEEDS_APP
class PasswordSignIn(unittest.TestCase):
    def client(self):
        client, _ = client_for(make_settings(DOCTOR_WEB_PASSWORD="pw", DOCTOR_WEB_USER="doctor",
                                             DOCTOR_OIDC_ISSUER=""))
        return client

    def test_without_the_password_the_page_is_told_to_sign_in(self):
        me = self.client().get("/api/me").json()
        self.assertFalse(me["authenticated"])
        self.assertFalse(me["oidc"])

    def test_with_the_password_the_page_knows_who_it_is(self):
        me = self.client().get("/api/me", headers=basic("doctor", "pw")).json()
        self.assertTrue(me["authenticated"])
        self.assertEqual(me["user"], "doctor")

    def test_a_wrong_password_is_still_signed_out(self):
        me = self.client().get("/api/me", headers=basic("doctor", "nope")).json()
        self.assertFalse(me["authenticated"])

    def test_the_sign_in_route_challenges_then_goes_home(self):
        client = self.client()
        challenge = client.get("/auth/password", follow_redirects=False)
        self.assertEqual(challenge.status_code, 401)
        self.assertIn("Basic", challenge.headers["WWW-Authenticate"])
        done = client.get("/auth/password", headers=basic("doctor", "pw"), follow_redirects=False)
        self.assertEqual((done.status_code, done.headers["location"]), (303, "/"))

    def test_background_reads_never_challenge(self):
        # A Basic challenge on these pops the browser's password dialog over
        # the page; with OIDC on, alongside the page's own sign-in.
        for settings in (dict(DOCTOR_OIDC_ISSUER=""),
                         dict(DOCTOR_OIDC_ISSUER="https://id.example",
                              DOCTOR_OIDC_CLIENT_ID="doctor",
                              DOCTOR_WEB_PASSWORD_FALLBACK="true")):
            client, _ = client_for(make_settings(DOCTOR_WEB_PASSWORD="pw", **settings))
            response = client.get("/api/status")
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("WWW-Authenticate", response.headers)

    def test_no_password_means_no_route(self):
        client, _ = client_for(make_settings(DOCTOR_WEB_PASSWORD="", DOCTOR_OIDC_ISSUER=""))
        self.assertEqual(client.get("/auth/password").status_code, 404)


if __name__ == "__main__":
    unittest.main()
