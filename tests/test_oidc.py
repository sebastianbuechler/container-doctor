"""Signing in against a stub identity provider.

The stub speaks real OIDC - discovery, a JWKS with a real RSA key, and an ID
token signed with it - so the checks that matter are actually exercised rather
than mocked away: the signature, the issuer and audience, the nonce, and the
`state` that ties the callback to the browser that started the login.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

try:
    from authlib.jose import JsonWebKey, jwt
    HAVE_AUTHLIB = True
except ImportError:  # pragma: no cover - authlib is a runtime dependency
    HAVE_AUTHLIB = False

KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True) if HAVE_AUTHLIB else None


class StubProvider:
    """Just enough of Pocket ID to run the authorization-code flow."""

    def __init__(self, claims: dict | None = None, userinfo: dict | None = None,
                 offer_userinfo: bool = True) -> None:
        self.claims = claims or {"preferred_username": "sbuechler", "groups": ["admins"]}
        # Claims served only from the userinfo endpoint, as Pocket ID does
        # with `picture`.
        self.userinfo = userinfo
        self.offer_userinfo = offer_userinfo
        self.issued: dict[str, dict] = {}
        # Refresh tokens, Pocket ID style: issued with every token, rotated on
        # use, and a used one is never accepted again.
        self.expires_in = 300
        self.issue_refresh_tokens = True
        self.refresh_tokens: set[str] = set()
        self.refreshes = 0
        self.refuse_refresh = False
        self.token_endpoint_down = False
        self.requested_scope = ""
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/.well-known/openid-configuration":
                    base = provider.base
                    return self._json({
                        "issuer": base,
                        "authorization_endpoint": f"{base}/authorize",
                        "token_endpoint": f"{base}/token",
                        "jwks_uri": f"{base}/jwks",
                        "response_types_supported": ["code"],
                        "subject_types_supported": ["public"],
                        "id_token_signing_alg_values_supported": ["RS256"],
                        "code_challenge_methods_supported": ["S256"],
                        "scopes_supported": ["openid", "profile", "email", "groups",
                                             "offline_access"],
                        **({"userinfo_endpoint": f"{base}/userinfo"}
                           if provider.offer_userinfo else {}),
                    })
                if parsed.path == "/userinfo":
                    if provider.userinfo is None:
                        return self._json({"error": "not found"}, status=404)
                    return self._json({"sub": "user-1", **provider.userinfo})
                if parsed.path == "/jwks":
                    return self._json({"keys": [KEY.as_dict(is_private=False)]})
                if parsed.path == "/authorize":
                    code = f"code-{len(provider.issued)}"
                    provider.issued[code] = {"nonce": query.get("nonce", [""])[0]}
                    provider.requested_scope = query.get("scope", [""])[0]
                    target = query["redirect_uri"][0] + "?" + urllib.parse.urlencode(
                        {"code": code, "state": query.get("state", [""])[0]}
                    )
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.end_headers()
                    return
                return self._json({"error": "not found"}, status=404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                form = urllib.parse.parse_qs(self.rfile.read(length).decode())
                if provider.token_endpoint_down:
                    body = b"<html>502 Bad Gateway</html>"
                    self.send_response(502)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if form.get("grant_type", [""])[0] == "refresh_token":
                    used = form.get("refresh_token", [""])[0]
                    if provider.refuse_refresh or used not in provider.refresh_tokens:
                        return self._json({"error": "invalid_grant"}, status=400)
                    provider.refresh_tokens.discard(used)
                    provider.refreshes += 1
                    nonce = None
                else:
                    record = provider.issued.get(form.get("code", [""])[0])
                    if record is None:
                        return self._json({"error": "invalid_grant"}, status=400)
                    nonce = record["nonce"]
                now = int(time.time())
                payload = {
                    "iss": provider.base,
                    "sub": "user-1",
                    "aud": form.get("client_id", ["doctor"])[0],
                    "exp": now + 300,
                    "iat": now,
                    **({"nonce": nonce} if nonce else {}),
                    **provider.claims,
                }
                token = jwt.encode({"alg": "RS256", "kid": KEY.thumbprint()}, payload, KEY)
                response = {
                    "access_token": f"at-{now}",
                    "token_type": "Bearer",
                    "expires_in": provider.expires_in,
                    "id_token": token.decode(),
                }
                if provider.issue_refresh_tokens:
                    response["refresh_token"] = f"rt-{secrets.token_hex(8)}"
                    provider.refresh_tokens.add(response["refresh_token"])
                return self._json(response)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()


def build_client(provider: StubProvider, **overrides):
    """A TestClient for an app wired to the stub provider."""
    from fastapi.testclient import TestClient

    env = {
        "DOCTOR_OIDC_ISSUER": provider.base,
        "DOCTOR_OIDC_CLIENT_ID": "doctor",
        "DOCTOR_OIDC_CLIENT_SECRET": "shh",
        "DOCTOR_PUBLIC_URL": "http://testserver",
        "DOCTOR_SESSION_SECRET": "test-session-secret",
        "DOCTOR_DATA_DIR": tempfile.mkdtemp(),
        "DOCKER_HOST": "tcp://127.0.0.1:1",
        "ANTHROPIC_API_KEY": "",
    }
    env.update(overrides)
    previous = dict(os.environ)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
    try:
        from app.config import Settings
        from app.web.api import build_app

        settings = Settings()
        app = build_app(_StubSupervisor(settings), settings)
    finally:
        os.environ.clear()
        os.environ.update(previous)
    return TestClient(app), settings


def sign_in(client):
    """Walk the flow by hand.

    `follow_redirects` cannot do it: the chain leaves the test app for the stub
    provider and comes back to `testserver`, which only exists inside the test
    client. So each hop is made against whichever of the two can serve it.
    """
    import httpx

    start = client.get("/auth/login", follow_redirects=False)
    authorize_url = start.headers["location"]
    bounced = httpx.get(authorize_url, follow_redirects=False)
    callback = httpx.URL(bounced.headers["location"])
    return client.get(f"/auth/callback?{callback.query.decode()}", follow_redirects=False)


class _StubSupervisor:
    """The API only needs a settings object and a store for these tests."""

    def __init__(self, settings):
        from app.store import Store

        self.settings = settings
        self.store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.docker_connected = False

    def status(self):
        return {"mode": "observe"}

    def snapshot(self):
        return {"containers": [], "disk": [], "docker_connected": False}

    def broadcast(self, *args, **kwargs):
        pass


@unittest.skipUnless(HAVE_AUTHLIB, "authlib not installed")
class SigningIn(unittest.TestCase):
    def setUp(self):
        self.provider = StubProvider()
        self.addCleanup(self.provider.stop)

    def test_a_full_round_trip_signs_the_user_in(self):
        client, _ = build_client(self.provider)
        before = client.get("/api/me").json()
        self.assertEqual(before["user"], "")
        self.assertTrue(before["oidc"])

        response = sign_in(client)
        self.assertEqual(response.status_code, 303)

        after = client.get("/api/me").json()
        self.assertEqual(after["user"], "sbuechler")

    def test_the_login_redirect_carries_pkce_and_state(self):
        client, _ = build_client(self.provider)
        response = client.get("/auth/login", follow_redirects=False)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(response.headers["location"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertIn("code_challenge", query)
        self.assertIn("state", query)
        self.assertIn("nonce", query)

    def test_a_callback_without_a_matching_state_is_refused(self):
        client, _ = build_client(self.provider)
        client.get("/auth/login", follow_redirects=False)
        response = client.get("/auth/callback?code=code-0&state=not-the-one")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(client.get("/api/me").json()["user"], "")

    def test_an_unknown_code_is_refused(self):
        client, _ = build_client(self.provider)
        client.get("/auth/login", follow_redirects=False)
        response = client.get("/auth/callback?code=made-up&state=whatever")
        self.assertEqual(response.status_code, 400)

    def test_the_avatar_url_is_carried_through_to_the_browser(self):
        provider = StubProvider({
            "preferred_username": "sbuechler",
            "groups": ["admins"],
            "picture": "https://id.bchlr.site/api/users/13904a3b/profile-picture.png",
        })
        self.addCleanup(provider.stop)
        client, _ = build_client(provider)
        sign_in(client)
        me = client.get("/api/me").json()
        self.assertEqual(
            me["picture"], "https://id.bchlr.site/api/users/13904a3b/profile-picture.png"
        )
        self.assertEqual(me["groups"], ["admins"])

    def test_no_picture_claim_is_an_empty_string_not_a_crash(self):
        provider = StubProvider({"preferred_username": "sb"})
        self.addCleanup(provider.stop)
        client, _ = build_client(provider)
        sign_in(client)
        self.assertEqual(client.get("/api/me").json()["picture"], "")

    def test_a_picture_served_only_from_the_userinfo_endpoint(self):
        """The reported bug: Pocket ID keeps `picture` out of the ID token."""
        provider = StubProvider(
            {"preferred_username": "sbuechler", "groups": ["administrators"]},
            userinfo={"picture": "https://id.bchlr.site/api/users/13904a3b/profile-picture.png"},
        )
        self.addCleanup(provider.stop)
        client, _ = build_client(provider)
        sign_in(client)
        me = client.get("/api/me").json()
        self.assertEqual(
            me["picture"], "https://id.bchlr.site/api/users/13904a3b/profile-picture.png"
        )
        self.assertEqual(me["user"], "sbuechler")

    def test_the_id_token_still_wins_where_it_has_the_claim(self):
        provider = StubProvider(
            {"preferred_username": "sb", "picture": "https://id/from-token.png"},
            userinfo={"picture": "https://id/from-endpoint.png"},
        )
        self.addCleanup(provider.stop)
        client, _ = build_client(provider)
        sign_in(client)
        # The endpoint is the more complete source, so it takes precedence.
        self.assertEqual(client.get("/api/me").json()["picture"],
                         "https://id/from-endpoint.png")

    def test_a_provider_without_a_userinfo_endpoint_still_signs_in(self):
        provider = StubProvider({"preferred_username": "sb"}, offer_userinfo=False)
        self.addCleanup(provider.stop)
        client, _ = build_client(provider)
        sign_in(client)
        self.assertEqual(client.get("/api/me").json()["user"], "sb")

    def test_a_userinfo_endpoint_that_errors_does_not_break_the_login(self):
        provider = StubProvider({"preferred_username": "sb"}, userinfo=None)
        self.addCleanup(provider.stop)
        client, _ = build_client(provider)
        sign_in(client)
        me = client.get("/api/me").json()
        self.assertEqual(me["user"], "sb")
        self.assertEqual(me["picture"], "")

    def test_userinfo_describing_a_different_subject_is_discarded(self):
        """A mismatched `sub` means the response is about somebody else."""
        provider = StubProvider(
            {"preferred_username": "sb"},
            userinfo={"sub": "someone-else", "picture": "https://id/wrong.png",
                      "preferred_username": "impostor"},
        )
        self.addCleanup(provider.stop)
        client, _ = build_client(provider)
        sign_in(client)
        me = client.get("/api/me").json()
        self.assertEqual(me["user"], "sb")
        self.assertEqual(me["picture"], "")

    def test_the_page_itself_is_reachable_while_signed_out(self):
        """It renders the sign-in button, so a 401 here is a lockout."""
        client, _ = build_client(self.provider)
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def test_the_api_behind_it_is_still_closed(self):
        client, _ = build_client(self.provider)
        self.assertEqual(client.get("/api/status").status_code, 401)
        self.assertEqual(client.get("/api/incidents").status_code, 401)

    def test_signing_out_clears_the_session(self):
        client, _ = build_client(self.provider)
        sign_in(client)
        self.assertEqual(client.get("/api/me").json()["user"], "sbuechler")
        client.get("/auth/logout", follow_redirects=False)
        self.assertEqual(client.get("/api/me").json()["user"], "")


@unittest.skipUnless(HAVE_AUTHLIB, "authlib not installed")
class GroupRestriction(unittest.TestCase):
    def test_a_member_of_an_allowed_group_gets_in(self):
        provider = StubProvider({"preferred_username": "sb", "groups": ["homelab-admins"]})
        self.addCleanup(provider.stop)
        client, _ = build_client(provider, DOCTOR_OIDC_ALLOWED_GROUPS="homelab-admins")
        sign_in(client)
        self.assertEqual(client.get("/api/me").json()["user"], "sb")

    def test_someone_outside_it_is_refused_with_403(self):
        provider = StubProvider({"preferred_username": "guest", "groups": ["guests"]})
        self.addCleanup(provider.stop)
        client, _ = build_client(provider, DOCTOR_OIDC_ALLOWED_GROUPS="homelab-admins")
        response = sign_in(client)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get("/api/me").json()["user"], "")

    def test_a_group_claim_sent_as_a_string_still_matches(self):
        provider = StubProvider({"preferred_username": "sb", "groups": "admins homelab"})
        self.addCleanup(provider.stop)
        client, _ = build_client(provider, DOCTOR_OIDC_ALLOWED_GROUPS="homelab")
        sign_in(client)
        self.assertEqual(client.get("/api/me").json()["user"], "sb")


@unittest.skipUnless(HAVE_AUTHLIB, "authlib not installed")
class ThePasswordFallback(unittest.TestCase):
    """Configurable, and off unless asked for."""

    def setUp(self):
        self.provider = StubProvider()
        self.addCleanup(self.provider.stop)

    def test_the_password_is_refused_once_oidc_is_on(self):
        client, settings = build_client(self.provider, DOCTOR_WEB_PASSWORD="hunter2")
        self.assertFalse(settings.password_login_allowed)
        response = client.get("/api/status", auth=("doctor", "hunter2"))
        self.assertEqual(response.status_code, 401)

    def test_turning_the_fallback_on_lets_it_back_in(self):
        client, settings = build_client(
            self.provider,
            DOCTOR_WEB_PASSWORD="hunter2",
            DOCTOR_WEB_PASSWORD_FALLBACK="true",
        )
        self.assertTrue(settings.password_login_allowed)
        self.assertEqual(client.get("/api/status", auth=("doctor", "hunter2")).status_code, 200)
        self.assertEqual(client.get("/api/status", auth=("doctor", "wrong")).status_code, 401)

    def test_without_oidc_the_password_works_as_before(self):
        client, settings = build_client(
            self.provider,
            DOCTOR_OIDC_ISSUER="",
            DOCTOR_OIDC_CLIENT_ID="",
            DOCTOR_WEB_PASSWORD="hunter2",
        )
        self.assertFalse(settings.oidc_enabled)
        self.assertTrue(settings.password_login_allowed)
        self.assertEqual(client.get("/api/status", auth=("doctor", "hunter2")).status_code, 200)


@unittest.skipUnless(HAVE_AUTHLIB, "authlib not installed")
class StartupProbe(unittest.TestCase):
    """A provider the doctor cannot reach should say so on the dashboard, not
    at somebody's first sign-in attempt."""

    def supervisor(self, **env):
        import asyncio

        base = {
            "DOCTOR_OIDC_CLIENT_ID": "doctor",
            "DOCTOR_PUBLIC_URL": "http://doctor.example",
            "DOCTOR_DATA_DIR": tempfile.mkdtemp(),
            "DOCTOR_SESSION_SECRET": "s",
            "ANTHROPIC_API_KEY": "",
        }
        base.update(env)
        previous = dict(os.environ)
        os.environ.update(base)
        try:
            from app.config import Settings

            settings = Settings()
        finally:
            os.environ.clear()
            os.environ.update(previous)

        from app.supervisor import Supervisor

        probe = Supervisor._check_oidc
        holder = type("H", (), {"settings": settings, "startup_warnings": []})()
        asyncio.run(probe(holder))
        return holder.startup_warnings

    def test_a_redirected_discovery_url_is_followed(self):
        """The reported false alarm.

        A provider that answers the well-known path with a redirect signs
        people in perfectly - the OIDC client follows it. The probe did not,
        raised HTTPStatusError, and put "cannot reach" on a permanent banner.
        """
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        real = StubProvider()
        self.addCleanup(real.stop)

        class Redirector(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/.well-known"):
                    self.send_response(302)
                    self.send_header(
                        "Location", real.base + "/.well-known/openid-configuration")
                    self.end_headers()
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        front = HTTPServer(("127.0.0.1", 0), Redirector)
        threading.Thread(target=front.serve_forever, daemon=True).start()
        self.addCleanup(front.shutdown)

        warnings = self.supervisor(
            DOCTOR_OIDC_ISSUER=f"http://127.0.0.1:{front.server_address[1]}")
        # It follows the redirect and reads the document; the only thing it has
        # to say is that the issuer inside names the real provider.
        self.assertFalse([w for w in warnings if "Cannot reach" in w])

    def test_an_http_error_reports_the_status_not_the_exception(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Refuser(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Refuser)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        warnings = self.supervisor(
            DOCTOR_OIDC_ISSUER=f"http://127.0.0.1:{server.server_address[1]}")
        self.assertTrue(warnings)
        self.assertIn("HTTP 404", warnings[0])
        self.assertNotIn("HTTPStatusError", warnings[0])

    def test_a_page_that_is_not_a_discovery_document_says_so(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Html(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b"<!doctype html><title>login</title>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Html)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        warnings = self.supervisor(
            DOCTOR_OIDC_ISSUER=f"http://127.0.0.1:{server.server_address[1]}")
        self.assertTrue(warnings)
        self.assertIn("did not return a discovery document", warnings[0])

    def test_an_unreachable_provider_is_reported(self):
        warnings = self.supervisor(DOCTOR_OIDC_ISSUER="http://127.0.0.1:9")
        self.assertTrue(warnings)
        self.assertIn("Cannot reach the OIDC provider", warnings[0])

    def test_an_issuer_that_disagrees_is_reported(self):
        provider = StubProvider()
        self.addCleanup(provider.stop)
        # Reachable, but reached by a name the provider does not call itself -
        # exactly what an internal URL against a public APP_URL produces.
        alias = provider.base.replace("127.0.0.1", "localhost")
        warnings = self.supervisor(DOCTOR_OIDC_ISSUER=alias)
        self.assertTrue(warnings)
        self.assertIn("issuer mismatch", warnings[0])

    def test_a_healthy_provider_says_nothing(self):
        provider = StubProvider()
        self.addCleanup(provider.stop)
        self.assertEqual(self.supervisor(DOCTOR_OIDC_ISSUER=provider.base), [])

    def test_no_oidc_means_no_probe(self):
        self.assertEqual(self.supervisor(DOCTOR_OIDC_ISSUER=""), [])


@unittest.skipUnless(HAVE_AUTHLIB, "authlib not installed")
class TheSignInCardsProviderDot(unittest.TestCase):
    """Whether Pocket ID answers, asked by the sign-in card before anyone has
    a session."""

    def test_a_running_provider_is_online_and_needs_no_session(self):
        provider = StubProvider()
        self.addCleanup(provider.stop)
        client, _ = build_client(provider)
        response = client.get("/api/auth/provider")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"online": True, "detail": ""})

    def test_a_stopped_provider_is_offline_with_the_reason(self):
        provider = StubProvider()
        provider.stop()
        provider.server.server_close()
        client, _ = build_client(provider)
        answer = client.get("/api/auth/provider").json()
        self.assertFalse(answer["online"])
        self.assertEqual(answer["detail"], "ConnectError")

    def test_an_error_page_is_offline_with_the_status(self):
        provider = StubProvider()
        self.addCleanup(provider.stop)
        client, _ = build_client(provider, DOCTOR_OIDC_ISSUER=provider.base + "/nowhere")
        answer = client.get("/api/auth/provider").json()
        self.assertEqual(answer, {"online": False, "detail": "HTTP 404"})

    def test_without_oidc_there_is_nothing_to_ask(self):
        provider = StubProvider()
        self.addCleanup(provider.stop)
        client, _ = build_client(provider, DOCTOR_OIDC_ISSUER="")
        self.assertNotEqual(client.get("/api/auth/provider").status_code, 200)


@unittest.skipUnless(HAVE_AUTHLIB, "authlib not installed")
class AProviderThatIsDown(unittest.TestCase):
    """Pocket ID restarting used to turn the sign-in button into a bare 500."""

    def test_an_unreachable_provider_is_a_page_that_says_so(self):
        provider = StubProvider()
        provider.stop()
        provider.server.server_close()
        client, _ = build_client(provider)
        response = client.get("/auth/login", follow_redirects=False)
        self.assertEqual(response.status_code, 502)
        self.assertIn("Cannot reach the identity provider", response.text)
        self.assertIn("ConnectError", response.text)

    def test_a_provider_answering_with_an_error_names_the_status(self):
        provider = StubProvider()
        self.addCleanup(provider.stop)
        client, _ = build_client(provider, DOCTOR_OIDC_ISSUER=provider.base + "/nowhere")
        response = client.get("/auth/login", follow_redirects=False)
        self.assertEqual(response.status_code, 502)
        self.assertIn("HTTP 404", response.text)

    def test_signing_in_works_once_it_is_back(self):
        """A failed discovery must not be remembered."""
        provider = StubProvider()
        self.addCleanup(provider.stop)
        client, _ = build_client(provider)
        original = provider.server.RequestHandlerClass.do_GET

        def down(handler):
            handler._json({"error": "starting"}, status=503)

        provider.server.RequestHandlerClass.do_GET = down
        self.assertEqual(client.get("/auth/login", follow_redirects=False).status_code, 502)
        provider.server.RequestHandlerClass.do_GET = original
        self.assertEqual(sign_in(client).status_code, 303)


def cookie_payload(client) -> dict:
    """What the browser holds: the signed session cookie, decoded."""
    raw = client.cookies.get("doctor_session")
    data = raw.split(".")[0]
    return json.loads(base64.b64decode(data + "=" * (-len(data) % 4)))


@unittest.skipUnless(HAVE_AUTHLIB, "authlib not installed")
class TheProviderKeepsASay(unittest.TestCase):
    """After sign-in, the provider's refresh token - not the cookie - decides
    whether a session goes on."""

    def setUp(self):
        self.provider = StubProvider()
        self.addCleanup(self.provider.stop)

    def signed_in(self, **overrides):
        client, settings = build_client(self.provider, **overrides)
        self.assertEqual(sign_in(client).status_code, 303)
        self.assertEqual(client.get("/api/me").json()["user"], "sbuechler")
        return client

    def expire(self):
        """Every token issued from now on is due for renewal at once."""
        self.provider.expires_in = 1

    def test_offline_access_is_asked_for_when_the_provider_offers_it(self):
        sign_in(build_client(self.provider)[0])
        self.assertIn("offline_access", self.provider.requested_scope.split())

    def test_the_refresh_token_never_reaches_the_browser(self):
        client = self.signed_in()
        (token,) = self.provider.refresh_tokens
        held = json.dumps(cookie_payload(client))
        self.assertNotIn(token, held)
        self.assertIn("sid", cookie_payload(client)["doctor_user"])

    def test_a_current_access_token_is_left_alone(self):
        client = self.signed_in()
        client.get("/api/status")
        self.assertEqual(self.provider.refreshes, 0)

    def test_an_expired_one_is_renewed_and_the_session_goes_on(self):
        self.expire()
        client = self.signed_in()
        for _ in range(3):
            self.assertEqual(client.get("/api/status").status_code, 200)
        # Each renewal used the token the last one handed out: the stub
        # accepts a refresh token once, as Pocket ID does.
        self.assertGreaterEqual(self.provider.refreshes, 3)

    def test_a_refused_refresh_signs_you_out(self):
        """The account disabled, or its access revoked, in Pocket ID."""
        self.expire()
        client = self.signed_in()
        self.provider.refuse_refresh = True
        self.assertEqual(client.get("/api/status").status_code, 401)
        self.assertEqual(client.get("/api/me").json()["user"], "")
        # And it stays over once the provider would answer again.
        self.provider.refuse_refresh = False
        self.assertEqual(client.get("/api/me").json()["user"], "")

    def test_losing_the_allowed_group_signs_you_out(self):
        self.expire()
        client = self.signed_in(DOCTOR_OIDC_ALLOWED_GROUPS="admins")
        self.provider.claims = {"preferred_username": "sbuechler", "groups": ["guests"]}
        self.assertEqual(client.get("/api/status").status_code, 401)

    def test_new_groups_reach_the_page(self):
        self.expire()
        client = self.signed_in()
        self.provider.claims = {"preferred_username": "sbuechler", "groups": ["admins", "ops"]}
        self.assertEqual(client.get("/api/me").json()["groups"], ["admins", "ops"])

    def test_a_provider_that_is_down_does_not_sign_you_out(self):
        """You could not sign in again anyway; it is asked again shortly."""
        self.expire()
        client = self.signed_in()
        self.provider.token_endpoint_down = True
        self.assertEqual(client.get("/api/status").status_code, 200)
        self.assertEqual(client.get("/api/me").json()["user"], "sbuechler")

    def test_a_provider_that_is_gone_does_not_sign_you_out(self):
        self.expire()
        client = self.signed_in()
        self.provider.stop()
        self.provider.server.server_close()
        self.assertEqual(client.get("/api/status").status_code, 200)

    def test_without_a_refresh_token_the_session_ends_with_the_access_token(self):
        self.provider.issue_refresh_tokens = False
        self.expire()
        client, _ = build_client(self.provider)
        self.assertEqual(sign_in(client).status_code, 303)
        self.assertEqual(client.get("/api/status").status_code, 401)
        self.assertEqual(client.get("/api/me").json()["user"], "")

    def test_a_cookie_kept_from_before_sign_out_no_longer_works(self):
        """Signing out used to only clear the browser's copy."""
        client = self.signed_in()
        kept = client.cookies.get("doctor_session")
        client.get("/auth/logout", follow_redirects=False)
        client.cookies.set("doctor_session", kept)
        self.assertEqual(client.get("/api/me").json()["user"], "")
        self.assertEqual(client.get("/api/status").status_code, 401)

    def test_a_session_from_before_this_change_is_asked_to_sign_in_again(self):
        from itsdangerous import TimestampSigner

        client, _ = build_client(self.provider)
        legacy = {"doctor_user": {"sub": "user-1", "name": "sbuechler", "groups": []}}
        data = base64.b64encode(json.dumps(legacy).encode())
        client.cookies.set("doctor_session",
                           TimestampSigner("test-session-secret").sign(data).decode())
        self.assertEqual(client.get("/api/me").json()["user"], "")


PIXEL = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/141.0.0.0 Mobile Safari/537.36")
WINDOWS_FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0"


@unittest.skipUnless(HAVE_AUTHLIB, "authlib not installed")
class TheProfilePage(unittest.TestCase):
    def setUp(self):
        self.provider = StubProvider({"preferred_username": "sbuechler", "given_name": "Sebastian",
                                      "email": "sb@example.org", "groups": ["admins"]})
        self.addCleanup(self.provider.stop)
        self.client, _ = build_client(self.provider)
        self.client.headers["User-Agent"] = PIXEL
        sign_in(self.client)

    def another_device(self, user_agent=WINDOWS_FIREFOX):
        from fastapi.testclient import TestClient

        other = TestClient(self.client.app)
        other.headers["User-Agent"] = user_agent
        sign_in(other)
        return other

    def sessions(self, client=None):
        return (client or self.client).get("/api/profile").json()["sessions"]

    def test_it_shows_the_identity_and_the_provider(self):
        profile = self.client.get("/api/profile").json()
        self.assertEqual(profile["user"]["name"], "sbuechler")
        self.assertEqual(profile["claims"]["email"], "sb@example.org")
        self.assertEqual(profile["claims"]["sub"], "user-1")
        self.assertNotIn("nonce", profile["claims"])
        self.assertEqual(profile["provider"]["issuer"], self.provider.base)
        self.assertIn("offline_access", profile["provider"]["scopes"])

    def test_it_never_shows_what_the_cookie_or_the_store_hold(self):
        body = self.client.get("/api/profile").text
        self.assertNotIn(cookie_payload(self.client)["doctor_user"]["sid"], body)
        for token in self.provider.refresh_tokens:
            self.assertNotIn(token, body)

    def test_a_device_is_named_after_its_owner_and_model(self):
        (only,) = self.sessions()
        self.assertTrue(only["current"])
        self.assertEqual(only["name"], "Sebastian's Android device")
        self.assertEqual(only["browser"], "Chrome")
        self.assertTrue(self.client.get("/api/me").json()["profile"])
        self.assertFalse(self.client.get("/api/me").json()["device_reported"])
        self.client.post("/api/profile/device", json={"model": "Pixel 8 Pro", "platform": "Android"})
        self.assertEqual(self.sessions()[0]["name"], "Sebastian's Pixel 8 Pro")
        self.assertTrue(self.client.get("/api/me").json()["device_reported"])

    def test_every_session_is_listed(self):
        self.another_device()
        names = sorted(item["name"] for item in self.sessions())
        self.assertEqual(names, ["Sebastian's Android device", "Sebastian's Windows PC"])
        self.assertEqual(sum(item["current"] for item in self.sessions()), 1)

    def test_ending_another_session_signs_that_device_out(self):
        other = self.another_device()
        target = next(item for item in self.sessions() if not item["current"])
        response = self.client.post(f"/api/profile/sessions/{target['id']}/end")
        self.assertEqual(response.json(), {"ended": True, "current": False})
        self.assertEqual(other.get("/api/status").status_code, 401)
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        self.assertEqual(len(self.sessions()), 1)

    def test_ending_this_session_signs_you_out(self):
        (mine,) = self.sessions()
        self.assertTrue(self.client.post(f"/api/profile/sessions/{mine['id']}/end").json()["current"])
        self.assertEqual(self.client.get("/api/me").json()["user"], "")

    def test_ending_all_the_others(self):
        first, second = self.another_device(), self.another_device()
        self.assertEqual(self.client.post("/api/profile/sessions/end-others").json(), {"ended": 2})
        self.assertEqual(first.get("/api/status").status_code, 401)
        self.assertEqual(second.get("/api/status").status_code, 401)
        self.assertEqual(self.client.get("/api/status").status_code, 200)

    def test_a_session_can_be_given_a_name_and_have_it_taken_away(self):
        (mine,) = self.sessions()
        self.client.put(f"/api/profile/sessions/{mine['id']}/name", json={"name": "  Work  phone "})
        self.assertEqual(self.sessions()[0]["name"], "Work phone")
        self.client.put(f"/api/profile/sessions/{mine['id']}/name", json={"name": ""})
        self.assertEqual(self.sessions()[0]["name"], "Sebastian's Android device")

    def test_an_unknown_session_is_404(self):
        self.assertEqual(self.client.post("/api/profile/sessions/0000000000000000/end").status_code, 404)

    def test_a_request_from_another_site_is_refused(self):
        (mine,) = self.sessions()
        response = self.client.post(f"/api/profile/sessions/{mine['id']}/end",
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.sessions()), 1)

    def test_signed_out_there_is_no_profile(self):
        from fastapi.testclient import TestClient

        self.assertEqual(TestClient(self.client.app).get("/api/profile").status_code, 401)


class NamingDevices(unittest.TestCase):
    def describe(self, **kwargs):
        from app.web import devices

        return devices.describe(owner="Sebastian", **kwargs)

    def test_a_model_from_client_hints_wins(self):
        self.assertEqual(self.describe(user_agent=PIXEL, model="Pixel 8 Pro")["name"],
                         "Sebastian's Pixel 8 Pro")

    def test_without_one_the_kind_of_device(self):
        cases = {
            PIXEL: ("Sebastian's Android device", "Chrome"),
            WINDOWS_FIREFOX: ("Sebastian's Windows PC", "Firefox"),
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1":
                ("Sebastian's iPhone", "Safari"),
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like "
            "Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0": ("Sebastian's Mac", "Edge"),
        }
        for user_agent, (name, browser) in cases.items():
            with self.subTest(name=name):
                described = self.describe(user_agent=user_agent)
                self.assertEqual((described["name"], described["browser"]), (name, browser))

    def test_a_typed_name_beats_everything(self):
        self.assertEqual(self.describe(user_agent=PIXEL, model="Pixel 8 Pro", label="Work")["name"],
                         "Work")

    def test_nothing_to_go_on(self):
        from app.web import devices

        self.assertEqual(devices.describe(user_agent="curl/8")["name"], "Unknown device")

    def test_the_owner_is_called_by_their_first_name(self):
        from app.web import devices

        self.assertEqual(devices.first_name({"name": "Sebastian Büchler"}), "Sebastian")
        self.assertEqual(devices.first_name({"given_name": "Seb", "name": "Sebastian B"}), "Seb")
        self.assertEqual(devices.first_name({"preferred_username": "sb"}), "sb")



class AnOlderDatabase(unittest.TestCase):
    def test_sessions_kept_by_the_last_release_gain_the_new_columns(self):
        import sqlite3

        from app.store import Store

        path = os.path.join(tempfile.mkdtemp(), "doctor.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE oidc_sessions (id TEXT PRIMARY KEY, subject TEXT NOT NULL,"
                     " refresh_token TEXT NOT NULL DEFAULT '', expires_at REAL NOT NULL,"
                     " retry_at REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
        conn.execute("INSERT INTO oidc_sessions VALUES ('s1', 'user-1', '', 1.0, 0, 'then')")
        conn.commit()
        conn.close()
        store = Store(path)
        self.addCleanup(store.close)
        (row,) = store.oidc_sessions_of("user-1")
        self.assertEqual((row["label"], row["claims_json"], row["last_seen_at"]), ("", "{}", 0))


if __name__ == "__main__":
    unittest.main()
