---
title: "Sign-in"
description: "Password or OIDC with PKCE, sessions, and getting back in when sign-in breaks."
---

The dashboard can restart containers, so it asks who you are. It is its own
OIDC client rather than trusting a header from a reverse proxy: it shares the
proxy's network with every other published service, and a header is only as
trustworthy as every container on that network.

Any provider with discovery and PKCE works - Pocket ID, Authentik, Authelia,
Keycloak, Zitadel. In Pocket ID, for example, add an OIDC client:

| Field | Value |
|---|---|
| Callback URL | `https://doctor.example.com/auth/callback` |
| Client type | Confidential (a client secret) — public/PKCE-only also works |

Then set, in the stack's environment:

```
DOCTOR_OIDC_ISSUER=https://id.example.com
DOCTOR_OIDC_CLIENT_ID=<from Pocket ID>
DOCTOR_OIDC_CLIENT_SECRET=<from Pocket ID>
DOCTOR_PUBLIC_URL=https://doctor.example.com
DOCTOR_SESSION_SECRET=<openssl rand -base64 32>
```

`DOCTOR_PUBLIC_URL` is what the callback URL is built from, and it has to match
what Pocket ID has registered exactly. `DOCTOR_SESSION_SECRET` signs the session
cookie; leave it unset and a random one is generated per start, so every restart
signs you out.

`DOCTOR_OIDC_ALLOWED_GROUPS` restricts access to members of a Pocket ID group
(comma-separated). Empty means any account the provider authenticates.

## How long a sign-in lasts

Pocket ID decides, through the client's **Token lifetimes**. The doctor keeps
the refresh token it gets at sign-in on the server (encrypted with
`DOCTOR_SESSION_SECRET`; the cookie only names it), and each time the access
token runs out it asks Pocket ID for new ones. When Pocket ID says no — the
account disabled, the refresh token expired or revoked, the user no longer in an
allowed group — the doctor signs you out on the spot, open dashboards included.
Groups are re-read on each renewal too.

- **Access token lifetime** is how often the doctor checks back: an hour means a
  revoked account is out within the hour.
- **Refresh token inactivity timeout** is how long the session survives without
  a renewal.
- `DOCTOR_SESSION_HOURS` (default 12) is the doctor's own limit on an *unused*
  session: the browser's cookie runs out after that long without a request.

While Pocket ID cannot be reached the session carries on, and the doctor asks
again every minute: signing in again would need the same Pocket ID. Pocket ID
issues a refresh token when asked for `offline_access`, which the doctor asks for
whenever the provider lists it. Without one, a session ends with its access
token.

## Profile and sessions

**Profile**, in the account menu, shows what Pocket ID says about you (its
claims, re-read at every renewal), how the doctor signs you in, and every
session you have, each with when it signed in, when it was last used and from
where. **Sign out** ends one at once: its next request finds nothing to stand
on, and an open dashboard drops within a minute. **Sign out all others** keeps
only the one you are using.

Sessions are named after the device: *Sebastian's Pixel 8 Pro*. Browsers do not
reveal the name you gave a device, so the model is the closest thing — Chrome
and Edge report it through User-Agent Client Hints. Firefox and Safari don't,
so those read as the kind of device (*Sebastian's iPhone*, *Sebastian's Windows
PC*). **Rename** gives any session a name of your own.

## The password, once OIDC is on

Without OIDC, `DOCTOR_WEB_PASSWORD` alone is the way in: the sign-in button
brings up the browser's own password prompt.

`DOCTOR_WEB_PASSWORD` is **refused** once OIDC is configured, so there is one way
in rather than two. Set `DOCTOR_WEB_PASSWORD_FALLBACK=true` to keep it working
alongside OIDC.

That is worth considering here rather than dismissing: the doctor is the tool
you would use to find out *why* Pocket ID is down, and it runs on the same host.
Leaving the fallback off is the cleaner posture; turning it on trades that for
not being locked out of your monitoring by the thing it monitors.

## When sign-in does not work

The doctor reaches Pocket ID at the same public URL your browser uses, which on
a home network can mean hairpinning back through the router. It fetches the
discovery document once at startup and puts a notice on the dashboard if it
cannot — rather than letting the first sign-in attempt be the thing that
discovers it. A mismatch between `DOCTOR_OIDC_ISSUER` and the issuer Pocket ID
reports is flagged the same way, because token validation would reject every
login.
