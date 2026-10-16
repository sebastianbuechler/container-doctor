"""The state of the host's disk arrays, read from /proc/mdstat.

A degraded array is the one failure on a storage host that loses data, and nothing the
doctor watched could see it: every container keeps running on one disk as
happily as on two, right up to the moment the second one goes too.

/proc/mdstat is the kernel's own account of every md array, readable from
inside a container without a mount or a privilege - /proc is the host's here,
and this file is not namespaced. Linux software RAID is md arrays, and so is
every DSM volume, SHR included - this is what Storage Manager reads too.

What makes an array degraded:

  - a member marked (F) - the kernel has failed it out
  - a rebuild or recovery running - it is degraded until that finishes
  - fewer members than slots, as `[2/1] [U_]`

except, on a Synology, for md0 and md1. DSM keeps its own system and swap
partitions as RAID 1 arrays sized for every bay the model could have, so a
two-disk four-bay NAS shows `[4/2] [UU__]` there permanently. On any other
host md0 is an ordinary array, often the data one, and is judged like any
other (see hostkind). For those the question is
whether a member has gone missing, answered against the most members the
doctor has seen them with.

A scrub (`check`) or a resync of an array with every member present is
routine, and is reported without being called a problem.

/proc/mdstat says whether an array is whole, not how it fits together. For
that the doctor reads /sys, which Docker mounts read-only in every container
and whose block devices are the host's:

  - /sys/block/mdN/holders and slaves: the LVM volumes built on each array.
    SHR on mixed drive sizes is several md arrays joined into one volume, so
    this is what tells "two raid5 arrays" apart from "one volume, two layers".
  - /sys/block/mdN/md/mismatch_cnt: what the last scrub found inconsistent.
  - /sys/block/mdN/md/dev-*/errors: read errors md corrected on each member,
    the first sign of a failing disk and one the array never reports itself.
    Added up per drive (drives()), since on SHR one disk is in several arrays,
    and remembered by the doctor, since md restarts the count on every boot.
  - /sys/block/sataN: the drive behind each member, its size and model.

All of it is optional: without /sys the panel shows what mdstat alone says.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

# DSM's system (md0) and swap (md1) arrays: RAID 1 across every bay. Only on
# a Synology - read() and parse() take the set, empty elsewhere.
SYSTEM_ARRAYS = frozenset({"md0", "md1"})

_BLOCKS = re.compile(r"^\s*(?P<blocks>\d+) blocks\b")
_HEADER = re.compile(r"^(?P<name>md\w+)\s*:\s*(?P<rest>.*)$")
_MEMBER = re.compile(r"(?P<device>[\w.-]+)\[(?P<slot>\d+)\](?P<flags>(?:\([A-Z]\))*)")
_STATUS = re.compile(r"\[(?P<slots>\d+)/(?P<active>\d+)\]\s*\[(?P<map>[U_]+)\]")
_PROGRESS = re.compile(
    r"(?P<op>recovery|resync|reshape|check|repair)\s*=\s*(?P<pct>[\d.]+)%"
    r"(?:.*?finish=(?P<finish>[\d.]+)min)?"
)
_DELAYED = re.compile(r"(?P<op>recovery|resync|reshape|check)\s*=\s*(?P<state>DELAYED|PENDING)")


@dataclass
class Array:
    name: str
    state: str  # active | inactive | ...
    level: str
    members: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    spares: list[str] = field(default_factory=list)
    slots: int = 0
    active: int = 0
    map: str = ""
    operation: str = ""  # recovery | resync | reshape | check | repair
    progress: float | None = None
    finish_minutes: float | None = None
    size_bytes: int = 0
    # From /sys, when it can be read: see inspect().
    mismatches: int | None = None
    last_sync: str = ""
    member_errors: dict[str, int] = field(default_factory=dict)
    drives: list[dict[str, Any]] = field(default_factory=list)
    # The names that are the host's own system arrays, not data.
    system_arrays: frozenset[str] = SYSTEM_ARRAYS

    @property
    def system(self) -> bool:
        return self.name in self.system_arrays

    @property
    def rebuilding(self) -> bool:
        return self.operation in ("recovery", "reshape")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "level": self.level,
            "members": self.members,
            "failed": self.failed,
            "spares": self.spares,
            "slots": self.slots,
            "active": self.active,
            "map": self.map,
            "operation": self.operation,
            "progress": self.progress,
            "finish_minutes": self.finish_minutes,
            "size_bytes": self.size_bytes,
            "mismatches": self.mismatches,
            "last_sync": self.last_sync,
            "member_errors": self.member_errors,
            "drives": self.drives,
            "system": self.system,
        }


def parse(text: str, system_arrays: frozenset[str] = SYSTEM_ARRAYS) -> list[Array]:
    """Every array in an mdstat file. Unknown lines are skipped, not fatal."""
    arrays: list[Array] = []
    current: Array | None = None
    for line in (text or "").splitlines():
        header = _HEADER.match(line.strip())
        if header:
            words = header["rest"].split()
            state = words[0] if words else "unknown"
            # "active raid1 sda1[0]..." or "active (auto-read-only) raid1 ..."
            level = next((w for w in words[1:] if w.startswith("raid") or w == "linear"), "")
            current = Array(name=header["name"], state=state, level=level,
                            system_arrays=system_arrays)
            for member in _MEMBER.finditer(header["rest"]):
                flags = member["flags"]
                if "(F)" in flags:
                    current.failed.append(member["device"])
                elif "(S)" in flags:
                    current.spares.append(member["device"])
                else:
                    current.members.append(member["device"])
            arrays.append(current)
            continue
        if current is None:
            continue
        blocks = _BLOCKS.match(line)
        if blocks:
            current.size_bytes = int(blocks["blocks"]) * 1024  # 1 KiB blocks
        status = _STATUS.search(line)
        if status:
            current.slots = int(status["slots"])
            current.active = int(status["active"])
            current.map = status["map"]
            continue
        progress = _PROGRESS.search(line)
        if progress:
            current.operation = progress["op"]
            current.progress = float(progress["pct"])
            if progress["finish"]:
                current.finish_minutes = float(progress["finish"])
            continue
        delayed = _DELAYED.search(line)
        if delayed:
            current.operation = delayed["op"]
            current.progress = None
        if not line.strip():
            current = None
    return arrays


@dataclass(frozen=True)
class Verdict:
    healthy: bool
    severity: str  # "" when healthy
    summary: str


def judge(array: Array, most_seen: int) -> Verdict:
    """Is this array missing a disk? `most_seen` is its best member count so far."""
    name = array.name
    if array.state == "inactive":
        return Verdict(False, "critical", f"Disk array {name} is inactive")
    if array.failed:
        return Verdict(False, "critical",
                       f"Disk array {name} has failed "
                       f"{'member' if len(array.failed) == 1 else 'members'} "
                       f"{', '.join(array.failed)}")
    if array.rebuilding:
        if array.progress is None:
            detail = "queued behind another array"
        else:
            detail = f"{array.progress:.1f}% done"
            if array.finish_minutes is not None:
                detail += f", about {_duration(array.finish_minutes)} to go"
        # A warning, not critical: someone has already replaced the disk. It
        # is still one failure away from data loss until this finishes.
        return Verdict(False, "warning", f"Disk array {name} is rebuilding ({detail})")
    if array.system:
        if most_seen and array.active < most_seen:
            return Verdict(False, "critical",
                           f"Disk array {name} is down to {array.active} of the "
                           f"{most_seen} disks it had")
        return Verdict(True, "", "")
    if array.slots and array.active < array.slots:
        return Verdict(False, "critical",
                       f"Disk array {name} is degraded: {array.active} of "
                       f"{array.slots} disks ({array.map})")
    return Verdict(True, "", "")


def _duration(minutes: float) -> str:
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} days"


def read(path: str, system_arrays: frozenset[str] = SYSTEM_ARRAYS) -> tuple[list[Array], str]:
    """(arrays, "") or ([], reason). No file is a reason, not an error: a host
    without software RAID has no md arrays to report."""
    if not path:
        return [], "off"
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except FileNotFoundError:
        return [], f"{path} does not exist on this host"
    except OSError as error:
        return [], f"cannot read {path} ({error.__class__.__name__})"
    return parse(text, system_arrays), ""


# --- What /sys adds -----------------------------------------------------------

_VOLUME = re.compile(r"(?:^|-)volume_?(?P<n>\d+)$")
_POOL = re.compile(r"^vg(?P<n>\d+)-")
_BAY = re.compile(r"^(?:sata|sas)(?P<n>\d+)$")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _natural(name: str) -> tuple[str, int]:
    match = re.match(r"^(\D*)(\d*)", name)
    head, digits = (match[1], match[2]) if match else (name, "")
    return head, int(digits) if digits else -1


class _Sys:
    """The parts of /sys/block the panel reads, each looked up once."""

    def __init__(self, root: str) -> None:
        self.block = os.path.join(root, "block")
        self._disks = _listdir(self.block)
        self._by_dev: dict[str, str] | None = None

    def available(self) -> bool:
        return bool(self._disks)

    def read(self, *parts: str) -> str:
        return _read(os.path.join(self.block, *parts))

    def entries(self, *parts: str) -> list[str]:
        return _listdir(os.path.join(self.block, *parts))

    def disk_of(self, member: str) -> str:
        """sata1p5 -> sata1: the disk whose directory holds the partition."""
        if member in self._disks:
            return member
        for disk in self._disks:
            if os.path.isdir(os.path.join(self.block, disk, member)):
                return disk
        return ""

    def name(self, device: str) -> str:
        """An LVM or device-mapper name where there is one: vg1-volume_1."""
        return self.read(device, "dm", "name") or device

    def by_dev(self, major_minor: str) -> str:
        if self._by_dev is None:
            self._by_dev = {self.read(d, "dev"): d for d in self._disks}
        return self._by_dev.get(major_minor, "")

    def by_name(self, name: str) -> str:
        if name in self._disks:
            return name
        return next((d for d in self._disks if self.read(d, "dm", "name") == name), "")

    def tops(self, device: str, depth: int = 0) -> list[str]:
        """The devices at the top of everything built on this one."""
        holders = self.entries(device, "holders")
        if not holders or depth > 8:
            return [device]
        out: list[str] = []
        for holder in holders:
            out.extend(t for t in self.tops(holder, depth + 1) if t not in out)
        return out

    def chain(self, device: str, depth: int = 0) -> list[str]:
        """This device and everything under it, down to the md arrays."""
        out = [device]
        if depth > 8:
            return out
        for slave in self.entries(device, "slaves"):
            out.extend(d for d in self.chain(slave, depth + 1) if d not in out)
        return out

    def size(self, device: str) -> int:
        return (_int(self.read(device, "size")) or 0) * 512  # always 512-byte sectors


def _mounts(mountinfo_path: str) -> list[tuple[str, str, str]]:
    """(mount point, major:minor, source) for every mount this process sees."""
    out: list[tuple[str, str, str]] = []
    for line in _read(mountinfo_path).splitlines():
        head, _, tail = line.partition(" - ")
        fields, rest = head.split(), tail.split()
        if len(fields) >= 5 and len(rest) >= 2:
            point = fields[4].replace("\\040", " ")
            out.append((point, fields[2], rest[1]))
    return out


def _mount_device(path: str, mounts: list[tuple[str, str, str]], sysfs: _Sys) -> str:
    """The block device a path lives on. A bind mount carries its source's
    device, so the doctor's /data names the host's volume."""
    best = None
    for point, major_minor, source in mounts:
        inside = path == point or path.startswith(point.rstrip("/") + "/")
        if inside and (best is None or len(point) > len(best[0])):
            best = (point, major_minor, source)
    if best is None:
        return ""
    _, major_minor, source = best
    # btrfs gives every subvolume an anonymous 0:N device; the source is real.
    if not major_minor.startswith("0:"):
        device = sysfs.by_dev(major_minor)
        if device:
            return device
    if source.startswith("/dev/mapper/"):
        return sysfs.by_name(source[len("/dev/mapper/"):])
    if source.startswith("/dev/"):
        return sysfs.by_name(source[len("/dev/"):])
    return ""


def _volume_label(chain_names: list[str], fallback: str) -> str:
    for name in chain_names:
        match = _VOLUME.search(name)
        if match:
            return f"Volume {match['n']}"
    return fallback


def inspect(arrays: list[Array], sys_root: str = "/sys",
            mountinfo_path: str = "/proc/self/mountinfo",
            paths: list[str] | tuple[str, ...] = (),
            synology: bool = True) -> list[dict[str, Any]]:
    """Fill in what /sys knows about each array, and group the data arrays
    into the storage pools they make up. [] when /sys cannot be read."""
    if not sys_root:
        return []
    sysfs = _Sys(sys_root)
    if not sysfs.available():
        return []
    for array in arrays:
        if array.state == "inactive":
            continue
        md = os.path.join(array.name, "md")
        array.mismatches = _int(sysfs.read(md, "mismatch_cnt"))
        array.last_sync = sysfs.read(md, "last_sync_action")
        for member in array.members + array.failed + array.spares:
            errors = _int(sysfs.read(md, f"dev-{member}", "errors"))
            if errors is not None:
                array.member_errors[member] = errors
            disk = sysfs.disk_of(member)
            bay = _BAY.match(disk or "")
            array.drives.append({
                "member": member,
                "disk": disk,
                "label": f"Drive {bay['n']}" if bay else (disk or member),
                "size_bytes": sysfs.size(disk) if disk else 0,
                "model": sysfs.read(disk, "device", "model") if disk else "",
                "errors": errors,
                "failed": member in array.failed,
                "spare": member in array.spares,
            })
        array.drives.sort(key=lambda d: _natural(d["disk"] or d["member"]))

    # Arrays that share anything built on top of them are one storage pool.
    data = [a for a in arrays if not a.system and a.state != "inactive"]
    tops = {a.name: [t for t in sysfs.tops(a.name)
                     if "reserved" not in sysfs.name(t)] for a in data}
    pools: list[dict[str, Any]] = []
    for array in sorted(data, key=lambda a: _natural(a.name)):
        joined = [p for p in pools if set(p["_tops"]) & set(tops[array.name])]
        pool = joined[0] if joined else {"_tops": [], "arrays": []}
        for other in joined[1:]:
            pool["_tops"] += [t for t in other["_tops"] if t not in pool["_tops"]]
            pool["arrays"] += other["arrays"]
            pools.remove(other)
        if not joined:
            pools.append(pool)
        pool["arrays"].append(array.name)
        pool["_tops"] += [t for t in tops[array.name] if t not in pool["_tops"]]

    mounts = _mounts(mountinfo_path) if paths else []
    on: dict[str, list[str]] = {}
    for path in paths:
        device = _mount_device(path, mounts, sysfs)
        for top in sysfs.tops(device) if device else []:
            on.setdefault(top, []).append(path)

    sizes = {a.name: a.size_bytes for a in arrays}
    out: list[dict[str, Any]] = []
    for pool in pools:
        pool["arrays"].sort(key=_natural)
        volumes = []
        pool_name = ""
        for top in pool["_tops"]:
            names = [sysfs.name(d) for d in sysfs.chain(top)]
            # DSM names its volume groups vgN after Storage Pool N; DSM 6 named
            # its SHR group vg1000, which is no pool number. Elsewhere vg0 is
            # just a volume group, and the pool keeps its default name.
            pool_name = pool_name or next(
                (f"Storage Pool {m['n']}" for m in map(_POOL.match, names)
                 if synology and m and m["n"] != "1000"), "")
            volumes.append({
                "name": _volume_label(names, sysfs.name(top)),
                "device": sysfs.name(top),
                "size_bytes": sysfs.size(top),
                "paths": on.get(top, []),
            })
        volumes.sort(key=lambda v: _natural(v["name"]))
        out.append({
            "pool": pool_name,
            "arrays": pool["arrays"],
            "size_bytes": sum(sizes.get(name, 0) for name in pool["arrays"]),
            "volumes": volumes,
        })
    return out


def warnings(array: Array) -> list[tuple[str, str]]:
    """(key, sentence) for what is wrong with the array itself short of
    degraded: a scrub that found the copies disagreeing. Read errors belong to
    a disk, not an array, and are counted per drive by drives()."""
    out: list[tuple[str, str]] = []
    # md0 and md1 are left out: swap on RAID 1 shows mismatches as a matter
    # of course, since a page can be dropped between writing its two copies.
    if (not array.system and array.mismatches and array.last_sync == "check"
            and not array.operation):
        out.append(("mismatch",
                    f"The last scrub of {array.name} found {array.mismatches} "
                    f"sectors that disagree between disks"))
    return out


# The window "is it getting worse" is judged over.
ERRORS_RECENT_HOURS = 24


def drives(arrays: list[Array]) -> list[dict[str, Any]]:
    """One entry per physical drive, its read errors added up over every array
    it is in. On SHR a drive is a member of every layer it has room for (and
    of md0 and md1), so per array the same failing disk shows up several times."""
    out: dict[str, dict[str, Any]] = {}
    for array in arrays:
        for drive in array.drives:
            if drive["errors"] is None:
                continue
            key = drive["disk"] or drive["member"]
            entry = out.setdefault(key, {
                "disk": key,
                "label": drive["label"],
                "model": drive["model"],
                "size_bytes": drive["size_bytes"],
                "errors": 0,
                "by_array": {},
            })
            entry["errors"] += drive["errors"]
            if drive["errors"]:
                entry["by_array"][array.name] = drive["errors"]
    return sorted(out.values(), key=lambda d: _natural(d["disk"]))


def error_history(samples: list[tuple[Any, int]], current: int, recent_since: Any,
                  window_since: Any) -> dict[str, Any]:
    """What a drive's past readings say beside today's count.

    `samples` are (time, count) oldest first, reaching back a little before
    `window_since`. A count lower than the one before it is md starting again
    from 0, not the disk healing: what it read after the reset is new on top
    of what it had before.

      seen:   errors over all the samples, resets included
      new:    errors since `window_since`, measured from the last reading
              before it. With none - the doctor has not watched the drive
              that long - everything it has is new, so a drive first seen
              with errors still warns for one window.
      recent: errors since `recent_since`, measured the same way but from
              the first reading when there is none before it, so errors a
              disk had before the doctor watched it are not called new.
      reset:  whether the count went back down in the meantime
    """
    values = [count for _, count in samples] + [current]
    seen, reset = values[0], False
    for before, after in zip(values, values[1:]):
        if after < before:
            reset = True
            seen += after
        else:
            seen += after - before

    def since(when: Any, unwatched_is_new: bool) -> int:
        start = None
        for index, (at, _) in enumerate(samples):
            if at < when:
                start = index
        if start is None:
            if unwatched_is_new:
                return seen
            start = 0
        total = 0
        for before, after in zip(values[start:], values[start + 1:]):
            total += after if after < before else after - before
        return total

    return {"seen": seen, "new": since(window_since, True), "recent": since(recent_since, False),
            "reset": reset}


def drive_warning(drive: dict[str, Any], per_day: int, days: int) -> tuple[str, str] | None:
    """(severity, sentence) for a drive md has had to correct reads from in
    the last `days`, or None. Any new corrected error is worth a warning: a
    healthy disk has none. Errors older than that, with none since, are
    history - md's count only goes back to 0 on a reboot, so it cannot be
    what clears the warning. It turns critical when they keep coming,
    `per_day` or more in a day."""
    current = drive["errors"]
    new = drive.get("new", current)
    recent = drive.get("recent") or 0
    if not new:
        return None
    label = drive["label"]
    arrays = sorted(drive["by_array"].items(), key=lambda item: _natural(item[0]))
    if len(arrays) == 1:
        where = f" in {arrays[0][0]}"
    else:
        where = " (" + ", ".join(f"{n} in {name}" for name, n in arrays) + ")" if arrays else ""
    if new == current:
        text = f"{label} has had {_errors(current)} corrected{where}"
    else:
        # Some are older than the window, or md's count restarted since.
        text = (f"{label} has had {_errors(new)} corrected in the last {days} days, "
                f"{current} since its arrays were last assembled{where}")
    if recent:
        text += f", {recent} of them in the last {ERRORS_RECENT_HOURS} hours"
    severity = "critical" if per_day > 0 and recent >= per_day else "warning"
    return severity, text


def _errors(n: int) -> str:
    return f"{n} read error{'s' if n != 1 else ''}"
