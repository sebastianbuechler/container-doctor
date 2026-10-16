"""ntfy push notifications.

This repo already runs an ntfy instance, so the doctor publishes there rather
than adding another notification channel. Failures to notify are logged and
swallowed - a broken notifier must never stop remediation.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)

_PRIORITY = {"info": "low", "warning": "default", "critical": "high"}
_TAGS = {
    "exit_nonzero": "boom",
    "oom_killed": "skull",
    "unhealthy": "face_with_thermometer",
    "restart_loop": "arrows_counterclockwise",
    "stuck_restarting": "arrows_counterclockwise",
    "high_cpu": "fire",
    "high_memory": "fire",
    "log_errors": "warning",
    "host_disk_pressure": "floppy_disk",
    "disk_filling": "floppy_disk",
    "raid_degraded": "rotating_light",
    "raid_errors": "warning",
}


class Notifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.ntfy_url and self.settings.ntfy_topic)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def notify_incident(
        self,
        incident: dict[str, Any],
        diagnosis: dict[str, Any] | None,
        decision_note: str,
        dashboard_url: str = "",
        awaiting_approval: bool = False,
    ) -> None:
        if not self.enabled:
            return

        name = incident["container_name"]
        kind = incident["kind"]
        severity = (diagnosis or {}).get("severity") or incident.get("severity", "warning")

        body_lines = [(diagnosis or {}).get("summary") or incident.get("summary", "")]
        if diagnosis and diagnosis.get("root_cause"):
            body_lines.append("")
            body_lines.append(f"Cause: {diagnosis['root_cause']}")
        if decision_note:
            body_lines.append("")
            body_lines.append(decision_note)
        steps = (diagnosis or {}).get("human_fix_steps") or []
        if steps:
            body_lines.append("")
            body_lines.append("Next steps:")
            body_lines.extend(f"- {step}" for step in steps[:4])

        await self._publish(
            title=f"{name}: {kind.replace('_', ' ')}",
            message="\n".join(line for line in body_lines if line is not None),
            priority=_PRIORITY.get(severity, "default"),
            tags=_TAGS.get(kind, "stethoscope"),
            click=dashboard_url,
            button="Review and approve" if awaiting_approval else "Open incident",
        )

    async def notify_action(
        self, container_name: str, action: str, success: bool, detail: str, dashboard_url: str = ""
    ) -> None:
        if not self.enabled:
            return
        verb = "applied" if success else "failed"
        await self._publish(
            title=f"{container_name}: {action.replace('_', ' ')} {verb}",
            message=detail,
            priority="default" if success else "high",
            tags="white_check_mark" if success else "x",
            click=dashboard_url,
            button="Open activity" if dashboard_url.endswith("#/activity") else "Open incident",
        )

    async def notify_recovered(self, title: str, message: str, dashboard_url: str = "") -> None:
        if not self.enabled:
            return
        await self._publish(title=title, message=message, priority="default",
                            tags="white_check_mark", click=dashboard_url)

    async def _publish(
        self, *, title: str, message: str, priority: str, tags: str, click: str = "",
        button: str = "",
    ) -> None:
        url = f"{self.settings.ntfy_url.rstrip('/')}/{self.settings.ntfy_topic}"
        headers = {
            "Title": _header_safe(title),
            "Priority": priority,
            "Tags": tags,
        }
        if click:
            # Tapping the notification opens the incident itself, not the top
            # of the dashboard.
            headers["Click"] = click
            if button:
                # A visible button as well: on Android the tap target is easy
                # to miss, and "Review and approve" says what waits there.
                headers["Actions"] = f"view, {_header_safe(button)}, {click}, clear=true"
        if self.settings.ntfy_token:
            headers["Authorization"] = f"Bearer {self.settings.ntfy_token}"
        try:
            response = await self._http().post(
                url, content=message.encode("utf-8"), headers=headers
            )
            response.raise_for_status()
        except Exception as exc:  # notification failures must never bubble up
            log.warning("ntfy publish failed: %s", exc)


def _header_safe(value: str) -> str:
    """HTTP headers are latin-1; ntfy titles arrive as headers."""
    return value.encode("ascii", errors="replace").decode("ascii")[:200]
