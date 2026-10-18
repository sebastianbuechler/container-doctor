---
title: "Sign-in"
description: "Password or OIDC with PKCE, sessions, and getting back in when sign-in breaks."
---

The dashboard can restart containers, so it asks who you are, and it refuses
to run open: with neither `DOCTOR_WEB_PASSWORD` nor OIDC configured, every
page and API request is answered `403 Set DOCTOR_WEB_PASSWORD or configure
OIDC`. Only `/healthz`, the static files and the sign-in status the page reads
to draw its sign-in button (`/api/me`, `/api/auth/provider`) are served.

There are two ways in:

- **A password**, `DOCTOR_WEB_PASSWORD` with `DOCTOR_WEB_USER` (default
  `doctor`). The sign-in button opens `/auth/password`, which answers with an
  HTTP Basic challenge, so the browser asks with its own password prompt and
  then sends the credentials with every request. Basic auth sends the
  password with every request, readable on the wire over plain HTTP, so beyond
  a trusted LAN put the dashboard behind a reverse proxy with TLS. Use a long
  random password (`openssl rand -base64 24`). Known placeholders
  (`change-me`, `changeme`, `change_me`, `password`, `admin`, `doctor`,
  `secret`, `123456`, or the user name itself) are refused: the doctor logs a
  warning and treats sign-in as unconfigured. A password shorter than 12
  characters works, with a warning at startup. Repeated wrong passwords from
  one client are slowed down: after 10 failures within 5 minutes, that client
  gets `429` for the rest of the window (see
  [Security](/container-doctor/guides/security/#the-dashboard)).
- **OIDC**, through your own provider. The doctor is its own OIDC client
  rather than trusting a header from a reverse proxy: a header is only as
  trustworthy as every container that can reach the doctor's port.

Both are set in the stack environment only. Who may sign in, and where
sign-in goes, cannot be changed from the Settings page (see
[What stays in the stack environment](/container-doctor/guides/settings/#what-stays-in-the-stack-environment)).

## Setting up OIDC

Any provider with discovery and PKCE works - Pocket ID, Authentik, Authelia,
Keycloak, Zitadel. In your provider, add an OIDC client. In Pocket ID, for
example:

| Field | Value |
|---|---|
| Callback URL | `https://doctor.example.com/auth/callback` |
| Client type | Confidential (a client secret) — public/PKCE-only also works |

Then set, in the stack's environment:

```
DOCTOR_OIDC_ISSUER=https://id.example.com
DOCTOR_OIDC_CLIENT_ID=<from your provider>
DOCTOR_OIDC_CLIENT_SECRET=<from your provider>
DOCTOR_PUBLIC_URL=https://doctor.example.com
```

`DOCTOR_PUBLIC_URL` is what the callback URL is built from, and it has to match
what the provider has registered exactly.

`DOCTOR_SESSION_SECRET` signs the session cookie. Leave it unset and one is
generated on the first start and kept in the data directory
(`/data/session_secret`, mode `0600`, readable only by the doctor's user), so
a restart does not sign anyone out. Set it (`openssl rand -base64 32`) to
manage it yourself; changing it signs everyone out.

`DOCTOR_OIDC_ALLOWED_GROUPS` restricts access to members of the listed groups
(comma-separated), read from the `DOCTOR_OIDC_GROUPS_CLAIM` claim (default
`groups`). Empty means any account the provider authenticates.
`DOCTOR_OIDC_SCOPES` (default `openid profile email groups`) is what the
doctor asks for; a provider that names its groups scope differently needs it
changed.

## How long a sign-in lasts

The provider decides, through the client's token lifetimes. The doctor keeps
the refresh token it gets at sign-in on the server (encrypted with the session
secret; the cookie only names it), and each time the access token runs out it
asks the provider for new ones. When the provider says no — the account
disabled, the refresh token expired or revoked, the user no longer in an
allowed group — the doctor signs you out on the spot, open dashboards included.
Groups are re-read on each renewal too.

- **Access token lifetime** is how often the doctor checks back: an hour means a
  revoked account is out within the hour.
- **Refresh token lifetime** (or inactivity timeout) is how long the session
  survives without a renewal.
- `DOCTOR_SESSION_HOURS` (default 12) is the doctor's own limit on an *unused*
  session: the browser's cookie runs out after that long without a request.

While the provider cannot be reached the session carries on, and the doctor
asks again every minute: signing in again would need the same provider. A
refresh token is asked for with `offline_access` whenever the provider lists
it. Without one, a session ends with its access token.

## Profile and sessions

**Profile**, in the account menu, shows what the provider says about you (its
claims, re-read at every renewal), how the doctor signs you in, and every
session you have, each with when it signed in, when it was last used and from
where. **Sign out** ends one at once: its next request finds nothing to stand
on, and an open dashboard drops within a minute. **Sign out all others** keeps
only the one you are using.

Sessions are named after the device: *Alex's Pixel 8 Pro*. Browsers do not
reveal the name you gave a device, so the model is the closest thing — Chrome
and Edge report it through User-Agent Client Hints. Firefox and Safari don't,
so those read as the kind of device (*Alex's iPhone*, *Alex's Windows PC*).
**Rename** gives any session a name of your own.

## The password, once OIDC is on

`DOCTOR_WEB_PASSWORD` is **refused** once OIDC is configured, so there is one way
in rather than two. Set `DOCTOR_WEB_PASSWORD_FALLBACK=true` to keep it working
alongside OIDC.

That is worth considering rather than dismissing: the doctor is the tool you
would use to find out *why* your identity provider is down, and it may run on
the same host. Leaving the fallback off is the cleaner posture; turning it on
trades that for not being locked out of your monitoring by the thing it
monitors.

## When sign-in does not work

The doctor reaches the provider at the same public URL your browser uses, which
on a home network can mean hairpinning back through the router. It fetches the
discovery document once at startup and puts a notice on the dashboard if it
cannot — rather than letting the first sign-in attempt be the thing that
discovers it. A mismatch between `DOCTOR_OIDC_ISSUER` and the issuer the
provider reports is flagged the same way, because token validation would
reject every login.
