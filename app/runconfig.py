"""How a container was configured to run, and what a redeploy changed about it.

The change timeline says a container was recreated and whether its image moved.
The other half of "what changed?" is the configuration it was started with: a
lowered memory limit, a new environment variable, a dropped capability, a mount
pointed somewhere else. Docker has all of it in the container's own inspect -
whatever deployed it, Portainer, a compose file on disk or `docker run` - so
reading it needs no repository, no Portainer token and no socket permission
the doctor does not already have.

A snapshot is taken of every container on every poll; when a container is
replaced, the old snapshot is compared against the new one.

Values follow the doctor's existing env policy (`redact.env_value_shown`): the
allowlisted, harmless ones - TZ, PUID, *_URL, *_LEVEL - are kept readable, after
redaction, so a diff can say "TZ UTC → Europe/Zurich". Every other env value,
and any label whose name says secret, is kept only as a keyed hash: enough to
tell "changed" from "the same", never the value. The key is random and made
once per install, so a copy of the database is not a table of hashed passwords
to look values up in. Weak values could still be guessed by someone holding
both the database and the key file; strong ones cannot.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from typing import Any

from .redact import env_value_shown, redact, secret_name

log = logging.getLogger(__name__)

KEY_FILE = "env-hash.key"

# Labels that change on every deploy, or that come from the image rather than
# from anyone's configuration: comparing them would report noise on every
# recreate. The compose config hash is read on its own, below.
_LABEL_NOISE = (
    "com.docker.compose.",
    "org.opencontainers.image.",
    "org.label-schema.",
    "desktop.docker.io/",
)
_LABEL_NOISE_EXACT = {"maintainer", "build_version"}
COMPOSE_CONFIG_HASH = "com.docker.compose.config-hash"

# How many differences go into a one-line summary; the rest are counted.
SUMMARY_ITEMS = 3


def load_key(data_dir: str) -> bytes:
    """The install's hashing key, made on first use.

    Without a writable data directory the key lives for this process only:
    every restart then reads each value as changed once, which is wrong but
    says so loudly, instead of hashing without a key.
    """
    path = os.path.join(data_dir, KEY_FILE)
    try:
        with open(path, "rb") as handle:
            key = handle.read()
        if len(key) >= 16:
            return key
    except FileNotFoundError:
        pass
    except OSError as error:
        log.warning("could not read %s: %s", path, error)
    key = secrets.token_bytes(32)
    try:
        os.makedirs(data_dir, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
    except OSError as error:
        log.warning("could not write %s, env values hash per process: %s", path, error)
    return key


def snapshot(attrs: dict[str, Any], key: bytes) -> dict[str, Any]:
    """The parts of an inspect that someone configures, in a stable shape."""
    config = attrs.get("Config") or {}
    host = attrs.get("HostConfig") or {}
    labels = config.get("Labels") or {}
    env: dict[str, str] = {}
    for entry in config.get("Env") or []:
        name, _, value = str(entry).partition("=")
        env[name] = _keep(key, value, shown=env_value_shown(name) and not secret_name(name))
    health = config.get("Healthcheck") or {}
    return {
        "env": env,
        "cmd": _words(config.get("Cmd")),
        "entrypoint": _words(config.get("Entrypoint")),
        "user": config.get("User") or "",
        "healthcheck": _words(health.get("Test")),
        "mem_limit": int(host.get("Memory") or 0),
        "cpus": round(int(host.get("NanoCpus") or 0) / 1e9, 2),
        "shm_size": int(host.get("ShmSize") or 0),
        "privileged": bool(host.get("Privileged")),
        "read_only": bool(host.get("ReadonlyRootfs")),
        "cap_add": sorted(host.get("CapAdd") or []),
        "cap_drop": sorted(host.get("CapDrop") or []),
        "security_opt": sorted(host.get("SecurityOpt") or []),
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name", "") or "",
        "network_mode": host.get("NetworkMode") or "",
        "networks": sorted(((attrs.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
        "ports": sorted(
            f"{binding.get('HostIp') or ''}:{binding.get('HostPort') or ''}->{port}".lstrip(":")
            for port, bindings in (host.get("PortBindings") or {}).items()
            for binding in (bindings or [])
        ),
        "mounts": sorted(
            f"{m.get('Name') or m.get('Source') or ''}:{m.get('Destination') or ''}"
            f":{'rw' if m.get('RW', True) else 'ro'}"
            for m in attrs.get("Mounts") or []
        ),
        "tmpfs": sorted((host.get("Tmpfs") or {}).keys()),
        "devices": sorted(f"{d.get('PathOnHost')}:{d.get('PathInContainer')}"
                          for d in host.get("Devices") or []),
        "labels": {name: _keep(key, str(value), shown=not secret_name(name))
                   for name, value in labels.items() if not _noise(name)},
        "compose_hash": labels.get(COMPOSE_CONFIG_HASH, ""),
    }


def diff(before: dict[str, Any] | None, now: dict[str, Any] | None) -> list[str] | None:
    """What differs, one short line each; None when either side is unknown.

    Unknown is not the same as unchanged: a container last seen by a doctor
    that did not take snapshots yet has nothing to compare against, and saying
    "config unchanged" there would be a claim with no evidence behind it.
    """
    if not before or not now:
        return None
    out: list[str] = []

    old_env, new_env = before.get("env") or {}, now.get("env") or {}
    for name in sorted(set(old_env) | set(new_env)):
        if name not in old_env:
            out.append(f"env {name} added")
        elif name not in new_env:
            out.append(f"env {name} removed")
        elif old_env[name] != new_env[name]:
            out.append(f"env {name}{_values(old_env[name], new_env[name])}")

    for field, label in (("mem_limit", "memory limit"), ("shm_size", "shm size")):
        if before.get(field, 0) != now.get(field, 0):
            out.append(f"{label} {_bytes(before.get(field, 0))} → {_bytes(now.get(field, 0))}")
    if before.get("cpus", 0) != now.get("cpus", 0):
        out.append(f"cpus {before.get('cpus') or 'unlimited'} → {now.get('cpus') or 'unlimited'}")

    for field in ("cmd", "entrypoint", "user", "healthcheck", "restart_policy", "network_mode",
                  "privileged", "read_only"):
        if before.get(field) != now.get(field):
            out.append(f"{field.replace('_', ' ')} {_show(before.get(field))} → "
                       f"{_show(now.get(field))}")

    for field in ("cap_add", "cap_drop", "security_opt", "networks", "ports", "mounts",
                  "tmpfs", "devices"):
        old, new = set(before.get(field) or []), set(now.get(field) or [])
        for item in sorted(new - old):
            out.append(f"{field.replace('_', ' ')} + {item}")
        for item in sorted(old - new):
            out.append(f"{field.replace('_', ' ')} - {item}")

    old_labels, new_labels = before.get("labels") or {}, now.get("labels") or {}
    for name in sorted(set(old_labels) | set(new_labels)):
        if name not in old_labels:
            out.append(f"label {name} added")
        elif name not in new_labels:
            out.append(f"label {name} removed")
        elif old_labels[name] != new_labels[name]:
            out.append(f"label {name}{_values(old_labels[name], new_labels[name])}")
    return out


def summarize(lines: list[str] | None, compose_same: bool | None) -> str:
    """The clause a change's one-line summary ends with."""
    if lines is None:
        return ""
    if not lines:
        if compose_same is False:
            # Something in the compose definition moved that inspect does
            # not show - a depends_on, a build section, an env_file resolving
            # to the same values.
            return "; the compose definition changed, but nothing Docker runs it with did"
        return "; configuration unchanged"
    shown = ", ".join(lines[:SUMMARY_ITEMS])
    more = len(lines) - SUMMARY_ITEMS
    return f"; config: {shown}" + (f" (+{more} more)" if more > 0 else "")


def compose_same(before: dict[str, Any] | None, now: dict[str, Any] | None) -> bool | None:
    """Whether Compose's own hash of the service definition stayed the same.

    Compose writes it on every container it creates; it moves when anything in
    the service's compose definition does, including what inspect cannot show
    (an env_file's contents resolved to the same values, a depends_on).
    """
    old = (before or {}).get("compose_hash") or ""
    new = (now or {}).get("compose_hash") or ""
    if not old or not new:
        return None
    return old == new


def _keep(key: bytes, value: str, *, shown: bool) -> str:
    """A value as stored: `v:` and the redacted text, or `h:` and a keyed hash."""
    if shown:
        return "v:" + redact(value)
    return "h:" + hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:16]


def _values(old: str, new: str) -> str:
    """` A → B` when both values may be shown, else that it changed."""
    if old.startswith("v:") and new.startswith("v:"):
        return f" {_show(old[2:])} → {_show(new[2:])}"
    return " changed (value not shown)"


def _noise(label: str) -> bool:
    return label in _LABEL_NOISE_EXACT or label.startswith(_LABEL_NOISE)


def _words(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value)


def _show(value: Any) -> str:
    if value in ("", None, [], False):
        return "none" if value is not False else "off"
    if value is True:
        return "on"
    text = str(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _bytes(value: int) -> str:
    if not value:
        return "none"
    for unit, size in (("g", 1024 ** 3), ("m", 1024 ** 2), ("k", 1024)):
        if value >= size and value % size == 0:
            return f"{value // size}{unit}"
    return f"{value}b"
