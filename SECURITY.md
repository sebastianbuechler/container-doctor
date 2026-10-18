# Security policy

Container Doctor reads your container logs, sends them to an LLM, and can
restart containers. Security reports are welcome and taken seriously.

## Supported versions

Only the latest release gets fixes. An image built from `main` (`edge`) is
fixed by the next push. There are no backports to older releases: upgrade to
the latest one.

## Reporting a vulnerability

Please report privately, through GitHub's private vulnerability reporting:

**<https://github.com/sebastianbuechler/container-doctor/security/advisories/new>**

Do not open a public issue for a vulnerability. Include what you can of:

- the version or commit (shown under **Settings → Build and mode**);
- the mode, the LLM provider, and whether sign-in is by password or OIDC;
- what an attacker needs (network access to the dashboard, a signed-in
  account, control of a container's logs, ...) and what they gain;
- steps to reproduce, or a proof of concept.

Leave real secrets out of the report and any logs you attach.

## What to expect

This is a hobby project maintained in spare time, so responses are best
effort. The aim is to acknowledge a report within a week, agree on a fix and
a disclosure date with you, and publish an advisory with the release that
fixes it. Credit is given unless you would rather not be named.

## Scope

In scope:

- the doctor itself (`app/`) and its image;
- the dashboard and its API: sign-in, sessions, CSRF and origin checks,
  the settings that can or cannot be saved from the page;
- redaction of secrets from logs and evidence before they are stored or sent;
- the reference `compose.example.yaml`, including the socket proxy's grants
  and the `doctor-smart` sidecar's;
- anything that lets a container's log output make the doctor do more than
  the policy engine allows.

Out of scope:

- vulnerabilities in third-party images or services (the socket proxy,
  smartctl_exporter, the LLM provider, ntfy, your OIDC provider) - report
  those upstream, though a note here about how they affect the doctor is
  welcome;
- a secret that reaches a model or the database in a shape redaction does not
  recognise is worth reporting as a bug with the pattern, but redaction is
  documented as best effort, not a guarantee;
- deployments that expose the dashboard to the internet without TLS, or run
  `auto` mode with `stop_container` or `prune_images` auto-applied, against
  the documentation's advice;
- denial of service that needs a signed-in account.

## Threat model, in short

- **Docker access.** The doctor never sees the Docker socket. It reaches
  Docker only through a socket proxy that allows reads, logs, events and
  restart/stop of a container, and denies everything else - above all
  `POST /containers/create`, which would allow arbitrary host mounts. It runs
  as a non-root user with a read-only filesystem and no capabilities.
- **Prompt injection from logs.** Container logs are attacker-influenced
  input. The model's answer is constrained to five fixed actions, and plain
  code decides whether one runs. A hostile log line can at most get the model
  to choose an allowed action on the incident's own container - and that
  action still has to pass the mode (`observe` never acts, `suggest` waits for
  you), the `DOCTOR_AUTO_ACTIONS` allowlist, the confidence threshold, the
  per-container and host-wide rate limits and the cooldown. Keep
  `stop_container` and `prune_images` out of `DOCTOR_AUTO_ACTIONS`.
- **Data sent to the model.** Unless you run a local model, the redacted log
  tail and container metadata go to a third party. Redaction is pattern-based
  and best effort.
- **The dashboard.** With neither a password nor OIDC configured, it refuses
  every request. State-changing requests must come from its own origin; the
  password check is rate-limited; responses carry a strict
  Content-Security-Policy and framing protection. Settings that decide where
  a secret is sent, or how sign-in works, can only be set in the environment.
  The password is HTTP Basic auth: beyond a trusted LAN, put a reverse proxy
  with TLS in front of it.

The [Security](https://sebastianbuechler.github.io/container-doctor/guides/security/) page of the documentation goes into
each of these.
