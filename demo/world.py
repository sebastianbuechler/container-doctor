"""The made-up host the demo runs against: its containers, what they log and
use, and what each image's registry would say.

Everything the dashboard shows in the docs comes from here, through the real
doctor - only Docker, the host's files, the registries and the model are
stand-ins. Times are relative to when the demo starts, so the screenshots
always look recent.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

GIB = 1024 ** 3
MIB = 1024 ** 2
HOST_MEMORY = 32 * GIB
CORES = 8

STARTED = datetime.now(timezone.utc).replace(microsecond=0)


def ago(**delta) -> datetime:
    return STARTED - timedelta(**delta)


def iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


@dataclass
class Box:
    """One container, and how it behaves."""
    name: str
    stack: str
    service: str
    image: str
    cpu: float                       # % of one core, docker stats style
    mem: int                         # bytes in use
    mem_limit: int = 0               # the configured cap, 0 for none
    state: str = "running"
    health: str = "none"
    exit_code: int = 0
    restart_count: int = 0
    created: datetime = field(default_factory=lambda: ago(days=9, hours=3))
    started: datetime | None = None
    networks: tuple[str, ...] = ()
    ports: tuple[tuple[str, str, str], ...] = ()   # (container port/proto, host ip, host port)
    labels: dict[str, str] = field(default_factory=dict)
    logs: str = "quiet"              # which log script it follows
    depends_on: str = ""
    source: str = ""                 # org.opencontainers.image.source

    @property
    def id(self) -> str:
        return hashlib.sha256(self.name.encode()).hexdigest()

    @property
    def image_id(self) -> str:
        return digest(f"image:{self.image}")


def _boxes() -> list[Box]:
    media = ("media_default", "proxy")
    return [
        Box("traefik", "proxy", "traefik", "traefik:v3.4.1", 1.2, 74 * MIB, 256 * MIB,
            networks=("proxy",), ports=(("80/tcp", "0.0.0.0", "80"), ("443/tcp", "0.0.0.0", "443")),
            labels={"doctor.critical": "true"}, logs="traefik",
            source="https://github.com/traefik/traefik"),
        Box("pocket-id", "auth", "pocket-id", "ghcr.io/pocket-id/pocket-id:v1.6.2", 0.3, 48 * MIB,
            128 * MIB, health="healthy", networks=("proxy",), logs="quiet",
            labels={"doctor.critical": "true"}, source="https://github.com/pocket-id/pocket-id"),
        Box("jellyfin", "media", "jellyfin", "jellyfin/jellyfin:10.10.7", 38.0, 1.9 * GIB, 4 * GIB,
            health="healthy", networks=media, ports=(("8096/tcp", "0.0.0.0", "8096"),),
            logs="jellyfin", source="https://github.com/jellyfin/jellyfin"),
        Box("sonarr", "media", "sonarr", "lscr.io/linuxserver/sonarr:latest", 2.4, 310 * MIB,
            1 * GIB, networks=media, logs="sonarr",
            source="https://github.com/linuxserver/docker-sonarr"),
        Box("radarr", "media", "radarr", "lscr.io/linuxserver/radarr:latest", 1.8, 290 * MIB,
            1 * GIB, networks=media, logs="arr",
            source="https://github.com/linuxserver/docker-radarr"),
        Box("prowlarr", "media", "prowlarr", "lscr.io/linuxserver/prowlarr:latest", 0.6, 160 * MIB,
            512 * MIB, networks=media, logs="arr",
            source="https://github.com/linuxserver/docker-prowlarr"),
        Box("sabnzbd", "media", "sabnzbd", "lscr.io/linuxserver/sabnzbd:4.5.3", 6.5, 420 * MIB,
            1 * GIB, networks=media, logs="quiet",
            source="https://github.com/linuxserver/docker-sabnzbd"),
        Box("jellyseerr", "media", "jellyseerr", "fallenbagel/jellyseerr:2.7.3", 0.4, 210 * MIB,
            512 * MIB, networks=media, logs="quiet", depends_on="jellyfin:service_started:false",
            source="https://github.com/fallenbagel/jellyseerr"),
        Box("paperless", "paperless", "webserver", "ghcr.io/paperless-ngx/paperless-ngx:2.17.1",
            3.1, 690 * MIB, 2 * GIB, health="starting", restart_count=4,
            created=ago(days=2, hours=5), started=ago(minutes=1),
            networks=("paperless_default", "proxy"), logs="paperless",
            depends_on="db:service_healthy:false,broker:service_started:false",
            source="https://github.com/paperless-ngx/paperless-ngx"),
        Box("paperless-db", "paperless", "db", "postgres:16.4", 4.8, 228 * MIB, 256 * MIB,
            health="unhealthy", created=ago(days=2, hours=5), started=ago(hours=6, minutes=12),
            restart_count=1, networks=("paperless_default",), logs="postgres",
            labels={"doctor.mode": "suggest"}),
        Box("paperless-redis", "paperless", "broker", "redis:7.4-alpine", 0.3, 12 * MIB, 128 * MIB,
            health="healthy", created=ago(days=2, hours=5), networks=("paperless_default",),
            logs="quiet"),
        Box("memos", "notes", "memos", "neosmartpenguin/memos:0.24.4", 0.1, 38 * MIB, 256 * MIB,
            networks=("proxy",), logs="quiet", source="https://github.com/usememos/memos"),
        Box("homepage", "tools", "homepage", "ghcr.io/gethomepage/homepage:v1.4.0", 0.5, 120 * MIB,
            256 * MIB, health="healthy", networks=("proxy",), logs="quiet",
            source="https://github.com/gethomepage/homepage"),
        Box("uptime-kuma", "tools", "uptime-kuma", "louislam/uptime-kuma:1", 1.1, 140 * MIB,
            512 * MIB, health="healthy", networks=("proxy",), logs="quiet",
            labels={"doctor.mode": "auto", "doctor.actions": "restart_container"},
            source="https://github.com/louislam/uptime-kuma"),
        Box("it-tools", "tools", "it-tools", "corentinth/it-tools:2024.10.22-7ca5933", 0.0,
            9 * MIB, 64 * MIB, networks=("proxy",), logs="quiet",
            labels={"doctor.mode": "auto"}),
        Box("stirling-pdf", "tools", "stirling-pdf", "frooodle/s-pdf:1.2.0", 0.2, 610 * MIB,
            1 * GIB, networks=("proxy",), logs="quiet",
            source="https://github.com/Stirling-Tools/Stirling-PDF"),
        Box("ntfy", "tools", "ntfy", "binwiederhier/ntfy:v2.13.0", 0.1, 22 * MIB, 128 * MIB,
            networks=("proxy",), logs="quiet", source="https://github.com/binwiederhier/ntfy"),
        Box("dozzle", "tools", "dozzle", "amir20/dozzle:v8.13.1", 0.2, 26 * MIB, 128 * MIB,
            networks=("proxy",), logs="quiet", source="https://github.com/amir20/dozzle"),
        Box("backup", "backup", "restic", "mazzolino/restic:1.8.0", 0.0, 0, 512 * MIB,
            state="exited", exit_code=0, started=ago(hours=9), networks=("backup_default",),
            logs="quiet", labels={"doctor.enable": "false"}),
        Box("container-doctor", "container-doctor", "container-doctor",
            "ghcr.io/sebastianbuechler/container-doctor:0.1.0", 0.9, 96 * MIB, 512 * MIB,
            networks=("container-doctor_default", "proxy"), logs="quiet",
            source="https://github.com/sebastianbuechler/container-doctor"),
        Box("doctor-socket-proxy", "container-doctor", "doctor-socket-proxy",
            "lscr.io/linuxserver/socket-proxy:3.2.3", 0.1, 8 * MIB, 64 * MIB,
            networks=("container-doctor_default",), logs="quiet"),
        Box("doctor-smart", "container-doctor", "doctor-smart",
            "prometheuscommunity/smartctl-exporter:v0.14.0", 0.0, 14 * MIB, 128 * MIB,
            networks=("container-doctor_default",), logs="quiet"),
    ]


BOXES = _boxes()

# Docker's networks: name -> (driver, subnet, internal)
NETWORKS = {
    "bridge": ("bridge", "172.17.0.0/16", False),
    "host": ("host", "", False),
    "none": ("null", "", False),
    "proxy": ("bridge", "172.20.0.0/16", False),
    "media_default": ("bridge", "172.21.0.0/16", False),
    "paperless_default": ("bridge", "172.22.0.0/16", True),
    "notes_default": ("bridge", "172.23.0.0/16", False),
    "backup_default": ("bridge", "172.24.0.0/16", False),
    "container-doctor_default": ("bridge", "172.25.0.0/16", False),
}


# -- logs ------------------------------------------------------------------------

LOG_SCRIPTS: dict[str, tuple[int, list[str], list[str], float]] = {
    # name: (seconds between lines, normal lines, error lines, share of errors)
    "quiet": (300, ["INFO  healthcheck ok", "INFO  served 14 requests in the last 5m"], [], 0.0),
    "traefik": (45, [
        'level=info msg="Configuration loaded from providers" providerName=docker',
        'level=debug msg="Skipping same configuration" providerName=docker',
    ], [], 0.0),
    "jellyfin": (30, [
        "[INF] [27] Jellyfin.Api.Helpers.MediaInfoHelper: User policy for anna. EnablePlaybackRemuxing: True",
        "[INF] [41] Emby.Server.Implementations.ScheduledTasks.TaskManager: Scan Media Library Completed after 0 minute(s)",
        "[INF] [12] Jellyfin.Api.Controllers.DynamicHlsController: Current HLS implementation doesn't support non-keyframe breaks",
    ], [], 0.0),
    "arr": (90, [
        "[Info] RssSyncService: Starting RSS Sync",
        "[Info] DownloadDecisionMaker: Processing 50 releases",
        "[Info] RssSyncService: RSS Sync Completed. Reports found: 50, Reports grabbed: 0",
    ], [], 0.0),
    "sonarr": (12, [
        "[Info] RssSyncService: Starting RSS Sync",
        "[Info] DownloadDecisionMaker: Processing 100 releases",
    ], [
        "[Error] Newznab: Indexer returned HTTP 401 Unauthorized: API key is invalid or revoked "
        "(https://indexer.example/api?t=tvsearch&apikey=(removed))",
    ], 0.55),
    "paperless": (8, [
        "[INFO] [paperless.management.consumer] Using inotify to watch directory for changes: /usr/src/paperless/consume",
        "Waiting for PostgreSQL to start...",
    ], [
        "django.db.utils.OperationalError: connection to server at \"db\" (172.22.0.3), "
        "port 5432 failed: the database system is in recovery mode",
    ], 0.35),
    "postgres": (20, [
        "LOG:  checkpoint starting: time",
        "LOG:  checkpoint complete: wrote 412 buffers (2.5%); 0 WAL file(s) added",
        "LOG:  database system was not properly shut down; automatic recovery in progress",
        "LOG:  redo starts at 2/7C1A3F28",
    ], [], 0.0),
}


def log_lines(box: Box, since: datetime | None, until: datetime | None, tail: int) -> list[str]:
    """What `docker logs --timestamps` would print, generated from the clock:
    one line every few seconds, chosen by its time, so each poll finds new
    ones and reading the same window twice gives the same lines."""
    every, normal, errors, share = LOG_SCRIPTS.get(box.logs, LOG_SCRIPTS["quiet"])
    now = datetime.now(timezone.utc)
    end = min(until or now, now)
    start = since or end - timedelta(hours=1)
    start = max(start, box.started or box.created)
    first = int(start.timestamp()) // every * every + every
    last = int(end.timestamp())
    stamps = list(range(first, last + 1, every))[-tail:] if tail else []
    out = []
    for stamp in stamps:
        pick = random.Random(f"{box.name}:{stamp}")
        text = (pick.choice(errors) if errors and pick.random() < share else pick.choice(normal))
        when = datetime.fromtimestamp(stamp, timezone.utc)
        out.append(f"{when.strftime('%Y-%m-%dT%H:%M:%S')}.{pick.randrange(10**9):09d}Z {text}")
    return out


# -- stats -----------------------------------------------------------------------

def stats(box: Box) -> dict:
    """A one-shot `docker stats` payload around the box's figures."""
    jitter = random.Random()
    cpu = max(0.0, box.cpu * jitter.uniform(0.75, 1.25))
    system_delta = 1_000_000_000 * CORES
    cpu_delta = int(cpu / 100 / CORES * system_delta)
    mem = int(box.mem * jitter.uniform(0.97, 1.03))
    if box.mem_limit:
        mem = min(mem, box.mem_limit - MIB)
    total = 900_000_000_000_000
    return {
        "cpu_stats": {"cpu_usage": {"total_usage": total + cpu_delta},
                      "system_cpu_usage": 50_000_000_000_000 + system_delta,
                      "online_cpus": CORES},
        "precpu_stats": {"cpu_usage": {"total_usage": total},
                         "system_cpu_usage": 50_000_000_000_000},
        "memory_stats": {"usage": mem, "limit": box.mem_limit or HOST_MEMORY,
                         "stats": {"inactive_file": 0}},
    }


# -- what the registries say -----------------------------------------------------

# image -> what a registry would answer: a newer build of the same tag, a newer
# version in the tag's line, and one still in its cooldown.
REGISTRY = {
    "lscr.io/linuxserver/sonarr:latest": {"update": True},
    "lscr.io/linuxserver/prowlarr:latest": {"update": True},
    "jellyfin/jellyfin:10.10.7": {"newer": ("10.11.2", "minor", 3, ""),
                                  "breaking": "10.11 migrates the library database on first start; "
                                              "back up /config before upgrading."},
    "traefik:v3.4.1": {"newer": ("v3.5.3", "minor", 6, "")},
    "postgres:16.4": {"newer": ("17.6", "major", 14, "16.10")},
    "ghcr.io/paperless-ngx/paperless-ngx:2.17.1": {
        "newer": ("2.18.2", "minor", 4, ""),
        "cooling": ("2.18.4", "patch", 2)},
    "lscr.io/linuxserver/sabnzbd:4.5.3": {"newer": ("4.5.5", "patch", 2, "")},
    "ghcr.io/gethomepage/homepage:v1.4.0": {"newer": ("v1.5.0", "minor", 2, "")},
    "binwiederhier/ntfy:v2.13.0": {"newer": ("v2.14.0", "minor", 1, "")},
}


# -- the host --------------------------------------------------------------------

HOST_NAME = "atlas"
KERNEL = "6.12.48+deb13-amd64"

# path -> (total bytes, used bytes now, bytes a day it grows)
DISKS = {
    "/srv/data": (7_800_000_000_000, 6_790_000_000_000, 58_000_000_000),
    "/": (980_000_000_000, 304_000_000_000, 150_000_000),
}
