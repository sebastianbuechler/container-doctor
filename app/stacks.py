"""Which containers belong together, read from Compose's own labels.

When a database goes unhealthy its application follows, and the doctor raises
two incidents for one problem - diagnosing each in isolation, without the one
fact that explains both.

Compose records the project, the service name and the declared dependencies on
every container it starts, so the relationship is already there to be read.
Nothing here decides whether an incident fires; it supplies the context that
turns "memos is unhealthy" into "memos is unhealthy and so is the database it
depends on".

A stack, to the doctor, is what the deploy workflow redeploys by name: a
folder in the repository. That is the Compose project's name whenever the
manager names projects after their stacks - Portainer does - and otherwise
`doctor.stack: <folder>` on one service of the project says it.
"""

from __future__ import annotations

from typing import Any

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
DEPENDS_LABEL = "com.docker.compose.depends_on"
# Which stack - repository folder - a container belongs to, where its Compose
# project is named something else. One service carrying it names the project.
STACK_LABEL = "doctor.stack"


def compose_info(labels: dict[str, str] | None) -> dict[str, Any]:
    labels = labels or {}
    project = labels.get(PROJECT_LABEL, "")
    return {
        "project": project,
        "stack": labels.get(STACK_LABEL, "").strip() or project,
        "service": labels.get(SERVICE_LABEL, ""),
        "depends_on": _dependencies(labels.get(DEPENDS_LABEL, "")),
    }


def resolve_stacks(containers: list[dict[str, Any]]) -> None:
    """Set each container's "stack", in place: its Compose project's, or the
    stack label of any container in that project, so labelling one service
    is enough. A label on a service outside any project names only itself."""
    named: dict[str, str] = {}
    for container in containers:
        info = compose_info(container.get("labels"))
        if info["project"] and info["stack"] != info["project"]:
            named.setdefault(info["project"], info["stack"])
    for container in containers:
        info = compose_info(container.get("labels"))
        container["stack"] = named.get(info["project"], info["stack"])


def _dependencies(raw: str) -> list[str]:
    """Service names out of the depends_on label.

    Compose writes entries as `service:condition:restart`, but the exact shape
    has changed between versions and is not something to bet on: take what is
    before the first colon and ignore the rest, so a plain `db` and a
    `db:service_healthy:false` both read as `db`.
    """
    names: list[str] = []
    for entry in raw.split(","):
        name = entry.split(":", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names


def _health_of(container: dict[str, Any]) -> str:
    """One word for how a container is doing."""
    if container.get("state") != "running":
        return str(container.get("state") or "unknown")
    health = container.get("health")
    if health and health != "none":
        return str(health)
    return "running"


def _troubled(container: dict[str, Any]) -> bool:
    return _health_of(container) not in ("running", "healthy")


def relatives(
    container: dict[str, Any], everything: list[dict[str, Any]]
) -> dict[str, Any]:
    """What this container depends on, what depends on it, and how they are.

    Only the parts worth putting in front of a diagnosis: a healthy stack adds
    nothing but tokens, so the unhealthy siblings are listed separately from
    the declared dependencies.
    """
    info = compose_info(container.get("labels"))
    project, service = info["project"], info["service"]
    if not project:
        return {}

    by_service: dict[str, dict[str, Any]] = {}
    for other in everything:
        other_info = compose_info(other.get("labels"))
        if other_info["project"] == project and other_info["service"]:
            by_service[other_info["service"]] = other

    def describe(name: str) -> dict[str, Any] | None:
        found = by_service.get(name)
        if found is None:
            return None
        return {"service": name, "name": found.get("name"), "status": _health_of(found)}

    depends_on = [d for d in (describe(n) for n in info["depends_on"]) if d]
    dependents = []
    for other_service, other in by_service.items():
        if other_service == service:
            continue
        if service in compose_info(other.get("labels"))["depends_on"]:
            described = describe(other_service)
            if described:
                dependents.append(described)

    unhealthy_siblings = [
        {"service": name, "name": other.get("name"), "status": _health_of(other)}
        for name, other in by_service.items()
        if name != service and _troubled(other)
        and name not in info["depends_on"]
    ]

    return {
        "project": project,
        "service": service,
        "depends_on": depends_on,
        "dependents": dependents,
        "unhealthy_siblings": unhealthy_siblings,
    }


def likely_root_cause(relations: dict[str, Any]) -> dict[str, Any] | None:
    """A dependency that is itself unwell.

    Deliberately only a hint: the incident still opens on its own container,
    because "the database is also down" is a reason to look there first, not
    proof that nothing is wrong here.
    """
    for dependency in relations.get("depends_on", []):
        if dependency["status"] not in ("running", "healthy"):
            return dependency
    return None
