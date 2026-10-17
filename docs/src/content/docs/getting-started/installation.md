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
| `doctor-smart` | Optional. Reads each drive's SMART data so the doctor never needs raw disk access. Remove it if you don't want drive health. |
| `container-doctor` | The doctor: non-root, read-only root filesystem, no capabilities, its database in `./data`. |

Settings go in a `.env` file next to it, or your Docker manager's stack
environment; [`.env.example`](https://github.com/sebastianbuechler/container-doctor/blob/main/.env.example)
documents every one, and so does the
[environment variable reference](/container-doctor/reference/environment-variables/).
Every setting is passed as `- X=${X:-}`; keep that pattern when you adapt the
file, because it is how the Settings page knows which settings the stack
environment can reach.

## Behind a reverse proxy

The dashboard listens on port 8080. Before it is reachable from outside your
LAN, put a reverse proxy with TLS in front of it, drop the `ports:` mapping in
favour of the proxy's network, and set `DOCTOR_PUBLIC_URL` to the address it is
served at - links in notifications and OIDC sign-in use it.

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
| `DOCTOR_DATA_VOLUME`, `DOCTOR_UID`, `DOCTOR_GID` | Where the database lives on the host and who owns it. Read by the compose file itself; the defaults are `./data` and `1000:1000`. On a Synology that is typically `/volume1/docker/container-doctor` and `1026:100`. |

Incidents about the host as a whole are filed under `host:system`. Earlier
versions called it `host:nas`; the database is renamed on the first start, and
open host incidents keep folding their repeats.

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
