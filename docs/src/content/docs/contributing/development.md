---
title: "Development"
description: "Run it locally, run the tests, and find your way around the code."
---

Container Doctor is a Python service (FastAPI, docker-py, SQLite) with a
dashboard in plain JavaScript - no build step. Everything a contributor runs is
below; every change also checks the docs - see
[Documentation](/container-doctor/contributing/documentation/).

## Tests

The parts that must not be wrong — redaction, fingerprint stability, action
coercion, the log views' auth boundary, and every policy guardrail — have
tests. No daemon needed:

```bash
git clone https://github.com/sebastianbuechler/container-doctor
cd container-doctor
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Install the requirements first: the suite imports the app, so it needs the
app's dependencies. The tests that drive the HTTP layer skip with a clear
reason rather than erroring if they are missing, and CI installs from
`requirements.txt` so it cannot drift from what ships.

Four tests need a Docker daemon and skip by default; run them with
`DOCTOR_PROXY_INTEGRATION=1`.

## Layout

```
container-doctor/
├── compose.example.yaml   a reference stack: socket proxy, SMART sidecar, doctor
├── Dockerfile             non-root, read-only rootfs; published to GHCR by CI
├── requirements.txt
├── .env.example           every setting, documented
├── tests/                 stdlib tests for redaction and the policy engine
├── docs/                  this documentation site (Astro Starlight)
├── examples/              the redeploy workflow
├── scripts/               the docs' generated reference, the diagram
└── app/
    ├── main.py            entrypoint
    ├── selftest.py        one synthetic incident, to validate a provider
    ├── preflight.py       what the Docker endpoint actually permits
    ├── config.py          every setting, declared once; env + page + per-container labels
    ├── docker_client.py   async wrapper over docker-py
    ├── detector.py        the rules that open incidents
    ├── changes.py         what changed about a container between polls
    ├── memory.py          memory-limit suggestions from hourly peaks
    ├── networks.py        the network map and its wiring notes
    ├── host.py            the host's own CPU, memory, disks and sensors
    ├── hostkind.py        a Synology told apart from any other Linux
    ├── connectivity.py    internet and DNS checks, and their verdicts
    ├── smart.py           drive health from the SMART sidecar
    ├── classifier.py      a decision model's second opinion on a signal
    ├── decision.py        decision models: Jev via OpenRouter, or any chat model
    ├── redact.py          secret scrubbing + failure fingerprinting
    ├── diagnoser.py       Claude / OpenAI-compatible, structured output, call log
    ├── remediator.py      policy engine + executor (no model in the loop)
    ├── notifier.py        ntfy
    ├── github_deploy.py   redeploying a stack through the Deploy stacks workflow
    ├── repo_context.py    a stack's definition and recent commits, for a diagnosis
    ├── store.py           SQLite: incidents, actions, cache, spend, calls, changes, settings
    ├── supervisor.py      orchestration
    └── web/
        ├── api.py         FastAPI + SSE
        └── static/        the dashboard (no build step)
```
