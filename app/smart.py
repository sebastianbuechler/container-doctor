"""Each drive's own health, as SMART reports it.

Reading SMART needs raw access to the disks, which the doctor deliberately
does not have. A small sidecar does it: Prometheus's smartctl_exporter, run
with just enough to talk to the drives, on the doctor's internal network
only. The doctor reads its /metrics page over HTTP and does the thinking
here - which counters matter, what is a warning and what is critical.

What is read, per drive:

- the overall verdict (smart_status), which is what a drive says about itself
  once a threshold is crossed - late, but never to be ignored;
- the counters that move first on a failing hard disk: reallocated, pending
  and uncorrectable sectors, and reported uncorrectable errors. Any of them
  above zero is worth a warning; growing is worth a fresh look;
- interface CRC errors, which point at a cable or backplane, not the disk -
  so only a count that grows is reported;
- for NVMe: the controller's critical warning, media errors, wear (percentage
  used) and spare capacity left;
- temperature and power-on time, for the page.

Drives asleep are not woken: the exporter asks smartctl to skip a drive in
standby, and it shows as asleep until it next spins up.
"""

from __future__ import annotations

import re
from typing import Any

# SATA counters, by attribute name, and how each is described.
_SECTOR_COUNTERS = {
    "Reallocated_Sector_Ct": "reallocated",
    "Current_Pending_Sector": "pending",
    "Offline_Uncorrectable": "uncorrectable",
    "Reported_Uncorrect": "reported uncorrectable",
}
_CRC = "UDMA_CRC_Error_Count"
_BAY = re.compile(r"^(?:sata|sas)(?P<n>\d+)$")
_SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)')
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_metrics(text: str) -> list[tuple[str, dict[str, str], float]]:
    """Prometheus text format: (name, labels, value) per sample."""
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if not match:
            continue
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        labels = {k: v.replace('\\"', '"').replace("\\\\", "\\")
                  for k, v in _LABEL.findall(match.group(2) or "")}
        out.append((match.group(1), labels, value))
    return out


def label_for(device: str) -> str:
    """DSM's own name for a bay: sata2 is Drive 2, as on the RAID panel."""
    bay = _BAY.match(device)
    return f"Drive {bay['n']}" if bay else device


def drives(samples: list[tuple[str, dict[str, str], float]]) -> list[dict[str, Any]]:
    """One record per drive the exporter reports."""
    by_device: dict[str, dict[str, Any]] = {}

    def drive(device: str) -> dict[str, Any]:
        return by_device.setdefault(device, {
            "device": device, "label": label_for(device), "model": "", "serial": "",
            "family": "", "protocol": "", "passed": None, "exit_status": None,
            "temperature": None, "power_on_hours": None, "power_cycles": None,
            "counters": {}, "crc_errors": None, "nvme": {},
        })

    for name, labels, value in samples:
        device = labels.get("device")
        if not device:
            continue
        d = drive(device)
        if name == "smartctl_device":
            d.update(model=labels.get("model_name", ""), serial=labels.get("serial_number", ""),
                     family=_known(labels.get("model_family")),
                     protocol=labels.get("protocol", ""),
                     firmware=labels.get("firmware_version", ""))
        elif name == "smartctl_device_smart_status":
            d["passed"] = value == 1
        elif name == "smartctl_device_smartctl_exit_status":
            d["exit_status"] = int(value)
        elif name == "smartctl_device_temperature" and labels.get("temperature_type") == "current":
            # 0 is what smartctl puts there when it read no temperature at
            # all - a SATA drive mistaken for SCSI - not a reading.
            d["temperature"] = value or None
        elif name == "smartctl_device_power_on_seconds":
            d["power_on_hours"] = int(value // 3600)
        elif name == "smartctl_device_power_cycle_count":
            d["power_cycles"] = int(value)
        elif name == "smartctl_device_attribute" and labels.get("attribute_value_type") == "raw":
            attribute = labels.get("attribute_name", "")
            if attribute in _SECTOR_COUNTERS:
                d["counters"][attribute] = int(value)
            elif attribute == _CRC:
                d["crc_errors"] = int(value)
        elif name in ("smartctl_device_critical_warning", "smartctl_device_media_errors",
                      "smartctl_device_percentage_used", "smartctl_device_available_spare",
                      "smartctl_device_available_spare_threshold"):
            d["nvme"][name.removeprefix("smartctl_device_")] = value
    for d in by_device.values():
        # smartctl's exit status is a bit mask; bit 1 (value 2) is "could not
        # open the device or read its identity" - which is also how a drive
        # in standby answers when smartctl is told not to wake it.
        d["asleep"] = d["passed"] is None and bool((d["exit_status"] or 0) & 2)
    # The bays in order, as DSM numbers them, then everything else.
    return sorted(by_device.values(),
                  key=lambda d: (not _BAY.match(d["device"]), _natural(d["device"])))


def _known(value: str | None) -> str:
    return "" if not value or value == "unknown" else value


def _natural(name: str) -> list:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def assess(drive: dict[str, Any], *, crc_before: int | None, temp_limit: float,
           nvme_temp_limit: float, wear_limit: float) -> tuple[str, list[str]]:
    """('' | 'warning' | 'critical', what is wrong) for one drive.

    `crc_before` is the CRC count first seen for this drive: CRC errors are a
    cable's history, so only new ones are news.
    """
    problems: list[str] = []
    severity = ""

    def flag(level: str, text: str) -> None:
        nonlocal severity
        problems.append(text)
        if level == "critical" or not severity:
            severity = level

    if drive.get("passed") is False:
        flag("critical", "SMART reports the drive as failing")
    counters = drive.get("counters") or {}
    sectors = [f"{counters[a]} {word}" for a, word in _SECTOR_COUNTERS.items() if counters.get(a)]
    if sectors:
        flag("warning", f"{', '.join(sectors)} sectors")
    crc = drive.get("crc_errors")
    if crc is not None and crc_before is not None and crc > crc_before:
        flag("warning", f"{crc - crc_before} new interface CRC errors - usually the cable "
                        "or the bay, not the disk")
    nvme = drive.get("nvme") or {}
    if nvme.get("critical_warning"):
        flag("critical", f"NVMe critical warning {int(nvme['critical_warning'])}")
    if nvme.get("media_errors"):
        flag("warning", f"{int(nvme['media_errors'])} NVMe media errors")
    spare, threshold = nvme.get("available_spare"), nvme.get("available_spare_threshold")
    if spare is not None and threshold and spare <= threshold:
        flag("critical", f"spare capacity down to {spare:.0f}% (threshold {threshold:.0f}%)")
    used = nvme.get("percentage_used")
    if used is not None and used >= wear_limit:
        flag("warning", f"{used:.0f}% of its rated write endurance used")
    temperature = drive.get("temperature")
    limit = nvme_temp_limit if drive.get("protocol") == "NVMe" else temp_limit
    if temperature is not None and temperature >= limit:
        flag("warning", f"at {temperature:.0f} °C")
    return severity, problems
