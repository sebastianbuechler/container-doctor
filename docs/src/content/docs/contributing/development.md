---
title: "Development"
description: "Run it locally, run the tests, and find your way around the code."
---

Container Doctor is a Python service (FastAPI, docker-py, SQLite) with a
dashboard in plain JavaScript - no build step. Everything a contributor runs is
below; every change also checks the docs - see
[Documentation](/container-doctor/contributing/documentation/).

## Setting up

The image runs Python 3.14 (the version is read from the `Dockerfile`), and
the tests should run on the same:

```bash
git clone https://github.com/sebastianbuechler/container-doctor.git
cd container-doctor
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

`requirements.txt` is what the image installs; `requirements-dev.txt` adds
what only the tests need (PyYAML, for reading the compose file).

## Tests

The parts that must not be wrong — redaction, fingerprint stability, action
coercion, the sign-in boundary, and every policy guardrail — have tests. No
Docker daemon needed:

```bash
python -m unittest discover -s tests -v
```

Install the requirements first: the suite imports the app, so it needs the
app's dependencies. The tests that drive the HTTP layer skip with a clear
reason rather than erroring if they are missing, and CI installs from the
same requirements files, so it cannot drift from what ships.

A few tests start the real socket proxy image to check what it permits, and
skip by default; with Docker running:

```bash
DOCTOR_PROXY_INTEGRATION=1 python -m unittest tests.test_socket_proxy -v
```

To try a change against a real host, build the image and point the compose
file at it, or run `python -m app.selftest` and `python -m app.preflight`
inside a running container to check the model and the Docker endpoint (see
[Commands](/container-doctor/reference/commands/)).

## CI

The **Tests** workflow runs the full suite on every pull request and push to
`main`, and **Socket proxy boundary** runs the proxy tests against the real
image. The image and release workflows run both before they build anything,
so an image is never published from a commit that fails them. Every action
in the workflows is pinned to a commit SHA, with its version in a comment;
Dependabot proposes updates.

## Layout

```
container-doctor/
├── compose.example.yaml    a reference stack: socket proxy, SMART sidecar, doctor
├── Dockerfile              non-root, read-only rootfs; published to GHCR by CI
├── requirements.txt        what the image installs
├── requirements-dev.txt    what the tests need on top
├── .env.example            every setting, documented
├── tests/                  900+ unit tests, stdlib unittest; no Docker daemon needed
├── docs/                   this documentation site (Astro Starlight)
├── examples/               the redeploy workflow
├── scripts/                the docs' generated reference, the diagram
└── app/
    ├── main.py             entrypoint: start the supervisor, then serve the dashboard
    ├── config.py           every setting, declared once; env + page + per-container labels
    ├── supervisor.py       orchestration: detect, gather, diagnose, decide, act, verify
    ├── docker_client.py    async wrapper over docker-py
    ├── detector.py         the rules that open incidents
    ├── deploys.py          telling a redeploy apart from a failure
    ├── stacks.py           which containers belong together, from Compose's labels
    ├── changes.py          what changed about a container between polls
    ├── runconfig.py        how a container was configured, and what a redeploy changed
    ├── memory.py           memory-limit suggestions from hourly peaks
    ├── forecast.py         how fast a disk is filling, and when it runs out
    ├── raid.py             the disk arrays, from /proc/mdstat and /sys
    ├── smart.py            drive health from the SMART sidecar
    ├── host.py             the host's own CPU, memory, disks and sensors
    ├── hostkind.py         a Synology told apart from any other Linux
    ├── networks.py         the network map and its wiring notes
    ├── connectivity.py     internet and DNS checks, and their verdicts
    ├── registry.py         whether a running image is still the tag's current build
    ├── versions.py         whether there is a newer release than the pinned version
    ├── redact.py           secret scrubbing + failure fingerprinting
    ├── classifier.py       a decision model's second opinion on a signal
    ├── decision.py         decision models: Jev via OpenRouter, or any chat model
    ├── diagnoser.py        Anthropic / OpenAI-compatible, structured output, call log
    ├── briefing.py         the summary at the top of the dashboard
    ├── remediator.py       policy engine + executor (no model in the loop)
    ├── notifier.py         ntfy
    ├── handoff.py          an incident as a Markdown brief for your own agent
    ├── github_deploy.py    redeploying a stack through a workflow in its repository
    ├── repo_context.py     a stack's definition and recent commits, for a diagnosis
    ├── store.py            SQLite: incidents, actions, cache, spend, calls, changes, settings
    ├── selftest.py         one synthetic incident, to validate a provider
    ├── preflight.py        what the Docker endpoint actually permits
    └── web/
        ├── api.py          FastAPI + SSE, sign-in and the security checks
        ├── oidc.py         OIDC sign-in against any provider with discovery
        ├── devices.py      naming the device a session signed in from
        └── static/         the dashboard (no build step), its offline page and service worker
```
