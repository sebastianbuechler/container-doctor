---
title: "Container labels"
description: "Every doctor.* label, set on a service to change how the doctor treats that one container."
---

Labels go on any service in any stack the doctor watches. They change how the
doctor treats that one container; everything else follows the global settings.
See [Per-container configuration](/container-doctor/guides/per-container/) for when to use
them.

```yaml title="compose.yaml"
services:
  memos:
    image: neosmemo/memos:0.24.1
    labels:
      - doctor.mode=auto
      - doctor.actions=restart_container
```

| Label | Values | Default | What it does |
|---|---|---|---|
| `doctor.enable` | `true`, `false` | `true` | `false` makes the doctor ignore the container entirely. With `DOCTOR_REQUIRE_OPT_IN=true`, only containers with `true` are watched. |
| `doctor.mode` | `observe`, `suggest`, `auto` | `DOCTOR_MODE` | The mode for this container, overriding the global one - typically `auto` on stateless apps where a restart is cheap. |
| `doctor.actions` | comma-separated: `restart_container`, `stop_container`, `prune_images`, `notify_only`, `none` | `DOCTOR_AUTO_ACTIONS` | Which actions may be applied to it without asking, in `auto`. |
| `doctor.max_actions_per_hour` | a whole number | `DOCTOR_MAX_ACTIONS_PER_CONTAINER_PER_HOUR` | A tighter circuit breaker for this container. |
| `doctor.critical` | `true`, `false` | `false` | Notify about every incident on it, even low-severity ones. |
| `doctor.ignore_log` | a regular expression | none | Log lines it emits harmlessly; matching lines never count as errors. |
| `doctor.stack` | a folder name | the Compose project | Which folder of the deploy repository this container's stack is, where the Docker manager names Compose projects its own way. Set it on one service; it applies to the whole project. See [Redeploying a new build](/container-doctor/guides/updates/#redeploying-a-new-build). |

Labels are read on every poll, so a change takes effect once the container is
recreated with it.
