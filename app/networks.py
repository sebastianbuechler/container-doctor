"""Which Docker networks there are, who is on them, and what is reachable.

Membership, addresses and published ports come from what inspecting a
container already returns. What only a network knows about itself - its
driver, whether it is internal, its subnets, that nobody is on it - comes from
`GET /networks`, which the bundled proxy permits read-only. Pointed at a proxy
that refuses it, the map still works: a network nobody is attached to does not
show, and `internal` is inferred from containers on it getting no gateway.

Nothing here opens an incident. The findings are wiring mistakes that cost an
evening to spot by hand - Traefik pointed at a network the container is not
on, a service published on every interface as well as behind the proxy - and
they are shown, not acted on. One that is meant can be dismissed: each has a
`key` that stays the same from poll to poll, and dismissed ones are listed
apart, out of the count.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from .stacks import compose_info

# Modes that are not a network of their own.
_SPECIAL_MODES = ("host", "none")
# Compose adds the container's short id as an alias on every network.
_ID_ALIAS = re.compile(r"^[0-9a-f]{12,64}$")


def container_network(attrs: dict[str, Any]) -> dict[str, Any]:
    """The network facts out of one container's inspect output."""
    settings = attrs.get("NetworkSettings") or {}
    host = attrs.get("HostConfig") or {}
    name = (attrs.get("Name") or "").lstrip("/")
    attached = []
    for net_name, net in sorted((settings.get("Networks") or {}).items()):
        net = net or {}
        aliases = net.get("Aliases") or []
        # Docker 25+ lists every name the container answers to on the network.
        aliases = list(dict.fromkeys([*aliases, *(net.get("DNSNames") or [])]))
        attached.append({
            "name": net_name,
            "id": (net.get("NetworkID") or "")[:12],
            "ip": net.get("IPAddress") or "",
            "prefix": int(net.get("IPPrefixLen") or 0),
            "gateway": net.get("Gateway") or "",
            "ipv6": net.get("GlobalIPv6Address") or "",
            "aliases": sorted(a for a in aliases if a and a != name and not _ID_ALIAS.match(a)),
        })
    return {
        "mode": host.get("NetworkMode") or "",
        "networks": attached,
        "ports": _ports(settings.get("Ports"), host.get("PortBindings")),
        "traefik_default": _traefik_default_network(attrs.get("Config") or {}),
    }


_TRAEFIK_FLAG = "--providers.docker.network"
_TRAEFIK_ENV = "TRAEFIK_PROVIDERS_DOCKER_NETWORK="


def _traefik_default_network(config: dict[str, Any]) -> str:
    """The network Traefik uses for every container without a
    traefik.docker.network label, when this container is a Traefik started
    with --providers.docker.network or its environment variable. A setting
    in a mounted traefik.yml cannot be seen from here."""
    args = [*(config.get("Entrypoint") or []), *(config.get("Cmd") or [])]
    for i, arg in enumerate(args):
        arg = str(arg)
        if arg.startswith(_TRAEFIK_FLAG + "="):
            return arg.split("=", 1)[1].strip()
        if arg == _TRAEFIK_FLAG and i + 1 < len(args):
            return str(args[i + 1]).strip()
    for entry in config.get("Env") or []:
        if str(entry).startswith(_TRAEFIK_ENV):
            return str(entry)[len(_TRAEFIK_ENV):].strip()
    return ""


def _ports(live: dict | None, configured: dict | None) -> list[dict[str, Any]]:
    """Published ports. The live map is empty while a container is stopped,
    so the configured bindings stand in for it then."""
    source = live if any(live.values() if live else ()) else configured
    out = []
    seen = set()
    for spec, bindings in (source or {}).items():
        if not bindings:
            continue  # EXPOSEd, not published
        port, _, proto = spec.partition("/")
        for binding in bindings:
            host_ip = (binding or {}).get("HostIp") or ""
            host_port = (binding or {}).get("HostPort") or ""
            # Docker lists 0.0.0.0 and :: separately for one dual-stack bind.
            key = (_all_interfaces(host_ip), host_port, port, proto)
            if not host_port or key in seen:
                continue
            seen.add(key)
            out.append({"host_ip": host_ip or "0.0.0.0", "host_port": host_port,
                        "container_port": port, "proto": proto or "tcp"})
    out.sort(key=lambda p: (_port_number(p["host_port"]), p["proto"]))
    return out


def _all_interfaces(host_ip: str) -> bool:
    return host_ip in ("", "0.0.0.0", "::")


def _port_number(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _subnet(ip: str, prefix: int) -> str:
    if not ip or not prefix:
        return ""
    try:
        return str(ipaddress.ip_interface(f"{ip}/{prefix}").network)
    except ValueError:
        return ""


def report(containers: list[dict[str, Any]],
           inventory: list[dict[str, Any]] | None = None,
           dismissed: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Networks, members and findings for the dashboard, from the last poll.

    `inventory` is Docker's own network list, or None when it could not be
    read - then everything is worked out from the containers alone.
    `dismissed` maps a finding's key to who dismissed it and when; those
    findings move to their own list.
    """
    networks: dict[str, dict[str, Any]] = {}
    host_mode: list[str] = []
    isolated: list[str] = []
    shared: list[dict[str, str]] = []
    exposed: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    multi_homed = 0
    # Traefik's own default for containers without traefik.docker.network.
    # Two Traefiks that disagree leave it unknown.
    defaults = {(c.get("network") or {}).get("traefik_default") for c in containers} - {"", None}
    traefik_default = next(iter(defaults)) if len(defaults) == 1 else ""

    for container in sorted(containers, key=lambda c: c.get("name", "")):
        net = container.get("network")
        if not net:
            continue
        name = container["name"]
        info = compose_info(container.get("labels"))
        mode = net.get("mode") or ""
        member = {"name": name, "stack": info["project"], "service": info["service"],
                  "state": container.get("state"), "health": container.get("health")}

        if mode == "host":
            host_mode.append(name)
        elif mode == "none":
            isolated.append(name)
        elif mode.startswith("container:") or mode.startswith("service:"):
            # Shares another container's network stack; Docker reports no
            # networks of its own, so say whose it is.
            shared.append({"name": name, "via": mode.split(":", 1)[1]})

        attached = net.get("networks") or []
        if mode not in _SPECIAL_MODES:
            if len(attached) > 1:
                multi_homed += 1
            for entry in attached:
                n = networks.setdefault(entry["name"], {
                    "name": entry["name"], "id": entry["id"], "subnet": "", "gateway": "",
                    "internal": True, "members": [],
                })
                n["subnet"] = n["subnet"] or _subnet(entry["ip"], entry["prefix"])
                if entry["gateway"]:
                    n["gateway"] = entry["gateway"]
                    n["internal"] = False
                n["members"].append({**member, "ip": entry["ip"], "aliases": entry["aliases"]})

        ports = net.get("ports") or []
        if ports:
            exposed.append({**member, "ports": ports})
        findings.extend(_findings(container, net, ports, traefik_default))

    for n in networks.values():
        # An internal network is a guess from the absence of a gateway, and
        # only a guess worth making once someone running is on it.
        n["internal"] = n["internal"] and any(m["state"] == "running" for m in n["members"])
    if inventory is not None:
        _merge_inventory(networks, inventory)

    out_networks = []
    in_use = _templates_in_use(list(networks.values()))
    for n in networks.values():
        running = [m for m in n["members"] if m["state"] == "running"]
        n.setdefault("source", "containers")
        n["unused"] = _unused(n, in_use)
        n["kind"] = _kind(n["name"])
        n["stacks"] = sorted({m["stack"] for m in n["members"] if m["stack"]})
        n["running"] = len(running)
        out_networks.append(n)
    # Shared networks first - they are what holds the homelab together - then
    # the per-stack ones by name.
    out_networks.sort(key=lambda n: (-len(n["stacks"]), -len(n["members"]), n["name"]))

    findings.extend(_overlaps(out_networks))
    findings.extend(_network_findings(out_networks))
    findings.sort(key=lambda f: (_SEVERITY_RANK[f["severity"]], f.get("container", ""), f["kind"]))
    for f in findings:
        f["key"] = f"{f['kind']}:{f['container'] or f.pop('subject', '')}"
    dismissed = dismissed or {}
    set_aside = [{**f, **dismissed[f["key"]]} for f in findings if f["key"] in dismissed]
    findings = [f for f in findings if f["key"] not in dismissed]
    return {
        "networks": out_networks,
        "host_mode": host_mode,
        "isolated": isolated,
        "shared_stack": shared,
        "exposed": exposed,
        "findings": findings,
        "dismissed": set_aside,
        # Whether drivers, flags and unused networks are Docker's word or
        # missing, for the page to say which.
        "inventory": inventory is not None,
        "summary": {
            "networks": len(out_networks),
            "multi_homed": multi_homed,
            "published_ports": sum(len(e["ports"]) for e in exposed),
            "host_mode": len(host_mode),
            "unused": sum(1 for n in out_networks
                          if _unused(n, _templates_in_use(out_networks))),
            "warnings": sum(1 for f in findings if f["severity"] == "warning"),
        },
    }


# Docker's own; never something to clean up or draw.
_BUILTIN = ("host", "none")
_ICC = "com.docker.network.bridge.enable_icc"


def _merge_inventory(networks: dict[str, dict[str, Any]],
                     inventory: list[dict[str, Any]]) -> None:
    """Docker's facts about each network over the inferred ones, and the
    networks nobody is on added."""
    for raw in inventory:
        name = raw.get("Name") or ""
        if not name or name in _BUILTIN:
            continue
        options = raw.get("Options") or {}
        labels = raw.get("Labels") or {}
        subnets = [c.get("Subnet") for c in ((raw.get("IPAM") or {}).get("Config") or [])
                   if c and c.get("Subnet")]
        n = networks.get(name)
        if n is None:
            if name == "bridge":
                continue  # always there; only news when someone uses it
            n = networks[name] = {"name": name, "id": (raw.get("Id") or "")[:12],
                                  "subnet": "", "gateway": "", "members": []}
        n.update({
            "source": "docker",
            "driver": raw.get("Driver") or "",
            "internal": bool(raw.get("Internal")),
            "subnets": subnets,
            "ipv6": bool(raw.get("EnableIPv6")),
            # Containers on it cannot talk to each other, only out.
            "icc": str(options.get(_ICC, "true")).lower() != "false",
            "parent": options.get("parent", ""),
            "project": labels.get("com.docker.compose.project", ""),
            "created": raw.get("Created") or "",
            # `docker network create --config-only` holds the settings a
            # macvlan is then created from with --config-from. It never has
            # containers of its own, and shares its child's subnet.
            "config_only": bool(raw.get("ConfigOnly")),
            "config_from": (raw.get("ConfigFrom") or {}).get("Network", ""),
        })
        n["subnet"] = next((s for s in subnets if ":" not in s), "") or n["subnet"]


def _unused(n: dict[str, Any], templates_in_use: set[str]) -> bool:
    if n.get("source") != "docker" or n["members"]:
        return False
    return not (n.get("config_only") and n["name"] in templates_in_use)


def _templates_in_use(networks: list[dict[str, Any]]) -> set[str]:
    return {n["config_from"] for n in networks if n.get("config_from")}


def _network_findings(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    in_use = _templates_in_use(networks)
    for n in networks:
        name = n["name"]
        if _unused(n, in_use):
            origin = f" (from stack {n['project']})" if n.get("project") else ""
            out.append({"kind": "unused_network", "severity": "info", "container": "", "subject": name,
                        "summary": f"Nobody is on {name}{origin}. Left over from a removed "
                                   "stack, or for one that is down - docker network rm "
                                   "clears it if it is not coming back."})
    return out


def _kind(name: str) -> str:
    if name == "bridge":
        return "default"
    if name.endswith("_default"):
        return "stack"
    return "named"


_SEVERITY_RANK = {"warning": 0, "info": 1}


def _findings(container: dict[str, Any], net: dict[str, Any],
              ports: list[dict[str, Any]], traefik_default: str = "") -> list[dict[str, Any]]:
    labels = container.get("labels") or {}
    name = container["name"]
    mode = net.get("mode") or ""
    names = [n["name"] for n in net.get("networks") or []]
    out: list[dict[str, Any]] = []

    def add(kind: str, severity: str, summary: str) -> None:
        out.append({"kind": kind, "severity": severity, "container": name, "summary": summary})

    routed = str(labels.get("traefik.enable", "")).lower() == "true"
    wanted = labels.get("traefik.docker.network", "")
    source = "traefik.docker.network"
    if not wanted and traefik_default:
        wanted, source = traefik_default, "Traefik's providers.docker.network"
    if routed and mode != "host":
        if wanted and wanted not in names and not any(n.endswith(f"_{wanted}") for n in names):
            add("traefik_network_missing", "warning",
                f"{source} names {wanted}, which {name} is not attached to - "
                "Traefik will answer 502/504 for it.")
        elif not wanted and len(names) > 1:
            add("traefik_network_ambiguous", "info",
                f"Routed by Traefik and on {len(names)} networks ({', '.join(names)}) without "
                "traefik.docker.network - which address Traefik picks is left to chance.")

    if routed:
        wide = [p for p in ports if _all_interfaces(p["host_ip"]) and p["proto"] == "tcp"]
        if wide:
            listed = ", ".join(p["host_port"] for p in wide)
            add("published_and_proxied", "info",
                f"Behind Traefik and also published on all interfaces (port {listed}), "
                "which reaches it without the proxy's TLS or sign-in.")

    if "bridge" in names and mode != "host":
        add("default_bridge", "info",
            "On Docker's default bridge, where containers cannot find each other by name. "
            "A user-defined network gives them DNS.")
    return out


def _overlaps(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two networks claiming the same addresses route traffic to whichever
    Docker set up last. Rare, and baffling when it happens."""
    parsed = []
    for n in networks:
        if n.get("config_only"):
            continue  # its child network claims the same addresses, by design
        for subnet in n.get("subnets") or [n["subnet"]]:
            try:
                parsed.append((n["name"], ipaddress.ip_network(subnet)))
            except ValueError:
                continue
    out = []
    for i, (a, net_a) in enumerate(parsed):
        for b, net_b in parsed[i + 1:]:
            if a != b and net_a.version == net_b.version and net_a.overlaps(net_b):
                out.append({"kind": "subnet_overlap", "severity": "warning", "container": "",
                            "subject": f"{a}|{b}",
                            "summary": f"{a} ({net_a}) and {b} ({net_b}) overlap."})
    return out
