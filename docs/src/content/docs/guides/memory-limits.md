---
title: "Memory limits"
description: "Memory-limit suggestions from each container's real peaks."
---

The doctor keeps each container's memory peak and mean per hour, for 14 days.
**Memory limits**, under Containers, compares the highest peak with the
container's limit:

| Verdict | When | Suggestion |
|---|---|---|
| raise it | the peak is at 90% of the limit or more, or it was OOM killed | at least a quarter above the current limit |
| tight | the peak is within a quarter of the limit | above the current limit |
| no limit | no `mem_limit` set | the peak plus a third |
| could be lower | the limit is several times the peak | the peak plus a third (optional) |

A suggestion is the highest peak plus a third, rounded to a limit you'd write:
64 MiB steps below 1 GiB, 256 MiB steps below 4 GiB, whole gigabytes above. It
comes as the line to add, e.g. `mem_limit: 704m` on the service in its stack's
compose file. A limit reserves nothing; what it buys is that a leak kills that
one container instead of starving the host. Containers without a limit are
folded into one row, largest first.

A verdict needs a day of hourly peaks. Until then the container reads
*collecting*. A container about to hit its limit also shows in the briefing.
