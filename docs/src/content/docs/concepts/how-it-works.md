---
title: "How it works"
description: "Rules decide, a model explains, plain code acts: the pipeline from a signal to an action."
---

Each stage in the diagram above, in more detail:

| Stage | What happens |
|---|---|
| **Detect** | A Docker event (`die`, `oom`, `health_status`) or a 30-second poll trips one of the rules below. No model is involved — this is what stops the expensive, non-deterministic part from running on every hiccup. |
| **Gather** | The container's log tail, stats, health-check output, restart history and recent incidents are collected, then run through redaction. Nothing leaves the host before that. |
| **Diagnose** | The model gets that evidence and returns structured JSON: root cause, confidence, severity, steps for you, and one `recommended_action` from a closed enum. Identical failures reuse a cached diagnosis instead of asking again. |
| **Decide** | Plain Python. Checks the mode, the action allowlist, the confidence floor, both circuit breakers and the per-failure cooldown. Anything that fails a check becomes a pending approval with the reason written out. |
| **Act** | `notify_only` sends an ntfy push. An approved action executes, then the doctor waits and re-checks the container — if it's still unhealthy the incident is marked **failed** rather than retried. |

Every incident, diagnosis, decision and action is written to SQLite and shown in
the dashboard, including the actions deliberately *not* taken and why.

## What opens an incident

| Rule | Fires when |
|---|---|
| `exit_nonzero` | A container dies with a non-zero exit code, or is found stopped with one (0 and 143 are treated as intentional stops) |
| `oom_killed` | The kernel OOM killer took it |
| `unhealthy` | A `health_status: unhealthy` event, or two consecutive polls showing unhealthy |
| `restart_loop` | 3+ restarts within 15 minutes |
| `stuck_restarting` | Two consecutive polls in the `restarting` state |
| `high_cpu` | ≥85% of the **whole host's** CPU for 4 consecutive polls |
| `high_memory` | ≥90% of the container's memory limit for 4 consecutive polls |
| `log_fatal` | A single `FATAL` / `CRITICAL` line — one is enough |
| `log_errors` | 5+ lines matching the error pattern, **or** 10+ lines at `ERROR` level, in the last 120 log lines |
| `host_disk_pressure` | A watched filesystem is ≥90% full (the volume holding the doctor's own `/data` mount by default) |
| `disk_filling` | A watched filesystem is on course to be full within `DOCTOR_DISK_FORECAST_DAYS` (default 21), whatever its current percentage |
| `raid_degraded` | A disk array has a failed member, is missing a disk, or is rebuilding (read from `/proc/mdstat`) |
| `raid_errors` | A warning short of degraded: the last scrub found sectors that disagree, or md has corrected read errors on a disk (read from `/sys`) |
| `smart_health` | A drive's SMART data: it reports itself failing, has reallocated, pending or uncorrectable sectors, new CRC errors, an NVMe critical warning, low spare or high wear, or runs hot (read by the `doctor-smart` sidecar) |
| `host_memory` | The host has less than 5% of its memory available (the kernel's `MemAvailable`, which counts reclaimable cache) |
| `host_swap` | The host reads 256+ pages a second back from swap. Swap merely in use is normal |
| `host_load` | The 5-minute load is above 2 per CPU |
| `host_iowait` | The CPUs spend 40%+ of their time waiting on disks |
| `host_temperature` | A hardware sensor is at 80 °C or more, where the host exposes its sensors |

The host rules need their condition to last 10 polls (five minutes) and close
by themselves once it has been gone as long. They never lead to an action: the
host as a whole has nothing to restart.

Thresholds require *consecutive* breaches, so a nightly backup spike or a single
slow health check doesn't page you.

## One problem, not two incidents

When a database goes unhealthy its application follows, and the doctor used to
raise two incidents for one problem — diagnosing each in isolation, without the
one fact that explains both.

Compose already records the project, the service name and the declared
dependencies on every container it starts, so the relationship is there to be
read. The evidence sent for diagnosis now carries it:

```
## Stack `docmost` (service `app`)
depends on: db (unhealthy), redis (healthy)
other unhealthy services: worker (unhealthy)
NOTE: this container depends on db, which is itself unhealthy. Say so if the
evidence fits, and prefer a fix there over restarting this one.
```

The `depends_on` label's exact shape has changed between Compose versions, so
only the part before the first colon is read: `db` and
`db:service_healthy:false` both mean `db`. An unhealthy *sibling* counts even
without a declared dependency, because plenty of stacks never write one. A
healthy stack adds nothing to the prompt but tokens, so only the unwell parts
are listed.

It is deliberately a hint, not a veto. The incident still opens on its own
container: "the database is also down" is a reason to look there first, not proof
that nothing is wrong here.

## Incidents that close themselves

A rule that is still true re-fires on every poll, which bumps the incident's
last-seen time. So silence means the condition is over, and after
`DOCTOR_AUTO_RESOLVE_HOURS` (default 12, `0` disables) the incident resolves
itself rather than sitting open forever.

Two exceptions, because absence of a signal is not always absence of a problem:

- **Host disk pressure never auto-resolves.** It suppresses its own re-alerts
  until usage climbs another 2%, so a quiet disk incident means a *steadily*
  full disk, not a fixed one.
- **Anything awaiting approval is left alone.** That decision is yours to make,
  not a timeout's.

## What it may do about it

The model can only return one of: `none`, `notify_only`, `restart_container`,
`stop_container`, `prune_images`. Anything else is coerced to `notify_only`.
Only `restart_container` is auto-applicable by default.

Before any mutating action runs, **all** of these must hold:

- mode is `auto` (in `suggest` it becomes a pending approval instead)
- the action is in `DOCTOR_AUTO_ACTIONS` (and the container's `doctor.actions`)
- the model's confidence ≥ `DOCTOR_MIN_CONFIDENCE`
- fewer than 3 actions on this container in the last hour
- fewer than 10 actions across the host in the last hour
- this exact action wasn't already tried for this exact failure in the last 30 minutes
- the container isn't the doctor itself, the socket proxy, or on the ignore list

`doctor.max_actions_per_hour` overrides the per-container hourly cap, including
zero to prevent automatic actions. Before an automatic action reaches Docker,
the cap and cooldown checks are repeated inside a SQLite write transaction that
reserves an execution slot. Concurrent incidents therefore cannot spend the
same allowance. An action that loses the reservation stays pending for approval,
with the current circuit-breaker or cooldown reason recorded.

Failing any of these turns the action into a pending approval with the reason
written out, rather than silently dropping it.

After a restart the doctor waits `DOCTOR_VERIFY_DELAY` seconds and re-checks the
container. If it's still not healthy, the incident is marked **failed** and you
get a notification — it does not try again on its own.
