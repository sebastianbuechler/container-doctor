"""Telling a redeploy apart from a failure.

A redeploy looks like an outage from the inside. Compose stops the old
container - which Docker reports as an exit, 143 or 137 when the app takes
its time - then creates the new one, and for the minute that takes, anything
talking to that service logs errors: a reverse proxy, a tunnel, a frontend.
Every one of those used to open an incident, and each was explained away by
its diagnosis ("stopped during a container replacement").

Two things tell them apart:

- A stop someone asked for is announced. `docker stop`, Compose and managers
  send the container a signal first, and Docker reports that as a `kill`
  event just before the `die`. A crash has no `kill`. So a die that follows a
  kill is a stop, not a failure.
- A redeploy has a shape: containers destroyed and created within seconds.
  Around each, a settle window is kept - from shortly before to a few minutes
  after - and log lines stamped inside it do not count toward an error
  burst. Errors logged after the window count as they always did, so a
  deploy that actually broke something still opens an incident.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# The stop signal comes this long before the new container exists: the old
# one's stop timeout (Compose's default is 10s) plus pulling the image.
LEAD = timedelta(seconds=90)
# A kill this close before a die makes the die a requested stop.
KILL_BEFORE_DIE = timedelta(seconds=60)
# How long a requested stop is remembered, for the poll that later finds the
# container exited: long enough that a stopped container is not reported the
# next morning as having failed.
REMEMBER_STOPS = timedelta(hours=24)
# Windows older than this are forgotten; the log tail never reaches back further.
KEEP = timedelta(hours=2)


class Settling:
    def __init__(self, settle_minutes: float) -> None:
        self.settle = timedelta(minutes=max(settle_minutes, 0))
        self._windows: list[tuple[datetime, datetime]] = []
        self._stops: dict[str, datetime] = {}

    # -- what happened ------------------------------------------------------------

    def note_deploy(self, at: datetime) -> None:
        """A container was stopped on request, destroyed, created or replaced."""
        if self.settle <= timedelta(0):
            return
        start, end = at - LEAD, at + self.settle
        merged: list[tuple[datetime, datetime]] = []
        for window in sorted([*self._windows, (start, end)]):
            if merged and window[0] <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], window[1]))
            else:
                merged.append(window)
        cutoff = datetime.now(timezone.utc) - KEEP
        self._windows = [w for w in merged if w[1] >= cutoff]

    def note_stop(self, container_id: str, at: datetime) -> None:
        """Docker sent this container a signal: someone asked it to stop."""
        if container_id:
            self._stops[container_id] = at
        cutoff = datetime.now(timezone.utc) - REMEMBER_STOPS
        self._stops = {k: v for k, v in self._stops.items() if v >= cutoff}

    # -- what it means --------------------------------------------------------------

    def requested_stop(self, container_id: str, died_at: datetime | None = None) -> bool:
        """Whether this container's exit was asked for rather than a crash."""
        asked = self._stops.get(container_id or "")
        if asked is None:
            return False
        if died_at is None:
            return True  # found stopped by a poll, any time after it was asked
        return timedelta(seconds=-5) <= died_at - asked <= KILL_BEFORE_DIE

    def active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return any(start <= now <= end for start, end in self._windows)

    def windows(self) -> list[tuple[datetime, datetime]]:
        return list(self._windows)

    def strip(self, log_text: str) -> tuple[str, int]:
        """The log tail without the lines stamped inside a settle window.

        Lines without a readable Docker timestamp are kept: better an
        incident too many than a real error hidden.
        """
        if not self._windows or not log_text:
            return log_text, 0
        kept, dropped = [], 0
        for line in log_text.splitlines():
            stamp = _stamp(line)
            if stamp is not None and any(start <= stamp <= end for start, end in self._windows):
                dropped += 1
                continue
            kept.append(line)
        return "\n".join(kept), dropped


def _stamp(line: str) -> datetime | None:
    """The RFC 3339 timestamp Docker puts in front of each line (timestamps=True)."""
    head = line[:40].split(" ", 1)[0]
    if len(head) < 20 or head[4] != "-" or "T" not in head:
        return None
    text = head.rstrip("Z")
    if "." in text:
        base, _, frac = text.partition(".")
        text = f"{base}.{frac[:6]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def event_time(event: dict) -> datetime:
    """When Docker says an event happened, or now."""
    nano = event.get("timeNano")
    if nano:
        try:
            return datetime.fromtimestamp(int(nano) / 1e9, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    try:
        return datetime.fromtimestamp(int(event.get("time")), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc)
