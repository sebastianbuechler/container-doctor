"""Prove what the configured Docker endpoint actually permits.

The doctor is usually pointed at its own socket proxy, but a homelab often
already runs one for Traefik or Homepage with quite different settings, and
`DOCKER_HOST` can be repointed at it. Guessing is bad in both directions: too
narrow and the doctor silently can't do its job; too wide and it is holding
more of the host than it needs.

So ask. Every probe below is non-destructive - the write probe targets a
container id that cannot exist, so an allowed endpoint answers 404 and a
blocked one answers 403.

Run it by hand any time:

    docker compose exec container-doctor python -m app.preflight
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Literal

import httpx

from .config import load_settings

log = logging.getLogger(__name__)

# A container id that is valid in form and cannot exist, so probing a write
# endpoint with it changes nothing.
_NOWHERE = "0" * 64

Need = Literal["required", "optional", "should_be_blocked"]


@dataclass
class Probe:
    name: str
    method: str
    path: str
    need: Need
    why: str


PROBES: tuple[Probe, ...] = (
    # Required.
    Probe("ping", "GET", "/_ping", "required", "docker-py's connection check"),
    Probe("version", "GET", "/version", "required", "docker-py's API version handshake"),
    Probe("containers", "GET", "/containers/json?all=1", "required",
          "list, inspect, logs and stats all live under /containers"),
    Probe("events", "GET", "/events?since=0&until=0", "required",
          "the die/oom/health event stream"),
    # Needed only for what they enable.
    Probe("write", "POST", f"/containers/{_NOWHERE}/restart", "optional",
          "restart and stop actions; without it the doctor is observe-only"),
    Probe("images", "GET", "/images/json", "optional",
          "reading image digests for the update check, and the prune_images action"),
    Probe("networks", "GET", "/networks", "optional",
          "network drivers, the internal flag and unused networks on the network map"),
    # Should be shut - the doctor has no use for them.
    Probe("exec", "GET", f"/exec/{_NOWHERE}/json", "should_be_blocked",
          "running commands inside containers"),
    Probe("volumes", "GET", "/volumes", "should_be_blocked", "reading and deleting volumes"),
    Probe("secrets", "GET", "/secrets", "should_be_blocked", "swarm secrets"),
    Probe("swarm", "GET", "/swarm", "should_be_blocked", "cluster control"),
    Probe("info", "GET", "/info", "should_be_blocked", "host details and config"),
)


@dataclass
class Result:
    probe: Probe
    allowed: bool
    detail: str

    @property
    def ok(self) -> bool:
        if self.probe.need == "required":
            return self.allowed
        if self.probe.need == "should_be_blocked":
            return not self.allowed
        return True  # optional: either answer is fine


def http_base(docker_host: str) -> tuple[str, str | None]:
    """Turn a DOCKER_HOST into (base_url, unix_socket_path)."""
    host = (docker_host or "").strip()
    if host.startswith("unix://"):
        return "http://localhost", host[len("unix://"):]
    for prefix in ("tcp://", "http://", "https://"):
        if host.startswith(prefix):
            scheme = "https" if prefix == "https://" else "http"
            return f"{scheme}://{host[len(prefix):]}".rstrip("/"), None
    return f"http://{host}".rstrip("/"), None


async def probe_endpoint(docker_host: str, timeout: float = 5.0) -> list[Result]:
    """Probe every endpoint. Never raises, and never takes long."""
    base, uds = http_base(docker_host)
    transport = httpx.AsyncHTTPTransport(uds=uds) if uds else None

    async with httpx.AsyncClient(base_url=base, timeout=timeout, transport=transport) as client:
        results: list[Result] = []
        for probe in PROBES:
            try:
                response = await client.request(probe.method, probe.path)
            except httpx.TimeoutException:
                # Held open, which /events does on a daemon that ignores
                # `until`. A blocked endpoint would have 403'd immediately.
                results.append(Result(probe, True, "open (streaming)"))
            except httpx.RequestError as exc:
                results.append(Result(probe, False, f"unreachable: {exc.__class__.__name__}"))
            else:
                # A proxy answers 403 for an endpoint it does not expose.
                # Anything else - including the 404 for the id that cannot
                # exist - means the request reached the daemon.
                blocked = response.status_code in (403, 405)
                results.append(Result(probe, not blocked, f"HTTP {response.status_code}"))
        return results


def summarize(
    docker_host: str, results: list[Result], mode: str | None = None
) -> tuple[list[str], list[str]]:
    """Return (blocking_problems, advisory_warnings)."""
    mode = mode if mode is not None else load_settings().mode
    problems: list[str] = []
    warnings: list[str] = []
    by_name = {r.probe.name: r for r in results}

    if results and all(not r.allowed and "unreachable" in r.detail for r in results):
        return ([f"Docker endpoint {docker_host} is unreachable."], [])

    missing = [r for r in results if r.probe.need == "required" and not r.allowed]
    if missing:
        problems.append(
            "Docker endpoint is missing access the doctor needs: "
            + ", ".join(f"{r.probe.name} ({r.probe.why})" for r in missing)
            + ". If this is a proxy you already run for something else, it is too narrow -"
            " use the bundled one instead."
        )

    write = by_name.get("write")
    if write and not write.allowed:
        warnings.append(
            "Docker endpoint is read-only, so no action can ever be executed. That is a fine "
            "posture for observe mode - set DOCTOR_PROXY_ALLOW_POST=1 (or point at a proxy "
            "that permits POST) when you want suggest/auto to be able to act."
        )

    if mode == "auto" and write and not write.allowed:
        problems.append(
            "DOCTOR_MODE=auto but the Docker endpoint refuses writes - approved actions "
            "will fail. Fix one or the other."
        )

    wide = [r for r in results if r.probe.need == "should_be_blocked" and r.allowed]
    if wide:
        warnings.append(
            "Docker endpoint exposes more than the doctor needs: "
            + ", ".join(f"{r.probe.name} ({r.probe.why})" for r in wide)
            + ". Harmless in itself - the doctor never calls them - but it widens what a "
            "compromise of this container would reach."
        )

    if docker_host.startswith("unix://"):
        warnings.append(
            "DOCKER_HOST is a raw Docker socket, not a proxy. The doctor then holds full "
            "control of the host. The bundled socket proxy exists to avoid exactly this."
        )
    return problems, warnings


def render(docker_host: str, results: list[Result]) -> str:
    lines = [f"Docker endpoint: {docker_host}", ""]
    groups: dict[Need, str] = {
        "required": "Required",
        "optional": "Optional",
        "should_be_blocked": "Should be blocked",
    }
    for need, heading in groups.items():
        lines.append(heading)
        for result in (r for r in results if r.probe.need == need):
            mark = "ok  " if result.ok else "WARN"
            state = "allowed" if result.allowed else "blocked"
            lines.append(f"  [{mark}] {result.probe.name:<11} {state:<8} {result.detail}")
        lines.append("")

    problems, warnings = summarize(docker_host, results)
    for problem in problems:
        lines.append(f"PROBLEM: {problem}")
    for warning in warnings:
        lines.append(f"NOTE:    {warning}")
    if not problems and not warnings:
        lines.append("This endpoint grants exactly what the doctor needs and nothing more.")
    return "\n".join(lines)


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    results = await probe_endpoint(settings.docker_host)
    print(render(settings.docker_host, results))
    problems, _ = summarize(settings.docker_host, results)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
