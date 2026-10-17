---
title: "Per-container configuration"
description: "Labels that change how the doctor treats one container."
---

Add labels to any service in your other stacks:

```yaml
labels:
  - doctor.enable=false                 # ignore this container entirely
  - doctor.mode=auto                    # override the global mode for it
  - doctor.actions=restart_container    # what may be auto-applied to it
  - doctor.max_actions_per_hour=1       # tighter circuit breaker
  - doctor.critical=true                # always notify, even for low severity
  - doctor.ignore_log=SomeNoisyProvider # log lines to never count as errors
```

Useful ones: `doctor.enable=false` on anything where a restart is
disruptive or meaningless, and `doctor.mode=auto` on the stateless web apps
(`it-tools`, `stirling-pdf`, `openspeedtest`) where a restart is cheap.

To flip the default to opt-in, set `DOCTOR_REQUIRE_OPT_IN=true`; only containers
with `doctor.enable=true` are then watched.
