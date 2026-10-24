"""Stand-ins for what the doctor reads from outside: the Docker API (as
docker-py objects), /proc and /sys, the SMART exporter, the registries, the
network checks and the model. Everything behind them is the real doctor."""

from __future__ import annotations

import math
import os
import random
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from . import world
from .world import BOXES, CORES, GIB, HOST_MEMORY, LAN_ADDRESSES, NETWORKS, Box, ago, digest, iso

ROOT = Path(__file__).resolve().parents[1]


# -- Docker, as docker-py would hand it over -------------------------------------

def _address(network: str, index: int, name: str = "") -> tuple[str, int, str]:
    subnet = NETWORKS[network][1]
    if NETWORKS[network][0] == "macvlan":
        base = subnet.split("/")[0].rsplit(".", 1)[0]
        return LAN_ADDRESSES[name], int(subnet.split("/")[1]), f"{base}.1"
    base = subnet.split("/")[0].rsplit(".", 2)[0]
    return f"{base}.0.{index + 2}", int(subnet.split("/")[1]), f"{base}.0.1"


def inspect(box: Box) -> dict:
    """The container as GET /containers/{id}/json describes it."""
    order = {b.name: i for i, b in enumerate(BOXES)}
    networks = {}
    for net in box.networks:
        ip, prefix, gateway = _address(net, order[box.name], box.name)
        internal = NETWORKS[net][2]
        networks[net] = {
            "NetworkID": digest(f"net:{net}")[7:], "IPAddress": ip, "IPPrefixLen": prefix,
            "Gateway": "" if internal else gateway, "Aliases": [box.service, box.name],
            "DNSNames": [box.name, box.service, box.id[:12]],
        }
    ports = {spec: [{"HostIp": ip, "HostPort": port}] for spec, ip, port in box.ports}
    # Docker binds nothing on the host for a container on macvlan alone, and
    # lists its ports unbound.
    lan_only = bool(box.networks) and all(NETWORKS[n][0] == "macvlan" for n in box.networks)
    live = {spec: None for spec in ports} if lan_only else ports
    labels = {
        "com.docker.compose.project": box.stack,
        "com.docker.compose.service": box.service,
        "com.docker.compose.project.working_dir": f"/opt/stacks/{box.stack}",
        "com.docker.compose.config-hash": digest(f"config:{box.name}")[7:],
        **({"com.docker.compose.depends_on": box.depends_on} if box.depends_on else {}),
        **box.labels,
    }
    if "proxy" in box.networks and box.name != "traefik":
        labels["traefik.enable"] = "true"
        labels[f"traefik.http.routers.{box.service}.rule"] = f"Host(`{box.name}.atlas.home.arpa`)"
    started = box.started or box.created + timedelta(seconds=3)
    health = None if box.health == "none" else {"Status": box.health, "FailingStreak":
                                                  5 if box.health == "unhealthy" else 0}
    return {
        "Id": box.id, "Name": f"/{box.name}", "Created": iso(box.created),
        "Image": box.image_id, "RestartCount": box.restart_count,
        "State": {"Status": box.state, "Running": box.state == "running",
                  "ExitCode": box.exit_code, "OOMKilled": False,
                  "StartedAt": iso(started),
                  "FinishedAt": iso(ago(hours=8, minutes=48)) if box.state == "exited"
                  else "0001-01-01T00:00:00Z",
                  **({"Health": health} if health else {})},
        "Config": {"Image": box.image, "Labels": labels, "Tty": True,
                   "ExposedPorts": {spec: {} for spec in box.exposed},
                   "Env": ["TZ=Europe/Zurich", "PUID=1000", "PGID=1000"],
                   "Cmd": None, "Entrypoint": None, "User": ""},
        "HostConfig": {"Memory": int(box.mem_limit),
                       "RestartPolicy": {"Name": "unless-stopped"},
                       "NetworkMode": box.networks[0] if box.networks else "bridge",
                       "PortBindings": ports, "Binds": [f"/srv/data/{box.stack}:/data"]},
        "NetworkSettings": {"Networks": networks,
                            "Ports": live if box.state == "running" else {}},
        "Mounts": [],
    }


class FakeContainer:
    def __init__(self, box: Box) -> None:
        self.box = box
        self.id = box.id
        self.short_id = box.id[:12]
        self.name = box.name

    @property
    def attrs(self) -> dict:
        return inspect(self.box)

    def stats(self, stream: bool = False) -> dict:
        time.sleep(0.05)
        return world.stats(self.box)

    def logs(self, tail=120, timestamps=True, stdout=True, stderr=True, since=None, until=None):
        lines = world.log_lines(self.box, since, until, int(tail) if tail != "all" else 10_000)
        if not timestamps:
            lines = [line.split(" ", 1)[1] for line in lines]
        return ("\n".join(lines) + ("\n" if lines else "")).encode()

    def restart(self, timeout: int = 20) -> None:
        self.box.started = datetime.now(timezone.utc)
        if self.box.name == "paperless-db":
            self.box.health = "healthy"
            self.box.logs = "quiet"
            self.box.mem = 140 * 1024 ** 2

    def stop(self, timeout: int = 20) -> None:
        self.box.state = "exited"


class FakeContainers:
    def list(self, all=True, ignore_removed=True):
        return [FakeContainer(box) for box in BOXES]

    def get(self, ident):
        for box in BOXES:
            if ident in (box.id, box.name) or box.id.startswith(ident):
                return FakeContainer(box)
        raise KeyError(ident)


class FakeImages:
    def get(self, ref):
        box = next((b for b in BOXES if b.image == ref), None)
        repo = ref.rsplit(":", 1)[0] if ":" in ref.rsplit("/", 1)[-1] else ref
        labels = {"org.opencontainers.image.source": box.source} if box and box.source else {}
        return SimpleNamespace(attrs={"RepoDigests": [f"{repo}@{digest('local:' + ref)}"],
                                      "Config": {"Labels": labels}})

    def prune(self, filters=None):
        return {"SpaceReclaimed": 1_400_000_000, "ImagesDeleted": [{}] * 6}


class FakeAPI:
    base_url = "http+docker://demo"

    def networks(self):
        out = []
        for name, (driver, subnet, internal) in NETWORKS.items():
            out.append({
                "Name": name, "Id": digest(f"net:{name}")[7:], "Driver": driver,
                "Scope": "local", "Internal": internal,
                "IPAM": {"Config": [{"Subnet": subnet}] if subnet else []},
                "Options": {"parent": "eth0"} if driver == "macvlan" else {},
                "Labels": ({"com.docker.compose.project": name.rsplit("_", 1)[0]}
                           if name.endswith("_default") else {}),
            })
        return out


class FakeDockerPy:
    """The slice of docker.DockerClient the doctor uses."""

    def __init__(self) -> None:
        self.containers = FakeContainers()
        self.images = FakeImages()
        self.api = FakeAPI()
        self._closed = threading.Event()

    def ping(self) -> bool:
        return True

    def events(self, decode=True, filters=None):
        while not self._closed.wait(3600):
            pass
        return iter(())

    def close(self) -> None:
        self._closed.set()


def evolve(polls: int) -> None:
    """What changes between polls: paperless keeps falling over while its
    database recovers, so the restart-loop rule sees the count climb."""
    paperless = next(b for b in BOXES if b.name == "paperless")
    if polls <= 6 and polls % 2 == 0:
        paperless.restart_count += 2
        paperless.started = datetime.now(timezone.utc)


def container_history(points: int) -> dict:
    """{name: deque of (time, cpu % of host, mem % of limit, bytes)}, a point a
    minute for the last `points` minutes, the way the supervisor keeps it."""
    now = datetime.now(timezone.utc)
    out = {}
    for box in BOXES:
        if box.state != "running":
            continue
        rng = random.Random(box.name)
        limit = box.mem_limit or HOST_MEMORY
        rows = deque(maxlen=points)
        for n in range(points, 0, -1):
            at = now - timedelta(minutes=n)
            busy = (1 - math.cos((at.hour + at.minute / 60 - 4) / 24 * 6.283)) / 2
            cpu = box.cpu * (0.5 + busy) * rng.uniform(0.7, 1.3) / CORES
            mem = min(box.mem * (0.85 + 0.2 * busy) * rng.uniform(0.98, 1.02), limit - 1024 ** 2)
            if box.name == "paperless-db" and at.hour == 2:
                mem = limit - 2 * 1024 ** 2
            rows.append((at, round(cpu, 2), round(mem / limit * 100, 2), int(mem)))
        out[box.name] = rows
    return out


# -- /proc and /sys --------------------------------------------------------------

class FakeHost:
    """A /proc and /sys tree on disk that a thread keeps ticking."""

    def __init__(self, root: Path) -> None:
        self.proc = root / "proc"
        self.sys = root / "sys"
        self.mdstat = self.proc / "mdstat"
        self.booted = time.time() - 23 * 86400 - 4 * 3600
        self.cpu = [0] * 8   # user nice system idle iowait irq softirq steal, in jiffies
        self.disk_io = {"nvme0n1": [0, 0], "sda": [0, 0], "sdb": [0, 0], "md0": [0, 0]}
        self._stop = threading.Event()
        self._write_static()
        self.tick()

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)

    def _write_static(self) -> None:
        self._write(self.proc / "sys" / "kernel" / "osrelease", world.KERNEL + "\n")
        self._write(self.proc / "version",
                    f"Linux version {world.KERNEL} (debian-kernel@lists.debian.org) "
                    "(x86_64-linux-gnu-gcc-14) #1 SMP PREEMPT_DYNAMIC Debian 6.12.48-1\n")
        self._write(self.proc / "mdstat", (
            "Personalities : [raid1] [linear] [raid0] [raid6] [raid5] [raid4] [raid10]\n"
            "md0 : active raid1 sdb1[1] sda1[0]\n"
            "      7813894144 blocks super 1.2 [2/2] [UU]\n"
            "      bitmap: 3/59 pages [12KB], 65536KB chunk\n\n"
            "unused devices: <none>\n"))
        block = self.sys / "block"
        for disk, model, size in (("sda", "WDC WD80EFPX-68C4ZN0", 8_001_563_222_016),
                                  ("sdb", "WDC WD80EFPX-68C4ZN0", 8_001_563_222_016),
                                  ("nvme0n1", "Samsung SSD 990 PRO 1TB", 1_000_204_886_016)):
            self._write(block / disk / "size", f"{size // 512}\n")
            self._write(block / disk / "device" / "model", model + "\n")
        for disk in ("sda", "sdb"):
            (block / disk / f"{disk}1" / "holders" / "md0").mkdir(parents=True, exist_ok=True)
        self._write(block / "md0" / "size", f"{7813894144 * 2}\n")
        self._write(block / "md0" / "dev", "9:0\n")
        self._write(block / "md0" / "md" / "mismatch_cnt", "0\n")
        self._write(block / "md0" / "md" / "last_sync_action", "check\n")
        for member in ("sda1", "sdb1"):
            self._write(block / "md0" / "md" / f"dev-{member}" / "errors", "0\n")
        (block / "md0" / "holders").mkdir(parents=True, exist_ok=True)
        hwmon = self.sys / "class" / "hwmon"
        self._write(hwmon / "hwmon0" / "name", "coretemp\n")
        self._write(hwmon / "hwmon0" / "temp1_label", "Package id 0\n")
        self._write(hwmon / "hwmon0" / "temp1_crit", "100000\n")
        self._write(hwmon / "hwmon1" / "name", "nvme\n")
        self._write(hwmon / "hwmon1" / "temp1_label", "Composite\n")
        self._write(hwmon / "hwmon1" / "temp1_crit", "84850\n")

    def tick(self) -> None:
        busy = random.uniform(0.14, 0.24)
        step = 100 * CORES * 2  # jiffies in two seconds
        for i, share in enumerate((busy * 0.7, 0.0, busy * 0.3, 1 - busy - 0.02, 0.015, 0, 0.005, 0)):
            self.cpu[i] += int(step * share)
        cores = "".join(f"cpu{n} " + " ".join(str(v // CORES) for v in self.cpu) + "\n"
                        for n in range(CORES))
        self._write(self.proc / "stat", "cpu  " + " ".join(map(str, self.cpu)) + "\n" + cores
                    + f"btime {int(self.booted)}\n")
        available = int(HOST_MEMORY * random.uniform(0.38, 0.41))
        kib = 1024
        self._write(self.proc / "meminfo", (
            f"MemTotal:       {HOST_MEMORY // kib} kB\n"
            f"MemFree:        {2 * GIB // kib} kB\n"
            f"MemAvailable:   {available // kib} kB\n"
            f"Buffers:        {300 * 1024} kB\n"
            f"Cached:         {9 * GIB // kib} kB\n"
            f"SwapTotal:      {4 * GIB // kib} kB\n"
            f"SwapFree:       {int(3.7 * GIB) // kib} kB\n"))
        self._write(self.proc / "vmstat", "pswpin 18233\npswpout 40122\n")
        lines = []
        for n, (disk, io) in enumerate(self.disk_io.items()):
            io[0] += random.randint(200, 900)
            io[1] += random.randint(400, 2400)
            major = {"nvme0n1": 259, "md0": 9}.get(disk, 8)
            lines.append(f" {major} {n * 16} {disk} {io[0] // 8} 0 {io[0]} 400 {io[1] // 8} 0 "
                         f"{io[1]} 900 0 {(io[0] + io[1]) // 50} 1300 0 0 0 0 0 0")
        self._write(self.proc / "diskstats", "\n".join(lines) + "\n")
        load = random.uniform(1.1, 1.9)
        self._write(self.proc / "loadavg", f"{load:.2f} {load * 0.9:.2f} {load * 0.8:.2f} 2/1384 99117\n")
        up = time.time() - self.booted
        self._write(self.proc / "uptime", f"{up:.2f} {up * 6.3:.2f}\n")
        for res, avg in (("cpu", 2.1), ("memory", 0.0), ("io", 1.4)):
            self._write(self.proc / "pressure" / res,
                        f"some avg10={avg:.2f} avg60={avg:.2f} avg300={avg:.2f} total=99\n")
        hwmon = self.sys / "class" / "hwmon"
        self._write(hwmon / "hwmon0" / "temp1_input", f"{random.randint(49, 54)}000\n")
        self._write(hwmon / "hwmon1" / "temp1_input", f"{random.randint(44, 47)}850\n")

    def run(self) -> None:
        def loop() -> None:
            while not self._stop.wait(2):
                self.tick()
        threading.Thread(target=loop, name="fake-host", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def disk_usage(paths) -> list[dict]:
    """shutil.disk_usage for the made-up volumes, growing as the clock runs."""
    out = []
    elapsed = (datetime.now(timezone.utc) - world.STARTED).total_seconds() / 86400
    for path in paths:
        total, used, per_day = world.DISKS[path]
        used = int(used + per_day * elapsed)
        free = total - used
        out.append({"path": path, "total_bytes": total, "used_bytes": used, "free_bytes": free,
                    "used_pct": round(used / total * 100, 1)})
    return out


# -- the SMART exporter ----------------------------------------------------------

def smart_metrics() -> str:
    """The test fixture, renamed onto this host's drives: two 8 TB disks in
    the mirror, one of them with sectors remapped, and the system SSD."""
    text = (ROOT / "tests" / "fixtures" / "smartctl_exporter.prom").read_text()
    text = (text.replace('device="sata1"', 'device="sda"').replace('device="sata2"', 'device="sdb"')
            .replace("WDC WD40EFZX-68AWUN0", "WDC WD80EFPX-68C4ZN0")
            .replace("4.000787030016e+12", "8.001563222016e+12")
            .replace("7.814037168e+09", "1.5628053168e+10")
            .replace("Samsung SSD 980 1TB", "Samsung SSD 990 PRO 1TB"))
    text = text.replace('smartctl_device_percentage_used{device="nvme0n1"} 93',
                        'smartctl_device_percentage_used{device="nvme0n1"} 7')
    text = re.sub(r'(attribute_name="Current_Pending_Sector",attribute_value_type="raw",'
                  r'device="sdb"\}) 8', r'\1 0', text)
    # How often each disk has spun up: a few a day over its three years.
    for device, count in (("sda", 3412), ("sdb", 3398)):
        text += (f'smartctl_device_attribute{{attribute_name="Start_Stop_Count",'
                 f'attribute_value_type="raw",device="{device}"}} {count}\n')
    return text


# -- registries ------------------------------------------------------------------

def patch_registries(supervisor_module, versions_module) -> None:
    async def remote_digest(client, reference):
        image = next((b.image for b in BOXES if b.image.endswith(
            f"{reference.repository.rsplit('/', 1)[-1]}:{reference.tag}")), "")
        if world.REGISTRY.get(image, {}).get("update"):
            return digest("remote:" + image), ""
        return digest("local:" + image), ""

    async def list_tags(client, reference):
        return ["placeholder"], ""

    async def settled(client, reference, tags, days, now=None):
        image = next((b.image for b in BOXES if b.image.endswith(
            f"{reference.repository.rsplit('/', 1)[-1]}:{reference.tag}")), "")
        entry = world.REGISTRY.get(image, {})
        newer = entry.get("newer")
        found = None
        if newer:
            tag, level, behind, same_major = newer
            found = versions_module.Newer(reference.tag, tag, level, behind, same_major)
        cooling = None
        if entry.get("cooling"):
            tag, level, days_left = entry["cooling"]
            ready = datetime.now(timezone.utc) + timedelta(days=days_left)
            cooling = {"tag": tag, "level": level, "ready_at": ready.isoformat(timespec="seconds")}
        return found, cooling

    async def release_notes(client, repository, found):
        image = next((b.image for b in BOXES if repository in b.source), "")
        tag = found.newest
        return {"url": f"https://github.com/{repository}/releases/tag/{tag}",
                "breaking": world.REGISTRY.get(image, {}).get("breaking", ""),
                "breaking_url": f"https://github.com/{repository}/releases/tag/{tag}"
                if world.REGISTRY.get(image, {}).get("breaking") else ""}

    supervisor_module.remote_digest = remote_digest
    supervisor_module.list_tags = list_tags
    supervisor_module.settled = settled
    supervisor_module.release_notes = release_notes


# -- connectivity ----------------------------------------------------------------

def patch_connectivity(connectivity_module) -> None:
    latency = {"tcp": (9, 16), "dns": (1, 6), "server": (8, 22), "http": (60, 140)}
    answers = {"cloudflare.com": "104.16.132.229, 104.16.133.229", "github.com": "140.82.121.4"}

    def result(check, when=None):
        low, high = latency[check.kind]
        ok = not (check.kind == "tcp" and random.random() < 0.01)
        detail = answers.get(check.target, "") if check.kind == "dns" else ""
        if check.kind == "http":
            detail = "HTTP 200"
        return connectivity_module.Result(check, ok, round(random.uniform(low, high), 1) if ok else None,
                                          detail if ok else "timed out")

    async def run_round(self, client=None):
        results = [result(check) for check in self.checks]
        self.record(results)
        return results

    connectivity_module.Monitor.run_round = run_round
    connectivity_module.demo_result = result


def backfill_connectivity(monitor, connectivity_module, rounds: int) -> None:
    """The recent rounds a monitor that has been running would remember."""
    now = datetime.now(timezone.utc)
    for n in range(rounds, 0, -1):
        at = (now - timedelta(seconds=monitor.settings.net_check_seconds * n)).isoformat(
            timespec="seconds")
        for check in monitor.checks:
            r = connectivity_module.demo_result(check)
            monitor.history.setdefault(check.key, deque(
                maxlen=connectivity_module.HISTORY_ROUNDS)).append((at, r.ok, r.ms))


# -- the Docker endpoint's permissions -------------------------------------------

def preflight_results(preflight_module):
    allowed = {"ping", "version", "containers", "events", "write", "images", "networks"}
    return [preflight_module.Result(probe, probe.name in allowed,
                                    "HTTP 200" if probe.name in allowed else "HTTP 403")
            for probe in preflight_module.PROBES]

