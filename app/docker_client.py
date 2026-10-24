"""Thin async wrapper around docker-py.

docker-py is synchronous, so every call is pushed onto a worker thread and the
blocking event stream runs in its own daemon thread that feeds an asyncio queue.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
from datetime import datetime
from typing import Any, Iterable

import docker
import requests.adapters
from docker.errors import DockerException

from . import networks, runconfig
from .stacks import resolve_stacks

log = logging.getLogger(__name__)

# Docker event types worth waking up for. Everything else is noise.
# `destroy` and `create` are not problems, but they are how a redeploy is
# recognised - see deploys.py.
WATCHED_ACTIONS = ("die", "oom", "health_status: unhealthy", "kill", "destroy", "create")


class DockerClient:
    def __init__(self, base_url: str, env_key: bytes = b"", max_pool_size: int = 10) -> None:
        self._base_url = base_url
        # Hashes env values in the run-config snapshot; without one no
        # snapshot is taken, rather than one hashed without a key.
        self._env_key = env_key
        # One connection per request in flight: a pool smaller than that
        # throws the extra connections away after each call, logging a
        # "Connection pool is full" warning every time.
        pool = max(10, max_pool_size)
        self._client = docker.DockerClient(base_url=base_url, timeout=30, max_pool_size=pool)
        # docker-py passes max_pool_size only to its unix, npipe and ssh
        # adapters; a tcp:// host - the socket proxy - keeps requests' own
        # pool of 10 unless it is replaced here.
        api = self._client.api
        if api.base_url.startswith("http://"):
            api.mount("http://", requests.adapters.HTTPAdapter(pool_maxsize=pool))
        self._events_thread: threading.Thread | None = None
        self._stop = threading.Event()


    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except DockerException as exc:
            log.warning("docker ping failed: %s", exc)
            return False

    def close(self) -> None:
        self._stop.set()
        try:
            self._client.close()
        except Exception:  # pragma: no cover - best effort on shutdown
            pass


    def _list_containers_sync(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # ignore_removed: a container can vanish between the list and the
        # per-container inspect docker-py issues for each entry.
        for container in self._client.containers.list(all=True, ignore_removed=True):
            attrs = container.attrs or {}
            state = attrs.get("State", {}) or {}
            config = attrs.get("Config", {}) or {}
            health = (state.get("Health") or {}).get("Status") or "none"
            host = attrs.get("HostConfig", {}) or {}
            out.append(
                {
                    "id": container.id,
                    "short_id": container.short_id,
                    "name": container.name,
                    # Config.Image only; reading container.image would hit the
                    # /images endpoint, which the socket proxy keeps closed.
                    "image": config.get("Image") or "",
                    # The image's own ID, from the container: it changes when a
                    # tag is re-pulled onto a new build, which the name above
                    # never shows.
                    "image_id": attrs.get("Image") or "",
                    "created": attrs.get("Created") or "",
                    # The configured cap, 0 when none: the stats' limit reads
                    # as the host's whole memory for an unlimited container.
                    "mem_limit_config": int(host.get("Memory") or 0),
                    "state": state.get("Status", "unknown"),
                    "health": health,
                    "exit_code": state.get("ExitCode"),
                    "started_at": state.get("StartedAt"),
                    "finished_at": state.get("FinishedAt"),
                    "oom_killed": bool(state.get("OOMKilled")),
                    "restart_count": int(attrs.get("RestartCount") or 0),
                    "labels": config.get("Labels") or {},
                    "restart_policy": (host.get("RestartPolicy") or {}).get("Name", ""),
                    # What it was configured to run with - compared when it
                    # is replaced. Env values already hashed or redacted.
                    "run_config": runconfig.snapshot(attrs, self._env_key) if self._env_key else None,
                    # Networks, addresses and published ports - from the
                    # inspect already done; /networks adds what only a
                    # network knows about itself.
                    "network": networks.container_network(attrs),
                }
            )
        resolve_stacks(out)
        return out

    def _image_sync(self, image_ref: str) -> dict[str, Any] | None:
        """RepoDigests and labels for an image, or None when /images is not
        permitted.

        None and an empty result mean different things: the proxy can be run
        with IMAGES=0, so "denied" has to be distinguishable from "this image
        was never pulled from a registry".
        """
        try:
            attrs = self._client.images.get(image_ref).attrs or {}
        except docker.errors.APIError as error:
            if getattr(error, "status_code", None) in (403, 405):
                return None
            return {"repo_digests": [], "labels": {}}
        except Exception:
            return {"repo_digests": [], "labels": {}}
        return {
            "repo_digests": list(attrs.get("RepoDigests") or []),
            # Where the image says it was built from - the way to its release
            # notes without a table of image names to repositories.
            "labels": dict((attrs.get("Config") or {}).get("Labels") or {}),
        }

    async def image(self, image_ref: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._image_sync, image_ref)

    async def repo_digests(self, image_ref: str) -> list[str] | None:
        details = await self.image(image_ref)
        return None if details is None else details["repo_digests"]

    def _networks_sync(self) -> list[dict[str, Any]] | None:
        """Every network as Docker lists it, or None when /networks is not
        permitted - the map then falls back to what the containers say."""
        try:
            return list(self._client.api.networks() or [])
        except docker.errors.APIError as error:
            if getattr(error, "status_code", None) in (403, 405):
                return None
            log.debug("listing networks failed: %s", error)
            return None
        except Exception as exc:
            log.debug("listing networks failed: %s", exc)
            return None

    async def networks(self) -> list[dict[str, Any]] | None:
        return await asyncio.to_thread(self._networks_sync)

    async def list_containers(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_containers_sync)

    def _stats_sync(self, container_id: str) -> dict[str, Any] | None:
        try:
            raw = self._client.containers.get(container_id).stats(stream=False)
        except Exception as exc:
            log.debug("stats failed for %s: %s", container_id, exc)
            return None
        return _parse_stats(raw)

    async def stats(self, container_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._stats_sync, container_id)

    def _logs_sync(
        self,
        container_id: str,
        lines: int,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "tail": lines, "timestamps": True, "stdout": True, "stderr": True
        }
        # `since`/`until` bound the window; with both set, `tail` still caps the
        # volume, so a chatty container in a wide window cannot flood the page.
        if since is not None:
            kwargs["since"] = since
        if until is not None:
            kwargs["until"] = until
        try:
            raw = self._client.containers.get(container_id).logs(**kwargs)
        except Exception as exc:
            # Reading logs must never break a poll cycle.
            return f"<could not read logs: {exc}>"
        return raw.decode("utf-8", errors="replace")

    async def logs(
        self,
        container_id: str,
        lines: int = 120,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> str:
        return await asyncio.to_thread(self._logs_sync, container_id, lines, since, until)

    def _inspect_sync(self, container_id: str) -> dict[str, Any] | None:
        try:
            return self._client.containers.get(container_id).attrs
        except Exception:
            return None

    async def inspect(self, container_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._inspect_sync, container_id)


    def _restart_sync(self, container_id: str, timeout: int) -> str:
        container = self._client.containers.get(container_id)
        container.restart(timeout=timeout)
        return f"restarted {container.name}"

    async def restart(self, container_id: str, timeout: int = 20) -> str:
        return await asyncio.to_thread(self._restart_sync, container_id, timeout)

    def _stop_sync(self, container_id: str, timeout: int) -> str:
        container = self._client.containers.get(container_id)
        container.stop(timeout=timeout)
        return f"stopped {container.name}"

    async def stop(self, container_id: str, timeout: int = 20) -> str:
        return await asyncio.to_thread(self._stop_sync, container_id, timeout)

    def _prune_images_sync(self) -> str:
        result = self._client.images.prune(filters={"dangling": True})
        reclaimed = int(result.get("SpaceReclaimed") or 0)
        deleted = len(result.get("ImagesDeleted") or [])
        return f"pruned {deleted} dangling image(s), reclaimed {reclaimed / 1_000_000:.1f} MB"

    async def prune_images(self) -> str:
        return await asyncio.to_thread(self._prune_images_sync)


    def start_event_watcher(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        """Stream Docker events into `queue` from a daemon thread."""
        if self._events_thread and self._events_thread.is_alive():
            return

        def run() -> None:
            backoff = 1
            while not self._stop.is_set():
                try:
                    stream = self._client.events(
                        decode=True, filters={"type": ["container"]}
                    )
                    backoff = 1
                    for event in stream:
                        if self._stop.is_set():
                            break
                        action = event.get("Action", "")
                        if not any(action.startswith(w) for w in WATCHED_ACTIONS):
                            continue
                        loop.call_soon_threadsafe(_offer, queue, event)
                except Exception as exc:  # network blips, proxy restarts, ...
                    if self._stop.is_set():
                        break
                    log.warning("docker event stream dropped (%s); retrying in %ss", exc, backoff)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 60)

        self._events_thread = threading.Thread(target=run, name="docker-events", daemon=True)
        self._events_thread.start()


def _offer(queue: asyncio.Queue, item: Any) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        log.warning("event queue full, dropping docker event")


def _parse_stats(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn a raw one-shot stats payload into cpu%/mem% numbers."""
    cpu_pct = 0.0
    online = 1
    try:
        cpu = raw["cpu_stats"]
        pre = raw["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        system_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        online = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or []) or 1
        if cpu_delta > 0 and system_delta > 0:
            cpu_pct = (cpu_delta / system_delta) * online * 100.0
    except (KeyError, TypeError, ZeroDivisionError):
        cpu_pct = 0.0

    mem_usage = 0
    mem_limit = 0
    try:
        mem = raw["memory_stats"]
        # `cache` double-counts page cache on cgroup v1; v2 exposes it as
        # inactive_file. Subtracting it matches what `docker stats` reports.
        stats_block = mem.get("stats") or {}
        cache = stats_block.get("inactive_file", stats_block.get("cache", 0)) or 0
        mem_usage = max(int(mem.get("usage", 0)) - int(cache), 0)
        mem_limit = int(mem.get("limit", 0))
    except (KeyError, TypeError, ValueError):
        pass

    mem_pct = (mem_usage / mem_limit * 100.0) if mem_limit else 0.0
    return {
        # `cpu_pct` uses `docker stats` semantics: 100% means one full core, so
        # it can exceed 100 on a multi-core host. `cpu_host_pct` is the share of
        # the whole machine, which is what alert thresholds should key off.
        "cpu_pct": round(cpu_pct, 2),
        "cpu_host_pct": round(cpu_pct / online, 2) if online else 0.0,
        "online_cpus": online,
        "mem_usage_bytes": mem_usage,
        "mem_limit_bytes": mem_limit,
        "mem_pct": round(mem_pct, 2),
    }


def disk_usage(paths: Iterable[str]) -> list[dict[str, Any]]:
    """Usage for host paths mounted into the doctor container."""
    out: list[dict[str, Any]] = []
    for path in paths:
        try:
            usage = shutil.disk_usage(path)
            fsid = os.statvfs(path).f_fsid
        except OSError as exc:
            log.debug("disk usage failed for %s: %s", path, exc)
            continue
        # df-style: percentage of the space actually available to this user,
        # which is what "the volume is full" means in practice.
        denominator = usage.used + usage.free
        used_pct = (usage.used / denominator * 100.0) if denominator else 0.0
        out.append(
            {
                "path": path,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "used_pct": round(used_pct, 1),
                # Which filesystem answered: the path stays the same when its
                # mount is moved to another volume, and the history must not
                # run on across that as if one disk had emptied. 0 is "no idea".
                "fs_id": f"{fsid:x}" if fsid else None,
            }
        )
    return out
