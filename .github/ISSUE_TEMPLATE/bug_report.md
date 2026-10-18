---
name: Bug report
about: Something does not work as the guide says it should
title: ""
labels: bug
---

<!--
Do not paste secrets. Logs, prompts and settings exports can contain API keys,
tokens, passwords and hostnames: read through anything you paste and replace
real values with placeholders. For a security vulnerability, do not open an
issue - see SECURITY.md.
-->

## What happened

<!-- What you saw, and what you expected instead. -->

## How to reproduce

1.
2.
3.

## Environment

- Version / commit (Settings → Build and mode):
- Mode (`observe` / `suggest` / `auto`):
- LLM provider and model:
- Host kind (Synology / other Linux, and the distribution):
- Docker Engine version (`docker version`):
- Docker Compose version, and the Docker manager if any (Portainer, Dockge...):
- Sign-in (password / OIDC, and the provider):

## Logs

<!--
The doctor's own log (`docker logs container-doctor`), and the socket proxy's
if it is about Docker access. Redact them first.
-->

```
```

## Anything else

<!-- The preflight's output (`docker compose exec container-doctor python -m app.preflight`) helps with anything about the Docker endpoint; the self-test's (`python -m app.selftest`) with anything about the model. -->
