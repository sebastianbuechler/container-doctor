"""What kind of host the doctor runs on: a Synology NAS, or any other Linux.

Almost nothing depends on it - the doctor reads the same /proc, /sys and
Docker on any Linux. What does is DSM's own layout: it keeps its system and
swap partitions as md0 and md1, RAID 1 across every bay the model could have,
so those two always show empty slots and must not be called degraded; and it
names drives, pools and volumes its own way.

DSM's kernel says so itself: /proc/sys/kernel/syno_hw_version holds the model
(DS920+, for one), and /proc is the host's inside a container. Nothing to
mount, nothing to configure - DOCTOR_HOST_KIND overrides the guess.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SYNOLOGY = "synology"
GENERIC = "generic"


@dataclass(frozen=True)
class Host:
    kind: str       # synology | generic
    model: str      # DS920+, or "" when unknown
    detected: bool  # False when DOCTOR_HOST_KIND decided it

    @property
    def synology(self) -> bool:
        return self.kind == SYNOLOGY


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def detect(proc_path: str = "/proc") -> Host:
    """The host as its kernel describes it."""
    proc = proc_path or "/proc"
    model = _read(os.path.join(proc, "sys", "kernel", "syno_hw_version"))
    if model or "synology" in _read(os.path.join(proc, "version")).lower():
        return Host(SYNOLOGY, model, True)
    return Host(GENERIC, "", True)


def resolve(setting: str, found: Host) -> Host:
    """DOCTOR_HOST_KIND applied to what detect() found: auto keeps it,
    synology or generic is taken as said."""
    if setting in (SYNOLOGY, GENERIC) and setting != found.kind:
        return Host(setting, found.model if setting == SYNOLOGY else "", False)
    return found


def describe(host: Host, name: str = "") -> str:
    """The host in a phrase for a model's instructions: "a Synology NAS
    (DS920+) called tower", or "a Linux host" when nothing is known."""
    what = "a Synology NAS" if host.synology else "a Linux host"
    if host.model:
        what += f" ({host.model})"
    if name:
        what += f" called {name}"
    return what


def called(name: str = "", *, start: bool = False) -> str:
    """What to call the host in a sentence: its DOCTOR_HOST_NAME, or "the
    host" - "The host" at the start of one."""
    if name:
        return name
    return "The host" if start else "the host"
