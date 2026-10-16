"""The host itself: CPU, memory, swap, load, disk activity, temperatures, uptime.

Everything here is read from files the kernel keeps system-wide, so a
container sees the host's numbers without any extra access: /proc/stat,
/proc/meminfo, /proc/loadavg, /proc/uptime, /proc/vmstat and /proc/diskstats
are not namespaced, and Docker mounts the host's /sys read-only already - the
same reason /proc/mdstat works for the RAID check.

Two things are left out because a container cannot see them: network traffic
(/proc/net/dev is the container's own namespace) and SMART data (raw disk
access). Pressure stall information and temperatures depend on the kernel and
the model; when a file is missing the reading is simply absent, never zero.
Pressure arrived in Linux 4.20, and DSM 7 still runs 4.4 on most models - so
the kernel's version is read too, to say which of the two it is.

Rates (CPU %, I/O wait, swap and disk throughput) need two readings, so the
first poll after a start has none of them.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Whole disks, not partitions, md arrays, device-mapper or loop devices. DSM
# names its drives sataN on newer models and sdX on older ones.
_DISK = re.compile(r"^(sd[a-z]+|sata\d+|nvme\d+n\d+|hd[a-z]+|vd[a-z]+|xvd[a-z]+)$")
# Kernel-reported sectors are always 512 bytes, whatever the drive's own size.
_SECTOR = 512
_PAGE = 4096
# A temperature outside this is a sensor reporting nonsense, not a reading.
_PLAUSIBLE_C = (1.0, 150.0)
_MAX_SENSORS = 16


# -- parsers: text in, numbers out ---------------------------------------------

def parse_stat(text: str) -> tuple[dict[str, int], int]:
    """The aggregate CPU counters and the number of CPUs."""
    totals: dict[str, int] = {}
    cores = 0
    names = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "cpu":
            values = [int(v) for v in parts[1:1 + len(names)]]
            totals = dict(zip(names, values + [0] * (len(names) - len(values))))
        elif re.fullmatch(r"cpu\d+", parts[0]):
            cores += 1
    return totals, cores


def cpu_usage(before: dict[str, int], after: dict[str, int]) -> tuple[float, float] | None:
    """Busy % and I/O-wait % of all CPUs between two readings."""
    if not before or not after:
        return None
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    total = sum(delta.values())
    if total <= 0:
        return None
    idle = delta.get("idle", 0) + delta.get("iowait", 0)
    return (round(100.0 * (total - idle) / total, 1),
            round(100.0 * delta.get("iowait", 0) / total, 1))


def parse_meminfo(text: str) -> dict[str, int]:
    """Fields in bytes. MemAvailable is the kernel's own estimate of what a new
    program could use without swapping - free memory alone undercounts it,
    because Linux keeps otherwise idle memory as cache."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        out[key.strip()] = value * 1024 if len(parts) > 1 and parts[1] == "kB" else value
    return out


def parse_loadavg(text: str) -> list[float]:
    try:
        return [float(v) for v in text.split()[:3]]
    except ValueError:
        return []


def parse_uptime(text: str) -> float | None:
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return None


def parse_vmstat(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("pswpin", "pswpout"):
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def parse_diskstats(text: str) -> dict[str, dict[str, int]]:
    """Sectors read and written, and milliseconds spent doing I/O, per disk."""
    out: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 14 or not _DISK.match(parts[2]):
            continue
        try:
            out[parts[2]] = {"read": int(parts[5]), "written": int(parts[9]),
                             "busy_ms": int(parts[12])}
        except ValueError:
            continue
    return out


def disk_rates(before: dict[str, dict[str, int]], after: dict[str, dict[str, int]],
               seconds: float) -> list[dict[str, Any]]:
    """How busy each disk was between two readings: the share of the time it
    had I/O in flight, and its throughput."""
    if seconds <= 0:
        return []
    out = []
    for name in sorted(after):
        if name not in before:
            continue
        a, b = after[name], before[name]
        busy = max(0, a["busy_ms"] - b["busy_ms"]) / (seconds * 1000) * 100
        out.append({
            "name": name,
            "busy_pct": round(min(100.0, busy), 1),
            "read_bps": round(max(0, a["read"] - b["read"]) * _SECTOR / seconds),
            "write_bps": round(max(0, a["written"] - b["written"]) * _SECTOR / seconds),
        })
    return out


def parse_pressure(text: str) -> dict[str, float]:
    """`some` and `full` avg60 from a /proc/pressure file: the share of the last
    minute something (some) or everything (full) waited on that resource."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        try:
            out[parts[0]] = float(fields["avg60"])
        except (KeyError, ValueError):
            continue
    return out


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_temperatures(sysfs: str) -> list[dict[str, Any]]:
    """Every temperature sensor the kernel exposes: hwmon chips first (they
    name themselves, and carry a critical point), then thermal zones."""
    root = Path(sysfs)
    out: list[dict[str, Any]] = []
    for chip in sorted((root / "class" / "hwmon").glob("hwmon*")):
        name = (_read(chip / "name") or chip.name).strip()
        for sensor in sorted(chip.glob("temp*_input")):
            celsius = _millidegrees(_read(sensor))
            if celsius is None:
                continue
            prefix = sensor.name.removesuffix("_input")
            label = (_read(chip / f"{prefix}_label") or "").strip()
            out.append({"name": name, "label": label or prefix,
                        "celsius": celsius,
                        "critical": _millidegrees(_read(chip / f"{prefix}_crit"))})
    for zone in sorted((root / "class" / "thermal").glob("thermal_zone*")):
        celsius = _millidegrees(_read(zone / "temp"))
        if celsius is None:
            continue
        out.append({"name": (_read(zone / "type") or zone.name).strip(), "label": zone.name,
                    "celsius": celsius, "critical": None})
    return out[:_MAX_SENSORS]


def _millidegrees(text: str | None) -> float | None:
    try:
        celsius = int((text or "").strip()) / 1000
    except ValueError:
        return None
    low, high = _PLAUSIBLE_C
    return round(celsius, 1) if low <= celsius <= high else None


# -- the reader -----------------------------------------------------------------

class HostReader:
    """Reads the host on every poll and keeps the last counters for rates."""

    def __init__(self, proc: str = "/proc", sysfs: str = "/sys") -> None:
        self.proc = Path(proc)
        self.sysfs = sysfs
        self._previous: tuple[float, dict[str, int], dict[str, int],
                              dict[str, dict[str, int]]] | None = None

    def read(self) -> dict[str, Any] | None:
        """One reading, or None when /proc is not the kernel's (a test box)."""
        stat_text = _read(self.proc / "stat")
        meminfo_text = _read(self.proc / "meminfo")
        if stat_text is None or meminfo_text is None:
            return None
        now = time.monotonic()
        cpu, cores = parse_stat(stat_text)
        vmstat = parse_vmstat(_read(self.proc / "vmstat") or "")
        disks = parse_diskstats(_read(self.proc / "diskstats") or "")
        mem = parse_meminfo(meminfo_text)
        load = parse_loadavg(_read(self.proc / "loadavg") or "")
        uptime = parse_uptime(_read(self.proc / "uptime") or "")
        cores = cores or os.cpu_count() or 1

        reading: dict[str, Any] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cores": cores,
            "cpu_pct": None,
            "iowait_pct": None,
            "load": load,
            "load_per_core": round(load[1] / cores, 2) if len(load) > 1 else None,
            "swap_in_per_s": None,
            "swap_out_per_s": None,
            "disks": [],
        }
        reading.update(memory_figures(mem))
        if uptime is not None:
            booted = datetime.now(timezone.utc) - timedelta(seconds=uptime)
            reading["uptime_seconds"] = int(uptime)
            reading["boot_time"] = booted.isoformat(timespec="seconds")

        if self._previous is not None:
            then, cpu_before, vm_before, disks_before = self._previous
            seconds = now - then
            usage = cpu_usage(cpu_before, cpu)
            if usage is not None:
                reading["cpu_pct"], reading["iowait_pct"] = usage
            if seconds > 0 and vmstat and vm_before:
                reading["swap_in_per_s"] = round(
                    max(0, vmstat.get("pswpin", 0) - vm_before.get("pswpin", 0)) / seconds, 1)
                reading["swap_out_per_s"] = round(
                    max(0, vmstat.get("pswpout", 0) - vm_before.get("pswpout", 0)) / seconds, 1)
            reading["disks"] = disk_rates(disks_before, disks, seconds)
        self._previous = (now, cpu, vmstat, disks)

        pressure = {}
        for resource in ("cpu", "memory", "io"):
            text = _read(self.proc / "pressure" / resource)
            if text:
                pressure[resource] = parse_pressure(text)
        reading["pressure"] = pressure or None
        reading["kernel"] = (_read(self.proc / "sys" / "kernel" / "osrelease") or "").strip() or None
        reading["temperatures"] = read_temperatures(self.sysfs) if self.sysfs else []
        return reading


def memory_figures(mem: dict[str, int]) -> dict[str, Any]:
    total = mem.get("MemTotal", 0)
    # Kernels before 3.14 lack MemAvailable; free + cache is the old estimate.
    available = mem.get("MemAvailable",
                        mem.get("MemFree", 0) + mem.get("Cached", 0) + mem.get("Buffers", 0))
    swap_total = mem.get("SwapTotal", 0)
    swap_used = max(0, swap_total - mem.get("SwapFree", 0))
    return {
        "mem_total": total,
        "mem_available": available,
        "mem_available_pct": round(100.0 * available / total, 1) if total else None,
        "mem_used_pct": round(100.0 * (total - available) / total, 1) if total else None,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_used_pct": round(100.0 * swap_used / swap_total, 1) if swap_total else None,
    }


def hottest(reading: dict[str, Any]) -> dict[str, Any] | None:
    sensors = reading.get("temperatures") or []
    return max(sensors, key=lambda s: s["celsius"]) if sensors else None


def compact(reading: dict[str, Any] | None) -> dict[str, Any] | None:
    """The few figures worth giving a diagnosis: what state the host as a whole
    was in when something failed on it."""
    if not reading:
        return None
    hot = hottest(reading)
    busiest = max(reading.get("disks") or [], key=lambda d: d["busy_pct"], default=None)
    return {
        "cpu_pct": reading.get("cpu_pct"),
        "iowait_pct": reading.get("iowait_pct"),
        "load": reading.get("load"),
        "cores": reading.get("cores"),
        "mem_available_pct": reading.get("mem_available_pct"),
        "mem_available_bytes": reading.get("mem_available"),
        "swap_used_pct": reading.get("swap_used_pct"),
        "swap_in_per_s": reading.get("swap_in_per_s"),
        "pressure": reading.get("pressure"),
        "hottest_sensor": hot,
        "busiest_disk": busiest,
        "uptime_seconds": reading.get("uptime_seconds"),
    }


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
