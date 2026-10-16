"""Rule-based incident detection.

The LLM is expensive and non-deterministic, so it never decides *whether*
something is wrong - these rules do. The LLM is only asked to explain an
incident the rules already opened.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque

from .config import ContainerPolicy, Settings
from .forecast import human_days
from . import hostkind
from .redact import dominant_templates, error_lines, fingerprint, leveled_lines

log = logging.getLogger(__name__)

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class Signal:
    kind: str
    severity: str
    summary: str
    container: dict[str, Any]
    details: dict[str, Any] = field(default_factory=dict)
    # Extra strings folded into the fingerprint (e.g. the error lines).
    signature_parts: list[str] = field(default_factory=list)
    # Still true but nothing new: keeps an open incident open without counting
    # another sighting, so "seen N times" means something changed N times.
    unchanged: bool = False

    @property
    def fingerprint(self) -> str:
        return fingerprint(
            self.container.get("name", ""),
            self.container.get("image", ""),
            self.kind,
            *self.signature_parts,
        )


class Detector:
    """Stateful rule engine. One instance per process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._error_regex = settings.log_error_regex
        # container name -> consecutive polls over the CPU/memory threshold
        self._cpu_breaches: dict[str, int] = defaultdict(int)
        self._mem_breaches: dict[str, int] = defaultdict(int)
        self._restarting_polls: dict[str, int] = defaultdict(int)
        # container name -> timestamps of observed restarts
        self._restarts: dict[str, Deque[datetime]] = defaultdict(lambda: deque(maxlen=50))
        self._last_restart_count: dict[str, int] = {}
        self._unhealthy_polls: dict[str, int] = defaultdict(int)
        self._unhealthy_reported: set[str] = set()
        # Already-reported terminal states, so a container someone left stopped
        # doesn't page every 30 minutes forever. Cleared when it runs again.
        self._reported_exit: dict[str, int] = {}
        # Disk alerts are noisy; only re-raise once the level changes materially.
        self._last_disk_alert: dict[str, float] = {}
        # raid_errors fingerprint -> the summary it last fired with.
        self._raid_warned: dict[str, str] = {}
        # Host rule kind -> consecutive polls over / back under its threshold.
        self._host_over: dict[str, int] = defaultdict(int)
        self._host_under: dict[str, int] = defaultdict(int)
        # container name -> Docker's timestamp of the newest log line already
        # read. The tail is re-read every poll for an hour; without this one
        # FATAL line was a fresh sighting on every poll until it aged out, and
        # reopened an incident someone had just closed.
        self._log_mark: dict[str, str] = {}
        self._log_mark_dirty: set[str] = set()

    # -- what the log rules have already read ---------------------------------

    def load_log_marks(self, marks: dict[str, str]) -> None:
        """Marks kept from before a restart of the doctor, so its first poll
        does not read the last hour of every container as news."""
        self._log_mark.update(marks)

    def drain_log_marks(self) -> dict[str, str]:
        """The marks that moved since the last call, to be kept."""
        moved = {name: self._log_mark[name] for name in self._log_mark_dirty
                 if name in self._log_mark}
        self._log_mark_dirty.clear()
        return moved

    # -- docker events ------------------------------------------------------

    def from_event(self, event: dict[str, Any], container: dict[str, Any]) -> Signal | None:
        action = event.get("Action", "")
        attrs = (event.get("Actor") or {}).get("Attributes") or {}
        name = container.get("name") or attrs.get("name", "unknown")

        if action == "oom":
            return Signal(
                kind="oom_killed",
                severity="critical",
                summary=f"{name} was killed by the kernel OOM killer",
                container=container,
                details={"event": action},
            )

        if action.startswith("health_status") and "unhealthy" in action:
            return Signal(
                kind="unhealthy",
                severity="warning",
                summary=f"{name} reported an unhealthy health check",
                container=container,
                details={"event": action},
            )

        if action == "die":
            try:
                exit_code = int(attrs.get("exitCode", container.get("exit_code") or 0))
            except (TypeError, ValueError):
                exit_code = 0
            if exit_code == 0:
                return None  # clean shutdown - a person or a Docker manager did that
            if container.get("oom_killed"):
                kind, severity = "oom_killed", "critical"
                summary = f"{name} exited after being OOM-killed (exit {exit_code})"
            else:
                kind, severity = "exit_nonzero", "critical"
                summary = f"{name} exited with code {exit_code}"
            return Signal(
                kind=kind,
                severity=severity,
                summary=summary,
                container=container,
                details={"exit_code": exit_code, "event": action},
                signature_parts=[f"exit={exit_code}"],
            )

        return None

    # -- polling ------------------------------------------------------------

    def from_poll(
        self,
        container: dict[str, Any],
        stats: dict[str, Any] | None,
        log_text: str,
        *,
        settling: bool = False,
    ) -> list[Signal]:
        """`settling` is True during and just after a redeploy: a failing
        health check is then held back, not dropped - it is reported on the
        first poll after, if it is still failing."""
        signals: list[Signal] = []
        name = container["name"]
        state = container.get("state")

        signals.extend(self._check_restart_loop(container))

        if state == "restarting":
            self._restarting_polls[name] += 1
            if self._restarting_polls[name] == 2:
                signals.append(
                    Signal(
                        kind="stuck_restarting",
                        severity="critical",
                        summary=f"{name} is stuck in a restart cycle",
                        container=container,
                        details={"consecutive_polls": self._restarting_polls[name]},
                    )
                )
        else:
            self._restarting_polls.pop(name, None)

        signals.extend(self._check_health(container, settling))
        signals.extend(self._check_exited(container))

        if state == "running" and stats:
            signals.extend(self._check_resources(container, stats))
        else:
            self._cpu_breaches.pop(name, None)
            self._mem_breaches.pop(name, None)

        if state == "running" and log_text:
            signals.extend(self._check_logs(container, log_text))

        return signals

    def _check_health(self, container: dict[str, Any], settling: bool = False) -> list[Signal]:
        """Catch a container that is already unhealthy - e.g. it went bad while
        the doctor was down, or its `health_status` event was missed."""
        name = container["name"]
        if container.get("state") != "running" or container.get("health") != "unhealthy":
            self._unhealthy_polls.pop(name, None)
            self._unhealthy_reported.discard(name)
            return []
        self._unhealthy_polls[name] += 1
        # One poll of grace against flapping, then once per bout - held while
        # a redeploy settles, when half the stack is briefly unreachable.
        if self._unhealthy_polls[name] < 2 or settling or name in self._unhealthy_reported:
            return []
        self._unhealthy_reported.add(name)
        return [
            Signal(
                kind="unhealthy",
                severity="warning",
                summary=f"{name} has been failing its health check for two checks running",
                container=container,
                details={"source": "poll"},
            )
        ]

    def _check_exited(self, container: dict[str, Any]) -> list[Signal]:
        """Catch a container found stopped with a failure exit code."""
        name = container["name"]
        state = container.get("state")
        if state not in ("exited", "dead"):
            self._reported_exit.pop(name, None)
            return []
        try:
            exit_code = int(container.get("exit_code") or 0)
        except (TypeError, ValueError):
            exit_code = 0
        # 0 is a clean stop; 143 is SIGTERM, i.e. someone (or a Docker manager) asked.
        if exit_code in (0, 143):
            return []
        if self._reported_exit.get(name) == exit_code:
            return []
        self._reported_exit[name] = exit_code
        return [
            Signal(
                kind="exit_nonzero",
                severity="critical",
                summary=f"{name} is stopped after exiting with code {exit_code}",
                container=container,
                details={"exit_code": exit_code, "source": "poll", "state": state},
                signature_parts=[f"exit={exit_code}"],
            )
        ]

    def _check_restart_loop(self, container: dict[str, Any]) -> list[Signal]:
        name = container["name"]
        count = int(container.get("restart_count") or 0)
        previous = self._last_restart_count.get(name)
        self._last_restart_count[name] = count

        if previous is None or count <= previous:
            return []

        now = datetime.now(timezone.utc)
        for _ in range(min(count - previous, 10)):
            self._restarts[name].append(now)

        window = now - timedelta(minutes=self.settings.restart_loop_window_minutes)
        recent = [t for t in self._restarts[name] if t >= window]
        if len(recent) < self.settings.restart_loop_count:
            return []

        # Reset so one loop doesn't fire on every subsequent poll.
        self._restarts[name].clear()
        return [
            Signal(
                kind="restart_loop",
                severity="critical",
                summary=(
                    f"{name} restarted {len(recent)} times in the last "
                    f"{self.settings.restart_loop_window_minutes} minutes"
                ),
                container=container,
                details={"restarts_in_window": len(recent), "restart_count": count},
            )
        ]

    def _check_resources(self, container: dict[str, Any], stats: dict[str, Any]) -> list[Signal]:
        name = container["name"]
        signals: list[Signal] = []
        needed = self.settings.resource_breach_polls

        # Threshold against the share of the whole host, not of one core -
        # a container pinning a single core on an 8-core host is normal.
        host_share = stats.get("cpu_host_pct", stats["cpu_pct"])
        if host_share >= self.settings.cpu_threshold_pct:
            self._cpu_breaches[name] += 1
            if self._cpu_breaches[name] == needed:
                signals.append(
                    Signal(
                        kind="high_cpu",
                        severity="warning",
                        summary=(
                            f"{name} has held {host_share:.0f}% of the host's CPU "
                            f"({stats['cpu_pct']:.0f}% of one core) for "
                            f"{needed} consecutive checks"
                        ),
                        container=container,
                        details=dict(stats),
                    )
                )
        else:
            self._cpu_breaches[name] = 0

        # A memory percentage is only meaningful when a limit is actually set.
        if stats["mem_limit_bytes"] and stats["mem_pct"] >= self.settings.mem_threshold_pct:
            self._mem_breaches[name] += 1
            if self._mem_breaches[name] == needed:
                signals.append(
                    Signal(
                        kind="high_memory",
                        severity="warning",
                        summary=(
                            f"{name} is using {stats['mem_pct']:.0f}% of its memory limit "
                            f"({_human_bytes(stats['mem_usage_bytes'])} of "
                            f"{_human_bytes(stats['mem_limit_bytes'])})"
                        ),
                        container=container,
                        details=dict(stats),
                    )
                )
        else:
            self._mem_breaches[name] = 0

        return signals

    def _ignore_regex(self, container: dict[str, Any]):
        """Lines this container emits harmlessly, per `doctor.ignore_log` or
        the global pattern. Applied before counting, so silencing known noise
        does not also hide the count that should page you."""
        policy = ContainerPolicy.from_labels(container.get("labels") or {})
        return policy.ignore_log_regex or self.settings.log_ignore_regex

    def _check_logs(self, container: dict[str, Any], log_text: str) -> list[Signal]:
        """Two questions of the same log tail, answered separately.

        A single FATAL is worth an incident by itself; a burst of ERRORs is
        worth one only in volume. Severity comes from the log's own level
        marker where the app emits one, and from the keyword pattern where it
        does not - neither alone covers the apps a typical homelab runs.

        Only lines written since the last poll can fire a rule. The tail is a
        window of up to an hour, re-read every poll: a burst is still counted
        over the whole window, but it needs at least one new line to be a
        sighting, and a FATAL counts once, on the poll that first reads it.
        """
        name = container["name"]
        ignore = self._ignore_regex(container)
        mark = self._log_mark.get(name)
        fresh_text = _lines_after(log_text, mark)
        newest = newest_log_stamp(log_text)
        if newest and (mark is None or newest > mark):
            self._log_mark[name] = newest
            self._log_mark_dirty.add(name)
        if not fresh_text:
            return []

        counts, samples = leveled_lines(log_text, ignore=ignore)
        fresh_counts, fresh_samples = leveled_lines(fresh_text, ignore=ignore)
        signals: list[Signal] = []

        if fresh_counts["fatal"] >= self.settings.log_fatal_threshold:
            signals.append(
                Signal(
                    kind="log_fatal",
                    severity="critical",
                    summary=f"{name} logged a fatal error",
                    container=container,
                    details={
                        "fatal_lines": fresh_samples["fatal"],
                        "counts": counts,
                        "source": "level",
                    },
                    signature_parts=dominant_templates(fresh_samples["fatal"]),
                )
            )
            return signals  # a fatal supersedes the error-burst signal

        limit = self.settings.log_error_burst + 5
        keyword_hits = error_lines(log_text, self._error_regex, limit=limit)
        fresh_hits = error_lines(fresh_text, self._error_regex, limit=limit)
        if ignore is not None:
            keyword_hits = [line for line in keyword_hits if not ignore.search(line)]
            fresh_hits = [line for line in fresh_hits if not ignore.search(line)]

        by_keyword = len(keyword_hits) >= self.settings.log_error_burst and bool(fresh_hits)
        by_level = (counts["error"] >= self.settings.log_level_error_burst
                    and fresh_counts["error"] > 0)
        if not (by_keyword or by_level):
            return signals

        reason = "matching the error pattern" if by_keyword else "at ERROR level"
        shown = keyword_hits[:10] if by_keyword else samples["error"]
        hit_count = len(keyword_hits) if by_keyword else counts["error"]
        signals.append(
            Signal(
                kind="log_errors",
                severity="warning",
                summary=(
                    f"{name} logged {hit_count} lines {reason} "
                    f"in the last {self.settings.log_lines} lines"
                ),
                container=container,
                details={
                    "matched_lines": shown,
                    "match_count": hit_count,
                    "counts": counts,
                    "source": "keyword" if by_keyword else "level",
                },
                # The kind of line, not which lines: Jellyfin names a
                # different movie every scan, and that is still one failure.
                signature_parts=dominant_templates(
                    keyword_hits if by_keyword else samples["error"]),
            )
        )
        return signals

    # -- host disk ----------------------------------------------------------

    def check_disk_trend(self, usages: list[dict[str, Any]]) -> list[Signal]:
        """A disk that is filling, whatever percentage it is at.

        Separate from check_disk on purpose: a disk can be 60% full and gone
        by Friday, or 92% full and stable for a year. The threshold answers
        neither question.
        """
        signals: list[Signal] = []
        for usage in usages:
            forecast = usage.get("trend") or {}
            days = forecast.get("days_until_full")
            if days is None:
                continue
            if days > self.settings.disk_forecast_days:
                continue
            path = usage["path"]
            severity = ("critical" if days <= self.settings.disk_forecast_critical_days
                        else "warning")
            signals.append(
                Signal(
                    kind="disk_filling",
                    severity=severity,
                    summary=(
                        f"Host path {path} is filling at {forecast['rate']} "
                        f"- full in about {human_days(days)}"
                    ),
                    container={"id": "", "name": f"host:{path}", "image": "", "labels": {}},
                    details={
                        "path": path,
                        "used_pct": usage["used_pct"],
                        "free_bytes": usage["free_bytes"],
                        "bytes_per_day": round(forecast["bytes_per_day"]),
                        "days_until_full": round(days, 1),
                        "fitted_over_days": round(forecast["span_days"], 1),
                        "samples": forecast["samples"],
                    },
                    signature_parts=[path, "filling"],
                )
            )
        return signals

    def check_raid(self, arrays: list[dict[str, Any]],
                   drives: list[dict[str, Any]] | None = None) -> list[Signal]:
        """An md array missing a disk, failing one, or rebuilding onto one;
        and, short of that, a scrub that found mismatches or a drive md has had
        to correct reads from."""
        signals = [raid_signal(array) for array in arrays if not array["healthy"]]
        for array in arrays:
            for warning in array.get("warnings") or []:
                signal = raid_warning_signal(array, warning)
                # The same scrub's result read again is not another sighting.
                signal.unchanged = self._raid_warned.get(signal.fingerprint) == signal.summary
                self._raid_warned[signal.fingerprint] = signal.summary
                signals.append(signal)
        signals.extend(drive_signal(drive) for drive in drives or [] if drive.get("summary"))
        return signals

    def check_disk(self, usages: list[dict[str, Any]]) -> list[Signal]:
        signals: list[Signal] = []
        for usage in usages:
            path, pct = usage["path"], usage["used_pct"]
            if pct < self.settings.disk_threshold_pct:
                self._last_disk_alert.pop(path, None)
                continue
            previous = self._last_disk_alert.get(path)
            if previous is not None and pct < previous + 2.0:
                continue  # already alerted, and it hasn't got materially worse
            self._last_disk_alert[path] = pct
            signals.append(
                Signal(
                    kind="host_disk_pressure",
                    severity=("critical" if pct >= self.settings.disk_critical_pct
                              else "warning"),
                    summary=f"Host path {path} is {pct:.0f}% full "
                            f"({_human_bytes(usage['free_bytes'])} free)",
                    # Not tied to a single container - use a synthetic one.
                    container={"id": "", "name": f"host:{path}", "image": "", "labels": {}},
                    details=dict(usage),
                    signature_parts=[path],
                )
            )
        return signals


    # -- the host itself ------------------------------------------------------

    def host_conditions(self, reading: dict[str, Any]) -> dict[str, tuple[str, str, dict]]:
        """Each host threshold crossed right now: kind -> (severity, summary,
        details). Not yet a signal; check_host decides how long it must last."""
        s = self.settings
        out: dict[str, tuple[str, str, dict]] = {}
        who = hostkind.called(s.host_name, start=True)
        available = reading.get("mem_available_pct")
        if available is not None and available < s.host_mem_available_pct:
            out["host_memory"] = (
                "critical" if available < s.host_mem_available_pct / 2 else "warning",
                f"{who} has {available:.1f}% of its memory available "
                f"({_human_bytes(reading.get('mem_available') or 0)} of "
                f"{_human_bytes(reading.get('mem_total') or 0)})",
                {k: reading.get(k) for k in ("mem_total", "mem_available", "mem_available_pct",
                                             "swap_used_pct", "swap_in_per_s")},
            )
        swap_in = reading.get("swap_in_per_s")
        if swap_in is not None and swap_in >= s.host_swap_in_per_s:
            out["host_swap"] = (
                "warning",
                f"{who} is reading {swap_in:.0f} pages a second back from swap - "
                "it is short of memory and slowing everything down",
                {k: reading.get(k) for k in ("swap_in_per_s", "swap_out_per_s", "swap_used_pct",
                                             "mem_available_pct")},
            )
        per_core = reading.get("load_per_core")
        if per_core is not None and per_core >= s.host_load_per_core:
            load = reading.get("load") or []
            out["host_load"] = (
                "warning",
                f"{who}'s 5-minute load is {load[1]:.1f} on {reading.get('cores')} CPUs "
                f"({per_core:.1f} per CPU)",
                {k: reading.get(k) for k in ("load", "cores", "load_per_core", "cpu_pct",
                                             "iowait_pct")},
            )
        iowait = reading.get("iowait_pct")
        if iowait is not None and iowait >= s.host_iowait_pct:
            busiest = max(reading.get("disks") or [], key=lambda d: d["busy_pct"], default=None)
            disk = f" - busiest disk {busiest['name']} at {busiest['busy_pct']:.0f}%" if busiest else ""
            out["host_iowait"] = (
                "warning",
                f"{who}'s CPUs spend {iowait:.0f}% of their time waiting on disks{disk}",
                {"iowait_pct": iowait, "cpu_pct": reading.get("cpu_pct"),
                 "disks": reading.get("disks")},
            )
        sensors = reading.get("temperatures") or []
        if sensors:
            hot = max(sensors, key=lambda t: t["celsius"])
            if hot["celsius"] >= s.host_temp_c:
                critical = hot.get("critical") or s.host_temp_c + 10
                where = f"{hot['name']} {hot['label']}".strip()
                out["host_temperature"] = (
                    "critical" if hot["celsius"] >= critical else "warning",
                    f"{who}'s {where} is at {hot['celsius']:.0f} °C",
                    {"sensor": hot, "threshold_c": s.host_temp_c},
                )
        return out

    def check_host(self, reading: dict[str, Any]) -> tuple[list[Signal], list[str]]:
        """Signals for host conditions that have lasted, and the kinds that have
        been back to normal just as long - those incidents can close.

        A condition fires once, when it has lasted host_breach_polls; after
        that every poll it still holds keeps the incident open without counting
        another sighting, so a day of high load is one incident seen once.
        """
        needed = max(1, self.settings.host_breach_polls)
        now = self.host_conditions(reading)
        signals: list[Signal] = []
        cleared: list[str] = []
        for kind in HOST_KINDS:
            if kind in now:
                self._host_under[kind] = 0
                self._host_over[kind] += 1
                if self._host_over[kind] < needed:
                    continue
                severity, summary, details = now[kind]
                signals.append(Signal(
                    kind=kind, severity=severity, summary=summary,
                    container=dict(HOST_CONTAINER), details=details,
                    signature_parts=[kind],
                    unchanged=self._host_over[kind] > needed,
                ))
            else:
                self._host_over[kind] = 0
                self._host_under[kind] += 1
                if self._host_under[kind] == needed:
                    cleared.append(kind)
        return signals, cleared


# The rules about the host as a whole, and the synthetic container their
# incidents are filed under. Was host:nas; Store renames old rows.
HOST_KINDS = ("host_memory", "host_swap", "host_load", "host_iowait", "host_temperature")
HOST_ID = "host:system"
HOST_CONTAINER = {"id": "", "name": HOST_ID, "image": "", "labels": {}}


def raid_signal(array: dict[str, Any]) -> Signal:
    """One incident per array, however its state moves: a degraded array that
    starts rebuilding is the same problem getting better, not a new one."""
    return Signal(
        kind="raid_degraded",
        severity=array["severity"] or "critical",
        summary=array["summary"],
        container={"id": "", "name": f"host:{array['name']}", "image": "", "labels": {}},
        details={key: array.get(key) for key in (
            "name", "level", "state", "members", "failed", "spares", "slots", "active",
            "most_seen", "map", "operation", "progress", "finish_minutes",
        )},
        signature_parts=[array["name"]],
    )


def raid_warning_signal(array: dict[str, Any], warning: dict[str, str]) -> Signal:
    """One incident per array and disk (or per array's scrub): a count that
    keeps rising is the same disk failing, not a new problem each poll."""
    return Signal(
        kind="raid_errors",
        severity="warning",
        summary=warning["summary"],
        container={"id": "", "name": f"host:{array['name']}", "image": "", "labels": {}},
        details={
            **{key: array.get(key) for key in (
                "name", "level", "members", "mismatches", "last_sync", "member_errors")},
            "drives": [{k: d.get(k) for k in ("member", "label", "model", "errors")}
                       for d in array.get("drives") or []],
        },
        signature_parts=[array["name"], warning["key"]],
    )


def drive_signal(drive: dict[str, Any]) -> Signal:
    """One incident per physical drive, however many arrays it is in and
    however its count moves: more errors on the same disk is the same disk
    failing. Critical opens a fresh incident, as any escalation does."""
    return Signal(
        kind="raid_errors",
        severity=drive["severity"],
        summary=drive["summary"],
        container={"id": "", "name": f"host:{drive['label']}", "image": "", "labels": {}},
        details={key: drive.get(key) for key in (
            "disk", "label", "model", "errors", "by_array", "seen", "new", "recent", "reset")},
        signature_parts=["drive", drive["disk"]],
        unchanged=not drive.get("grew"),
    )


# Docker's own timestamp, which `logs(timestamps=True)` puts first on each
# line. RFC 3339 with up to nine fractional digits, trailing zeros dropped -
# so it is padded before two are compared as text.
_LOG_STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z\s")


def log_stamp(line: str) -> str | None:
    """A line's Docker timestamp, padded to compare as text, or None."""
    match = _LOG_STAMP.match(line)
    if not match:
        return None
    return f"{match.group(1)}.{(match.group(2) or '').ljust(9, '0')}Z"


def newest_log_stamp(log_text: str) -> str | None:
    stamps = [stamp for line in log_text.splitlines() if (stamp := log_stamp(line))]
    return max(stamps) if stamps else None


def _lines_after(log_text: str, mark: str | None) -> str:
    """The lines newer than `mark`. A line without a timestamp - a
    continuation, or a tail read without them - goes with the line before it;
    with no mark at all, every line is new."""
    if mark is None:
        return log_text
    kept: list[str] = []
    fresh = False
    for line in log_text.splitlines():
        stamp = log_stamp(line)
        if stamp is not None:
            fresh = stamp > mark
        if fresh:
            kept.append(line)
    return "\n".join(kept)


def _human_bytes(value: int | float) -> str:
    step = 1024.0
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < step:
            return f"{amount:.1f} {unit}"
        amount /= step
    return f"{amount:.1f} PiB"
