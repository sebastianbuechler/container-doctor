"""Is the host online, and does DNS work? Checked from inside the doctor.

Every check runs from the doctor's own container, which reaches the outside
the way every other container does: through Docker's bridge and its embedded
DNS, which forwards to the host's resolver. Four kinds, each answering a
different question, so a failure says where the problem is:

- tcp:    a TCP connection to an address, no name involved. All of them
          failing means the internet is unreachable - router, modem, ISP.
- dns:    a name resolved the way containers resolve names. Failing while
          tcp works means DNS is broken, not the line.
- server: a name asked of one DNS server directly (a Pi-hole, AdGuard, the
          router). Failing while the others answer points at that server.
- http:   an HTTPS request, through DNS and TLS end to end.

Each check is a few packets once a minute by default, and nothing here opens
a listening port.
"""

from __future__ import annotations

import asyncio
import random
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

# Rounds kept in memory for the dashboard's strips: two hours at the default.
HISTORY_ROUNDS = 120
NET_CONTAINER = {"id": "", "name": "host:network", "image": "", "labels": {}}
NET_KINDS = ("net_internet", "net_dns", "net_dns_server", "net_http")


@dataclass
class Check:
    kind: str           # tcp | dns | server | http
    target: str         # what is checked: host:port, a name, a server, a URL
    name: str = ""      # for a server check: the name asked for
    label: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.target}"


@dataclass
class Result:
    check: Check
    ok: bool
    ms: float | None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.check.kind, "target": self.check.target, "key": self.check.key,
                "label": self.check.label, "ok": self.ok, "ms": self.ms, "detail": self.detail}


def plan(settings) -> list[Check]:
    """The checks the settings ask for."""
    checks: list[Check] = []
    for target in settings.net_tcp_targets:
        checks.append(Check("tcp", target, label=f"Reach {target}"))
    for name in settings.net_dns_names:
        checks.append(Check("dns", name, label=f"Resolve {name}"))
    probe = settings.net_dns_names[0] if settings.net_dns_names else "cloudflare.com"
    for server in settings.net_dns_servers:
        checks.append(Check("server", server, name=probe, label=f"Ask {server} for {probe}"))
    for url in settings.net_http_urls:
        checks.append(Check("http", url, label=f"Fetch {url}"))
    return checks


# -- the checks -------------------------------------------------------------------

async def check_tcp(check: Check, timeout: float) -> Result:
    host, _, port = check.target.rpartition(":")
    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout)
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        return Result(check, False, None, _why(exc))
    ms = (time.monotonic() - started) * 1000
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return Result(check, True, round(ms, 1))


async def check_dns(check: Check, timeout: float) -> Result:
    """The system resolver, as any container would use it."""
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(check.target, None, type=socket.SOCK_STREAM), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        return Result(check, False, None, _why(exc))
    addresses = sorted({info[4][0] for info in infos})
    return Result(check, True, round((time.monotonic() - started) * 1000, 1),
                  ", ".join(addresses[:3]))


async def check_server(check: Check, timeout: float) -> Result:
    """One DNS server asked directly, over UDP - no resolver library needed."""
    host, _, port = check.target.partition(":")
    query_id = random.randint(0, 0xFFFF)
    packet = build_query(query_id, check.name)
    loop = asyncio.get_running_loop()
    answer: asyncio.Future = loop.create_future()

    class Protocol(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            if not answer.done():
                answer.set_result(data)

        def error_received(self, exc):
            if not answer.done():
                answer.set_exception(exc)

    started = time.monotonic()
    transport = None
    try:
        transport, _ = await loop.create_datagram_endpoint(
            Protocol, remote_addr=(host, int(port or 53)))
        transport.sendto(packet)
        data = await asyncio.wait_for(answer, timeout)
        ms = round((time.monotonic() - started) * 1000, 1)
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        return Result(check, False, None, _why(exc))
    finally:
        if transport is not None:
            transport.close()
    try:
        rcode, addresses = parse_response(data, query_id)
    except ValueError as exc:
        return Result(check, False, ms, str(exc))
    if rcode != 0:
        return Result(check, False, ms, RCODES.get(rcode, f"error code {rcode}"))
    if not addresses:
        return Result(check, False, ms, "no address in the answer")
    return Result(check, True, ms, ", ".join(addresses[:3]))


async def check_http(check: Check, timeout: float, client: httpx.AsyncClient) -> Result:
    started = time.monotonic()
    try:
        response = await client.get(check.target, timeout=timeout)
    except httpx.HTTPError as exc:
        return Result(check, False, None, _why(exc))
    ms = round((time.monotonic() - started) * 1000, 1)
    # Any answer from the server means the path works; a 5xx is the site's
    # problem, not the host's connection.
    return Result(check, response.status_code < 500, ms, f"HTTP {response.status_code}")


def _why(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out"
    if isinstance(exc, socket.gaierror):
        return "name not resolved"
    text = str(exc) or exc.__class__.__name__
    return text[:120]


# -- a DNS message, by hand --------------------------------------------------------

RCODES = {1: "format error", 2: "server failure", 3: "no such name", 4: "not implemented",
          5: "refused"}


def build_query(query_id: int, name: str) -> bytes:
    """An A query with recursion desired."""
    header = struct.pack(">HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    labels = b"".join(bytes([len(part)]) + part.encode("idna")
                      for part in name.rstrip(".").split(".") if part)
    return header + labels + b"\x00" + struct.pack(">HH", 1, 1)


def _skip_name(data: bytes, offset: int) -> int:
    while True:
        if offset >= len(data):
            raise ValueError("truncated answer")
        length = data[offset]
        if length & 0xC0 == 0xC0:  # a pointer ends the name
            return offset + 2
        if length == 0:
            return offset + 1
        offset += 1 + length


def parse_response(data: bytes, query_id: int) -> tuple[int, list[str]]:
    """The response code and the IPv4 addresses in the answer."""
    if len(data) < 12:
        raise ValueError("truncated answer")
    rid, flags, qdcount, ancount, _, _ = struct.unpack(">HHHHHH", data[:12])
    if rid != query_id:
        raise ValueError("answer to a different question")
    if not flags & 0x8000:
        raise ValueError("not a response")
    offset = 12
    for _ in range(qdcount):
        offset = _skip_name(data, offset) + 4
    addresses = []
    for _ in range(ancount):
        offset = _skip_name(data, offset)
        if offset + 10 > len(data):
            raise ValueError("truncated answer")
        rtype, _, _, length = struct.unpack(">HHIH", data[offset:offset + 10])
        offset += 10
        if rtype == 1 and length == 4:
            addresses.append(socket.inet_ntoa(data[offset:offset + 4]))
        offset += length
    return flags & 0x000F, addresses


# -- rounds and verdicts ------------------------------------------------------------

@dataclass
class Monitor:
    """Runs every check once per round, keeps the recent rounds, and decides
    what lasting failure each round adds up to."""

    settings: Any
    checks: list[Check] = field(default_factory=list)
    history: dict[str, deque] = field(default_factory=dict)
    latest: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_round_at: str | None = None
    verdict: dict[str, Any] = field(default_factory=dict)
    _bad: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.checks = self.checks or plan(self.settings)

    async def run_round(self, client: httpx.AsyncClient | None = None) -> list[Result]:
        timeout = self.settings.net_timeout_seconds
        jobs = []
        for check in self.checks:
            if check.kind == "tcp":
                jobs.append(check_tcp(check, timeout))
            elif check.kind == "dns":
                jobs.append(check_dns(check, timeout))
            elif check.kind == "server":
                jobs.append(check_server(check, timeout))
            elif check.kind == "http" and client is not None:
                jobs.append(check_http(check, timeout, client))
        results = list(await asyncio.gather(*jobs))
        self.record(results)
        return results

    def record(self, results: list[Result]) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.last_round_at = now
        for result in results:
            key = result.check.key
            self.history.setdefault(key, deque(maxlen=HISTORY_ROUNDS)).append(
                (now, result.ok, result.ms))
            self.latest[key] = {**result.to_dict(), "at": now}
        self.verdict = verdict(results)

    def evaluate(self) -> list[tuple[str, str, str, str, dict, bool]]:
        """What has been failing for net_fail_rounds rounds in a row.

        Each as (kind, key, severity, summary, details, unchanged): key is the
        signature that tells one server or URL from another, and unchanged is
        true once it has already been reported, so later rounds keep its
        incident open without counting new sightings. Recovery is the
        supervisor's to judge, against the incidents actually open.
        """
        needed = max(1, self.settings.net_fail_rounds)
        failing = self.verdict.get("failing", {})
        for slot in list(self._bad):
            if slot not in failing:
                del self._bad[slot]
        firing = []
        for slot, (severity, summary, details) in failing.items():
            self._bad[slot] = self._bad.get(slot, 0) + 1
            if self._bad[slot] >= needed:
                kind, _, key = slot.partition("|")
                firing.append((kind, key, severity, summary, details, self._bad[slot] > needed))
        return firing

    def view(self) -> dict[str, Any]:
        rows = []
        for check in self.checks:
            history = list(self.history.get(check.key, ()))
            oks = [ok for _, ok, _ in history]
            rows.append({
                **(self.latest.get(check.key) or {"kind": check.kind, "target": check.target,
                                                  "key": check.key, "label": check.label,
                                                  "ok": None, "ms": None, "detail": ""}),
                "history": [[at, ok, ms] for at, ok, ms in history],
                "uptime_pct": round(100.0 * sum(oks) / len(oks), 1) if oks else None,
            })
        return {
            "checks": rows,
            "last_round_at": self.last_round_at,
            "interval_seconds": self.settings.net_check_seconds,
            "internet": self.verdict.get("internet"),
            "dns": self.verdict.get("dns"),
            "failing": sorted(self.verdict.get("failing", {})),
        }


def verdict(results: list[Result]) -> dict[str, Any]:
    """What one round says, as conditions keyed by kind|signature.

    Ordered so each failure is blamed once: no internet explains failing DNS
    and HTTPS too, and failing DNS explains failing HTTPS.
    """
    by_kind: dict[str, list[Result]] = {}
    for result in results:
        by_kind.setdefault(result.check.kind, []).append(result)
    tcp, dns = by_kind.get("tcp", []), by_kind.get("dns", [])
    internet = any(r.ok for r in tcp) if tcp else None
    resolving = any(r.ok for r in dns) if dns else None
    failing: dict[str, tuple[str, str, dict]] = {}

    def detail(rs: list[Result]) -> dict:
        return {"checks": [r.to_dict() for r in rs]}

    if internet is False:
        failing["net_internet|"] = (
            "critical",
            f"The host cannot reach the internet: none of {', '.join(r.check.target for r in tcp)} "
            "answer",
            detail(tcp))
        return {"internet": False, "dns": resolving, "failing": failing}
    if resolving is False:
        failing["net_dns|"] = (
            "critical",
            f"DNS is failing on the host: {', '.join(r.check.target for r in dns)} do not resolve "
            f"({dns[0].detail}), though the internet is reachable",
            detail(dns))
    servers = by_kind.get("server", [])
    if any(r.ok for r in servers) or resolving:
        for r in servers:
            if not r.ok:
                failing[f"net_dns_server|{r.check.target}"] = (
                    "warning",
                    f"The DNS server {r.check.target} does not answer for {r.check.name} "
                    f"({r.detail}) while other lookups work",
                    detail([r]))
    if resolving is not False:
        for r in by_kind.get("http", []):
            if not r.ok:
                failing[f"net_http|{r.check.target}"] = (
                    "warning",
                    f"{r.check.target} cannot be fetched from the host ({r.detail})",
                    detail([r]))
    return {"internet": internet, "dns": resolving, "failing": failing}
