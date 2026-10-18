---
title: "The socket proxy"
description: "Why the doctor reaches Docker only through a proxy, and reusing yours."
---

Many homelabs already run a socket proxy for Traefik or Homepage. This stack ships its
**own**, deliberately, and does not touch yours.

**Why a dedicated one, when yours runs the same image?** Because the settings
are what matter, and settings are per-container. The doctor is the only thing on
the host that needs to restart containers, so it is the only one that should
carry `ALLOW_RESTARTS=1`. Grant that on a shared proxy and Traefik, Homepage and
everything else on it can restart your containers too. The reverse is just as
annoying: if Traefik's proxy later needs `INFO=1`, you don't want that
silently widening what the doctor holds.

One container is a cheap price for keeping each consumer's grant sized to its
job. And since both run `lscr.io/linuxserver/socket-proxy`, there is no second
image to pull, trust or keep updated — Dependabot bumps one line either way.

Nothing collides. The bundled proxy publishes no ports, its container is named
`container-doctor-socket-proxy`, and it lives on a stack-scoped `internal`
network that nothing else can reach.

:::caution
**One subtlety worth knowing:** the doctor is attached to both its private
network (`doctor_internal`) *and* a second one (`doctor_external`, or the
reverse proxy's network you join it to), and Docker's
DNS resolves names across **every** attached network. A proxy named
`docker-socket-proxy` sitting on that shared network could therefore
answer instead of the intended one. The bundled proxy is reached by the
distinctive alias `doctor-socket-proxy`, and the preflight below tells you
which endpoint actually answered.
:::

## Reusing yours instead

Point `DOCTOR_DOCKER_HOST` at it, join its network, and delete the
`doctor-socket-proxy` service:

```
DOCTOR_DOCKER_HOST=tcp://your-existing-proxy:2375
```

Whether that is a good idea depends on one thing: **what you would have to
change on the shared proxy to make it work.**

- **It is already read-only and you run `observe` mode.** Reuse it. Detection,
  diagnosis and notifications all work, nothing can act, and you widen nothing.
  It needs `CONTAINERS=1`, `EVENTS=1`, `ALLOW_LOGS=1`, `PING=1`, `VERSION=1` —
  a proxy set up for Traefik often lacks `ALLOW_LOGS`, and sometimes `EVENTS`.
  `IMAGES=1` is optional: without it everything works except the image update
  check, which says so rather than failing. So is `NETWORKS=1`: without it the
  network map falls back to what the containers report.
- **You want the doctor to act.** Then the shared proxy needs
  `ALLOW_RESTARTS=1`, which grants restart to every other consumer of it. If
  that is acceptable in your setup, fine — but it is the question to answer
  deliberately rather than by accident.
- **Never reach for `POST=1`** to make it work. That opens
  `POST /containers/create`, which mounts arbitrary host paths. If something
  seems to need it, the preflight will tell you what is actually missing, and
  it is almost always `ALLOW_LOGS`.

Whichever you pick, don't guess what yours permits — ask. You can probe any
endpoint with the published image, without changing the stack:

```bash
docker run --rm --network <your-proxy-network> \
  -e DOCKER_HOST=tcp://your-existing-proxy:2375 \
  ghcr.io/sebastianbuechler/container-doctor:latest python -m app.preflight
```

or, on a deployed stack:

```bash
docker compose exec container-doctor python -m app.preflight
```

```
Docker endpoint: tcp://your-existing-proxy:2375

Required
  [ok  ] ping        allowed  HTTP 200
  [ok  ] version     allowed  HTTP 200
  [ok  ] containers  allowed  HTTP 200
  [WARN] events      blocked  HTTP 403

Optional
  [ok  ] write       blocked  HTTP 403
  [ok  ] images      blocked  HTTP 403

PROBLEM: Docker endpoint is missing access the doctor needs: events (the
         die/oom/health event stream). If this is a proxy you already run for
         something else, it is too narrow - use the bundled one instead.
NOTE:    Docker endpoint is read-only, so no action can ever be executed.
```

It exits non-zero when something is genuinely wrong, and it runs automatically
at startup — problems and warnings land in the dashboard's banner, so a
mis-pointed `DOCKER_HOST` surfaces immediately rather than when an action fails
at 3am.

Every probe is non-destructive: the write check targets a container id of 64
zeros, so a permitted endpoint answers `404` and a blocked one answers `403`,
and nothing is ever restarted to find out.

What it looks for:

| Need | Sections | If missing / present |
|---|---|---|
| **Required** | `ping`, `version`, `containers`, `events` | Missing any → the doctor can't work. A read-only Traefik proxy usually has `containers` but not `events`. |
| **Optional** | `write` (POST), `images`, `networks` | No POST → observe-only, which is a perfectly good posture. `images` only matters for the update check and `prune_images`; `networks` for drivers, flags and unused networks on the network map. `DOCTOR_MODE=auto` with no POST is reported as a problem, since approved actions would just fail. |
| **Should be blocked** | `exec`, `volumes`, `secrets`, `swarm`, `info` | Open → warned about. The doctor never calls them, but they widen what a compromise of this container would reach. A raw `unix://` socket is called out loudly. |

That last row is the useful one if you point it at an existing proxy: it tells
you how much of the host you just handed to an LLM-driven process.
