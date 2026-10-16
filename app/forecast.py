"""How fast a disk is filling, and when it runs out.

"92% full" is close to useless on a big disk: it can sit there for a year, or be
gone by Friday, and the number reads the same either way. A rate and a date
say which.

Least squares over recent samples rather than first-to-last: a single restart
that frees a few gigabytes should bend the line, not reset it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Below this the line is noise, not a trend: a few bytes a day extrapolates to
# a date centuries out, which is a number nobody should be shown.
_MIN_BYTES_PER_DAY = 1_000_000.0

# Two points can only ever draw a straight line through themselves.
MIN_SAMPLES = 4


@dataclass(frozen=True)
class Trend:
    bytes_per_day: float
    days_until_full: float | None  # None when it is not filling
    samples: int
    span_days: float

    @property
    def filling(self) -> bool:
        return self.bytes_per_day >= _MIN_BYTES_PER_DAY


def trend(samples: list[tuple[datetime, int]], total_bytes: int) -> Trend | None:
    """Fit `used_bytes` against time. None when there is not enough to fit.

    `samples` is [(when, used_bytes), ...] in any order.
    """
    if len(samples) < MIN_SAMPLES or total_bytes <= 0:
        return None

    ordered = sorted(samples, key=lambda point: point[0])
    origin = ordered[0][0]
    xs = [(when - origin).total_seconds() / 86400.0 for when, _ in ordered]
    ys = [float(used) for _, used in ordered]
    span = xs[-1] - xs[0]
    if span <= 0:
        return None

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / variance

    days_left: float | None = None
    if slope >= _MIN_BYTES_PER_DAY:
        # Project from the most recent reading, not from the fitted line: the
        # question is how long from *now*, and the last sample is now.
        free = max(0.0, float(total_bytes) - ys[-1])
        days_left = free / slope
    return Trend(bytes_per_day=slope, days_until_full=days_left,
                 samples=n, span_days=span)


def human_rate(bytes_per_day: float) -> str:
    step = 1024.0
    amount = abs(float(bytes_per_day))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < step:
            return f"{amount:.1f} {unit}/day"
        amount /= step
    return f"{amount:.1f} PiB/day"


def human_days(days: float) -> str:
    if days < 1:
        return "under a day"
    if days < 90:
        return f"{days:.0f} days"
    if days < 730:
        return f"{days / 30.44:.0f} months"
    return f"{days / 365.25:.0f} years"
