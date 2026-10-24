"""FastAPI surface for the dashboard."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import (ENV_FOR, SETTINGS, Settings, check_value, check_values, choices,
                      current, default_values, editable, passed_env, resolve, source, storable)
from ..supervisor import Supervisor
from .oidc import SESSION_USER, KeepSessionsCurrent, OIDCClient, OIDCSessions, client_of

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

# Browsers may reuse a file served without Cache-Control for as long as they
# guess it is fresh. After an upgrade that paired a new index.html with the
# old app.js from cache: the new buttons drew, and nothing answered them.
# So every static file is revalidated (a 304 when unchanged), and the page
# names its script and stylesheet by content, so a new build is a new URL.
REVALIDATE = {"Cache-Control": "no-cache"}
_ASSET = re.compile(r'/static/([\w.-]+\.(?:js|css))"')

# How long one look at the identity provider answers the sign-in card's dot.
PROVIDER_STATUS_SECONDS = 15

# Wrong passwords: this many from one client within the window, and that
# client is refused outright until the window is over.
PASSWORD_FAILURES = 10
PASSWORD_WINDOW_SECONDS = 300

NO_SIGN_IN = "Set DOCTOR_WEB_PASSWORD or configure OIDC to use the dashboard."

# The page loads nothing from elsewhere but the OIDC avatar, which may be on
# any host; inline styles are set from the script.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src * data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _offline_csp() -> str:
    """The offline page must stand alone - it is shown exactly when nothing
    else loads - so its one inline script is allowed by its hash."""
    import base64

    page = (STATIC_DIR / "offline.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
        for body in scripts)
    return SECURITY_HEADERS["Content-Security-Policy"].replace(
        "script-src 'self'", f"script-src 'self' {hashes}")


def session_secret_for(settings: Settings) -> str:
    """DOCTOR_SESSION_SECRET, else one generated once and kept in the data
    directory, so a restart does not sign everyone out. Only when the data
    directory cannot be written is it random per start."""
    if settings.session_secret:
        return settings.session_secret
    path = Path(settings.data_dir) / "session_secret"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError as error:
        log.warning("cannot read %s (%s); using a secret for this run only", path, error)
        return secrets.token_urlsafe(32)
    secret = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secret)
    except FileExistsError:  # another start got there first
        return path.read_text(encoding="utf-8").strip() or secret
    except OSError as error:
        log.warning("cannot save %s (%s); using a secret for this run only", path, error)
    return secret


async def _probe_provider(url: str) -> tuple[bool, str]:
    """Fetch the discovery document. Online means it answered with one, the
    same thing the sign-in button needs first. Redirects are followed, as the
    OIDC client follows them."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=4, follow_redirects=True) as client:
            response = await client.get(url)
    except Exception as error:
        return False, error.__class__.__name__
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"
    try:
        document = response.json()
    except ValueError:
        return False, "not a discovery document"
    if not isinstance(document, dict) or "authorization_endpoint" not in document:
        return False, "not a discovery document"
    return True, ""


class _RevalidatedStatic(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(REVALIDATE)
        return response


def _index_html() -> str:
    """index.html with each script and stylesheet URL carrying its hash."""
    def stamp(match: re.Match) -> str:
        name = match.group(1)
        digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
        return f'/static/{name}?v={digest}"'
    return _ASSET.sub(stamp, (STATIC_DIR / "index.html").read_text(encoding="utf-8"))


class RediagnoseRequest(BaseModel):
    # Generous at the door; the supervisor keeps the first 2000 characters.
    context: str = Field(default="", max_length=8000)


class RedeployRequest(BaseModel):
    container: str = Field(min_length=1, max_length=128)


class SessionName(BaseModel):
    name: str = Field(default="", max_length=60)


class DeviceReport(BaseModel):
    model: str = Field(default="", max_length=64)
    platform: str = Field(default="", max_length=32)


class ManualActionRequest(BaseModel):
    container: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=64)


def _auth_error(message: str, *, status: int):
    """A plain page rather than JSON: this is the end of a browser redirect."""
    from fastapi.responses import HTMLResponse
    from html import escape

    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>Sign-in</title>"
        f"<body style=\"font:16px system-ui;padding:2rem;max-width:32rem\">"
        f"<h1 style=\"font-size:1.1rem\">Sign-in</h1><p>{escape(message)}</p>"
        f"<p><a href=\"/auth/login\">Try again</a></p>",
        status_code=status,
    )


# A row read from several variables shows the strongest source among them.
_SOURCE_ORDER = ("env", "saved", "default")


class SettingValue(BaseModel):
    # As it would be typed into the stack environment; empty resets to the default.
    value: str = Field(default="", max_length=4000)


# What an export is, so an import can tell it from any other JSON file.
EXPORT_FORMAT = "container-doctor-settings"
EXPORT_VERSION = 1


class SettingsImport(BaseModel):
    format: str = EXPORT_FORMAT
    version: int = EXPORT_VERSION
    # Strings as exported; a number or true/false typed into the file by hand
    # is read the way the environment would read it.
    settings: dict[str, str | bool | int | float] = Field(max_length=500)
    # Reset every saved setting the file leaves out, so the page ends up
    # holding exactly the file's - rather than adding the file's to it.
    replace: bool = False
    # Report what would change, and change nothing.
    dry_run: bool = False


def describe_rows(sections: list[dict[str, Any]],
                  passed: frozenset[str] | None,
                  defaults: dict[tuple[str, str], str],
                  saved: dict[str, str] | None = None,
                  pending: dict[tuple[str, str], str] | None = None,
                  can_edit: bool = False) -> list[dict[str, Any]]:
    """Settings rows for the page: what each is, and whether it can be changed.

    kind is "setting" (the stack environment sets it), "fixed" (read from a
    variable the deployment does not pass - see config.passed_env - so only
    an edit to its compose file changes it) or "status" (a fact, not a
    setting). With `passed` None, which ones are passed is unknown and
    nothing is called fixed.

    source says where the value comes from: "env" (the stack environment,
    which overrides anything saved), "saved" (the Settings page) or "default"
    (the code). None for a fact.

    With can_edit, each variable the page may save says so, with what it
    holds now as it would be typed and, for a closed set, its options. A row
    whose saved value waits for a restart carries it as `pending`.
    """
    saved = saved or {}
    pending = pending or {}

    def variable(name: str) -> dict[str, Any]:
        spec = SETTINGS.get(name)
        item: dict[str, Any] = {
            "name": name,
            "key": name,
            "settable": bool(passed and name in passed),
            "source": source(name, saved),
            "editable": bool(can_edit and spec and editable(name)),
        }
        if item["editable"]:
            item.update(current=current(name, saved), type=spec.kind, restart=spec.restart,
                        options=[c.value for c in spec.choices] if spec.choices else None)
        return item

    def secret_row(names: tuple[str, ...]) -> bool:
        """A row about a secret is not edited here, even where some of its
        variables could be: the secret itself stays in the stack environment."""
        return any(name in SETTINGS and SETTINGS[name].secret for name in names)

    described = []
    for section in sections:
        items = []
        for label, value, note in section["items"]:
            names = ENV_FOR.get((section["title"], label), ())
            env = [variable(name) for name in names]
            if secret_row(names):
                for e in env:
                    e["editable"] = False
                    for key in ("current", "type", "options", "restart"):
                        e.pop(key, None)
            if not env:
                kind = "status"
            elif passed is None or any(e["settable"] for e in env):
                kind = "setting"
            else:
                kind = "fixed"
            default = defaults.get((section["title"], label)) if kind != "status" else None
            items.append({
                "label": label, "value": str(value), "note": note, "kind": kind,
                "env": env, "default": default,
                "changed": default is not None and str(value) != default,
                "choices": choices(section["title"], label, str(value)),
                "source": next((s for s in _SOURCE_ORDER if any(e["source"] == s for e in env)),
                               None),
                "pending": pending.get((section["title"], label)),
            })
        described.append({"title": section["title"], "group": section["group"], "items": items})
    return described


def _exit_for_restart() -> None:
    """Stop the way `docker stop` would; Docker's restart policy
    (unless-stopped) starts the doctor again, reading what was saved."""
    os.kill(os.getpid(), signal.SIGTERM)


def build_app(supervisor: Supervisor, settings: Settings,
              restart: Callable[[], None] = _exit_for_restart) -> FastAPI:
    app = FastAPI(title="Container Doctor", docs_url=None, redoc_url=None)
    from starlette.middleware.gzip import GZipMiddleware

    # The event stream is left alone: GZipMiddleware skips text/event-stream.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    # Read once: what the deployment passes does not change while the doctor
    # runs, and the defaults are built by swapping the environment, which is
    # no thing to do per request.
    passed = passed_env()
    defaults = default_values()

    oidc: OIDCClient | None = None
    sessions: OIDCSessions | None = None
    if settings.oidc_enabled:
        from starlette.middleware.sessions import SessionMiddleware

        session_secret = session_secret_for(settings)
        oidc = OIDCClient(settings)
        sessions = OIDCSessions(oidc, supervisor.store, settings, session_secret)
        # Added first, so it runs inside SessionMiddleware and sees the session.
        app.add_middleware(KeepSessionsCurrent, sessions=sessions)
        app.add_middleware(
            SessionMiddleware,
            secret_key=session_secret,
            session_cookie="doctor_session",
            https_only=settings.public_url.startswith("https://"),
            same_site="lax",  # the provider redirects back with a GET
            max_age=settings.session_max_age_seconds,
        )

    def session_payload(request: Request) -> dict[str, Any]:
        if oidc is None:
            return {}
        try:
            return dict(request.session.get(SESSION_USER) or {})
        except Exception:  # no session middleware on this request
            return {}

    def session_user(request: Request) -> str:
        return str(session_payload(request).get("name") or "")

    failures: dict[str, list[float]] = {}

    def too_many_failures(client: str) -> bool:
        cutoff = time.monotonic() - PASSWORD_WINDOW_SECONDS
        recent = [at for at in failures.get(client, ()) if at > cutoff]
        if recent:
            failures[client] = recent
        else:
            failures.pop(client, None)
        return len(recent) >= PASSWORD_FAILURES

    def password_matches(user: str, password: str) -> bool:
        # Bytes, so any input compares rather than raising; both always
        # compared, so the time taken does not say which one was wrong.
        user_ok = secrets.compare_digest(user.encode(), settings.web_user.encode())
        password_ok = secrets.compare_digest(password.encode(), settings.web_password.encode())
        return user_ok and password_ok

    def identify(request: Request) -> str:
        """Who is asking. Refuses everything when no sign-in is configured:
        the API carries log lines and prompts, which are not for anyone who
        can reach the port."""
        if oidc is None and not settings.web_password:
            raise HTTPException(status_code=403, detail=NO_SIGN_IN)
        signed_in = session_user(request)
        if signed_in:
            return signed_in
        if oidc is not None and not settings.password_login_allowed:
            # OIDC is the only way in, so say so rather than prompting the
            # browser for a password that will never be accepted.
            raise HTTPException(
                status_code=401,
                detail="sign in with your identity provider at /auth/login",
            )
        if not settings.password_login_allowed:
            raise HTTPException(status_code=401, detail="authentication required")
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            import base64

            client = client_of(request.scope)[0]
            if too_many_failures(client):
                raise HTTPException(status_code=429,
                                    detail="too many wrong passwords; try again later")
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                user, _, password = decoded.partition(":")
            except Exception:
                user, password = "", ""
            if password_matches(user, password):
                return user
            failures.setdefault(client, []).append(time.monotonic())
        # No Basic challenge here: the browser answers one with its own
        # password dialog, and these are the page's background reads. A tab
        # left open past its session got that dialog from the stream and the
        # poll, on top of the page's own sign-in. Only /auth/password asks.
        raise HTTPException(status_code=401, detail="authentication required")

    def require_login(request: Request) -> str:
        """Endpoints that need a real, configured login rather than the
        read-only fall-through: mutations, and anything that returns live log
        content rather than what an incident already captured."""
        if settings.oidc_enabled:
            return identify(request)
        if not settings.web_password:
            raise HTTPException(
                status_code=403,
                detail="Set DOCTOR_WEB_PASSWORD to enable approvals, manual actions "
                       "and live log reads.",
            )
        return identify(request)

    def require_writer(request: Request) -> str:
        return require_login(request)

    def require_settings_writer(request: Request) -> str:
        """Saving a setting: a writer, while the page is editable. That the
        request came from this site's own page, `guard` has checked."""
        if not settings.settings_editable:
            raise HTTPException(status_code=403,
                                detail="DOCTOR_SETTINGS_EDITABLE is off: the page is read-only.")
        return require_writer(request)

    def refuse_cross_site(request: Request) -> None:
        """A write whose Origin is not this site's - scheme, host and port -
        is refused. "null" (a sandboxed frame, a file) is never this site.
        Without Origin, a browser's Sec-Fetch-Site still says cross-site;
        with neither, the client is not a browser and carries no one's
        cached credentials, so it is let through to authenticate."""
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            schemes = {request.url.scheme,
                       request.headers.get("x-forwarded-proto", "").split(",")[0].strip()}
            allowed = {f"{scheme}://{host}" for scheme in schemes if scheme}
            if settings.public_url:
                public = urlparse(settings.public_url)
                allowed.add(f"{public.scheme}://{public.netloc}")
            if origin.rstrip("/").lower() not in {a.lower() for a in allowed}:
                raise HTTPException(status_code=403, detail="cross-site request refused")
        elif request.headers.get("sec-fetch-site", "") in ("cross-site", "same-site"):
            raise HTTPException(status_code=403, detail="cross-site request refused")

    offline_csp = _offline_csp()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Every write from this site's own page only, and every response
        with headers that keep it out of frames and other sites' reach.

        Session cookies are SameSite=lax, but a browser sends Basic
        credentials it has cached with any request to the site, a cross-site
        form's included. A browser names the page a write came from in
        Origin, so one from elsewhere is refused; a client that sends no
        Origin (curl, a script) is not a browser carrying someone's cookies."""
        if request.method in UNSAFE_METHODS:
            try:
                refuse_cross_site(request)
            except HTTPException as refused:
                response: Response = JSONResponse({"detail": refused.detail},
                                                  status_code=refused.status_code)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path == "/static/offline.html":
            response.headers["Content-Security-Policy"] = offline_csp
        return response

    # -- sign in ------------------------------------------------------------

    if oidc is not None:
        from authlib.integrations.starlette_client import OAuthError
        from fastapi.responses import RedirectResponse

        from .oidc import oauth_http

        def _provider_unreachable(error: Exception):
            # Authlib fetches the discovery document on the first sign-in and
            # the token and keys on the callback; a provider that is down,
            # restarting or not routed yet surfaced as a bare 500 with nothing
            # to say which. Not cached: the next attempt asks again.
            detail = (f"HTTP {error.response.status_code}"
                      if isinstance(error, oauth_http.HTTPStatusError)
                      else error.__class__.__name__)
            log.warning("identity provider unreachable at %s (%s)",
                        settings.oidc_issuer, detail)
            return _auth_error(
                f"Cannot reach the identity provider at {settings.oidc_issuer} "
                f"({detail}). Check that it is up and reachable from the doctor, "
                "then try again.",
                status=502,
            )

        @app.get("/auth/login")
        async def login(request: Request):
            try:
                return await oidc.authorize_redirect(request, settings.oidc_redirect_url)
            except (oauth_http.HTTPError, json.JSONDecodeError) as error:
                return _provider_unreachable(error)

        @app.get("/auth/callback")
        async def callback(request: Request):
            try:
                user, token, claims = await oidc.complete(request)
            except PermissionError as denied:
                log.warning("sign-in refused: %s", denied)
                return _auth_error(str(denied), status=403)
            except OAuthError as failed:
                # Includes a mismatched state, a bad signature and an expired
                # code - all of which mean "start again", not "try harder".
                log.warning("sign-in failed: %s", failed)
                return _auth_error("Sign-in failed. Please try again.", status=400)
            except (oauth_http.HTTPError, json.JSONDecodeError) as error:
                return _provider_unreachable(error)
            user["sid"] = await sessions.start(user, token, claims, client_of(request.scope))
            request.session[SESSION_USER] = user
            log.info("%s signed in", user["name"])
            return RedirectResponse(url="/", status_code=303)

        @app.get("/auth/logout")
        async def logout(request: Request):
            name = session_user(request)
            await sessions.end(request.session)
            request.session.clear()
            if name:
                log.info("%s signed out", name)
            return RedirectResponse(url="/", status_code=303)

        # Whether the provider answers right now, for the dot on the sign-in
        # card. Open to anyone, like /api/me: it is asked before there is a
        # session. Cached briefly so a row of signed-out tabs polling it is
        # one request to the provider, not one each.
        provider_state: dict[str, Any] = {"at": 0.0, "answer": None}
        provider_lock = asyncio.Lock()

        @app.get("/api/auth/provider")
        async def provider_status() -> dict[str, Any]:
            async with provider_lock:
                if (provider_state["answer"] is not None
                        and time.monotonic() - provider_state["at"] < PROVIDER_STATUS_SECONDS):
                    return provider_state["answer"]
                online, detail = await _probe_provider(settings.oidc_discovery_url)
                answer = {"online": online, "detail": detail}
                provider_state.update(at=time.monotonic(), answer=answer)
                return answer

        # -- the profile page: who you are, and where you are signed in ----

        def own_session(request: Request) -> dict[str, Any]:
            """The caller's OIDC session; the password has no sessions to list."""
            if not session_user(request) or not session_payload(request).get("sid"):
                raise HTTPException(status_code=401,
                                    detail="sign in with your identity provider at /auth/login")
            return request.session

        def their_session(request: Request, public: str) -> str:
            session_id = sessions.find(own_session(request), public)
            if session_id is None:
                raise HTTPException(status_code=404, detail="no such session")
            return session_id

        @app.get("/api/profile")
        async def profile(request: Request) -> dict[str, Any]:
            return await asyncio.to_thread(sessions.profile, own_session(request))

        @app.post("/api/profile/sessions/{public}/end")
        async def end_session(public: str, request: Request) -> dict[str, Any]:
            """Sign one of your sessions out. It is gone from the store at once,
            so its next request - or its open stream, within a minute - finds
            nothing to stand on."""
            session_id = await asyncio.to_thread(their_session, request, public)
            current = session_id == session_payload(request).get("sid")
            await asyncio.to_thread(supervisor.store.delete_oidc_session, session_id)
            log.info("%s ended a session%s", session_user(request),
                     " (this one)" if current else "")
            if current:
                request.session.clear()
            return {"ended": True, "current": current}

        @app.post("/api/profile/sessions/end-others")
        async def end_other_sessions(request: Request) -> dict[str, Any]:
            own_session(request)
            current = session_payload(request).get("sid")
            others = [row["id"] for row in await asyncio.to_thread(
                supervisor.store.oidc_sessions_of, session_payload(request).get("sub") or "")
                if row["id"] != current]
            for session_id in others:
                await asyncio.to_thread(supervisor.store.delete_oidc_session, session_id)
            if others:
                log.info("%s ended %d other session(s)", session_user(request), len(others))
            return {"ended": len(others)}

        @app.put("/api/profile/sessions/{public}/name")
        async def name_session(public: str, body: SessionName, request: Request) -> dict[str, Any]:
            """Empty goes back to the name made from the device."""
            session_id = await asyncio.to_thread(their_session, request, public)
            await asyncio.to_thread(lambda: supervisor.store.describe_oidc_session(
                session_id, label=" ".join(body.name.split())))
            return {"ok": True}

        @app.post("/api/profile/device")
        async def report_device(body: DeviceReport, request: Request) -> dict[str, Any]:
            """What User-Agent Client Hints told the page about this device.
            Only ever a label: it names the session, it decides nothing."""
            own_session(request)
            session_id = session_payload(request)["sid"]
            await asyncio.to_thread(lambda: supervisor.store.describe_oidc_session(
                session_id, model=body.model.strip(), platform=body.platform.strip()))
            return {"ok": True}

    if settings.password_login_allowed and settings.web_password:
        from fastapi.responses import RedirectResponse as _Redirect

        @app.get("/auth/password")
        async def password_login(request: Request):
            """The sign-in button's target without OIDC: answers 401 with a
            Basic challenge until the browser has the password, then goes home.
            Nothing else challenges, so this is the one way to be asked for
            the password at all."""
            if not basic_user(request):
                raise HTTPException(
                    status_code=401,
                    detail="authentication required",
                    headers={"WWW-Authenticate": 'Basic realm="container-doctor"'},
                )
            return _Redirect(url="/", status_code=303)

    def basic_user(request: Request) -> str:
        """The password user, if this request carries the right password."""
        if not (settings.password_login_allowed and settings.web_password):
            return ""
        try:
            return identify(request)
        except HTTPException:
            return ""

    @app.get("/api/me")
    async def me(request: Request) -> dict[str, Any]:
        """Who the browser is, and how it may sign in. Never 401s: the page
        needs an answer in order to render a sign-in button."""
        try:
            name = session_user(request) or basic_user(request)
        except Exception:
            name = ""
        payload = session_payload(request)  # never raises: {} without a session
        # Whether the page should ask the browser what device it is: once per
        # session, where Client Hints can say.
        device_reported = True
        if sessions is not None and payload.get("sid") and name:
            row = await asyncio.to_thread(sessions.current_row, request.session)
            device_reported = bool(row and row["platform"])
        return {
            "user": name,
            "picture": payload.get("picture") or "",
            "groups": payload.get("groups") or [],
            "oidc": oidc is not None,
            "profile": bool(sessions is not None and payload.get("sid") and name),
            "device_reported": device_reported,
            "password_login": settings.password_login_allowed,
            "authenticated": bool(name),
            # Neither a password nor OIDC: the page says what to set instead
            # of offering a sign-in that cannot work.
            "configured": oidc is not None or bool(settings.web_password),
        }

    # -- reads --------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "docker": supervisor.docker_connected}

    @app.get("/api/status")
    async def status(_: str = Depends(identify)) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.status)

    background: set[asyncio.Task] = set()

    def _spawn(coro) -> None:
        task = asyncio.create_task(coro)
        background.add(task)  # held, or the loop may drop it half done
        task.add_done_callback(background.discard)

    async def _check_github() -> None:
        before = getattr(supervisor, "_github", {}).get("checked_at")
        try:
            await supervisor._refresh_deploy_stacks()
        except Exception:
            log.exception("checking GitHub for the settings page failed")
            return
        if getattr(supervisor, "_github", {}).get("checked_at") != before:
            supervisor.broadcast("settings", {"github": True})

    @app.get("/api/settings")
    async def configuration(_: str = Depends(identify)) -> dict[str, Any]:
        """The effective configuration, grouped. Secrets are set/not set only.

        Each row says which stack variables set it, its default, and whether
        it differs - so the page can tell a setting from a fixed value.
        """
        sections = await asyncio.to_thread(supervisor.settings.overview)
        # At most once an hour, so opening Settings costs GitHub little - and
        # in the background: it is two round trips to GitHub that the page
        # used to wait out. Open pages hear about the answer on the stream.
        _spawn(_check_github())
        live = await asyncio.to_thread(supervisor.github_status)
        for section in sections:
            if section.get("id") == "github":
                section["items"] = [*section["items"], *live]
        saved = await asyncio.to_thread(supervisor.store.saved_settings)
        # What a restart would run with, where it differs from what runs now.
        upcoming = {(section["title"], label): str(value)
                    for section in resolve(saved).overview()
                    for label, value, _ in section["items"]}
        pending = {(section["title"], label): upcoming[(section["title"], label)]
                   for section in sections for label, value, _ in section["items"]
                   if (section["title"], label) in upcoming
                   and upcoming[(section["title"], label)] != str(value)}
        return {"sections": describe_rows(sections, passed, defaults, saved, pending,
                                          can_edit=settings.settings_editable),
                "compose_read": passed is not None,
                "editable": settings.settings_editable,
                "pending": bool(pending),
                # Names the stack environment in the page's labels; may be empty.
                "manager": supervisor.settings.docker_manager,
                # Who changed what from this page. Never a secret: none is saved here.
                "changes": await asyncio.to_thread(supervisor.store.setting_changes, 50)}

    # One save or import at a time: each checks against what is saved, then
    # writes, then applies - two at once could each pass on their own and
    # together leave a combination nobody checked.
    settings_lock = asyncio.Lock()

    def apply_saved() -> dict[str, list[str]]:
        """Run with what is saved now, as far as that needs no restart. On
        the event loop, where the poll and the rest of the doctor run, so
        none of them sees half a change."""
        saved = supervisor.store.saved_settings()
        try:
            return supervisor.apply_settings(resolve(saved))
        except Exception:
            log.exception("saved settings could not be applied while running")
            return {"applied": [], "restart": ["(all - applying failed, see the log)"]}

    def env_names(fields: list[str]) -> list[str]:
        from ..config import FIELD_SPECS
        return [FIELD_SPECS[f].env if f in FIELD_SPECS else f for f in fields]

    @app.put("/api/settings/{env}")
    async def save_setting(env: str, payload: SettingValue,
                           user: str = Depends(require_settings_writer)) -> dict[str, Any]:
        """Save one setting, and apply it unless it waits for a restart."""
        async with settings_lock:
            saved = await asyncio.to_thread(supervisor.store.saved_settings)
            value = payload.value.strip()
            problem = await asyncio.to_thread(check_value, env, value, saved)
            if problem:
                raise HTTPException(status_code=422, detail=problem)
            changed = await asyncio.to_thread(supervisor.store.save_setting, env, value,
                                              by=user or "dashboard")
            if changed:
                log.info("%s %s %s from the Settings page", user or "dashboard",
                         "reset" if not value else "set", env)
            result = apply_saved()
        return {"ok": True, "changed": changed, "applied": env_names(result["applied"]),
                "restart": env_names(result["restart"])}

    @app.get("/api/settings/export")
    async def export_settings(user: str = Depends(require_writer)) -> Response:
        """What the page saved, as a file to keep or import elsewhere. Only
        saved values: a default or a variable the stack sets is not the page's,
        and no secret is ever saved here."""
        saved = storable(await asyncio.to_thread(supervisor.store.saved_settings))
        from datetime import datetime, timezone

        body = {"format": EXPORT_FORMAT, "version": EXPORT_VERSION,
                "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "build": supervisor.settings.build,
                "release": supervisor.settings.version,
                "settings": dict(sorted(saved.items()))}
        return Response(json.dumps(body, indent=2) + "\n", media_type="application/json",
                        headers={"Content-Disposition":
                                 'attachment; filename="container-doctor-settings.json"'})

    @app.post("/api/settings/import")
    async def import_settings(payload: SettingsImport,
                              user: str = Depends(require_settings_writer)) -> dict[str, Any]:
        """Save a file's settings, checked as a whole, in one transaction.

        A variable the page cannot save - unknown to this version, a secret,
        or one the stack environment sets - is skipped and listed. An unreadable value,
        or a configuration problem the file's values add together, refuses
        the whole file. dry_run reports the changes and saves nothing."""
        if payload.format != EXPORT_FORMAT:
            raise HTTPException(status_code=422, detail="not a container-doctor settings file")
        if payload.version != EXPORT_VERSION:
            raise HTTPException(status_code=422,
                                detail=f"settings file version {payload.version} is not "
                                       f"{EXPORT_VERSION}, which this build reads")
        async with settings_lock:
            saved = await asyncio.to_thread(supervisor.store.saved_settings)
            values = {env: ("on" if value else "off") if isinstance(value, bool) else str(value)
                      for env, value in payload.settings.items()}
            to_save, skipped, errors = await asyncio.to_thread(
                check_values, values, saved, replace=payload.replace)
            if errors:
                raise HTTPException(status_code=422, detail=" ".join(errors))
            current_saved = storable(saved)
            changes = [{"env": env, "old_value": current_saved.get(env, ""), "new_value": value}
                       for env, value in sorted(to_save.items())
                       if current_saved.get(env, "") != value]
            report = {"ok": True, "changes": changes,
                      "skipped": [{"env": env, "reason": why} for env, why in sorted(skipped.items())]}
            if payload.dry_run:
                return {**report, "dry_run": True}
            changed = await asyncio.to_thread(supervisor.store.save_settings,
                                              {c["env"]: c["new_value"] for c in changes},
                                              by=user or "dashboard")
            log.info("%s imported settings: %d changed, %d skipped", user or "dashboard",
                     len(changed), len(skipped))
            result = apply_saved()
        return {**report, "applied": env_names(result["applied"]),
                "restart": env_names(result["restart"])}

    @app.post("/api/settings/restart")
    async def restart_to_apply(user: str = Depends(require_settings_writer)) -> dict[str, Any]:
        """Restart the doctor so saved settings take effect. Answered first,
        then stopped, so the page hears that it worked."""
        log.warning("%s restarted the doctor to apply saved settings", user or "dashboard")
        asyncio.get_running_loop().call_later(0.5, restart)
        return {"ok": True}

    @app.get("/api/state")
    async def state(_: str = Depends(identify)) -> dict[str, Any]:
        return supervisor.snapshot()

    @app.get("/api/host/history")
    async def host_history(hours: int = 24, _: str = Depends(identify)) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.host_history, max(1, min(hours, 24 * 30)))

    @app.get("/api/networks")
    async def network_map(_: str = Depends(identify)) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.network_report)

    @app.get("/api/incidents")
    async def incidents(
        limit: int = 50, offset: int = 0, status: str | None = None,
        _: str = Depends(identify),
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        rows = await asyncio.to_thread(
            supervisor.store.list_incidents, limit=limit, status=status, offset=offset
        )
        # One query for every row's actions rather than one per row: at a few
        # hundred incidents that was a few hundred round trips per refresh.
        grouped = await asyncio.to_thread(
            supervisor.store.actions_for_incidents, [row["id"] for row in rows]
        )
        for row in rows:
            row["actions"] = grouped.get(row["id"], [])
        total = await asyncio.to_thread(supervisor.store.count_incidents, status)
        return {"incidents": rows, "total": total, "offset": offset, "limit": limit}

    @app.get("/api/incidents/{incident_id}")
    async def incident(incident_id: str, _: str = Depends(identify)) -> dict[str, Any]:
        row = await asyncio.to_thread(supervisor.store.get_incident, incident_id)
        if not row:
            raise HTTPException(status_code=404, detail="unknown incident")
        row["actions"] = await asyncio.to_thread(
            supervisor.store.list_actions, incident_id=incident_id
        )
        return row

    @app.get("/api/incidents/{incident_id}/occurrences")
    async def incident_occurrences(
        incident_id: str, limit: int = 50, offset: int = 0, _: str = Depends(identify),
    ) -> dict[str, Any]:
        """Every sighting folded into this incident: when, and the lines it
        fired on. The same redacted evidence /api/incidents/{id} serves, so
        the same check; fetched on demand, since the list view never needs it."""
        row = await asyncio.to_thread(supervisor.store.get_incident, incident_id)
        if not row:
            raise HTTPException(status_code=404, detail="unknown incident")
        result = await asyncio.to_thread(
            supervisor.store.list_occurrences, incident_id,
            limit=max(1, min(limit, 200)), offset=max(0, offset),
        )
        return {**result, "total": row.get("occurrences") or 1, "offset": max(0, offset)}

    @app.get("/api/incidents/{incident_id}/handoff")
    async def incident_handoff(
        incident_id: str, download: bool = False, _: str = Depends(identify)
    ) -> Response:
        """The incident as Markdown for an LLM agent the operator runs.

        The same data /api/incidents/{id} already returns, so the same check:
        it is what the doctor captured and redacted, not a live read.
        """
        text = await asyncio.to_thread(supervisor.incident_handoff, incident_id)
        if text is None:
            raise HTTPException(status_code=404, detail="unknown incident")
        headers = {"Cache-Control": "no-store"}
        if download:
            safe = "".join(ch for ch in incident_id if ch.isalnum())[:32] or "incident"
            headers["Content-Disposition"] = f'attachment; filename="incident-{safe}.md"'
        return Response(content=text, media_type="text/markdown; charset=utf-8", headers=headers)

    @app.get("/api/briefing")
    async def get_briefing(_: str = Depends(identify)) -> dict[str, Any]:
        """The summary as cached, facts included. Starts rewriting whatever is
        stale in the background and returns at once - the page is told over
        the stream when a section lands."""
        await supervisor.briefing.ensure()
        return await asyncio.to_thread(supervisor.briefing.view)

    @app.post("/api/briefing/refresh")
    async def refresh_briefing(_: str = Depends(require_login)) -> dict[str, Any]:
        """Rewrite every section now. Behind a login: each press costs calls."""
        started = await supervisor.briefing.ensure(force=True)
        view = await asyncio.to_thread(supervisor.briefing.view)
        return {**view, "started": started}

    @app.post("/api/images/refresh")
    async def refresh_images(_: str = Depends(require_login)) -> dict[str, Any]:
        """Ask the registries again now, rather than at the next scheduled check."""
        return supervisor.refresh_images()

    @app.get("/api/images/notes")
    async def image_notes(image: str, tag: str, _: str = Depends(identify)) -> dict[str, Any]:
        """Release notes between an image's pin and a newer tag, read on request.

        Public GitHub content, so the same check as the rest of the page."""
        result = await supervisor.image_release_notes(image[:300], tag[:128])
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "unavailable"))
        return result

    @app.post("/api/poll")
    async def poll_now(_: str = Depends(require_login)) -> dict[str, Any]:
        """Read Docker again now, rather than at the next scheduled poll."""
        return supervisor.request_poll()

    @app.get("/api/actions")
    async def actions(
        limit: int = 50, offset: int = 0, _: str = Depends(identify)
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        rows = await asyncio.to_thread(
            supervisor.store.list_actions, limit=limit, offset=offset
        )
        total = await asyncio.to_thread(supervisor.store.count_actions)
        return {"actions": rows, "total": total, "offset": offset, "limit": limit}

    @app.get("/api/usage")
    async def usage(_: str = Depends(identify)) -> dict[str, Any]:
        return {"history": await asyncio.to_thread(supervisor.store.usage_history, 14)}

    @app.get("/api/llm/spend")
    async def llm_spend(days: int = 30, _: str = Depends(identify)) -> dict[str, Any]:
        """Daily spend for the chart, split by what it paid for."""
        days = max(1, min(days, max(supervisor.settings.retention_days, 7)))
        return await asyncio.to_thread(supervisor.llm_spend, days)

    @app.get("/api/llm/calls")
    async def llm_calls(
        limit: int = 20, offset: int = 0, purpose: str | None = None,
        _: str = Depends(identify),
    ) -> dict[str, Any]:
        """The call log, newest first, without the request and answer bodies."""
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        rows = await asyncio.to_thread(
            supervisor.store.list_llm_calls, limit=limit, offset=offset, purpose=purpose)
        total = await asyncio.to_thread(supervisor.store.count_llm_calls, purpose)
        return {"calls": rows, "total": total, "offset": offset, "limit": limit,
                "log_days": supervisor.settings.llm_log_days}

    @app.get("/api/classifier")
    async def classifier(limit: int = 30, _: str = Depends(identify)) -> dict[str, Any]:
        """The classifier's mode, meter and recent verdicts."""
        return await asyncio.to_thread(supervisor.classifier_status, max(1, min(limit, 200)))

    @app.get("/api/llm/calls/{call_id}")
    async def llm_call(call_id: str, _: str = Depends(identify)) -> dict[str, Any]:
        """One call as it went out: prompts, schema, parameters - and the answer.

        Behind the same check as the incidents: it is their evidence, already
        redacted, and nothing a reader of the incident could not see.
        """
        row = await asyncio.to_thread(supervisor.store.get_llm_call, call_id)
        if not row:
            raise HTTPException(status_code=404, detail="unknown call")
        return row

    @app.get("/api/changes")
    async def container_changes(
        limit: int = 30, offset: int = 0, container: str | None = None,
        _: str = Depends(identify),
    ) -> dict[str, Any]:
        """What changed about the containers: recreates, new builds, restarts."""
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        rows = await asyncio.to_thread(
            supervisor.store.list_changes, container_name=container, limit=limit, offset=offset)
        total = await asyncio.to_thread(supervisor.store.count_changes)
        return {"changes": rows, "total": total, "offset": offset, "limit": limit}

    @app.get("/api/memory")
    async def memory_limits(_: str = Depends(identify)) -> dict[str, Any]:
        """A memory-limit verdict per watched container, most urgent first."""
        return {"window_days": 14,
                "containers": await asyncio.to_thread(supervisor.memory_report)}

    @app.get("/api/containers/{container_name}")
    async def container_detail(container_name: str, _: str = Depends(identify)) -> dict[str, Any]:
        detail = await asyncio.to_thread(supervisor.container_detail, container_name)
        if detail is None:
            raise HTTPException(status_code=404, detail="unknown container")
        return detail

    @app.get("/api/drive/temperature")
    async def drive_temperature(drive: str, hours: int = 24,
                                _: str = Depends(identify)) -> dict[str, Any]:
        hours = max(1, min(hours, 24 * supervisor.settings.retention_days))
        return await asyncio.to_thread(supervisor.drive_temp_history, drive[:128], hours)

    @app.get("/api/drive/sleep")
    async def drive_sleep(drive: str, hours: int = 24,
                          _: str = Depends(identify)) -> dict[str, Any]:
        hours = max(1, min(hours, 24 * supervisor.settings.retention_days))
        return await asyncio.to_thread(supervisor.drive_sleep_history, drive[:128], hours)

    @app.get("/api/disk/history")
    async def disk_history(days: int = 14, _: str = Depends(identify)) -> dict[str, Any]:
        days = max(1, min(days, supervisor.settings.retention_days))
        return {"days": days,
                "disks": await asyncio.to_thread(supervisor.disk_history, days)}

    @app.get("/api/containers/{container_name}/logs")
    async def container_logs(
        container_name: str,
        lines: int = 200,
        _: str = Depends(require_login),
    ) -> dict[str, Any]:
        """A tail fetched now, rather than the one frozen at detection.

        Not a log browser - see guides/investigating in the docs. Redaction applies as everywhere
        else, so a secret in a log line does not reach the page.
        """
        result = await supervisor.container_logs(container_name, lines=max(1, min(lines, 2000)))
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "unavailable"))
        return result

    @app.get("/api/incidents/{incident_id}/context")
    async def incident_context(
        incident_id: str,
        minutes: int = 5,
        _: str = Depends(require_login),
    ) -> dict[str, Any]:
        """The window around a detection, including what else was failing."""
        result = await supervisor.incident_context(incident_id, minutes=max(1, min(minutes, 60)))
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "unavailable"))
        return result

    @app.get("/api/stream")
    async def stream(request: Request, _: str = Depends(identify)) -> StreamingResponse:
        queue = supervisor.subscribe()
        # The middleware checks the session once, as the stream opens; an open
        # stream outlives the access token, so it asks again as it goes.
        signed_in = dict(request.session) if sessions is not None and session_user(request) else None

        async def generator():
            checked = time.monotonic()
            try:
                yield _sse({"type": "state", "payload": supervisor.snapshot()})
                while True:
                    if await request.is_disconnected():
                        break
                    if signed_in is not None and time.monotonic() - checked > 60:
                        checked = time.monotonic()
                        if not await sessions.check(signed_in):
                            break
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(message)
            finally:
                supervisor.unsubscribe(queue)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- writes -------------------------------------------------------------

    @app.post("/api/actions/{action_id}/approve")
    async def approve(action_id: str, user: str = Depends(require_writer)) -> dict[str, Any]:
        result = await supervisor.approve_action(action_id, actor=user)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error", "cannot approve"))
        return result

    @app.post("/api/actions/{action_id}/reject")
    async def reject(action_id: str, user: str = Depends(require_writer)) -> dict[str, Any]:
        result = await supervisor.reject_action(action_id, actor=user)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error", "cannot reject"))
        return result

    @app.post("/api/incidents/{incident_id}/dismiss")
    async def dismiss(incident_id: str, user: str = Depends(require_writer)) -> dict[str, Any]:
        result = await supervisor.dismiss_incident(incident_id, actor=user)
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "cannot dismiss"))
        return result

    # Registered before the /{incident_id}/... routes so "bulk-dismiss" is not
    # read as an incident id.
    @app.post("/api/incidents/bulk-dismiss")
    async def bulk_dismiss(
        kind: str | None = None,
        container: str | None = None,
        user: str = Depends(require_writer),
    ) -> dict[str, Any]:
        count = await asyncio.to_thread(
            supervisor.store.dismiss_all, kind=kind, container_name=container
        )
        log.info("%s dismissed %d incidents in bulk", user, count)
        supervisor.broadcast("incident", {"bulk_dismissed": count})
        return {"ok": True, "dismissed": count}

    @app.post("/api/incidents/{incident_id}/diagnose")
    async def rediagnose(
        incident_id: str,
        payload: RediagnoseRequest | None = None,
        user: str = Depends(require_writer),
    ) -> dict[str, Any]:
        # No body is the old call and still works: re-diagnose without a note.
        result = await supervisor.rediagnose(
            incident_id, context=payload.context if payload else "", actor=user
        )
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error", "cannot diagnose"))
        return result

    @app.post("/api/manual-action")
    async def manual_action(
        payload: ManualActionRequest, user: str = Depends(require_writer)
    ) -> dict[str, Any]:
        result = await supervisor.manual_action(payload.container, payload.action, actor=user)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "cannot run action"))
        return result

    @app.post("/api/redeploy")
    async def redeploy(
        payload: RedeployRequest, user: str = Depends(require_writer)
    ) -> dict[str, Any]:
        """Redeploy the container's stack through its redeploy webhook."""
        result = await supervisor.redeploy(payload.container, actor=user or "dashboard")
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error", "cannot redeploy"))
        return result

    # -- static -------------------------------------------------------------

    @app.get("/")
    async def index() -> Response:
        """Served without authentication on purpose.

        This page renders the sign-in button, so gating it behind `identify`
        locked the browser out of the only route back in: with OIDC on and no
        session, `/` answered with the 401 JSON instead of the app. The page
        carries no data of its own - every endpoint it calls is still checked.
        """
        return HTMLResponse(_index_html(), headers=REVALIDATE)

    @app.get("/sw.js")
    async def service_worker() -> Response:
        """The offline fallback's worker, served from the root for its scope.

        Unauthenticated like the page itself: it carries no data, and the
        browser fetches it without asking first. It is stamped with the
        offline page's hash, so changing that page installs a new worker.
        """
        digest = hashlib.sha256((STATIC_DIR / "offline.html").read_bytes()).hexdigest()[:12]
        script = (STATIC_DIR / "sw.js").read_text(encoding="utf-8").replace("__VERSION__", digest)
        return Response(script, media_type="text/javascript", headers=REVALIDATE)

    app.mount("/static", _RevalidatedStatic(directory=STATIC_DIR), name="static")
    return app


def _sse(message: dict[str, Any]) -> str:
    return f"data: {json.dumps(message, default=str)}\n\n"
