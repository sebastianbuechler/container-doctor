"""What changed about a container, from one poll to the next.

"What changed?" is the first question of almost every diagnosis, and Docker
answers it only in passing: a redeploy replaces the container, a re-pulled tag
swaps the image under the same name, a crash loop bumps a counter. None of it
is an incident, and all of it is the likeliest explanation for one.

So each poll is compared against the last state seen for the same container
name - kept in SQLite, so a change made while the doctor was down is still
caught when it comes back - and the difference is written down. The times are
the container's own (`Created`, `StartedAt`), not the poll's, so a timeline
read against logs lines up to the second rather than to the poll interval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import runconfig
from .stacks import compose_info

@dataclass
class Change:
    kind: str       # recreated | renamed | restarted | started | stopped | appeared | removed
    at: str         # ISO time the change happened, as Docker recorded it
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


def observe(container: dict[str, Any]) -> dict[str, Any]:
    """The tracked state of one container, as stored between polls."""
    info = compose_info(container.get("labels"))
    return {
        "container_id": container.get("id") or "",
        "image": container.get("image") or "",
        "image_id": container.get("image_id") or "",
        "created": _iso(container.get("created")),
        "started_at": _iso(container.get("started_at")),
        "restart_count": int(container.get("restart_count") or 0),
        "state": container.get("state") or "",
        # Not compared, only read: when a stop happened, and where it
        # belonged - which is what tells a rename from a new container.
        "finished_at": _iso(container.get("finished_at")),
        "stack": info["project"],
        "service": info["service"],
        # None when no snapshot was taken; compared only on a replacement.
        "config": container.get("run_config"),
    }


def diff(name: str, before: dict[str, Any] | None, now: dict[str, Any],
         *, first_run: bool) -> list[Change]:
    """The changes between two observations of the container called `name`.

    `before` None is a name never seen: on the first run ever that is every
    container and says nothing, afterwards it is a container that appeared.
    """
    if before is None:
        if first_run:
            return []
        return [Change("appeared", now["created"] or now["started_at"],
                       f"{name} appeared, on {now['image']}",
                       {"image": now["image"], "image_id": _short(now["image_id"])})]

    if now["container_id"] != before["container_id"]:
        return [_recreated(name, before, now)]

    changes: list[Change] = []
    started_again = now["started_at"] and now["started_at"] != before["started_at"]
    if started_again:
        if before["state"] == "running":
            restarts = now["restart_count"] - before["restart_count"]
            by_docker = restarts > 0
            changes.append(Change(
                "restarted", now["started_at"],
                f"{name} restarted" + (" by its restart policy" if by_docker else ""),
                {"restart_count": now["restart_count"], "by": "docker" if by_docker else ""},
            ))
        else:
            changes.append(Change("started", now["started_at"], f"{name} started", {}))
    elif before["state"] == "running" and now["state"] not in ("running", "restarting", ""):
        changes.append(Change("stopped", now.get("finished_at", ""), f"{name} stopped ({now['state']})",
                              {"state": now["state"]}))
    return changes


def removed(name: str, before: dict[str, Any], at: str) -> Change:
    return Change("removed", at, f"{name} was removed",
                  {"image": before.get("image", "")})


def _recreated(name: str, before: dict[str, Any], now: dict[str, Any]) -> Change:
    detail, summary = _image_change(before, now)
    summary += _config_change(before, now, detail)
    return Change("recreated", now["created"] or now["started_at"],
                  f"{name} was recreated {summary}", detail)


def pair_renames(changes: list[tuple[str, str, Change]],
                 before: dict[str, dict[str, Any]],
                 now: dict[str, dict[str, Any]]) -> list[str]:
    """Turn "B appeared" into "A was renamed to B" where the evidence says so.

    Renaming a service or its `container_name` in a compose file makes Docker
    remove one container and create another, and a diff by name reads that
    as a brand-new container - with no earlier image, so the diagnosis goes
    looking for a regression that never happened. A container that appears in
    a stack while one that was in the same stack is missing, and shares its
    service name or its exact image, is that same service under a new name.
    Only an unambiguous pairing counts: two candidates and it stays "appeared".

    Rewrites `changes` in place and returns the old names it consumed, whose
    stored state goes now instead of after the removal grace period.
    """
    missing = {name: row for name, row in before.items()
               if name not in now and row.get("stack")}
    consumed: list[str] = []
    for index, (name, stack, change) in enumerate(changes):
        if change.kind != "appeared" or not stack:
            continue
        new = now[name]
        candidates = [
            old for old, row in missing.items()
            if old not in consumed and row.get("stack") == stack
            and ((new.get("service") and row.get("service") == new["service"])
                 or (new["image_id"] and row.get("image_id") == new["image_id"]))
        ]
        if len(candidates) != 1:
            continue
        old = candidates[0]
        detail, summary = _image_change(missing[old], new)
        summary += _config_change(missing[old], new, detail)
        detail["from_name"] = old
        changes[index] = (name, stack, Change(
            "renamed", change.at, f"{name} replaced {old} (renamed in stack {stack}) {summary}",
            detail))
        consumed.append(old)
    return consumed


def _config_change(before: dict[str, Any], now: dict[str, Any],
                   detail: dict[str, Any]) -> str:
    """Compare the run configuration too; note it in `detail`, return the clause.

    `detail["config"]` is the list of differences, empty for "unchanged", and
    absent when there was no earlier snapshot to compare against.
    """
    lines = runconfig.diff(before.get("config"), now.get("config"))
    same_definition = runconfig.compose_same(before.get("config"), now.get("config"))
    if lines is not None:
        detail["config"] = lines
    if same_definition is not None:
        detail["compose_definition"] = "same" if same_definition else "changed"
    return runconfig.summarize(lines, same_definition)


def _image_change(before: dict[str, Any], now: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """How the image differs between two containers, and how to say it."""
    detail: dict[str, Any] = {
        "from_image": before.get("image", ""), "to_image": now["image"],
        "from_build": _short(before.get("image_id", "")), "to_build": _short(now["image_id"]),
    }
    same_build = bool(now["image_id"]) and now["image_id"] == before.get("image_id")
    if now["image"] != before.get("image") and same_build:
        # Named differently, run the same: a digest pin on the tag already
        # deployed (server:2026.8.3 -> server:2026.8.3@sha256:...), or a
        # tag that resolves to the same build. Not a new image, and nothing
        # to roll back to.
        detail["change"] = "same"
        return detail, (f"on the same image, now named {now['image']} "
                        f"(was {before.get('image')}, same build {_short(now['image_id'])})")
    if now["image"] != before.get("image"):
        # A tag bump in the compose file: sonarr:4.0.20 -> sonarr:4.0.21.
        detail["change"] = "version"
        return detail, f"on {now['image']} (was {before.get('image')})"
    if now["image_id"] and before.get("image_id") and now["image_id"] != before["image_id"]:
        # Same tag, different image: the tag moved and a redeploy pulled it.
        # The case nobody chose, and the one worth saying out loud.
        detail["change"] = "build"
        return detail, (f"on a new build of {now['image']} "
                        f"({_short(before['image_id'])} → {_short(now['image_id'])})")
    detail["change"] = "same"
    return detail, "on the same image"


def image_moved(detail: dict[str, Any]) -> str:
    """version | build | same, from a stored change's detail.

    Read again rather than trusted: rows written before a digest pin on the
    same build was told apart say "version" for it.
    """
    change = detail.get("change", "")
    if change in ("version", "build") and detail.get("from_build") \
            and detail.get("from_build") == detail.get("to_build"):
        return "same"
    return change


# What a redeploy does to a container: replace it, under its name or a new one.
REDEPLOYED = ("recreated", "renamed")


def group_redeploys(changes: list[tuple[str, str, Change]]) -> None:
    """Mark recreations that happened together in one stack as one redeploy.

    `changes` is (container name, stack, change). A stack redeploy
    recreates every service it touches in the same few seconds; saying so
    tells "someone redeployed arr-stack" apart from "sonarr alone changed".
    """
    by_stack: dict[str, list[str]] = {}
    for name, stack, change in changes:
        if change.kind in REDEPLOYED and stack:
            by_stack.setdefault(stack, []).append(name)
    for name, stack, change in changes:
        others = by_stack.get(stack, [])
        if change.kind in REDEPLOYED and len(others) > 1:
            change.detail["redeploy"] = {"stack": stack, "containers": sorted(others)}
            change.summary += f", with {len(others) - 1} other container" \
                              f"{'' if len(others) == 2 else 's'} of stack {stack}"


def _short(image_id: str) -> str:
    """sha256:4f1e... -> 4f1e2a3b9c0d, the way `docker images` shows it."""
    value = (image_id or "").split(":", 1)[-1]
    return value[:12]


def _iso(value: Any) -> str:
    """Docker's timestamps, with the nanoseconds and the zero date dropped."""
    text = str(value or "")
    if not text or text.startswith("0001-01-01"):
        return ""
    if "." in text:
        head, _, tail = text.partition(".")
        zone = ""
        for mark in ("Z", "+", "-"):
            if mark in tail:
                zone = tail[tail.index(mark):]
                break
        text = head + (zone if zone != "Z" else "+00:00")
    elif text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return text
