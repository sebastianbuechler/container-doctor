"""Memory limits, suggested from what each container has actually used.

A limit in Docker is a cap, not a reservation: setting one frees nothing. What
it buys is that one runaway container is killed on its own instead of taking
the host down with it - and a cap set too tight buys an OOM kill every night at
the same time. So the suggestion is always the same shape: the highest peak in
the window, plus headroom, rounded to something a person would write.
"""

from __future__ import annotations

import math
from typing import Any

MIB = 1024 * 1024
GIB = 1024 * MIB

# Headroom over the highest hourly peak. Peaks are hourly maxima of 30-second
# readings, so a spike shorter than a poll is not in them; a third covers it.
HEADROOM = 1.3
FLOOR = 64 * MIB
# Less than a day of hours says nothing about the nightly job.
MIN_HOURS = 24

# (verdict, dashboard level) in the order they matter.
VERDICTS = {
    "raise": "critical",   # at or over its limit already, or OOM killed
    "tight": "warning",    # peaks within a quarter of its limit
    "unlimited": "info",   # no cap: one runaway can starve the host
    "loose": "neutral",    # the cap is several times what it uses
    "ok": "good",
    "collecting": "neutral",
}


def nice(value: float) -> int:
    """Round up to a limit a person would write: 64m steps below 1g, 256m to
    4g, whole gigabytes above."""
    value = max(value, FLOOR)
    step = 64 * MIB if value < GIB else 256 * MIB if value < 4 * GIB else GIB
    return int(math.ceil(value / step) * step)


def compose_value(limit: int) -> str:
    """The value for `mem_limit:` - 768m, 1536m, 6g."""
    if limit >= 4 * GIB and limit % GIB == 0:
        return f"{limit // GIB}g"
    return f"{limit // MIB}m"


def suggest(
    name: str,
    hours: list[dict[str, Any]],
    *,
    limit: int,
    service: str = "",
    stack: str = "",
    oom_killed: bool = False,
) -> dict[str, Any]:
    """One container's verdict: its peak, its limit, and what to set."""
    out: dict[str, Any] = {
        "name": name, "stack": stack, "service": service,
        "limit_bytes": limit, "hours": len(hours),
    }
    if len(hours) < MIN_HOURS:
        out.update(verdict="collecting", level=VERDICTS["collecting"],
                   reason=f"{len(hours)} of {MIN_HOURS} hours of history so far")
        return out

    peak = max(h["max"] for h in hours)
    typical = sorted(h["mean"] for h in hours)[len(hours) // 2]
    suggested = nice(peak * HEADROOM)
    out.update(peak_bytes=peak, typical_bytes=typical, suggested_bytes=suggested)

    if limit <= 0:
        verdict = "unlimited"
        reason = "No memory limit: a leak or a runaway job can use all of the host's memory."
    elif oom_killed or peak >= limit * 0.9:
        verdict = "raise"
        suggested = max(suggested, nice(limit * 1.25))
        out["suggested_bytes"] = suggested
        reason = ("It was killed for running out of memory." if oom_killed
                  else "Its peak is at its limit, so the next spike is an OOM kill.")
    elif peak >= limit * 0.75:
        verdict = "tight"
        reason = "Its peak is within a quarter of its limit."
        if suggested <= limit:
            suggested = nice(limit * 1.25)
            out["suggested_bytes"] = suggested
    elif suggested <= limit * 0.4 and limit - suggested >= 256 * MIB:
        verdict = "loose"
        reason = ("Its limit is several times its peak - a tighter cap would stop "
                  "a runaway sooner. Optional: a limit reserves nothing.")
    else:
        verdict = "ok"
        reason = "Its peak sits comfortably under its limit."

    out.update(verdict=verdict, level=VERDICTS[verdict], reason=reason,
               compose=f"mem_limit: {compose_value(out['suggested_bytes'])}")
    return out


def rank(suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Most urgent first; within a verdict, the biggest user first."""
    order = list(VERDICTS)
    return sorted(suggestions, key=lambda s: (order.index(s["verdict"]),
                                              -int(s.get("peak_bytes") or 0), s["name"]))
