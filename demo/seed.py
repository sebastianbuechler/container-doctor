"""Two weeks of history for the demo host, written through the doctor's own
store with its clock turned back, so every chart and timeline has something
to show from the first poll."""

from __future__ import annotations

import contextlib
import json
import math
import random
from datetime import datetime, timedelta

import dataclasses
from types import SimpleNamespace

from app import changes, store as store_module
from app.classifier import ELIGIBLE, QUESTIONS, Classifier
from app.config import Settings
from app.decision import Decision, read_answers, wire_questions
from app.diagnoser import DIAGNOSIS_SCHEMA, _cost, system_prompt
from app.store import Store

from . import world
from .model import _choice
from .world import BOXES, MIB, STARTED, iso

DAYS = 14
SERIALS = {"sda": "WD-WX12345", "sdb": "WD-WX67890", "nvme0n1": "S64ANS0T123456"}
MODEL = "claude-opus-5-5"


class _Clock(datetime):
    at: datetime = STARTED

    @classmethod
    def now(cls, tz=None):
        return cls.at if tz else cls.at.replace(tzinfo=None)


@contextlib.contextmanager
def clock():
    """Point the store's idea of now wherever the seed needs it."""
    real = store_module.datetime
    store_module.datetime = _Clock
    try:
        yield _Clock
    finally:
        store_module.datetime = real


def _daily(hour_of_day: float) -> float:
    """0 at four in the morning, 1 in the evening."""
    return (1 - math.cos((hour_of_day - 4) / 24 * 2 * math.pi)) / 2


def host(store: Store, at: type[_Clock]) -> None:
    rng = random.Random(1)
    step = timedelta(minutes=5)
    t = STARTED - timedelta(days=7)
    while t < STARTED - step:
        at.at = t
        busy = _daily(t.hour + t.minute / 60)
        cpu = 6 + 22 * busy + rng.uniform(-3, 3) + (35 if t.hour == 3 and t.minute < 30 else 0)
        store.record_host_sample({
            "cpu_pct": round(max(cpu, 1), 1),
            "iowait_pct": round(rng.uniform(0.3, 2.5) + (9 if t.hour == 3 else 0), 1),
            "mem_used_pct": round(56 + 8 * busy + rng.uniform(-1, 1), 1),
            "swap_used_pct": 7.5,
            "load": [round(0.6 + 2.4 * busy + rng.uniform(0, 0.4), 2)],
            "temperatures": [{"celsius": round(44 + 10 * busy + rng.uniform(-1, 1), 1)}],
        }, bucket_minutes=5)
        # The mirror sleeps through the night but for the 3 a.m. backup, and
        # through a quiet spell after lunch. Asleep, it has no temperature.
        hour = t.hour + t.minute / 60
        asleep = 0.5 <= hour < 3 or 3.5 <= hour < 7.5 or 13.5 <= hour < 15
        temps = {SERIALS["nvme0n1"]: 41 + 7 * busy + rng.uniform(-1, 1)}
        if not asleep:
            temps[SERIALS["sda"]] = 33 + 3 * busy + rng.uniform(-0.5, 0.5)
            temps[SERIALS["sdb"]] = 35 + 4 * busy + rng.uniform(-0.5, 0.5)
        store.record_drive_temps(temps, bucket_minutes=5)
        store.record_drive_sleep({SERIALS["sda"]: asleep, SERIALS["sdb"]: asleep}, bucket_minutes=5)
        # Reads and writes: none asleep, the backup reading the mirror at 3,
        # streams in the evening, and the SSD always busy with the databases.
        backup = t.hour == 3 and t.minute < 30
        disk = 0 if asleep else (80e6 if backup else (0.4e6 + 2.5e6 * busy) * rng.uniform(0.3, 1.4))
        store.record_drive_io({
            SERIALS["sda"]: {"read_bps": disk, "write_bps": disk * 0.08, "busy_pct": min(95, disk / 1.2e6)},
            SERIALS["sdb"]: {"read_bps": disk * 0.9, "write_bps": disk * 0.08, "busy_pct": min(95, disk / 1.3e6)},
            SERIALS["nvme0n1"]: {"read_bps": 2e6 + 9e6 * busy, "write_bps": 3e6 + 4e6 * busy,
                                 "busy_pct": 2 + 6 * busy},
        }, bucket_minutes=5)
        if t.minute < 5:  # once an hour
            days_back = (STARTED - t).total_seconds() / 86400
            wakes = int((7 - days_back) * 3)
            store.record_drive_counters({
                SERIALS["sda"]: {"reallocated": 0, "pending": 0, "uncorrectable": 0,
                                 "crc_errors": 0, "spin_ups": 3412 - 21 + wakes},
                # Drive 2 remapped another eight sectors four days ago.
                SERIALS["sdb"]: {"reallocated": 16 if days_back > 4 else 24, "pending": 0,
                                 "uncorrectable": 0, "crc_errors": 3, "spin_ups": 3398 - 21 + wakes},
                SERIALS["nvme0n1"]: {"media_errors": 0},
            })
        t += step


def disks(store: Store, at: type[_Clock]) -> None:
    rng = random.Random(2)
    for path, (total, used, per_day) in world.DISKS.items():
        t = STARTED - timedelta(days=30)
        while t < STARTED - timedelta(hours=1):
            at.at = t
            days_back = (STARTED - t).total_seconds() / 86400
            now_used = int(used - per_day * days_back + rng.uniform(-0.15, 0.15) * per_day)
            store.record_disk_sample(path, now_used, total, free_bytes=total - now_used,
                                     bucket_minutes=60)
            t += timedelta(hours=1)


def memory(store: Store, at: type[_Clock]) -> None:
    rng = random.Random(3)
    t = STARTED - timedelta(days=DAYS)
    while t < STARTED - timedelta(hours=1):
        at.at = t
        busy = _daily(t.hour)
        samples = []
        for box in BOXES:
            if box.state != "running" or not box.mem:
                continue
            level = box.mem * (0.8 + 0.25 * busy) * rng.uniform(0.95, 1.05)
            if box.name == "jellyfin" and 19 <= t.hour <= 22:
                level *= 1.6   # evening transcodes
            if box.name == "paperless-db" and t.hour == 2:
                level = 254 * MIB  # the nightly index rebuild, at the limit
            if box.mem_limit:
                level = min(level, box.mem_limit - MIB)
            samples.append((box.name, int(level)))
        store.record_mem_samples(samples)
        # CPU as a share of the whole host; the nightly jobs show up at night.
        cpu = []
        for box in BOXES:
            if box.state != "running":
                continue
            load = box.cpu * (0.5 + busy) * rng.uniform(0.7, 1.3)
            if box.name == "jellyfin" and 19 <= t.hour <= 22:
                load += 140   # evening transcodes
            if box.name == "paperless-db" and t.hour == 2:
                load += 90    # the nightly index rebuild
            cpu.append((box.name, round(load / world.CORES, 2)))
        store.record_cpu_samples(cpu)
        t += timedelta(hours=1)


def _state(box_name: str, image: str, build: str) -> dict:
    box = next(b for b in BOXES if b.name == box_name)
    return {"container_id": build, "image": image, "image_id": world.digest(build),
            "created": "", "started_at": "", "restart_count": 0, "state": "running",
            "finished_at": "", "stack": box.stack, "service": box.service, "config": None}


def history_of_changes(store: Store) -> None:
    found: list[tuple[datetime, str, str, changes.Change]] = []

    def recreated(when: datetime, name: str, before_image: str, image: str, same: bool = False):
        box = next(b for b in BOXES if b.name == name)
        before = _state(name, before_image, f"{name}-old")
        now = _state(name, image, f"{name}-old" if same else f"{name}-{image}")
        change = changes._recreated(name, before, now)
        change.at = iso(when)
        found.append((when, name, box.stack, change))

    media = STARTED - timedelta(days=9, hours=3, minutes=12)
    for name in ("sonarr", "radarr", "prowlarr"):
        image = next(b.image for b in BOXES if b.name == name)
        recreated(media, name, image, image)
    recreated(STARTED - timedelta(days=6, hours=20), "traefik", "traefik:v3.3.6", "traefik:v3.4.1")
    paperless = STARTED - timedelta(days=2, hours=5)
    recreated(paperless, "paperless", "ghcr.io/paperless-ngx/paperless-ngx:2.16.3",
              "ghcr.io/paperless-ngx/paperless-ngx:2.17.1")
    recreated(paperless, "paperless-db", "postgres:16.4", "postgres:16.4", same=True)
    recreated(paperless, "paperless-redis", "redis:7.4-alpine", "redis:7.4-alpine", same=True)

    for stack in ({w[2] for w in found}):
        group = [(n, s, c) for _, n, s, c in found if s == stack]
        changes.group_redeploys(group)

    for when, name, stack, change in found:
        store.record_change(name, stack, change.kind, change.at, change.summary, change.detail)

    restarts = [
        (STARTED - timedelta(days=4, hours=2, minutes=40), "jellyfin", "doctor"),
        (STARTED - timedelta(days=1, hours=7, minutes=3), "uptime-kuma", "doctor"),
        (STARTED - timedelta(hours=6, minutes=12), "paperless-db", "docker"),
    ]
    for when, name, by in restarts:
        box = next(b for b in BOXES if b.name == name)
        summary = (f"{name} was restarted by the doctor" if by == "doctor"
                   else f"{name} restarted by its restart policy")
        store.record_change(name, box.stack, "restarted", iso(when), summary,
                            {"restart_count": 1, "by": by})
    for day in range(DAYS):
        ran = (STARTED - timedelta(days=day)).replace(hour=1, minute=30, second=0)
        if ran > STARTED:
            continue
        store.record_change("backup", "backup", "started", iso(ran), "backup started", {})
        store.record_change("backup", "backup", "stopped", iso(ran + timedelta(minutes=47)),
                            "backup stopped (exited)", {"state": "exited"})


PAST = [
    # days ago, container, kind, rule severity, summary, diagnosis, action, by, outcome
    (8.2, "stirling-pdf", "high_memory", "warning",
     "stirling-pdf is using 93% of its memory limit (952 MiB of 1.0 GiB)",
     {"summary": "A 400-page OCR job filled stirling-pdf's memory; it finished on its own.",
      "root_cause": "Memory rose during one OCR conversion and fell back once it completed.",
      "confidence": 0.81, "severity": "info", "is_transient": True,
      "recommended_action": "none", "action_rationale": "It recovered without help.",
      "human_fix_steps": [], "evidence_used": ["mem 93% for 4 checks, then 41%"]},
     None, "", "dismissed"),
    (6.8, "traefik", "exit_nonzero", "critical",
     "traefik is stopped after exiting with code 1",
     {"summary": "Traefik 3.4 refused the old static configuration after the upgrade.",
      "root_cause": "\"field not found, node: swarmMode\" - the option moved in v3; traefik "
                    "exits on unknown keys in its static config.",
      "confidence": 0.9, "severity": "critical", "is_transient": False,
      "recommended_action": "notify_only",
      "action_rationale": "A restart reads the same file and fails the same way.",
      "human_fix_steps": ["Remove providers.docker.swarmMode from traefik.yml.",
                          "Redeploy the proxy stack."],
      "evidence_used": ["level=error msg=\"field not found, node: swarmMode\""]},
     None, "", "resolved"),
    (5.1, "host:network", "net_dns", "warning",
     "DNS lookups failed for 3 rounds in a row (cloudflare.com, github.com)",
     {"summary": "The router's DNS forwarder stopped answering for four minutes.",
      "root_cause": "Both names failed at once while TCP to 1.1.1.1 kept working: the "
                    "resolver, not the line.",
      "confidence": 0.74, "severity": "warning", "is_transient": True,
      "recommended_action": "notify_only",
      "action_rationale": "The network is fixed at the router.",
      "human_fix_steps": ["If it repeats, give the host a second DNS server."],
      "evidence_used": ["dns: 0/2 ok", "tcp 1.1.1.1:443 ok"]},
     None, "", "resolved"),
    (4.1, "jellyfin", "unhealthy", "warning",
     "jellyfin has been failing its health check for two checks running",
     {"summary": "Jellyfin hung after a transcode crashed; a restart brings it back.",
      "root_cause": "ffmpeg exited with \"Conversion failed!\" and the web server stopped "
                    "answering its health check right after.",
      "confidence": 0.82, "severity": "warning", "is_transient": False,
      "recommended_action": "restart_container",
      "action_rationale": "The process is alive but stuck; a restart is safe for a stateless "
                          "web server.",
      "human_fix_steps": ["Turn off hardware tone mapping if it happens again."],
      "evidence_used": ["[ERR] ffmpeg: Conversion failed!", "health: unhealthy"]},
     "restart_container", "anna", "resolved"),
    (3.3, "radarr", "log_errors", "warning",
     "radarr logged 7 lines matching the error pattern in the last 120 lines",
     {"summary": "radarr could not reach its download client for ten minutes during a "
                 "sabnzbd update.",
      "root_cause": "Connection refused to sabnzbd:8080 while sabnzbd was being recreated.",
      "confidence": 0.88, "severity": "info", "is_transient": True,
      "recommended_action": "none", "action_rationale": "It reconnected by itself.",
      "human_fix_steps": [], "evidence_used": ["Connection refused (sabnzbd:8080)"]},
     None, "", "resolved"),
    (1.3, "uptime-kuma", "unhealthy", "warning",
     "uptime-kuma has been failing its health check for two checks running",
     {"summary": "uptime-kuma's SQLite database was locked by a long vacuum.",
      "root_cause": "\"SQLITE_BUSY: database is locked\" for six minutes after the weekly "
                    "vacuum; the health endpoint times out meanwhile.",
      "confidence": 0.84, "severity": "warning", "is_transient": False,
      "recommended_action": "restart_container",
      "action_rationale": "Restarting releases the lock; the container runs in auto mode.",
      "human_fix_steps": [], "evidence_used": ["SQLITE_BUSY: database is locked"]},
     "restart_container", "policy", "resolved"),
]


def past_incidents(store: Store, at: type[_Clock]) -> None:
    for days, name, kind, severity, summary, diagnosis, action, by, outcome in PAST:
        opened = STARTED - timedelta(days=days)
        box = next((b for b in BOXES if b.name == name), None)
        at.at = opened
        container = {"name": name, "image": box.image if box else "", "state": "running",
                     "health": "unhealthy" if kind == "unhealthy" else "healthy"}
        evidence = {"kind": kind, "summary": summary, "detected_at": iso(opened),
                    "container": container, "logs": "\n".join(diagnosis["evidence_used"])}
        if kind in ELIGIBLE and (name, kind) in PAST_VERDICTS:
            evidence["classification"] = _classify(store, at, name, kind, severity, summary,
                                                   PAST_VERDICTS[(name, kind)])
        incident_id = store.open_incident(
            container_id=box.id if box else "", container_name=name,
            image=box.image if box else "", kind=kind, fingerprint=f"demo:{name}:{kind}:{days}",
            severity=severity, summary=summary, evidence=evidence,
            mode="suggest", lines=diagnosis["evidence_used"][:1])
        at.at = opened + timedelta(seconds=40)
        _call(store, at, name, kind, summary, diagnosis, incident_id)
        diagnosis = {**diagnosis, "diagnosed_at": iso(at.at)}
        store.set_diagnosis(incident_id, diagnosis, diagnosis["severity"])
        if action:
            at.at = opened + timedelta(minutes=2 if by == "policy" else 26)
            action_id = store.record_action(
                incident_id=incident_id, container_name=name, action=action,
                status="pending", decided_by=by,
                reason=diagnosis["action_rationale"])
            at.at += timedelta(seconds=25)
            store.finish_action(action_id, "succeeded",
                                f"restarted {name}; healthy again after 18s")
        at.at = opened + timedelta(minutes=35 if action else 140)
        store.set_incident_status(incident_id, outcome, resolved=outcome == "resolved")


def _call(store: Store, at: type[_Clock], name: str, kind: str, summary: str,
          answer: dict, ref: str = "", purpose: str = "container_diagnosis") -> None:
    prompt = f"## Incident\nkind: {kind}\nrule summary: {summary}\n\n## Container\nname: {name}\n"
    input_tokens = random.randint(5200, 9800)
    output_tokens = random.randint(700, 1500)
    cached = random.choice((0, 3100))
    cost = _cost(MODEL, input_tokens, output_tokens, cached)
    store.record_llm_call({
        "at": iso(at.at), "purpose": purpose, "ref": ref, "provider": "anthropic",
        "model": MODEL, "endpoint": "https://api.anthropic.com/v1/messages", "status": "ok",
        "latency_ms": random.randint(6000, 19000), "input_tokens": input_tokens,
        "output_tokens": output_tokens, "cached_tokens": cached, "cost_usd": cost,
        "request": {"system": system_prompt(host=f"a Linux host called {world.HOST_NAME}"),
                    "prompt": prompt, "schema_name": purpose, "schema": DIAGNOSIS_SCHEMA,
                    "effort": "medium", "max_tokens": 4000},
        "response": json.dumps(answer, indent=2),
    })
    _usage(store, input_tokens, output_tokens, cached, cost)


# The classifier's verdicts on the past incidents of a judgement kind - all
# of them real enough to open - and on signals it hid as noise, which never
# became incidents. Answers in the Decisions API's shape, as model.py's.
PAST_VERDICTS = {
    ("stirling-pdf", "high_memory"): {
        "verdict": _choice("expected_transient", {"expected_transient": 0.71, "real_fault": 0.2,
                                                  "benign_noise": 0.07, "unclear": 0.02}),
        "severity": _choice("warning", {"warning": 0.64, "info": 0.33, "critical": 0.03}),
        "cause": _choice("resources", {"resources": 0.83, "unknown": 0.17}),
        "self_heals": {"noul": 0.77},
    },
    ("jellyfin", "unhealthy"): {
        "verdict": _choice("real_fault", {"real_fault": 0.89, "expected_transient": 0.08,
                                          "benign_noise": 0.02, "unclear": 0.01}),
        "severity": _choice("warning", {"warning": 0.7, "critical": 0.27, "info": 0.03}),
        "cause": _choice("image", {"image": 0.46, "resources": 0.31, "unknown": 0.23}),
        "self_heals": {"noul": 0.12},
    },
    ("radarr", "log_errors"): {
        "verdict": _choice("expected_transient", {"expected_transient": 0.62, "real_fault": 0.31,
                                                  "benign_noise": 0.05, "unclear": 0.02}),
        "severity": _choice("info", {"info": 0.58, "warning": 0.4, "critical": 0.02}),
        "cause": _choice("dependency", {"dependency": 0.86, "network": 0.1, "unknown": 0.04}),
        "self_heals": {"noul": 0.81},
    },
    ("uptime-kuma", "unhealthy"): {
        "verdict": _choice("real_fault", {"real_fault": 0.84, "expected_transient": 0.13,
                                          "benign_noise": 0.02, "unclear": 0.01}),
        "severity": _choice("warning", {"warning": 0.81, "critical": 0.14, "info": 0.05}),
        "cause": _choice("storage", {"storage": 0.57, "resources": 0.3, "unknown": 0.13}),
        "self_heals": {"noul": 0.22},
    },
}

HIDDEN = [
    # days ago, container, kind, rule severity, summary, answers
    (12.4, "jellyfin", "log_errors", "warning",
     "jellyfin logged 9 lines matching the error pattern in the last 120 lines",
     {"verdict": _choice("benign_noise", {"benign_noise": 0.93, "expected_transient": 0.04,
                                          "real_fault": 0.02, "unclear": 0.01}),
      "severity": _choice("info", {"info": 0.9, "warning": 0.09, "critical": 0.01}),
      "cause": _choice("storage", {"storage": 0.52, "unknown": 0.48}),
      "self_heals": {"noul": 0.91}}),
    (9.9, "sabnzbd", "high_cpu", "warning",
     "sabnzbd has held 88% of the host's CPU (704% of one core) for 4 consecutive checks",
     {"verdict": _choice("expected_transient", {"expected_transient": 0.95, "real_fault": 0.03,
                                                "benign_noise": 0.01, "unclear": 0.01}),
      "severity": _choice("info", {"info": 0.84, "warning": 0.15, "critical": 0.01}),
      "cause": _choice("resources", {"resources": 0.9, "unknown": 0.1}),
      "self_heals": {"noul": 0.94}}),
    (7.3, "homepage", "log_errors", "warning",
     "homepage logged 6 lines matching the error pattern in the last 120 lines",
     {"verdict": _choice("benign_noise", {"benign_noise": 0.91, "expected_transient": 0.05,
                                          "real_fault": 0.03, "unclear": 0.01}),
      "severity": _choice("info", {"info": 0.87, "warning": 0.12, "critical": 0.01}),
      "cause": _choice("network", {"network": 0.66, "dependency": 0.3, "unknown": 0.04}),
      "self_heals": {"noul": 0.88}}),
    (4.6, "jellyfin", "log_errors", "warning",
     "jellyfin logged 12 lines matching the error pattern in the last 120 lines",
     {"verdict": _choice("benign_noise", {"benign_noise": 0.92, "expected_transient": 0.05,
                                          "real_fault": 0.02, "unclear": 0.01}),
      "severity": _choice("info", {"info": 0.89, "warning": 0.1, "critical": 0.01}),
      "cause": _choice("storage", {"storage": 0.55, "unknown": 0.45}),
      "self_heals": {"noul": 0.9}}),
    (2.1, "sabnzbd", "high_cpu", "warning",
     "sabnzbd has held 91% of the host's CPU (728% of one core) for 4 consecutive checks",
     {"verdict": _choice("expected_transient", {"expected_transient": 0.96, "real_fault": 0.02,
                                                "benign_noise": 0.01, "unclear": 0.01}),
      "severity": _choice("info", {"info": 0.86, "warning": 0.13, "critical": 0.01}),
      "cause": _choice("resources", {"resources": 0.92, "unknown": 0.08}),
      "self_heals": {"noul": 0.95}}),
]

def _classify(store: Store, at: type[_Clock], name: str, kind: str, severity: str,
              summary: str, answers: dict) -> dict:
    """One verdict, worked out by the classifier's own judge() and written the
    way a live one is: the verdict, its call in the log, and its cost."""
    settings = dataclasses.replace(Settings(), classifier_mode="on")
    tokens_in = random.randint(1400, 2600)
    cost = tokens_in * settings.classifier_price_per_mtok / 1_000_000
    decision = Decision(read_answers(answers, QUESTIONS), settings.classifier_model, True,
                        tokens_in, 24, cost, random.randint(380, 900))
    signal = SimpleNamespace(kind=kind, severity=severity, container={"name": name},
                             details={}, fingerprint=f"demo:{name}:{kind}")
    verdict = Classifier(settings, None, store).judge(signal, decision).to_dict()
    state = f"## Incident\nkind: {kind}\nrule summary: {summary}\n\n## Container\nname: {name}\n"
    store.record_classification(f"demo:{name}:{kind}:{at.at:%j%H}", signal.fingerprint, name,
                                kind, severity, verdict)
    store.record_llm_call({
        "at": iso(at.at), "purpose": "classifier_signal", "provider": "openrouter",
        "model": settings.classifier_model, "endpoint": settings.classifier_url, "status": "ok",
        "latency_ms": decision.latency_ms, "input_tokens": tokens_in, "output_tokens": 24,
        "cost_usd": cost,
        "request": {"kind": "decision", "system": json.dumps(wire_questions(QUESTIONS), indent=2),
                    "prompt": state, "schema_name": "classifier_signal"},
        "response": json.dumps(answers, indent=2),
    })
    store.reserve_classifier_call(0)
    store.add_classifier_usage(cost, tokens_in, 24)
    return verdict


def hidden_signals(store: Store, at: type[_Clock]) -> None:
    """Signals the classifier hid as noise: in its log, never incidents."""
    for days, name, kind, severity, summary, answers in HIDDEN:
        at.at = STARTED - timedelta(days=days)
        _classify(store, at, name, kind, severity, summary, answers)


def _usage(store: Store, input_tokens: int, output_tokens: int, cached: int, cost: float) -> None:
    store.reserve_call(0)
    store.add_usage(input_tokens=input_tokens, output_tokens=output_tokens,
                    cached_tokens=cached, cost_usd=cost)


def briefings(store: Store, at: type[_Clock]) -> None:
    """The briefing's own calls: a few a day, whenever something changed."""
    rng = random.Random(5)
    for day in range(DAYS, 0, -1):
        for _ in range(rng.randint(3, 8)):
            at.at = STARTED - timedelta(days=day) + timedelta(minutes=rng.randint(0, 1439))
            input_tokens, output_tokens = rng.randint(500, 900), rng.randint(60, 140)
            cost = _cost(MODEL, input_tokens, output_tokens, 0)
            store.record_llm_call({
                "at": iso(at.at), "purpose": "briefing_section", "provider": "anthropic",
                "model": MODEL, "endpoint": "https://api.anthropic.com/v1/messages",
                "status": "ok", "latency_ms": rng.randint(1500, 4000),
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "cost_usd": cost,
                "request": {"system": "You write one section of the briefing ...",
                            "prompt": "Section: containers\nLevel from the rules: ok",
                            "schema_name": "briefing_section"},
                "response": json.dumps({"headline": "All 22 containers running normally",
                                        "points": [], "level": "ok"}, indent=2),
            })
            _usage(store, input_tokens, output_tokens, 0, cost)


def seed(db_path: str) -> None:
    store = Store(db_path)
    random.seed(4)
    with clock() as at:
        host(store, at)
        disks(store, at)
        memory(store, at)
        past_incidents(store, at)
        hidden_signals(store, at)
        briefings(store, at)
    history_of_changes(store)
    store.set_host_state("boot_time", iso(STARTED - timedelta(days=23, hours=4)))
    store.close()



