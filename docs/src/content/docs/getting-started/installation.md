---
title: "Installation"
description: "Requirements, the reference compose file, and running it on any Linux host or Docker manager."
---

The [quick start](/container-doctor/getting-started/quick-start/) gets it running; this page is what it set up
and how to adapt it.

## Requirements

- A Linux host with Docker Engine and Compose v2. amd64 and arm64 images are
  published.
- A model: an Anthropic API key, or any OpenAI-compatible endpoint, local ones
  included. Without one, the doctor still detects incidents; it just cannot
  explain them.
- Memory: the compose file caps the doctor at 512 MB, the proxy at 128 MB and
  the SMART sidecar at 64 MB.

## What the reference compose file runs

[`compose.example.yaml`](https://github.com/sebastianbuechler/container-doctor/blob/main/compose.example.yaml)
is a complete stack:

| Service | What it is |
|---|---|
| `doctor-socket-proxy` | LinuxServer's socket proxy, the doctor's only way to Docker: it may list, inspect, read logs and stats, and restart - never create, exec or touch volumes. See [The socket proxy](/container-doctor/guides/socket-proxy/). |
| `doctor-smart` | Optional, behind the Compose profile `smart`. Reads each drive's SMART data so the doctor never needs raw disk access. See [Drive health](/container-doctor/guides/storage/#drive-health). |
| `container-doctor` | The doctor: non-root, read-only root filesystem, no capabilities, its database in `./data`. |

Settings go in a `.env` file next to it, or your Docker manager's stack
environment; [`.env.example`](https://github.com/sebastianbuechler/container-doctor/blob/main/.env.example)
documents every one, and so does the
[environment variable reference](/container-doctor/reference/environment-variables/).
Every setting is passed as `- X=${X:-}`; keep that pattern when you adapt the
file, because it is how the Settings page knows which settings the stack
environment can reach.

A dashboard password (`DOCTOR_WEB_PASSWORD`) or OIDC is required: with
neither configured, the dashboard and its API refuse every request. See
[Signing in](/container-doctor/guides/sign-in/).

## The user it runs as

The image runs as UID:GID `1000:1000`. Compose's `user:` line, set from
`DOCTOR_UID` and `DOCTOR_GID`, overrides it, so the data directory can belong
to any user. An install whose data directory belongs to another user - an
earlier image ran as `1026:100` - either sets `DOCTOR_UID=1026` and
`DOCTOR_GID=100`, or `chown`s the directory to `1000:1000`.

Pin `DOCTOR_IMAGE_TAG` to a release (`X.Y.Z`) rather than `latest` once it
works, so a redeploy never brings a version you did not choose.

## Behind a reverse proxy

The dashboard listens on port 8080. Before it is reachable from outside your
LAN, put a reverse proxy with TLS in front of it, drop the `ports:` mapping in
favour of the proxy's network, and set `DOCTOR_PUBLIC_URL` to the address it is
served at - links in notifications and OIDC sign-in use it. With the proxy on
the same host, publish the port as `127.0.0.1:8080:8080` instead, so only the
proxy reaches it.

## Notifications

The doctor pushes incidents, approvals waiting and the results of actions
through [ntfy](https://ntfy.sh). Notifications are off until both a server and
a topic are set:

```ini title=".env"
DOCTOR_NTFY_URL=https://ntfy.sh
DOCTOR_NTFY_TOPIC=<a long random name: openssl rand -hex 16>
DOCTOR_NTFY_TOKEN=
```

**On ntfy.sh a topic is public.** Anyone who knows or guesses its name can
subscribe and read every notification - container names, what failed and the
model's summary of why. A name like `container-doctor` is the first thing
anyone would try. Pick a long random topic, protect it with an access token
(`DOCTOR_NTFY_TOKEN`, on a server with access control), or run your own ntfy
server and point `DOCTOR_NTFY_URL` at it.

`DOCTOR_NTFY_URL` and the token are set in the stack environment only, since
the token is sent to that server; the topic can also be changed on the
Settings page. With `DOCTOR_PUBLIC_URL` set, each notification links to its
incident (see
[From a notification to the incident](/container-doctor/guides/dashboard/#from-a-notification-to-the-incident)).

## Which host

Any Linux machine running Docker: the doctor reads the same `/proc`, `/sys`
and Docker socket everywhere. A Synology is recognised by its kernel
(`/proc/sys/kernel/syno_hw_version`, which also names the model), and only
there do DSM's ways apply - md0 and md1 as system arrays that always show empty
bays, `vgN` as Storage Pool N, DSM's names in a few hints. Nothing to set up on
either.

| | |
|---|---|
| `DOCTOR_HOST_KIND` | `auto` (the default) detects; `synology` or `generic` overrides it, should the guess be wrong. |
| `DOCTOR_HOST_NAME` | What diagnoses, notifications and the Host page call the host, e.g. `tower`. Empty: "the host", and the Host page shows the detected model (`Synology DS920+`). |
| `DOCTOR_DATA_VOLUME`, `DOCTOR_UID`, `DOCTOR_GID` | Where the database lives on the host and who owns it. Read by the compose file itself; the defaults are `./data` and `1000:1000`. On a Synology, a directory under a shared folder such as `/volume1/docker/container-doctor`, owned by your DSM user (often `1026:100`), works the same way. |

Incidents about the host as a whole are filed under `host:system`.

## Which Docker manager

The doctor never talks to the manager that deploys the stacks. It reads Docker
through the socket proxy and Compose's own labels on each container, which every
Compose-based manager sets - Portainer, Coolify, Komodo, Dockge, or plain
`docker compose up`. Three things are worth telling it, all optional:

| | |
|---|---|
| `DOCTOR_DOCKER_MANAGER` | The manager's name: `Portainer`, `Coolify`... It is named in the diagnosis's and the hand-off's instructions ("not in Portainer or on the host directly") and on the Settings page, whose mark for a variable set in the stack environment reads as the name. With `Portainer`, a redeploy that left the old build running says which Portainer settings cause that. Empty: the doctor speaks of "the stack environment". |
| `DOCTOR_DEPLOY_METHOD` | How a change reaches the host, in words, e.g. `a push to main; a GitHub Actions workflow calls each changed stack's webhook`. It goes to the diagnosis and the hand-off, so their fix steps say where a change belongs. |
| `doctor.stack: <folder>` | A label, on any one service of a stack, naming its folder in the repository. Needed only when the manager names Compose projects its own way rather than after the stack: Redeploy and the repository context both find a stack by its folder. Portainer names the project after the stack, so a Portainer git stack needs none. |

Neither variable changes what the doctor does beyond those words. Redeploying
needs a workflow that can redeploy a stack - see
[Redeploying a new build](/container-doctor/guides/updates/#redeploying-a-new-build).
