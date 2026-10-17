---
title: "Commands"
description: "The checks you can run inside the doctor's container - what the Docker endpoint permits, and whether the model answers."
---

Both run inside the doctor's container, change nothing, and print a verdict.

## `python -m app.preflight`

```bash
docker compose exec container-doctor python -m app.preflight
```

Proves what the configured Docker endpoint actually permits. Every probe is
non-destructive - the write probe targets a container ID that cannot exist, so
an allowed endpoint answers 404 and a blocked one 403. It tells you whether the
doctor can do its job (list, inspect, logs, events, restart) and whether it
holds more than it needs (create, exec, volumes, secrets). Run it after pointing
`DOCTOR_DOCKER_HOST` at a different proxy - see
[The socket proxy](/container-doctor/guides/socket-proxy/).

## `python -m app.selftest`

```bash
docker compose exec container-doctor python -m app.selftest
```

Sends one synthetic incident to the configured model, with the same prompt,
schema and coercion as a real diagnosis, and prints the answer, the latency,
the token counts and the cost. It does not touch Docker, the database or the
spend meter - a 30-second loop for trying a vendor. See
[Models and cost](/container-doctor/guides/models/).
