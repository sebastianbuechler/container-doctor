---
title: "Troubleshooting"
description: "Common problems and how to fix them."
---

Start with the two built-in [checks](/container-doctor/reference/commands/): `app.preflight`
for the Docker endpoint and `app.selftest` for the model.

## "docker unreachable" in the dashboard

The socket proxy isn't up, or the
host (a DSM update, say) changed the socket path. Check
`docker logs container-doctor-socket-proxy`, then
`docker compose exec container-doctor python -m app.preflight` to see what the
endpoint answers.

## Actions fail but detection works

The endpoint is read-only. The preflight
says so explicitly; either set `DOCTOR_PROXY_ALLOW_POST=1` on the bundled proxy
or point `DOCTOR_DOCKER_HOST` at one that permits POST.

## Incidents but no diagnoses

Either credentials are unset (the dashboard
shows a warning banner), or today's USD budget or call cap is spent — the action
log row says which. To check the provider itself, run
`docker compose exec container-doctor python -m app.selftest`.

## A new vendor returns nothing useful

Run the self-test: it prints the raw
diagnosis and flags an answer that ignores the evidence. If it reports a schema
error the fallback to plain JSON mode has also failed, which usually means the
endpoint is not really OpenAI-compatible.

## Too many incidents

Raise `DOCTOR_LOG_ERROR_BURST`, extend
`DOCTOR_RESOURCE_BREACH_POLLS`, or narrow `DOCTOR_LOG_ERROR_PATTERN`. A chatty
container that logs "connection refused" as a matter of routine is best handled
with `doctor.enable=false`.
Incidents that come with every redeploy, errors from a proxy while its
backend restarts, mean the settle window is too short for that stack: raise
`DOCTOR_DEPLOY_SETTLE_MINUTES`.

## A pending action never runs

Its `reason` in the action log says why —
usually a circuit breaker or a confidence below the threshold. That's the system
working; deal with the underlying problem rather than raising the limits.
