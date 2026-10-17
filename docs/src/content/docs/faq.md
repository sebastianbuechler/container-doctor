---
title: "FAQ"
description: "Short answers to the questions people ask first."
---

## Can it break my containers?

Not on its own. It starts in `observe` mode and changes nothing until you set
`suggest` (it asks before every action) or `auto` (it acts only where you
allowed it). Even then the model can only pick one of five fixed actions -
restart, stop, prune dangling images, notify, or nothing - and a deterministic
policy engine decides whether that action may run, inside per-container and
host-wide rate limits and cooldowns. The socket proxy makes creating a
container impossible whatever the mode. See
[How it works](/container-doctor/concepts/how-it-works/).

## What is sent to the model?

The evidence for one incident: the rule that fired, the container's state and
configuration, what changed on it, its stack's neighbours, the host's state,
and a tail of its logs with secrets stripped by pattern. Nothing is sent until a
rule has already opened an incident, and identical failures reuse a cached
diagnosis. Every request sent can be read back on the dashboard's LLM page. See
[Models and cost](/container-doctor/guides/models/) and [Security](/container-doctor/guides/security/).

## What does it cost?

One diagnosis is roughly 1,500 tokens in and 200 out. A model is only called
when a rule has opened an incident, so a normal week is a handful of calls - cents
a month on a cheap model, nothing on a free tier or a local one. A daily budget
(`DOCTOR_DAILY_USD_BUDGET`) and call cap hard-stop spend either way.

## Does it work without a model?

Yes. It still detects incidents, folds and resolves them, and shows everything
on the dashboard; it just cannot explain them.

## Does it need internet access?

Only for what you use: the model's API (unless the model is local), the
registries for the update check, your ntfy server, GitHub for Redeploy, and the
internet and DNS checks you configure.

## Which hosts and Docker setups does it support?

One Linux host running Docker, deployed however you like - plain `docker
compose`, Portainer, Coolify, Komodo, Dockge. A Synology is detected and its
DSM layout handled. Swarm and Kubernetes are not supported. See
[Installation](/container-doctor/getting-started/installation/).

## Why does it need a socket proxy?

Because the raw Docker socket is root on the host. The proxy lets the doctor
list, inspect, read logs and restart, and nothing that could escape a
container. See [The socket proxy](/container-doctor/guides/socket-proxy/).

## How do I update it?

Pin a release, `ghcr.io/sebastianbuechler/container-doctor:X.Y.Z@sha256:...`,
and let Dependabot or Renovate propose the next one - or track `latest`, which
moves only with a release. Every release is listed on
[GitHub](https://github.com/sebastianbuechler/container-doctor/releases).
