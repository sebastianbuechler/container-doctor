"""Deterministic policy engine and action executor.

Nothing the model says reaches Docker without passing through `Policy.decide`,
which is ordinary Python with no model in the loop. The model can only ever
*narrow* what happens - it picks one action from a closed enum, and the policy
then decides whether that action is permitted at all.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any

from .config import ContainerPolicy, Settings
from .docker_client import DockerClient
from .store import Store

log = logging.getLogger(__name__)

# Actions that mutate Docker state (as opposed to just telling a human).
MUTATING_ACTIONS = ("restart_container", "stop_container", "prune_images")


@dataclass
class Decision:
    action: str
    status: str  # "approved" | "pending" | "skipped"
    reason: str
    decided_by: str = "policy"

    @property
    def is_mutating(self) -> bool:
        return self.action in MUTATING_ACTIONS


class Policy:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store

    def effective_mode(self, container_policy: ContainerPolicy) -> str:
        return container_policy.mode or self.settings.mode

    def protected(self, container_name: str) -> str | None:
        """Return a reason if this container must never be acted upon."""
        if not container_name:
            return None
        if container_name == self.settings.self_container_name:
            return "container-doctor will not act on itself"
        if container_name.startswith("host:"):
            return None  # host-level incidents have no container to touch
        if "docker-socket-proxy" in container_name or "socket-proxy" in container_name:
            return "acting on the socket proxy would cut off the doctor's own access"
        if container_name in self.settings.ignore_containers:
            return "container is on DOCTOR_IGNORE_CONTAINERS"
        return None

    def decide(
        self,
        *,
        incident: dict[str, Any],
        diagnosis: dict[str, Any] | None,
        container_policy: ContainerPolicy,
    ) -> Decision:
        container_name = incident["container_name"]
        action = (diagnosis or {}).get("recommended_action", "notify_only")
        try:
            confidence = float((diagnosis or {}).get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if not math.isfinite(confidence):  # NaN would pass any threshold
            confidence = 0.0

        if incident.get("kind") in ("raid_degraded", "raid_errors"):
            # Whatever the model suggests: restarting a container cannot bring
            # a disk back, and pruning images while an array rebuilds only adds
            # load to the disks that are left.
            return Decision("notify_only", "skipped",
                            "a disk array is repaired on the host - Storage Manager on DSM, mdadm "
                            "elsewhere - not by the doctor")

        if str(incident.get("kind", "")).startswith("host_") and incident["kind"] != "host_disk_pressure":
            # The host as a whole: there is no container to restart, and the
            # diagnosis names which one to look at.
            return Decision("notify_only", "skipped",
                            "the host as a whole is not something the doctor acts on")
        if str(incident.get("kind", "")).startswith("net_"):
            return Decision("notify_only", "skipped",
                            "the network is fixed at the router or the DNS server, "
                            "not by restarting a container")

        if incident.get("kind") == "smart_health":
            return Decision("notify_only", "skipped",
                            "a failing drive is replaced on the host, not by the doctor")

        if not diagnosis:
            return Decision("notify_only", "skipped", "no diagnosis available")

        if action in ("none", "notify_only"):
            return Decision(action, "skipped", (diagnosis.get("action_rationale") or "")[:300])

        if not container_policy.enabled:
            return Decision("notify_only", "skipped", "container has doctor.enable=false")

        protected = self.protected(container_name)
        if protected:
            return Decision("notify_only", "skipped", protected)

        allowed = container_policy.actions if container_policy.actions is not None \
            else self.settings.auto_actions
        if action not in allowed:
            return Decision(
                action, "skipped",
                f"'{action}' is not in the allowed action list ({', '.join(allowed) or 'none'})",
            )

        mode = self.effective_mode(container_policy)
        if mode == "observe":
            return Decision(action, "skipped", "observe mode: diagnosis recorded, nothing executed")
        if mode == "suggest":
            return Decision(action, "pending", "suggest mode: waiting for approval in the dashboard")

        # --- auto mode: every guardrail below must pass -------------------
        if confidence < self.settings.min_confidence:
            return Decision(
                action, "pending",
                f"confidence {confidence:.2f} is below the {self.settings.min_confidence:.2f} "
                "auto-apply threshold",
            )

        blocked = self.circuit_breaker_reason(
            container_name, incident.get("fingerprint", ""), action, container_policy
        )
        if blocked:
            return Decision(action, "pending", blocked)

        return Decision(action, "approved", diagnosis.get("action_rationale", "")[:300])

    def _limits(self, container_policy: ContainerPolicy) -> dict[str, int]:
        cap = container_policy.max_actions_per_hour
        return {
            "per_container_cap": (self.settings.max_actions_per_container_per_hour
                                  if cap is None else cap),
            "global_cap": self.settings.max_actions_global_per_hour,
            "cooldown_minutes": self.settings.action_cooldown_minutes,
        }

    def circuit_breaker_reason(
        self, container_name: str, fingerprint: str, action: str,
        container_policy: ContainerPolicy | None = None,
    ) -> str | None:
        return self.store.action_limit_reason(
            container_name, fingerprint, action,
            **self._limits(container_policy or ContainerPolicy()),
        )

    def claim_action(self, action_id: str, container_policy: ContainerPolicy) -> str | None:
        """Recheck limits and reserve execution without a check-then-act race."""
        return self.store.claim_action(action_id, **self._limits(container_policy))


class Executor:
    def __init__(self, settings: Settings, docker: DockerClient, store: Store) -> None:
        self.settings = settings
        self.docker = docker
        self.store = store

    async def execute(self, action: str, container_id: str, container_name: str) -> str:
        if action == "restart_container":
            return await self.docker.restart(container_id)
        if action == "stop_container":
            return await self.docker.stop(container_id)
        if action == "prune_images":
            return await self.docker.prune_images()
        raise ValueError(f"executor refuses unknown action: {action}")

    async def verify(self, container_id: str, container_name: str,
                     action: str = "restart_container") -> tuple[bool, str]:
        """Check whether the container ended up as the action meant it to:
        stopped after a stop, running and healthy again after a restart."""
        await asyncio.sleep(self.settings.verify_delay_seconds)
        containers = await self.docker.list_containers()
        current = next((c for c in containers if c["id"] == container_id), None)
        if action == "stop_container":
            if current is None or current.get("state") in ("exited", "created", "dead"):
                return True, f"{container_name} is stopped"
            return False, (f"{container_name} is '{current.get('state')}' "
                           f"{self.settings.verify_delay_seconds}s after the stop")
        if current is None:
            return False, f"{container_name} is gone after the action"
        state, health = current.get("state"), current.get("health")
        if state != "running":
            return False, f"{container_name} is '{state}' {self.settings.verify_delay_seconds}s later"
        if health == "unhealthy":
            return False, f"{container_name} is running but still unhealthy"
        if health == "starting":
            # Still inside its start_period. Give it the benefit of the doubt:
            # if it goes on to fail, the next poll opens a fresh incident, and
            # the per-fingerprint cooldown stops a second restart.
            return True, f"{container_name} restarted and is still starting up"
        return True, f"{container_name} is running and healthy"
