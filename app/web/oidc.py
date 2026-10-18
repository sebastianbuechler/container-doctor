"""OIDC login, against any provider with discovery and PKCE (Pocket ID,
Authentik, Keycloak, ...).

The doctor is its own OIDC client rather than trusting a header set by the
reverse proxy. A reverse proxy usually shares a network with every other
published service, so a header is only as trustworthy as every container on
that network - and this one can restart containers.

What the provider is asked to prove, and what is checked here:

  signature   against the provider's JWKS, fetched from its discovery document
  iss/aud     that the token came from this provider, for this client
  exp         that it has not expired
  nonce       that it answers *this* login, not a replayed one
  state       that the callback answers a request this browser started

Authlib does the first four as part of `authorize_access_token`; the state is
carried in the session cookie it signs.

Signing in is not the end of it. The provider's refresh token is kept here,
encrypted, and used whenever the access token runs out: a provider that
refuses - the account disabled, its access revoked, the group taken away -
ends the session on the doctor too. So the provider's token lifetimes, not a
cookie, decide how long a sign-in lasts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from authlib.integrations.starlette_client import OAuth, OAuthError
from cryptography.fernet import Fernet, InvalidToken

from ..config import Settings
from . import devices

# Whichever HTTP library Authlib settles on: httpx2 when installed, httpx
# otherwise - the same choice Authlib makes. Catching plain httpx misses the
# former's errors.
try:
    import httpx2 as oauth_http
except ImportError:  # an Authlib from before httpx2
    import httpx as oauth_http

log = logging.getLogger(__name__)

SESSION_USER = "doctor_user"

# The provider did not answer at all, as opposed to answering "no".
# Not OSError: PermissionError, the refusal for a group, is one.
UNREACHABLE = (oauth_http.HTTPError, json.JSONDecodeError)

# Refresh this long before the access token runs out, not after.
REFRESH_LEEWAY_SECONDS = 30
# While the provider is unreachable the session carries on - it is the same
# provider you would need to sign in again - and asks again after this long.
REFRESH_RETRY_SECONDS = 60
# For a token that does not say when it expires.
DEFAULT_TOKEN_SECONDS = 15 * 60
# How stale "last active" on the profile page may be: a write every request
# would be one per poll, per tab.
TOUCH_SECONDS = 5 * 60
# Claims that describe the token rather than the person; left off the profile.
_TOKEN_CLAIMS = {"iss", "aud", "azp", "exp", "iat", "nbf", "jti", "nonce",
                 "at_hash", "c_hash", "sid", "auth_time", "amr", "acr"}


class OIDCClient:
    """Wraps the provider registration and the claims-to-identity mapping."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._oauth = OAuth()
        self._oauth.register(
            name="provider",
            server_metadata_url=settings.oidc_discovery_url,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret or None,
            client_kwargs={
                "scope": settings.oidc_scopes,
                # PKCE on top of the client secret. Pocket ID supports public
                # clients too, where it is the only thing binding the code to
                # this login.
                "code_challenge_method": "S256",
            },
        )

    @property
    def provider(self) -> Any:
        return self._oauth.provider

    async def authorize_redirect(self, request: Any, redirect_uri: str) -> Any:
        # A refresh token is what lets the provider end a session later, and
        # some providers issue one only for offline_access.
        metadata = await self.provider.load_server_metadata()
        scopes = self.settings.oidc_scopes.split()
        if ("offline_access" in metadata.get("scopes_supported", ())
                and "offline_access" not in scopes):
            scopes.append("offline_access")
        return await self.provider.authorize_redirect(
            request, redirect_uri, scope=" ".join(scopes))

    async def complete(self, request: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Finish the callback. Returns the session payload, the token it came
        with and the identity claims, or raises."""
        token = await self.provider.authorize_access_token(request)
        claims = dict(token.get("userinfo") or {})
        if not claims:
            raise OAuthError(description="provider returned no identity claims")

        subject = claims.get("sub")
        if not subject:
            raise OAuthError(description="identity token has no subject")

        claims.update(await self._from_userinfo_endpoint(token, subject))

        groups = self._permitted_groups(claims)
        return {
            "sub": subject,
            "name": _display_name(claims),
            "groups": groups,
            # An absolute URL the browser fetches from the provider directly;
            # it falls back to initials when that fails.
            "picture": str(claims.get("picture") or ""),
        }, token, identity_claims(claims)

    async def refresh(self, refresh_token: str, subject: str
                      ) -> tuple[dict[str, Any], list[str] | None, dict[str, Any]]:
        """Trade the refresh token for new tokens, and check the identity they
        describe is still the same person, still permitted. Returns the token,
        the groups (None when the provider said nothing about them) and the
        identity claims ({} when there were none).

        Raises OAuthError when the provider refuses, PermissionError when the
        groups no longer allow it, and one of UNREACHABLE when it never answered.
        """
        token = await self.provider.fetch_access_token(
            grant_type="refresh_token", refresh_token=refresh_token)
        if "id_token" in token:
            # Same checks as at sign-in: signature, issuer, audience, expiry.
            # A refreshed ID token answers no login of its own, so no nonce.
            claims = dict(await self.provider.parse_id_token(token, nonce=None))
        else:
            claims = await self._from_userinfo_endpoint(token, subject)
        if claims.get("sub") and str(claims["sub"]) != subject:
            raise OAuthError(description="refreshed tokens describe a different subject")
        if self.settings.oidc_groups_claim not in claims:
            return token, None, identity_claims(claims)
        return token, self._permitted_groups(claims), identity_claims(claims)

    def _permitted_groups(self, claims: dict[str, Any]) -> list[str]:
        groups = _as_list(claims.get(self.settings.oidc_groups_claim))
        allowed = self.settings.oidc_allowed_groups
        if allowed and not set(allowed) & set(groups):
            # Authenticated, but not permitted. Distinct from a failed login,
            # and worth saying so rather than looping them back to the provider.
            raise PermissionError(
                f"{_display_name(claims)} is not in {', '.join(allowed)}"
            )
        return groups

    async def _from_userinfo_endpoint(self, token: Any, subject: str) -> dict[str, Any]:
        """Claims the ID token did not carry.

        `authorize_access_token` fills `userinfo` from the ID token, and a
        provider is free to keep profile fields out of it - Pocket ID returns
        `picture` only from the userinfo endpoint, so reading the ID token
        alone left every avatar blank.

        Never fatal: an identity good enough to sign in has already been
        established, and losing an avatar is not worth losing the session.
        """
        # The access token alone: handed the refresh token and an expiry
        # within its leeway, Authlib refreshes on its own first - spending a
        # single-use refresh token and dropping the one it got back.
        bare = {"access_token": token["access_token"],
                "token_type": token.get("token_type", "Bearer")}
        try:
            extra = dict(await self.provider.userinfo(token=bare) or {})
        except Exception as error:  # no endpoint, or it refused
            log.info("userinfo endpoint unavailable (%s)", error.__class__.__name__)
            return {}

        # The spec requires these to agree; a mismatch means the response
        # describes somebody else, so none of it can be trusted.
        if extra.get("sub") and str(extra["sub"]) != str(subject):
            log.warning("userinfo subject does not match the ID token - ignoring it")
            return {}
        return extra


def _as_list(value: Any) -> list[str]:
    """Groups arrive as a list, or as a space/comma-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part]
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value]
    return []


def _display_name(claims: dict[str, Any]) -> str:
    """Whatever the provider gives, in the order that reads best in an audit log."""
    for key in ("preferred_username", "email", "name", "sub"):
        value = claims.get(key)
        if value:
            return str(value)
    return "unknown"


class OIDCSessions:
    """The server side of a sign-in: one row per session in the store, holding
    the refresh token. The cookie names the row and nothing more.

    Pocket ID rotates refresh tokens and treats a reused one as stolen, so two
    requests must never refresh at once; the lock sees to that. A home lab
    has a handful of sessions at most, so one lock for all of them is plenty.
    """

    def __init__(self, client: OIDCClient, store: Any, settings: Settings, secret: str) -> None:
        self.client = client
        self.store = store
        self.settings = settings
        # Keyed off the cookie's secret: without a fixed one, a restart makes
        # the stored tokens unreadable - and the cookies naming them invalid.
        digest = hashlib.sha256(b"container-doctor refresh tokens\0" + secret.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))
        self._lock = asyncio.Lock()

    async def start(self, user: dict[str, Any], token: dict[str, Any],
                    claims: dict[str, Any] | None = None,
                    client: tuple[str, str] = ("", "")) -> str:
        """Record a fresh sign-in; returns the id the cookie carries.
        `client` is the browser's address and User-Agent."""
        session_id = secrets.token_urlsafe(24)
        ip, user_agent = client
        await asyncio.to_thread(
            lambda: self.store.save_oidc_session(
                session_id, user["sub"], self._seal(token.get("refresh_token") or ""),
                _expires_at(token), ip=ip, user_agent=user_agent, claims=claims))
        await asyncio.to_thread(
            self.store.prune_oidc_sessions, time.time() - self.settings.session_max_age_seconds)
        if not token.get("refresh_token"):
            log.info("the provider issued no refresh token: %s will have to sign in "
                     "again when the access token expires", user["name"])
        return session_id

    async def check(self, session: dict[str, Any], client: tuple[str, str] | None = None) -> bool:
        """Is this signed-in session still good? Refreshes the tokens when they
        are due. False means the session is over and should be cleared.
        `client`, the browser's address and User-Agent, marks it as used."""
        payload = session.get(SESSION_USER) or {}
        session_id = payload.get("sid")
        if not session_id:
            return False  # signed in before sessions were kept: once more, please
        row = await asyncio.to_thread(self.store.oidc_session, session_id)
        if row is None:
            return False
        if client is not None and time.time() - row["last_seen_at"] > TOUCH_SECONDS:
            await asyncio.to_thread(self.store.touch_oidc_session, session_id, *client)
        if _current(row):
            return True
        async with self._lock:
            # Another request may have refreshed it while this one waited.
            row = await asyncio.to_thread(self.store.oidc_session, session_id)
            if row is None:
                return False
            if _current(row):
                return True
            groups = await self._refresh(row, payload.get("name") or row["subject"])
        if groups is False:
            return False
        if groups is not None:
            session[SESSION_USER] = {**payload, "groups": groups}
        return True

    async def end(self, session: dict[str, Any]) -> None:
        session_id = (session.get(SESSION_USER) or {}).get("sid")
        if session_id:
            await asyncio.to_thread(self.store.delete_oidc_session, session_id)

    async def _refresh(self, row: dict[str, Any], name: str) -> list[str] | None | bool:
        """New groups, None for no news, or False for an ended session."""
        session_id = row["id"]
        refresh_token = self._open(row["refresh_token"])
        if not refresh_token:
            return await self._drop(session_id, name, "the access token expired "
                                    "and there is no refresh token to renew it")
        try:
            token, groups, claims = await self.client.refresh(refresh_token, row["subject"])
        except (OAuthError, PermissionError) as refused:
            return await self._drop(session_id, name, f"the provider refused it ({refused})")
        except UNREACHABLE as error:
            log.warning("cannot reach the identity provider to renew %s's session (%s); "
                        "keeping it and trying again in %ds",
                        name, error.__class__.__name__, REFRESH_RETRY_SECONDS)
            await asyncio.to_thread(self.store.defer_oidc_session, session_id,
                                    time.time() + REFRESH_RETRY_SECONDS)
            return None
        except Exception as error:  # a token that fails validation, among others
            log.exception("renewing %s's session failed", name)
            return await self._drop(session_id, name, error.__class__.__name__)
        await asyncio.to_thread(
            self.store.renew_oidc_session, session_id,
            # A provider that does not rotate keeps the one it has.
            self._seal(token.get("refresh_token") or refresh_token), _expires_at(token), claims)
        return groups

    # -- the profile page ---------------------------------------------------

    def sessions_of(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        """The signed-in person's sessions, as the profile page lists them.
        Each carries a public id: the real one is what the cookie holds."""
        payload = session.get(SESSION_USER) or {}
        current = payload.get("sid") or ""
        rows = self.store.oidc_sessions_of(payload.get("sub") or "")
        listed = []
        for row in rows:
            claims = _loads(row["claims_json"])
            listed.append({
                "id": public_id(row["id"]),
                "current": row["id"] == current,
                **devices.describe(user_agent=row["user_agent"], model=row["model"],
                                   hinted_platform=row["platform"],
                                   owner=devices.first_name(claims), label=row["label"]),
                "label": row["label"],
                "ip": row["ip"],
                "signed_in_at": row["created_at"],
                "last_seen_at": _iso(row["last_seen_at"]),
                "renewed_at": _iso(row["renewed_at"]),
                "access_expires_at": _iso(row["expires_at"]),
                "renewable": bool(row["refresh_token"]),
            })
        return listed

    def profile(self, session: dict[str, Any]) -> dict[str, Any]:
        """Everything the profile page shows: who, by whose word, and where."""
        payload = session.get(SESSION_USER) or {}
        row = self.current_row(session) or {}
        settings = self.settings
        scopes = settings.oidc_scopes.split()
        if "offline_access" not in scopes:
            scopes.append("offline_access")  # asked for when the provider lists it
        return {
            "user": {
                "sub": payload.get("sub") or "",
                "name": payload.get("name") or "",
                "picture": payload.get("picture") or "",
                "groups": payload.get("groups") or [],
            },
            "claims": _loads(row.get("claims_json", "{}")),
            "provider": {
                "issuer": settings.oidc_issuer,
                "client_id": settings.oidc_client_id,
                "scopes": scopes,
                "groups_claim": settings.oidc_groups_claim,
                "allowed_groups": list(settings.oidc_allowed_groups),
                "idle_hours": settings.session_max_age_seconds // 3600,
            },
            "sessions": self.sessions_of(session),
        }

    def current_row(self, session: dict[str, Any]) -> dict[str, Any] | None:
        session_id = (session.get(SESSION_USER) or {}).get("sid")
        return self.store.oidc_session(session_id) if session_id else None

    def find(self, session: dict[str, Any], public: str) -> str | None:
        """The real id behind a public one, if it is one of this person's."""
        sub = (session.get(SESSION_USER) or {}).get("sub") or ""
        for row in self.store.oidc_sessions_of(sub):
            if secrets.compare_digest(public_id(row["id"]), public):
                return row["id"]
        return None

    async def _drop(self, session_id: str, name: str, why: str) -> bool:
        log.warning("signed %s out: %s", name, why)
        await asyncio.to_thread(self.store.delete_oidc_session, session_id)
        return False

    def _seal(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode() if value else ""

    def _open(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken:  # sealed under another secret
            return ""


class KeepSessionsCurrent:
    """ASGI middleware, inside SessionMiddleware: checks every signed-in
    request's session against the provider's say, and clears the ones it has
    ended. Plain ASGI rather than BaseHTTPMiddleware, which buffers the stream."""

    def __init__(self, app: Any, sessions: OIDCSessions) -> None:
        self.app = app
        self.sessions = sessions

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            session = scope.get("session")
            if (session and session.get(SESSION_USER)
                    and not await self.sessions.check(session, client_of(scope))):
                session.clear()
        await self.app(scope, receive, send)


def identity_claims(claims: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in claims.items() if key not in _TOKEN_CLAIMS}


def public_id(session_id: str) -> str:
    """Names a session on the page without handing out what the cookie holds."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def client_of(scope: Any) -> tuple[str, str]:
    """The browser's address and User-Agent, from an ASGI scope.

    Traefik sets X-Real-Ip to the address it saw; X-Forwarded-For's first
    entry is whatever the client claimed. Only ever shown, never trusted."""
    headers = {key.decode("latin-1").lower(): value.decode("latin-1")
               for key, value in scope.get("headers") or ()}
    ip = headers.get("x-real-ip") or (scope.get("client") or ("",))[0] or ""
    return ip[:64], headers.get("user-agent", "")[:512]


def _iso(epoch: float) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _current(row: dict[str, Any]) -> bool:
    now = time.time()
    return now < row["expires_at"] - REFRESH_LEEWAY_SECONDS or now < row["retry_at"]


def _expires_at(token: dict[str, Any]) -> float:
    if token.get("expires_at"):
        return float(token["expires_at"])
    if token.get("expires_in"):
        return time.time() + float(token["expires_in"])
    return time.time() + DEFAULT_TOKEN_SECONDS
