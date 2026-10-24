"""The orchestrator: detect -> gather evidence -> diagnose -> decide -> act -> verify."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

import dataclasses

from .config import FIELD_SPECS, ContainerPolicy, Settings
from . import hostkind
from .detector import HOST_ID, HOST_KINDS, SEVERITY_ORDER, Detector, Signal, raid_signal
from .classifier import Classifier
from .decision import ChatDecisions, OpenRouterDecisions
from .diagnoser import Diagnoser, _render_incident
from .docker_client import DockerClient, disk_usage
from .forecast import human_days, human_rate, trend
from .notifier import Notifier
from .preflight import probe_endpoint, summarize
from . import (briefing, changes, connectivity, deploys, handoff, host, memory, networks,
               raid, runconfig, smart)
from .registry import compare, parse_reference, remote_digest
from .versions import (TOO_MANY_TAGS, list_tags, parse_tag, release_list, release_notes,
                       settled, source_repository)
from .stacks import compose_info, likely_root_cause, relatives
from .redact import error_lines, redact, redact_obj
from . import github_deploy, repo_context
from .remediator import Executor, Policy
from .store import Store, utcnow

log = logging.getLogger(__name__)

# CPU and memory for the container detail view: a point a minute for a day.
# In memory only - a restart forgets it, which is fine for "what has it been
# doing lately" and not worth a table that grows by forty rows a minute.
HISTORY_POINTS = 1440
# What the operator may add to a re-diagnosis. A paragraph or two of "what I
# know that the logs do not" - not a place to paste a log file.
OPERATOR_CONTEXT_CHARS = 2000
HISTORY_STEP_SECONDS = 60

# Kinds whose silence does not mean "over". host_disk_pressure only re-signals
# once usage climbs another 2%, so a quiet incident means a steadily full disk.
AUTO_RESOLVE_EXCLUDED_KINDS = ("host_disk_pressure",)

# A container missing for less than this is mid-redeploy, not removed: the
# old one is gone a few seconds before the new one exists, and a poll landing
# in between would otherwise read one recreate as "removed" then "appeared".
REMOVED_AFTER = timedelta(minutes=10)
# A crash loop restarts every few seconds; within this, restarts count up one
# row of the timeline instead of adding forty.
RESTART_FOLD_MINUTES = 30
# How far back memory limits are judged from, and changes shown to a diagnosis.
MEMORY_WINDOW = timedelta(days=14)
CHANGES_IN_EVIDENCE = timedelta(days=7)


def docker_pool_size(settings: Settings) -> int:
    """Connections to Docker that can be open at once: a poll reads stats
    and logs for `poll_concurrency` containers together, and the event
    stream, incident work and the dashboard need theirs alongside it."""
    return 2 * max(1, settings.poll_concurrency) + 10


class Supervisor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.db_path)
        self.docker = DockerClient(settings.docker_host,
                                   env_key=runconfig.load_key(settings.data_dir),
                                   max_pool_size=docker_pool_size(settings))
        self.detector = Detector(settings)
        self.detector.load_log_marks(self.store.log_marks())
        self.diagnoser = Diagnoser(settings)
        self.diagnoser.host_description = self.describe_host
        self.notifier = Notifier(settings)
        self.policy = Policy(settings, self.store)
        self.executor = Executor(settings, self.docker, self.store)
        if settings.llm_log_days > 0:
            self.diagnoser.recorder = self.store.record_llm_call
        self.classifier = Classifier(settings, self._decision_model(), self.store)
        self.settling = deploys.Settling(settings.deploy_settle_minutes)

        self.containers: dict[str, dict[str, Any]] = {}
        self.disk: list[dict[str, Any]] = []
        self.last_poll_at: str | None = None
        # How long the last poll took, so the dashboard knows when one is late.
        self.last_poll_seconds: float | None = None
        self._polling = False
        # How far the running poll is: {"done", "total", "reading"}.
        self._poll_progress: dict[str, Any] | None = None
        self._poll_now = asyncio.Event()
        self.docker_connected: bool = False
        self.startup_warnings: list[str] = settings.validate()
        self.docker_access: list[dict[str, Any]] = []
        # Docker's own list of networks, refreshed every poll; None when the
        # endpoint does not permit /networks.
        self.network_inventory: list[dict[str, Any]] | None = None
        self.image_updates: dict[str, Any] = {"checked_at": None, "images": []}
        # One registry check at a time, whether the timer or a person asked.
        self._image_checking = False
        # How far the running check is: {"done", "total", "current"}.
        self._image_progress: dict[str, Any] | None = None
        # Each image's result as the running check reaches it, so the
        # dashboard can fill in its list before the whole check is done.
        self._image_partial: list[dict[str, Any]] = []
        # Stacks being redeployed from the dashboard: stack -> who and when.
        self._redeploying: dict[str, dict[str, str]] = {}
        # The repository's stacks, which the deploy workflow redeploys by
        # folder name. None until read: then every stack gets the button,
        # and a wrong name fails the run rather than hiding the button.
        self._deploy_stacks: list[str] | None = None
        # (stack, service) -> (loop time read, what the repository says)
        self._repo_context: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._deploy_stacks_read = -1e9
        # What the last read of GitHub found, for the Settings page.
        self._github: dict[str, Any] = {"checked_at": None, "error": "", "workflow": None}
        # Release notes read on request: (repo, from, to) -> (read at, answer).
        self._notes_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
        self.raid: dict[str, Any] = {"arrays": [], "pools": [], "reason": "", "drives": []}
        self.history: dict[str, deque] = {}
        # What the machine is - a Synology or any other Linux - read from its
        # kernel once; DOCTOR_HOST_KIND is applied on top, live (see platform).
        self._platform_found = hostkind.detect(settings.proc_path or "/proc")
        # The host's own CPU, memory, load and disks, read every poll.
        self.host_reader = (host.HostReader(settings.proc_path, settings.sysfs_path)
                            if settings.proc_path else None)
        self.host: dict[str, Any] | None = None
        # Internet and DNS, checked on a timer of their own.
        self.net = connectivity.Monitor(settings) if settings.net_check_seconds > 0 else None
        # Open network incident fingerprint -> rounds in a row it has not failed.
        self._net_ok_rounds: dict[str, int] = {}
        # Each drive's SMART health, from the sidecar; None until first read.
        self.smart: dict[str, Any] | None = None
        self._smart_read_at = 0.0
        # Drive incident fingerprint -> the summary it last fired with.
        self._smart_said: dict[str, str] = {}
        # Device -> who the drive in it was when last read awake.
        self._drive_ids: dict[str, dict[str, Any]] = {}

        self.briefing = briefing.Briefing(
            store=self.store,
            enabled=settings.briefing and self.diagnoser.enabled,
            refresh_minutes=settings.briefing_refresh_minutes,
            model=settings.briefing_model or settings.model,
            collect=self._briefing_facts,
            call=self._briefing_call,
            budget_ok=self._briefing_budget,
            record=self._record_usage,
            notify=lambda: self.broadcast("briefing", {}),
        )

        self._events: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers: set[asyncio.Queue] = set()
        self._tasks: list[asyncio.Task] = []
        self._work_lock = asyncio.Semaphore(3)

    # -- settings changed while running -------------------------------------

    def apply_settings(self, new: Settings) -> dict[str, list[str]]:
        """Run with `new` from now on, as far as that can happen without a
        restart. Returns the fields applied, and those that wait for one.

        A setting marked `restart` (read once at start) keeps its running
        value. Every other one is swapped in: each part of the doctor that
        holds the settings gets the same new object, and what was built from
        them - the log pattern, the settle window, the briefing, the
        classifier's backend, the call log, the connectivity checks - is
        built again. Everything is built before anything is swapped, so a
        failure leaves the doctor as it was.
        """
        old = self.settings
        # Environment-only settings are never the page's to change, and the
        # environment does not change under a running process.
        changed = [f.name for f in dataclasses.fields(Settings)
                   if not FIELD_SPECS[f.name].env_only
                   and getattr(old, f.name) != getattr(new, f.name)]
        live = [name for name in changed if not FIELD_SPECS[name].restart]
        later = [name for name in changed if FIELD_SPECS[name].restart]
        if not live:
            return {"applied": [], "restart": later}
        merged = dataclasses.replace(old, **{name: getattr(new, name) for name in live})

        # Built first: nothing below is assigned until all of it exists.
        holders = [obj for obj in self._settings_holders() if obj.settings is old]
        error_regex = merged.log_error_regex
        settle = timedelta(minutes=max(merged.deploy_settle_minutes, 0))
        self.settings = merged  # _decision_model reads it
        try:
            decision_model = self._decision_model()
            checks = connectivity.plan(merged) if self.net is not None else None
            warnings = merged.validate()
        except Exception:
            self.settings = old
            raise

        for obj in holders:
            obj.settings = merged
        self.detector._error_regex = error_regex
        self.settling.settle = settle
        self.diagnoser.recorder = self.store.record_llm_call if merged.llm_log_days > 0 else None
        self.classifier.model = decision_model
        self.briefing.enabled = (merged.briefing and self.diagnoser.enabled
                                 and self.briefing.call is not None)
        self.briefing.refresh = timedelta(minutes=max(0, merged.briefing_refresh_minutes))
        self.briefing.model = merged.briefing_model or merged.model
        if checks is not None:
            self.net.checks = checks
        self.startup_warnings = warnings
        log.info("settings applied without a restart: %s%s", ", ".join(live),
                 f"; waiting for a restart: {', '.join(later)}" if later else "")
        self.broadcast("settings", {"applied": live, "restart": later})
        return {"applied": live, "restart": later}

    def _settings_holders(self) -> list[Any]:
        """The parts of the doctor that keep a reference to the settings."""
        return [obj for obj in vars(self).values()
                if isinstance(getattr(obj, "settings", None), Settings)]

    def _decision_model(self):
        """The classifier's backend, or None when it is off or unconfigured."""
        s = self.settings
        if s.classifier_mode not in ("shadow", "on"):
            return None
        if s.classifier_provider == "openrouter" and s.classifier_key:
            return OpenRouterDecisions(
                url=s.classifier_url, api_key=s.classifier_key, model=s.classifier_model,
                timeout=s.classifier_timeout_seconds, price_in_per_mtok=s.classifier_price_per_mtok,
                recorder=self.store.record_llm_call if s.llm_log_days > 0 else None,
            )
        if s.classifier_provider == "chat" and self.diagnoser.enabled:
            return ChatDecisions(self.diagnoser.structured, s.model)
        return None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        for warning in self.startup_warnings:
            log.warning("config: %s", warning)
        self.docker_connected = await asyncio.to_thread(self.docker.ping)
        if not self.docker_connected:
            log.error("cannot reach Docker at %s - retrying in the background",
                      self.settings.docker_host)
        loop = asyncio.get_running_loop()
        self.docker.start_event_watcher(loop, self._events)
        self._tasks = [
            asyncio.create_task(self._event_loop(), name="events"),
            asyncio.create_task(self._poll_loop(), name="poll"),
            asyncio.create_task(self._housekeeping_loop(), name="housekeeping"),
            # Deliberately not awaited: a slow or hanging endpoint must not stop
            # the doctor coming up. Results reach the dashboard when they land.
            asyncio.create_task(self._check_docker_access(), name="preflight"),
            asyncio.create_task(self._check_oidc(), name="oidc-preflight"),
            asyncio.create_task(self._image_update_loop(), name="image-updates"),
        ]
        if self.net is not None:
            self._tasks.append(asyncio.create_task(self._net_loop(), name="connectivity"))
        log.info(
            "container-doctor started in %s mode (provider=%s model=%s)",
            self.settings.mode, self.settings.llm_provider, self.settings.model,
        )

    async def _check_docker_access(self) -> None:
        """Ask the Docker endpoint what it actually permits.

        A homelab often already runs a socket proxy with different settings,
        and DOCKER_HOST can be repointed at it. Finding out at startup that the
        endpoint is too narrow - or wider than the doctor needs - beats finding
        out when an action fails at 3am.
        """
        try:
            results = await asyncio.wait_for(
                probe_endpoint(self.settings.docker_host), timeout=90
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("could not probe the Docker endpoint: %s", exc)
            return

        self.docker_access = [
            {
                "name": r.probe.name,
                "need": r.probe.need,
                "allowed": r.allowed,
                "ok": r.ok,
                "why": r.probe.why,
                "detail": r.detail,
            }
            for r in results
        ]
        problems, warnings = summarize(self.settings.docker_host, results)
        for problem in problems:
            log.error("docker access: %s", problem)
        for warning in warnings:
            log.warning("docker access: %s", warning)
        self.startup_warnings.extend(problems + warnings)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.diagnoser.aclose()
        await self.notifier.aclose()
        self.docker.close()
        self.store.close()

    # -- pub/sub for the dashboard -----------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        message = {"type": event_type, "at": utcnow(), "payload": payload}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)

    # -- background loops ---------------------------------------------------

    async def _event_loop(self) -> None:
        while True:
            try:
                event = await self._events.get()
                await self._handle_docker_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("error handling docker event")

    async def _poll_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            self._poll_now.clear()
            started = loop.time()
            self._polling = True
            self.broadcast("poll", {"polling": True})
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll failed")
            took = loop.time() - started
            if self._polling:
                # It ended without publishing a snapshot; say it is over.
                self._polling = False
                self._poll_progress = None
                self.broadcast("poll", {"polling": False})
            if took > self.settings.poll_interval_seconds:
                log.info("poll took %.1fs, longer than the %ss interval",
                         took, self.settings.poll_interval_seconds)
            # The interval runs from the start of one poll to the next, so
            # "every 30s" means every 30s however long reading Docker takes.
            wait = max(0.0, self.settings.poll_interval_seconds - took)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._poll_now.wait(), timeout=wait)

    def _note_poll_progress(self, done: int, total: int, reading: list[str]) -> None:
        """Tell open dashboards how far the poll is - a small event, not the snapshot."""
        self._poll_progress = {"done": done, "total": total, "reading": list(reading)}
        self.broadcast("poll", {"polling": True, "progress": self._poll_progress})

    def request_poll(self) -> dict[str, Any]:
        """Poll now, from the dashboard, rather than at the next tick."""
        if self._polling:
            return {"started": False, "polling": True}
        self._poll_now.set()
        # Said now rather than when the loop wakes, so the button that asked
        # does not flick back to idle in between.
        self.broadcast("poll", {"polling": True})
        return {"started": True, "polling": True}

    async def _housekeeping_loop(self) -> None:
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                removed = await asyncio.to_thread(self.store.prune, self.settings.retention_days)
                removed += await asyncio.to_thread(
                    self.store.prune_llm_calls, max(self.settings.llm_log_days, 0))
                if removed:
                    log.info("pruned %d rows older than %d days", removed, self.settings.retention_days)
                resolved = await self._auto_resolve()
                if resolved:
                    log.info("auto-resolved %d incidents that stopped recurring", resolved)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("housekeeping failed")

    async def _check_oidc(self) -> None:
        """Fetch the provider's discovery document once, at startup.

        The doctor reaches its provider by the same public URL the browser
        uses, which on a home network can mean hairpinning back through the
        router. When that does not work, the only symptom is a failed sign-in
        later; this turns it into a warning on the dashboard now.
        """
        if not self.settings.oidc_enabled:
            return
        url = self.settings.oidc_discovery_url
        try:
            # Redirects are followed because a provider may answer the
            # well-known path with one, and the OIDC client does follow them.
            # Not following them here reported a provider that signs people in
            # perfectly as unreachable - a false alarm on a permanent banner,
            # which is worse than no alarm at all.
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(url)
        except Exception as error:
            self.startup_warnings.append(
                f"Cannot reach the OIDC provider at {url} ({error.__class__.__name__}). "
                "Sign-in will fail until the doctor can resolve and reach that URL."
            )
            return

        if response.status_code >= 400:
            # The status, not the name of the exception it raised: 404 and 403
            # need entirely different fixes, and "HTTPStatusError" says neither.
            self.startup_warnings.append(
                f"The OIDC provider answered HTTP {response.status_code} at {url}. "
                "Check DOCTOR_OIDC_ISSUER points at the provider's base URL."
            )
            return

        try:
            issuer = str(response.json().get("issuer", ""))
        except ValueError:
            self.startup_warnings.append(
                f"{url} did not return a discovery document "
                f"(content-type {response.headers.get('content-type', 'unknown')}). "
                "Check DOCTOR_OIDC_ISSUER points at the provider's base URL."
            )
            return
        if issuer.rstrip("/") != self.settings.oidc_issuer:
            self.startup_warnings.append(
                f"OIDC issuer mismatch: DOCTOR_OIDC_ISSUER is "
                f"{self.settings.oidc_issuer} but the provider calls itself {issuer}. "
                "Token validation will reject every login until they agree."
            )

    async def _image_update_loop(self) -> None:
        while True:
            hours = self.settings.image_check_hours
            if hours <= 0:
                return
            if not self.containers:
                # Started before the first poll landed. Waiting a few seconds
                # beats spending the whole interval on an empty list.
                await asyncio.sleep(5)
                continue
            await self._run_image_check()
            await asyncio.sleep(hours * 3600)

    async def _run_image_check(self) -> None:
        """One check, announced to the dashboard as it starts and ends."""
        if self._image_checking:
            return
        self._image_checking = True
        self.broadcast("state", self.snapshot())
        try:
            try:
                await self._refresh_deploy_stacks()
            except Exception:
                log.exception("listing the deploy workflow's stacks failed")
            await self._check_image_updates()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("image update check failed")
        finally:
            self._image_checking = False
            self._image_progress = None
            self._image_partial = []
            self.broadcast("state", self.snapshot())

    def refresh_images(self) -> dict[str, Any]:
        """Check the registries now, from the dashboard.

        Not more than once a minute: each check is a request per image to
        the registries, and Docker Hub rate-limits anonymous callers.
        """
        if self._image_checking:
            return {"started": False, "checking": True}
        last = _parse_iso(self.image_updates.get("checked_at"))
        if last and datetime.now(timezone.utc) - last < timedelta(seconds=60):
            return {"started": False, "checking": False,
                    "reason": "Checked less than a minute ago."}
        if not self.containers:
            return {"started": False, "checking": False,
                    "reason": "No containers polled yet."}
        asyncio.create_task(self._run_image_check(), name="image-refresh")
        return {"started": True, "checking": True}

    async def _check_image_updates(self) -> None:
        """Compare what is running against what each registry publishes.

        The doctor never pulls - the proxy denies POST /images - and updating
        a host unattended is how you discover at 3am which service did not
        survive it. A new build is redeployed only when someone asks, through
        the stack's redeploy webhook.
        """
        images = sorted({
            str(c.get("image") or "") for c in self.containers.values() if c.get("image")
        })
        if not images:
            return

        results: list[dict[str, Any]] = []
        self._image_partial = results  # filled in as each image is answered
        denied = False
        async with httpx.AsyncClient(timeout=20) as client:
            for done, image in enumerate(images):
                self._note_image_progress(done, len(images), image)
                entry, image_denied = await self._check_image(client, image)
                denied = denied or image_denied
                results.append(entry)

        if denied:
            # One notice rather than the same sentence against thirty images.
            # A pinned image is still pinned - that much we know without /images.
            # A newer version is still known too: tags come from the registry.
            results = [
                r if r["state"] == "pinned" else
                {**r, "state": "unknown",
                 "detail": "the Docker endpoint does not permit reading images"}
                for r in results
            ]
        self._note_image_progress(len(images), len(images), None)
        self.image_updates = self._image_report(results, utcnow(), denied)
        log.info(
            "image check: %d of %d have updates, %d have a newer version",
            self.image_updates["available"], len(results),
            self.image_updates["newer_available"],
        )

    @staticmethod
    def _image_report(results: list[dict[str, Any]], checked_at: str | None,
                      denied: bool) -> dict[str, Any]:
        return {
            "checked_at": checked_at,
            "images": results,
            "available": sum(1 for r in results if r["state"] == "update"),
            "newer_available": sum(1 for r in results if r.get("newer")),
            "cooling": sum(1 for r in results if r.get("cooling")),
            "needs_images_permission": denied,
        }

    async def _check_image(
        self, client: httpx.AsyncClient, image: str
    ) -> tuple[dict[str, Any], bool]:
        """One image against its registry. Returns (entry, /images denied)."""
        def describe(status) -> dict[str, Any]:
            return {
                "image": status.image,
                "state": status.state,
                "detail": status.detail,
                "containers": sorted(
                    c["name"] for c in self.containers.values()
                    if c.get("image") == status.image
                ),
            }

        reference = parse_reference(image)
        pinned = reference is not None and reference.pinned
        if pinned and parse_tag(reference.tag) is None:
            # Nothing to ask anyone: a digest names exactly one image, and
            # without a version tag there is no release line to follow.
            return describe(compare(image, [], "", "")), False

        details = await self.docker.image(image)
        denied = details is None
        if denied:
            details = {"repo_digests": [], "labels": {}}
        remote, reason = ("", "")
        # A digest names exactly one image: no new build to ask about. Its tag
        # still names a release line, so a newer version is looked for below.
        if reference is not None and not denied and not pinned:
            remote, reason = await remote_digest(client, reference)
        entry = describe(compare(image, details["repo_digests"], remote, reason))
        if reference is not None:
            # Where its release notes live, for reading them on request.
            entry["source"] = (source_repository(details["labels"], reference)
                               if self.settings.release_notes else "")
            # Newer releases still in their cooldown ride along apart
            # from `newer`: shown, but nothing counts or announces them.
            entry["newer"], entry["cooling"] = await self._newer_version(
                client, reference, details["labels"])
        return entry, denied

    def _note_image_progress(self, done: int, total: int, current: str | None) -> None:
        """Tell open dashboards how far the check is, and what it found so far.

        A small event rather than the snapshot: the results so far ride along
        so the list fills in as the check goes, instead of all at the end.
        """
        self._image_progress = {"done": done, "total": total, "current": current}
        self.broadcast("images", {"checking": True, "progress": self._image_progress,
                                  "partial": list(self._image_partial)})

    async def _newer_version(
        self, client: httpx.AsyncClient, reference, labels: dict[str, str]
    ) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        """A newer release in the pinned tag's own line, with its notes.

        Returns (newer, cooling): the newest release out of its update
        cooldown, and the newest one still inside it, if any.
        """
        if parse_tag(reference.tag) is None:
            return None, None  # latest, stable: the digest check covers those
        tags, reason = await list_tags(client, reference)
        if reason:
            # Past the page limit the newer-version check is off for this image
            # for good, not for one run: say so where it gets seen.
            if TOO_MANY_TAGS in reason:
                log.warning("could not list tags for %s: %s", reference, reason)
            else:
                log.info("could not list tags for %s: %s", reference, reason)
            return None, None
        found, cooling = await settled(
            client, reference, tags, self.settings.update_cooldown_days)
        if found is None:
            # Everything newer is still inside its cooldown: nothing to take yet.
            return None, cooling
        newer: dict[str, Any] = {
            "tag": found.newest,
            "level": found.level,
            "behind": found.behind,
            "same_major": found.newest_same_major,
            "notes_url": "",
            "breaking": "",
            "breaking_url": "",
        }
        repository = source_repository(labels, reference)
        if repository:
            newer["notes_url"] = f"https://github.com/{repository}/releases"
        if repository and self.settings.release_notes:
            notes = await release_notes(client, repository, found)
            if notes:
                newer["notes_url"] = notes["url"]
                newer["breaking"] = notes.get("breaking", "")
                newer["breaking_url"] = notes.get("breaking_url", "")
        return newer, cooling

    def _record_disk_trend(self, usages: list[dict[str, Any]]) -> None:
        """Store a sample and attach the fitted trend, in one pass.

        Runs on a worker thread: sqlite writes and a least-squares fit have no
        business holding up the event loop, small as they are.
        """
        window = timedelta(days=self.settings.disk_trend_window_days)
        since = datetime.now(timezone.utc) - window
        for usage in usages:
            path = usage["path"]
            try:
                self.store.record_disk_sample(
                    path, usage["used_bytes"], usage["total_bytes"],
                    free_bytes=usage["free_bytes"],
                    bucket_minutes=self.settings.disk_sample_minutes,
                )
                # Full means no free space left, the way the percentage counts
                # it - not the filesystem total, which includes the blocks
                # reserved for root that no container can fill.
                fitted = trend(
                    self.store.disk_samples(path, since),
                    usage["used_bytes"] + usage["free_bytes"],
                )
                # A plain dict, not the dataclass: this rides along in the API
                # payload and the evidence bundle, both of which are JSON.
                usage["trend"] = None if fitted is None else {
                    "bytes_per_day": fitted.bytes_per_day,
                    "days_until_full": fitted.days_until_full,
                    "samples": fitted.samples,
                    "span_days": fitted.span_days,
                    "rate": human_rate(fitted.bytes_per_day),
                    "until_full": (None if fitted.days_until_full is None
                                   else human_days(fitted.days_until_full)),
                }
            except Exception:
                # A missing trend costs a forecast, never a poll.
                log.exception("could not update the disk trend for %s", path)
                usage["trend"] = None

    def _record_history(self, snapshots: dict[str, dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc)
        for name, container in snapshots.items():
            stats = container.get("stats") or {}
            if not stats:
                continue
            points = self.history.setdefault(name, deque(maxlen=HISTORY_POINTS))
            if points and (now - points[-1][0]).total_seconds() < HISTORY_STEP_SECONDS - 1:
                continue
            points.append((
                now,
                round(float(stats.get("cpu_host_pct", stats.get("cpu_pct", 0)) or 0), 2),
                round(float(stats.get("mem_pct", 0) or 0), 2),
                int(stats.get("mem_usage_bytes", 0) or 0),
            ))
        for gone in set(self.history) - set(snapshots):
            del self.history[gone]

    def _track_changes(self, snapshots: dict[str, dict[str, Any]]) -> None:
        """Compare this poll against the last and write down what changed.

        Also takes the hour's memory reading. Runs on a worker thread.
        """
        before = self.store.container_states()
        first_run = not before
        now = datetime.now(timezone.utc)
        states: dict[str, dict[str, Any]] = {}
        found: list[tuple[str, str, changes.Change]] = []
        for name, container in snapshots.items():
            if not container.get("watched"):
                continue
            observed = changes.observe(container)
            states[name] = observed
            for change in changes.diff(name, before.get(name), observed, first_run=first_run):
                found.append((name, observed["stack"], change))
                if change.kind in ("recreated", "appeared"):
                    # Also caught when the events were missed - a doctor
                    # restarted mid-deploy, a dropped event stream.
                    when = _parse_iso(change.at)
                    if when:
                        self.settling.note_deploy(when)
        renamed = changes.pair_renames(found, before, states)
        for name, stack, change in list(found):
            if change.kind == "renamed":
                old = change.detail["from_name"]
                found.append((old, stack, changes.Change(
                    "removed", change.at, f"{old} was renamed to {name}",
                    {"image": before[old].get("image", ""), "renamed_to": name})))
        gone = renamed + [
            name for name, row in before.items()
            if name not in snapshots and name not in renamed
            and (seen := _parse_iso(row.get("seen_at"))) is not None and now - seen > REMOVED_AFTER
        ]
        for name in gone:
            if name not in renamed:
                found.append((name, before[name].get("stack", ""),
                              changes.removed(name, before[name], utcnow())))
        changes.group_redeploys(found)

        for name, stack, change in found:
            if change.kind == "restarted" and self._restarted_by_doctor(name, change.at):
                change.detail["by"] = "doctor"
                change.summary = f"{name} was restarted by the doctor"
            self.store.record_change(
                name, stack, change.kind, change.at, change.summary, change.detail,
                fold_minutes=RESTART_FOLD_MINUTES if change.kind == "restarted" else 0,
            )
            log.info("change: %s", change.summary)
        self.store.save_container_states(states, gone)

        self.store.record_mem_samples([
            (name, int(container["stats"]["mem_usage_bytes"]))
            for name, container in snapshots.items()
            if container.get("watched") and (container.get("stats") or {}).get("mem_usage_bytes")
        ])

    def _restarted_by_doctor(self, name: str, at: str) -> bool:
        """Whether one of the doctor's own restarts explains this one."""
        when = _parse_iso(at) or datetime.now(timezone.utc)
        for action in self.store.list_actions(limit=5, container_name=name):
            if action["action"] != "restart_container" or action["status"] == "pending":
                continue
            ran = _parse_iso(action.get("executed_at") or action.get("created_at"))
            if ran and timedelta(minutes=-1) <= when - ran <= timedelta(minutes=5):
                return True
        return False

    def memory_report(self, name: str | None = None) -> list[dict[str, Any]]:
        """A memory-limit verdict per watched container, most urgent first."""
        since = datetime.now(timezone.utc) - MEMORY_WINDOW
        history = self.store.mem_history(since, name)
        out = []
        for container in self.containers.values():
            if not container.get("watched") or (name and container["name"] != name):
                continue
            info = _stack_of(container)
            out.append(memory.suggest(
                container["name"], history.get(container["name"], []),
                limit=int(container.get("mem_limit_config") or 0),
                stack=info["stack"], service=info["service"],
                oom_killed=bool(container.get("oom_killed")),
            ))
        return memory.rank(out)

    def describe_host(self) -> str:
        """The host in a phrase for a model's instructions."""
        return hostkind.describe(self.platform, self.settings.host_name)

    @property
    def platform(self) -> hostkind.Host:
        """The host's kind and model, DOCTOR_HOST_KIND over what was detected."""
        return hostkind.resolve(self.settings.host_kind, self._platform_found)

    def _read_raid(self) -> dict[str, Any]:
        """Every md array with a verdict attached. Runs on a worker thread."""
        synology = self.platform.synology
        arrays, reason = raid.read(self.settings.mdstat_path,
                                   raid.SYSTEM_ARRAYS if synology else frozenset())
        pools = raid.inspect(arrays, self.settings.sysfs_path, paths=self.settings.disk_paths,
                             synology=synology)
        out: list[dict[str, Any]] = []
        for array in arrays:
            most = array.active
            if array.state != "inactive":
                most = self.store.raid_most_members(array.name, array.active)
            verdict = raid.judge(array, most)
            out.append({
                **array.to_dict(),
                "most_seen": most,
                "healthy": verdict.healthy,
                "severity": verdict.severity,
                "summary": verdict.summary,
                "warnings": [{"key": key, "summary": text}
                             for key, text in raid.warnings(array)],
            })
        return {"arrays": out, "pools": pools, "reason": reason,
                "drives": self._drive_errors(raid.drives(arrays))}

    def _drive_errors(self, drives: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Each drive's read errors against what the doctor has seen of it
        before: across a reboot, which restarts md's count, and over the last
        day, which says whether the disk is getting worse."""
        now = datetime.now(timezone.utc)
        days = self.settings.raid_errors_days
        window = now - timedelta(days=days)
        for drive in drives:
            # Twice the window, so the reading it is measured from survives
            # the doctor having been down when the window began.
            samples = self.store.drive_error_samples(drive["disk"], window - timedelta(days=days))
            self.store.record_drive_errors(drive["disk"], drive["errors"])
            drive.update(raid.error_history(
                samples, drive["errors"], now - timedelta(hours=raid.ERRORS_RECENT_HOURS), window))
            # Grew since the last reading: that, not every poll, is a sighting.
            drive["grew"] = drive["errors"] > (samples[-1][1] if samples else 0)
            warning = raid.drive_warning(drive, self.settings.raid_errors_per_day, days)
            drive["severity"], drive["summary"] = warning or ("", "")
        return drives

    async def _resolve_cleared_raid_warnings(self, signals: list[Signal]) -> None:
        """Close a raid_errors incident once nothing fires it any more: the
        scrub that found mismatches was followed by a clean one, or a drive
        has had no new error in DOCTOR_RAID_ERRORS_DAYS.
        Also retires incidents from before errors were counted per drive,
        which opened one per array for the same disk."""
        arrays = {a["name"]: a for a in self.raid.get("arrays") or []}
        if not any(a.get("drives") for a in arrays.values()):
            return  # /sys unreadable this poll: absence of a warning says nothing
        firing = {s.fingerprint for s in signals if s.kind == "raid_errors"}
        incidents = await asyncio.to_thread(self.store.open_incidents_of_kind, "raid_errors")
        for incident in incidents:
            if incident["fingerprint"] in firing:
                continue
            array = arrays.get(incident["container_name"].removeprefix("host:"))
            if array is not None and array.get("operation"):
                continue  # mid-scrub: its verdict is not in yet
            await asyncio.to_thread(
                self.store.set_incident_status, incident["id"], "resolved", resolved=True)
            log.info("%s no longer fires (incident %s)", incident["summary"], incident["id"])
            self.broadcast("incident", {"id": incident["id"], "status": "resolved"})

    async def _check_host(self) -> list[Signal]:
        """Read the host, store the reading, notice a reboot, and run the host
        rules. Never fails a poll: a host that cannot be read is a blank
        Host page, not a missing container check."""
        try:
            reading = await asyncio.to_thread(self.host_reader.read)
        except Exception:
            log.exception("could not read the host")
            return []
        self.host = reading
        if reading is None:
            return []
        try:
            await asyncio.to_thread(self.store.record_host_sample, reading,
                                    bucket_minutes=self.settings.host_sample_minutes)
            await self._notice_reboot(reading)
        except Exception:
            log.exception("could not record the host reading")
        signals, cleared = self.detector.check_host(reading)
        reading["alerts"] = sorted(self.detector.host_conditions(reading))
        for kind in cleared:
            await self._resolve_host_incidents(kind)
        return signals

    async def _net_loop(self) -> None:
        """Check the internet and DNS every net_check_seconds, for as long as
        the doctor runs. A round that fails to run is logged and skipped."""
        async with httpx.AsyncClient(follow_redirects=False,
                                     headers={"User-Agent": "container-doctor"}) as client:
            while True:
                try:
                    await self.net.run_round(client)
                    await self._net_round_done()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("connectivity round failed")
                await asyncio.sleep(self.settings.net_check_seconds)

    async def _net_round_done(self) -> None:
        for kind, key, severity, summary, details, unchanged in self.net.evaluate():
            await self._process_signal(Signal(
                kind=kind, severity=severity, summary=summary,
                container=dict(connectivity.NET_CONTAINER), details=details,
                signature_parts=[key] if key else [], unchanged=unchanged))
        # Anything failing this round, lasting or not, holds its incident open.
        failing = self._failing_now()
        # Recovery is judged against what is open, not against this run's
        # memory, so an incident left open across a restart still closes.
        needed = max(1, self.settings.net_fail_rounds)
        for kind in connectivity.NET_KINDS:
            for incident in await asyncio.to_thread(self.store.open_incidents_of_kind, kind):
                fp = incident["fingerprint"]
                if fp in failing:
                    self._net_ok_rounds[fp] = 0
                    continue
                self._net_ok_rounds[fp] = self._net_ok_rounds.get(fp, 0) + 1
                if self._net_ok_rounds[fp] < needed:
                    continue
                self._net_ok_rounds.pop(fp, None)
                await self._resolve_incident(incident, "The network is back to normal")
        self.broadcast("connectivity", self.net.view())

    def _failing_now(self) -> set[str]:
        """Fingerprints of what the last round found failing, lasting or not."""
        out = set()
        for slot in (self.net.verdict.get("failing") or {}) if self.net else {}:
            kind, _, key = slot.partition("|")
            out.add(Signal(kind=kind, severity="", summary="",
                           container=dict(connectivity.NET_CONTAINER),
                           signature_parts=[key] if key else []).fingerprint)
        return out

    async def _check_smart(self) -> list[Signal]:
        """Read the SMART sidecar at most once a minute - it reads the drives
        itself every few minutes - and turn each drive's state into a signal.
        A sidecar that is not there is a note on the Storage page, never a
        failed poll."""
        now = asyncio.get_running_loop().time()
        if self.smart is not None and now - self._smart_read_at < 60:
            return []
        self._smart_read_at = now
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(self.settings.smart_url)
                response.raise_for_status()
            drives = smart.drives(smart.parse_metrics(response.text))
        except Exception as exc:
            self.smart = {"drives": [], "error": f"{exc.__class__.__name__}: {exc}"[:200],
                          "checked_at": utcnow()}
            return []
        await self._recall_drives(drives)
        signals = []
        for drive in drives:
            key = f"smart_crc:{drive['serial'] or drive['device']}"
            baseline = await asyncio.to_thread(self.store.host_state, key)
            if baseline is None and drive["crc_errors"] is not None:
                await asyncio.to_thread(self.store.set_host_state, key, str(drive["crc_errors"]))
            crc_before = int(baseline) if baseline is not None else drive["crc_errors"]
            severity, problems = smart.assess(
                drive, crc_before=crc_before, temp_limit=self.settings.smart_temp_c,
                nvme_temp_limit=self.settings.smart_nvme_temp_c,
                wear_limit=self.settings.smart_wear_pct)
            drive["severity"], drive["problems"] = severity, problems
            if not severity:
                continue
            signal = smart_signal(drive)
            # Read again unchanged, it keeps its incident open without
            # counting as another sighting; numbers that moved are news.
            signal.unchanged = self._smart_said.get(signal.fingerprint) == signal.summary
            self._smart_said[signal.fingerprint] = signal.summary
            signals.append(signal)
        self.smart = {"drives": drives, "error": None, "checked_at": utcnow()}
        temps = {drive_key(d): d["temperature"] for d in drives
                 if d["temperature"] is not None and not d["asleep"]}
        try:
            await asyncio.to_thread(self.store.record_drive_temps, temps,
                                    bucket_minutes=self.settings.host_sample_minutes)
        except Exception:
            log.exception("could not record drive temperatures")
        sleep = {drive_key(d): d["asleep"] for d in drives if smart.can_sleep(d)}
        try:
            await asyncio.to_thread(self.store.record_drive_sleep, sleep,
                                    bucket_minutes=self.settings.host_sample_minutes)
        except Exception:
            log.exception("could not record drive sleep")
        await self._resolve_healthy_drives(drives)
        return signals

    async def _recall_drives(self, drives: list[dict[str, Any]]) -> None:
        """A drive in standby answers with nothing but that. Fill in who it is
        from its last reading awake - kept across restarts - so it keeps its
        name and model, and its history stays filed under its serial."""
        for d in drives:
            key = f"smart_id:{d['device']}"
            if d["serial"]:
                known = {k: d.get(k) for k in smart.IDENTITY}
                if self._drive_ids.get(d["device"]) != known:
                    self._drive_ids[d["device"]] = known
                    await asyncio.to_thread(self.store.set_host_state, key, json.dumps(known))
            elif d["asleep"]:
                if d["device"] not in self._drive_ids:
                    stored = await asyncio.to_thread(self.store.host_state, key)
                    try:
                        self._drive_ids[d["device"]] = json.loads(stored) if stored else {}
                    except ValueError:
                        self._drive_ids[d["device"]] = {}
                d.update({k: v for k, v in self._drive_ids[d["device"]].items()
                          if k in smart.IDENTITY and v not in (None, "")})

    async def _resolve_healthy_drives(self, drives: list[dict[str, Any]]) -> None:
        """Close a drive's incident once nothing is wrong with it any more -
        pending sectors are remapped, a hot drive has cooled. A drive missing
        from this reading (asleep, unplugged) says nothing either way."""
        healthy = {f"host:{d['label']}" for d in drives if not d.get("severity") and not d["asleep"]}
        for incident in await asyncio.to_thread(self.store.open_incidents_of_kind, "smart_health"):
            if incident["container_name"] in healthy:
                await self._resolve_incident(incident, "Drive healthy again")

    async def _resolve_incident(self, incident: dict[str, Any], title: str) -> None:
        await asyncio.to_thread(
            self.store.set_incident_status, incident["id"], "resolved", resolved=True)
        log.info("%s is back to normal (incident %s)", incident["kind"], incident["id"])
        self.broadcast("incident", {"id": incident["id"], "status": "resolved"})
        await self.notifier.notify_recovered(
            title, f"Resolved: {incident['summary']}",
            dashboard_url=self.incident_url(incident["id"]))

    async def _notice_reboot(self, reading: dict[str, Any]) -> None:
        """A boot time later than the one stored means the host restarted while
        the doctor was down with it. Said once: a line on the timeline, and a
        notification - nothing else would tell you it happened."""
        booted = reading.get("boot_time")
        if not booted:
            return
        previous = await asyncio.to_thread(self.store.host_state, "boot_time")
        last_seen = await asyncio.to_thread(self.store.host_state, "last_seen")
        await asyncio.to_thread(self.store.set_host_state, "last_seen", reading["at"])
        before, now = _parse_dt(previous), _parse_dt(booted)
        # Boot time is derived from uptime, so it wobbles by a second or two.
        if before is not None and now is not None and abs((now - before).total_seconds()) < 120:
            return
        await asyncio.to_thread(self.store.set_host_state, "boot_time", booted)
        if before is None or now is None or now < before:
            return  # first reading ever, or a clock that moved backwards
        seen = _parse_dt(last_seen)
        was_up = f", after {host.human_duration((seen - before).total_seconds())} up" if seen else ""
        down = ""
        if seen and now > seen:
            down = f"; last seen running {host.human_duration((now - seen).total_seconds())} before"
        when = now.astimezone().strftime("%Y-%m-%d %H:%M")
        name = self.settings.host_name
        summary = f"{hostkind.called(name, start=True)} restarted at {when}{was_up}{down}"
        log.info(summary)
        await asyncio.to_thread(
            self.store.record_change, HOST_ID, "", "rebooted", booted, summary,
            {"boot_time": booted, "previous_boot": previous, "last_seen": last_seen})
        self.broadcast("change", {"container": HOST_ID, "kind": "rebooted"})
        await self.notifier.notify_recovered(
            f"{hostkind.called(name, start=True)} restarted", summary,
            dashboard_url=f"{self.settings.public_url}/#/host" if self.settings.public_url else "")

    async def _resolve_host_incidents(self, kind: str) -> None:
        """Close a host incident once its condition has been gone as long as it
        had to last to open one."""
        for incident in await asyncio.to_thread(self.store.open_incidents_of_kind, kind):
            who = hostkind.called(self.settings.host_name, start=True)
            await self._resolve_incident(incident, f"{who} is back to normal")

    def host_history(self, hours: int) -> dict[str, Any]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        return {
            "samples": self.store.host_history(since),
            "step_minutes": self.settings.host_sample_minutes,
            "reboots": self.store.list_changes(container_name=HOST_ID, since=since, limit=20),
        }

    def drive_temp_history(self, drive: str, hours: int) -> dict[str, Any]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        return {
            "drive": drive,
            "samples": [[row["at"], row["celsius"]]
                        for row in self.store.drive_temp_history(drive, since)],
            "step_minutes": self.settings.host_sample_minutes,
        }

    def drive_sleep_history(self, drive: str, hours: int) -> dict[str, Any]:
        """Each bucket's share of reads that found the drive asleep, and over
        the range: how much of the time it slept and how often it woke. A
        bucket counts as asleep when most of its reads were; a wake is an
        awake bucket right after an asleep one, so a gap - the doctor down -
        is never counted as one."""
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        step = timedelta(minutes=self.settings.host_sample_minutes)
        samples, slept, wakes = [], 0, 0
        before: tuple[datetime, bool] | None = None
        for row in self.store.drive_sleep_history(drive, since):
            start = datetime.fromisoformat(row["bucket"]).replace(tzinfo=timezone.utc)
            share = row["asleep"] / row["polls"] if row["polls"] else 0.0
            asleep = share > 0.5
            if before and before[1] and not asleep and start - before[0] <= step:
                wakes += 1
            slept += asleep
            before = (start, asleep)
            samples.append([start.isoformat(timespec="seconds"), round(share, 2)])
        return {
            "drive": drive,
            "samples": samples,
            "step_minutes": self.settings.host_sample_minutes,
            "asleep_pct": round(slept / len(samples) * 100) if samples else None,
            "wakes": wakes,
        }

    async def _resolve_recovered_arrays(self, arrays: list[dict[str, Any]]) -> None:
        """Close an array's incident the poll it is whole again.

        Not left to auto-resolve: that waits hours of silence, and "your array
        is healthy again" is news worth having as soon as it is true.
        """
        for array in arrays:
            if not array["healthy"]:
                continue
            fingerprint = raid_signal(array).fingerprint
            existing = await asyncio.to_thread(
                self.store.find_open_by_fingerprint, fingerprint,
                self.settings.incident_dedup_hours,
            )
            if not existing:
                continue
            await asyncio.to_thread(
                self.store.set_incident_status, existing["id"], "resolved", resolved=True)
            log.info("disk array %s is healthy again (incident %s)", array["name"], existing["id"])
            self.broadcast("incident", {"id": existing["id"], "status": "resolved"})
            await self.notifier.notify_recovered(
                f"{array['name']}: healthy again",
                f"Disk array {array['name']} has all {array['active']} of its disks again.",
                dashboard_url=self.incident_url(existing["id"]),
            )

    def incident_url(self, incident_id: str) -> str:
        """Where a notification about this incident should land, or ''."""
        if not self.settings.public_url or not incident_id:
            return ""
        return f"{self.settings.public_url}/#incident/{incident_id}"

    def activity_url(self) -> str:
        """Where a notification about a dashboard-requested action lands, or ''."""
        return f"{self.settings.public_url}/#/activity" if self.settings.public_url else ""

    async def _auto_resolve(self) -> int:
        hours = self.settings.auto_resolve_hours
        if hours <= 0:
            return 0
        count = await asyncio.to_thread(
            self.store.auto_resolve_stale, hours, AUTO_RESOLVE_EXCLUDED_KINDS
        )
        if count:
            self.broadcast("incident", {"auto_resolved": count})
        return count

    # -- polling ------------------------------------------------------------

    async def _poll_once(self) -> None:
        started = asyncio.get_running_loop().time()
        try:
            containers = await self.docker.list_containers()
            self.docker_connected = True
        except Exception as exc:
            self.docker_connected = False
            log.warning("could not list containers: %s", exc)
            return

        # One cheap request; the network map reads it, nothing else does.
        self.network_inventory = await self.docker.networks()

        snapshots: dict[str, dict[str, Any]] = {}
        signals: list[Signal] = []

        for container in containers:
            policy = ContainerPolicy.from_labels(container.get("labels") or {})
            container["policy"] = {
                "enabled": policy.enabled,
                "mode": self.policy.effective_mode(policy),
                "critical": policy.critical,
            }
            container["watched"] = self._is_watched(container, policy)
            container["protected_reason"] = self.policy.protected(container["name"])
            container["stats"] = {}
            snapshots[container["name"]] = container

        # Docker is read for several containers at once: `docker stats` waits
        # for a second CPU sample, so reading them one by one is what made a
        # poll of a few dozen containers take over a minute.
        cutoff = self._log_cutoff()
        gate = asyncio.Semaphore(max(1, self.settings.poll_concurrency))
        total = sum(1 for c in containers if c["watched"] and c.get("state") == "running")
        reading: list[str] = []
        done = 0
        self._note_poll_progress(done, total, reading)

        async def read(container: dict[str, Any]) -> str:
            nonlocal done
            if not (container["watched"] and container.get("state") == "running"):
                return ""
            async with gate:
                reading.append(container["name"])
                self._note_poll_progress(done, total, reading)
                try:
                    stats, log_text = await asyncio.gather(
                        self.docker.stats(container["id"]),
                        self.docker.logs(container["id"], self.settings.log_lines, since=cutoff),
                    )
                finally:
                    reading.remove(container["name"])
                    done += 1
                    self._note_poll_progress(done, total, reading)
            container["stats"] = stats or {}
            return log_text

        logs = await asyncio.gather(*(read(c) for c in containers))

        # Detection stays in order and one at a time: it keeps per-container
        # counts across polls.
        for container, log_text in zip(containers, logs):
            if not container["watched"]:
                continue
            # What a redeploy made it log is not what is wrong with it.
            log_text, _ = self.settling.strip(log_text)
            found = self.detector.from_poll(container, container["stats"] or None, log_text,
                                            settling=self.settling.active())
            signals.extend(self._unrequested(container, found))
        await asyncio.to_thread(self.store.save_log_marks, self.detector.drain_log_marks())

        if self.settings.disk_paths:
            self.disk = await asyncio.to_thread(disk_usage, self.settings.disk_paths)
            await asyncio.to_thread(self._record_disk_trend, self.disk)
            signals.extend(self.detector.check_disk(self.disk))
            signals.extend(self.detector.check_disk_trend(self.disk))

        if self.settings.mdstat_path:
            self.raid = await asyncio.to_thread(self._read_raid)
            raid_signals = self.detector.check_raid(self.raid["arrays"], self.raid["drives"])
            signals.extend(raid_signals)
            await self._resolve_recovered_arrays(self.raid["arrays"])
            await self._resolve_cleared_raid_warnings(raid_signals)

        if self.settings.proc_path and self.host_reader is not None:
            signals.extend(await self._check_host())

        if self.settings.smart_url:
            signals.extend(await self._check_smart())

        self.containers = snapshots
        self.last_poll_at = utcnow()
        self.last_poll_seconds = round(asyncio.get_running_loop().time() - started, 1)
        self._polling = False
        self._poll_progress = None
        self._record_history(snapshots)
        try:
            await asyncio.to_thread(self._track_changes, snapshots)
        except Exception:
            # A missed change costs a line of the timeline, never a poll.
            log.exception("could not record container changes")
        self.broadcast("state", self.snapshot())

        for signal in signals:
            asyncio.create_task(self._process_signal(signal))

    async def _handle_docker_event(self, event: dict[str, Any]) -> None:
        actor = (event.get("Actor") or {}).get("Attributes") or {}
        name = actor.get("name", "")
        action = event.get("Action", "")
        container_id = (event.get("Actor") or {}).get("ID") or event.get("id") or ""
        at = deploys.event_time(event)

        # A signal sent to a container, and containers destroyed or created:
        # someone is stopping or redeploying something. Never an incident in
        # itself; it opens a settle window and marks the stop as requested.
        if action == "kill":
            self.settling.note_stop(container_id, at)
            self.settling.note_deploy(at)
            return
        if action in ("destroy", "create"):
            self.settling.note_deploy(at)
            return
        if action == "die" and _exit_code(actor) != 0:
            if self.settling.requested_stop(container_id, at):
                log.info("%s exited %s after being asked to stop - not a failure",
                         name, actor.get("exitCode"))
                return
            # Wait to see whether it is being replaced before calling it a crash.
            asyncio.create_task(self._settle_die(event, container_id, name))
            return
        if action.startswith("health_status") and self.settling.active():
            return  # the poll reports it after the window, if it is still unhealthy
        await self._signal_from_event(event, name)

    def _unrequested(self, container: dict[str, Any], found: list[Signal]) -> list[Signal]:
        """The poll's signals without the exit of a container someone stopped."""
        asked = self.settling.requested_stop(container.get("id", ""))
        return [s for s in found if not (asked and s.kind == "exit_nonzero")]

    async def _settle_die(self, event: dict[str, Any], container_id: str, name: str) -> None:
        """A nonzero exit with no stop asked for, looked at again a moment later.

        A redeploy that did not signal first still replaces the container
        within seconds; a crash leaves the same container behind - exited, or
        restarted by its policy.
        """
        await asyncio.sleep(self.settings.die_grace_seconds)
        try:
            containers = await self.docker.list_containers()
        except Exception as exc:
            log.warning("could not re-check %s after it exited: %s", name, exc)
            containers = []
        now = next((c for c in containers if c["name"] == name), None)
        if containers and (now is None or now["id"] != container_id):
            log.info("%s exited %s while being %s - not a failure", name,
                     (event.get("Actor") or {}).get("Attributes", {}).get("exitCode"),
                     "removed" if now is None else "replaced")
            self.settling.note_deploy(deploys.event_time(event))
            return
        await self._signal_from_event(event, name, fresh=now)

    async def _signal_from_event(self, event: dict[str, Any], name: str,
                                 fresh: dict[str, Any] | None = None) -> None:
        container = fresh or self.containers.get(name)
        if container is None:
            # Event for something we haven't polled yet - look it up directly.
            containers = await self.docker.list_containers()
            container = next((c for c in containers if c["name"] == name), None)
            if container is None:
                return
            container["policy"] = {}
        policy = ContainerPolicy.from_labels(container.get("labels") or {})
        if not self._is_watched(container, policy):
            return
        signal = self.detector.from_event(event, container)
        if signal:
            await self._process_signal(signal)

    def _is_watched(self, container: dict[str, Any], policy: ContainerPolicy) -> bool:
        name = container.get("name", "")
        if name == self.settings.self_container_name:
            return False
        if name in self.settings.ignore_containers:
            return False
        if not policy.enabled:
            return False
        if self.settings.require_opt_in:
            return (container.get("labels") or {}).get("doctor.enable", "").lower() in (
                "1", "true", "yes", "on",
            )
        return True

    # -- the pipeline -------------------------------------------------------

    async def _process_signal(self, signal: Signal) -> None:
        async with self._work_lock:
            try:
                await self._process_signal_inner(signal)
            except Exception:
                log.exception("failed to process %s for %s", signal.kind, signal.container.get("name"))

    async def _process_signal_inner(self, signal: Signal) -> None:
        name = signal.container.get("name", "unknown")
        existing = await asyncio.to_thread(
            self.store.find_open_by_fingerprint,
            signal.fingerprint,
            self.settings.incident_dedup_hours,
        )
        # Against `rule_severity`, never `severity`: a diagnosis overwrites
        # `severity` with the model's own reading, and the model routinely
        # downgrades a rule's `critical` to `info` ("these are not fatal
        # errors"). Comparing against that made every subsequent poll look like
        # an escalation, so the same failure opened a fresh incident forever.
        if existing and SEVERITY_ORDER.get(signal.severity, 0) > SEVERITY_ORDER.get(
            existing["rule_severity"] or existing["severity"], 0
        ):
            # The same condition, but it has got worse - a disk at 92% is a
            # warning, at 96% it is critical. Let it open its own incident so
            # it is diagnosed and notified afresh rather than folded into a
            # row someone has already read and mentally filed.
            log.info(
                "%s for %s escalated from %s to %s",
                signal.kind, name, existing["rule_severity"], signal.severity,
            )
            if signal.kind == "raid_errors":
                # A drive's warning turning critical replaces it: one disk,
                # one incident on the list. (Disk pressure keeps both: an
                # action may still be waiting on the older one.)
                await asyncio.to_thread(
                    self.store.set_incident_status, existing["id"], "resolved", resolved=True)
                self.broadcast("incident", {"id": existing["id"], "status": "resolved"})
            existing = None

        if existing and signal.unchanged:
            await asyncio.to_thread(self.store.keep_incident_open, existing["id"])
            return
        if not existing and signal.unchanged and await asyncio.to_thread(
                self.store.was_dismissed, signal.fingerprint):
            return  # dismissed, and nothing has changed since: only news reopens it

        if existing:
            # The same failure, still open: count the sighting rather than
            # stacking up another identical incident (and another diagnosis).
            seen = await asyncio.to_thread(
                self.store.touch_incident, existing["id"],
                summary=signal.summary, lines=_sighting_lines(signal),
            )
            log.info(
                "%s for %s recurred (incident %s, seen %d times)",
                signal.kind, name, existing["id"], seen,
            )
            self.broadcast("incident", {"id": existing["id"], "summary": existing["summary"],
                                        "kind": signal.kind, "occurrences": seen})
            return

        # A signal already judged noise: said once, not re-read every poll -
        # checked before the evidence, which is a log read and an inspect.
        if self.classifier.eligible(signal):
            hit = await asyncio.to_thread(self.classifier.cached, signal)
            if hit is not None and hit.applied and hit.action == "suppress":
                return

        policy = ContainerPolicy.from_labels(signal.container.get("labels") or {})
        mode = self.policy.effective_mode(policy)

        evidence = await self._gather_evidence(signal)
        severity = signal.severity
        if self.classifier.eligible(signal):
            verdict = await self.classifier.review(
                signal, _render_incident(evidence, closing=False))
            if verdict is not None:
                evidence["classification"] = verdict.to_dict()
                self.broadcast("classification", {"container": name, "kind": signal.kind,
                                                  "action": verdict.action,
                                                  "applied": verdict.applied})
                if verdict.applied and verdict.action == "suppress":
                    log.info("classifier: %s for %s is noise (%s) - no incident",
                             signal.kind, name, verdict.reason)
                    return
                if verdict.applied and verdict.action in ("downgrade", "escalate"):
                    severity = verdict.severity
        incident_id = await asyncio.to_thread(
            self.store.open_incident,
            container_id=signal.container.get("id", ""),
            container_name=name,
            image=signal.container.get("image", ""),
            kind=signal.kind,
            fingerprint=signal.fingerprint,
            severity=severity,
            summary=signal.summary,
            evidence=evidence,
            mode=mode,
            rule_severity=signal.severity,
            lines=_sighting_lines(signal),
        )
        log.info("incident %s opened: %s", incident_id, signal.summary)
        self.broadcast("incident", {"id": incident_id, "summary": signal.summary, "kind": signal.kind})

        diagnosis, note = await self._diagnose(incident_id, signal, evidence)
        incident = await asyncio.to_thread(self.store.get_incident, incident_id)
        if incident is None:
            return

        decision = self.policy.decide(
            incident=incident, diagnosis=diagnosis, container_policy=policy
        )
        if note and decision.status == "skipped" and not diagnosis:
            decision.reason = note

        await self._apply_decision(incident, diagnosis, decision, policy)

    def _top_containers(self, count: int = 5) -> dict[str, list[dict[str, Any]]]:
        """Who is using the host: the heaviest containers by CPU and by memory."""
        rows = [{"name": c["name"], "cpu_host_pct": (c.get("stats") or {}).get("cpu_host_pct"),
                 "mem_bytes": (c.get("stats") or {}).get("mem_usage_bytes")}
                for c in self.containers.values() if c.get("stats")]
        return {
            "by_cpu": sorted(rows, key=lambda r: -(r["cpu_host_pct"] or 0))[:count],
            "by_memory": sorted(rows, key=lambda r: -(r["mem_bytes"] or 0))[:count],
        }

    def _log_cutoff(self) -> datetime | None:
        """Oldest log line worth reading, or None to read the whole tail."""
        minutes = self.settings.log_max_age_minutes
        if minutes <= 0:
            return None
        return datetime.now(timezone.utc) - timedelta(minutes=minutes)

    async def _gather_evidence(self, signal: Signal) -> dict[str, Any]:
        container = signal.container
        evidence: dict[str, Any] = {
            "kind": signal.kind,
            "summary": signal.summary,
            "detected_at": utcnow(),
            "details": signal.details,
            "container": {
                key: container.get(key)
                for key in (
                    "name", "image", "state", "health", "exit_code", "restart_count",
                    "oom_killed", "started_at", "finished_at", "restart_policy",
                )
            },
            "stats": container.get("stats") or None,
        }

        # What else in this stack is unwell. A database taking its application
        # down with it is two incidents about one problem, and the diagnosis
        # for the application is guesswork without this.
        relations = relatives(container, list(self.containers.values()))
        if relations and (relations.get("depends_on")
                          or relations.get("dependents")
                          or relations.get("unhealthy_siblings")):
            evidence["stack"] = relations
            root = likely_root_cause(relations)
            if root:
                evidence["likely_root_cause"] = root

        container_id = container.get("id")
        if container_id:
            raw_logs = await self.docker.logs(
                container_id, self.settings.log_lines, since=self._log_cutoff()
            )
            evidence["logs"] = redact(raw_logs)[-12000:]

            attrs = await self.docker.inspect(container_id)
            if attrs:
                health = (attrs.get("State", {}) or {}).get("Health") or {}
                entries = health.get("Log") or []
                if entries:
                    last = entries[-1]
                    evidence["health_log"] = redact(
                        f"exit={last.get('ExitCode')} output={last.get('Output', '')}"
                    )[:2000]

        if signal.kind == "host_disk_pressure" or self.disk:
            evidence["disk"] = self.disk

        # The host as a whole when this happened: a container OOM-killed on a
        # host with memory to spare hit its own limit; on a host out of
        # memory, it was a victim.
        if self.host:
            evidence["host"] = host.compact(self.host)
        if signal.kind in HOST_KINDS:
            evidence["top_containers"] = self._top_containers()
        # Whether the host could reach the internet and resolve names: a
        # container failing to call an API is a different story when nothing
        # on the host can.
        if self.net is not None and self.net.last_round_at:
            evidence["connectivity"] = {
                "internet": self.net.verdict.get("internet"),
                "dns": self.net.verdict.get("dns"),
                "failing": sorted(self.net.verdict.get("failing") or {}),
                "checked_at": self.net.last_round_at,
            }
        # A disk problem read two ways: md's count of corrected reads and the
        # drive's own SMART counters.
        if signal.kind in ("raid_degraded", "raid_errors", "smart_health") and self.smart:
            evidence["smart"] = [
                {k: d.get(k) for k in ("label", "model", "passed", "temperature",
                                       "power_on_hours", "counters", "crc_errors", "nvme",
                                       "problems", "asleep")}
                for d in self.smart.get("drives") or []]

        # What changed about it lately - a recreate on a new build ten minutes
        # before the errors started is most of a diagnosis on its own.
        recent_changes = await asyncio.to_thread(
            self.store.list_changes, container_name=container.get("name"),
            since=datetime.now(timezone.utc) - CHANGES_IN_EVIDENCE, limit=8,
        )
        if recent_changes:
            evidence["changes"] = [
                {"at": c["at"], "kind": c["kind"], "summary": c["summary"], "count": c["count"],
                 # version | build | same: whether the image moved, which
                 # decides whether a rollback is even a question.
                 "image_change": changes.image_moved(c.get("detail") or {}),
                 # What it runs with, compared across the replacement: None
                 # when there was nothing earlier to compare against.
                 "config_changes": (c.get("detail") or {}).get("config"),
                 "compose_definition": (c.get("detail") or {}).get("compose_definition", "")}
                for c in recent_changes
            ]

        repo = await self._stack_in_repo(container)
        if repo:
            evidence["repo"] = repo

        history = await asyncio.to_thread(self.store.list_incidents, limit=200)
        related = [
            f"{item['opened_at']} {item['kind']}: {item['summary']}"
            for item in history
            if item["container_name"] == container.get("name")
        ][:5]
        if related:
            evidence["history"] = "\n".join(related)

        # Single choke point: nothing is persisted or sent to a model until it
        # has been through redaction, whatever produced it.
        return redact_obj(evidence)

    REPO_CONTEXT_SECONDS = 600.0

    async def _stack_in_repo(self, container: dict[str, Any]) -> dict[str, Any] | None:
        """The stack's recent commits and the service's definition, from the
        repository it is deployed from - when a token can read it."""
        if not (self.settings.github_token and self.settings.deploy_repo):
            return None
        where = _stack_of(container)
        stack, service = where["stack"], where["service"]
        if not stack or not service:
            return None
        if self._deploy_stacks is not None and stack not in self._deploy_stacks:
            return None  # not deployed from this repository
        loop = asyncio.get_running_loop()
        cached = self._repo_context.get((stack, service))
        if cached and loop.time() - cached[0] < self.REPO_CONTEXT_SECONDS:
            context = cached[1]
        else:
            settings = self.settings
            async with httpx.AsyncClient(timeout=10) as client:
                context = await repo_context.stack_context(
                    client, settings.github_token, settings.deploy_repo,
                    settings.deploy_ref, stack, service)
            if context["error"]:
                log.warning("could not read %s from %s: %s",
                            stack, settings.deploy_repo, context["error"])
            self._repo_context[(stack, service)] = (loop.time(), context)
        created = str(container.get("created") or "")
        return {**context, "commits": [
            {**commit, "deployed": repo_context.deployed(commit["at"], created)}
            for commit in context["commits"]]}

    async def _diagnose(
        self, incident_id: str, signal: Signal, evidence: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        if not self.diagnoser.enabled:
            return None, "no LLM configured - incident recorded without a diagnosis"

        cached = await asyncio.to_thread(
            self.store.cached_diagnosis, signal.fingerprint, self.settings.diagnosis_cache_hours
        )
        if cached:
            cached = dict(cached, from_cache=True)
            await asyncio.to_thread(self.store.set_diagnosis, incident_id, cached, cached.get("severity"))
            log.info("incident %s reused a cached diagnosis", incident_id)
            return cached, ""

        usage = await asyncio.to_thread(self.store.usage_today)
        spent = self._budget_spent(usage)
        if spent:
            note = f"{spent} - diagnosis skipped"
            log.warning(note)
            return None, note
        # Free tiers are metered in requests per day, so that is the cap that
        # actually bites there - the USD meter would read zero all day. The
        # slot is claimed before the request so concurrent incidents cannot all
        # slip through on the same reading.
        if not await asyncio.to_thread(self.store.reserve_call, self.settings.daily_call_limit):
            note = (
                f"daily limit of {self.settings.daily_call_limit} LLM calls reached "
                f"({usage['calls']} today) - diagnosis skipped"
            )
            log.warning(note)
            return None, note

        await asyncio.to_thread(self.store.set_incident_status, incident_id, "diagnosing")
        try:
            diagnosis, call_usage = await self.diagnoser.diagnose(evidence, ref=incident_id)
        except Exception as exc:
            log.warning("diagnosis failed for incident %s: %s", incident_id, exc)
            return None, f"diagnosis failed: {exc}"

        await asyncio.to_thread(
            self.store.add_usage,
            input_tokens=call_usage["input_tokens"],
            output_tokens=call_usage["output_tokens"],
            cached_tokens=call_usage["cached_tokens"],
            cost_usd=call_usage["cost_usd"],
        )
        await asyncio.to_thread(self.store.cache_diagnosis, signal.fingerprint, diagnosis)
        await asyncio.to_thread(
            self.store.set_diagnosis, incident_id, diagnosis, diagnosis.get("severity")
        )
        self.broadcast("diagnosis", {"incident_id": incident_id, "summary": diagnosis["summary"]})
        return diagnosis, ""

    async def _apply_decision(
        self,
        incident: dict[str, Any],
        diagnosis: dict[str, Any] | None,
        decision,
        policy: ContainerPolicy,
    ) -> None:
        incident_id = incident["id"]
        name = incident["container_name"]

        if decision.status == "skipped":
            await asyncio.to_thread(
                self.store.record_action,
                incident_id=incident_id,
                container_name=name,
                action=decision.action,
                status="skipped",
                decided_by="policy",
                reason=decision.reason,
            )
            await asyncio.to_thread(self.store.set_incident_status, incident_id, "open")
            severity = (diagnosis or {}).get("severity") or incident.get("severity")
            if severity in ("warning", "critical") or policy.critical:
                await self.notifier.notify_incident(
                    incident, diagnosis, decision.reason,
                    dashboard_url=self.incident_url(incident_id))
            self.broadcast("decision", {"incident_id": incident_id, "status": "skipped"})
            return

        action_id = await asyncio.to_thread(
            self.store.record_action,
            incident_id=incident_id,
            container_name=name,
            action=decision.action,
            status="pending",
            decided_by=decision.decided_by,
            reason=decision.reason,
        )

        if decision.status == "approved":
            blocked = await asyncio.to_thread(self.policy.claim_action, action_id, policy)
            if blocked:
                decision.status = "pending"
                decision.reason = blocked

        if decision.status == "pending":
            await asyncio.to_thread(self.store.set_incident_status, incident_id, "awaiting_approval")
            await self.notifier.notify_incident(
                incident, diagnosis,
                f"Proposed: {decision.action.replace('_', ' ')} - {decision.reason} "
                "(approve it in the dashboard)",
                dashboard_url=self.incident_url(incident_id),
                awaiting_approval=True,
            )
            self.broadcast("decision", {"incident_id": incident_id, "status": "awaiting_approval"})
            return

        await self._run_action(action_id, incident, diagnosis)

    async def _run_action(
        self, action_id: str, incident: dict[str, Any], diagnosis: dict[str, Any] | None
    ) -> None:
        action_row = await asyncio.to_thread(self.store.get_action, action_id)
        # Only an action claimed for execution runs - by the policy's claim or
        # a person's take_pending_action - and each claim runs it once.
        if not action_row or action_row["status"] != "executing":
            return
        action = action_row["action"]
        incident_id = incident["id"]
        name = incident["container_name"]
        container_id = incident["container_id"]
        # A dashboard request is not an incident; its outcome lives in Activity.
        link = (self.activity_url() if incident.get("kind") == self.store.MANUAL
                else self.incident_url(incident_id))

        await asyncio.to_thread(self.store.finish_action, action_id, "executing", "started")
        await asyncio.to_thread(self.store.set_incident_status, incident_id, "acting")
        log.info("executing %s on %s (incident %s)", action, name, incident_id)

        try:
            result = await self.executor.execute(action, container_id, name)
        except Exception as exc:
            await asyncio.to_thread(self.store.finish_action, action_id, "failed", str(exc))
            await asyncio.to_thread(self.store.set_incident_status, incident_id, "failed")
            await self.notifier.notify_action(
                name, action, False, str(exc),
                dashboard_url=link)
            self.broadcast("action", {"incident_id": incident_id, "action": action, "status": "failed"})
            return

        if action == "prune_images":
            await asyncio.to_thread(self.store.finish_action, action_id, "succeeded", result)
            await asyncio.to_thread(self.store.set_incident_status, incident_id, "resolved", resolved=True)
            await self.notifier.notify_action(
                name, action, True, result,
                dashboard_url=link)
            self.broadcast("action", {"incident_id": incident_id, "action": action, "status": "succeeded"})
            return

        healthy, detail = await self.executor.verify(container_id, name, action)
        status = "succeeded" if healthy else "failed"
        await asyncio.to_thread(self.store.finish_action, action_id, status, f"{result}; {detail}")
        await asyncio.to_thread(
            self.store.set_incident_status,
            incident_id,
            "resolved" if healthy else "failed",
            resolved=healthy,
        )
        await self.notifier.notify_action(
            name, action, healthy, f"{result}; {detail}",
            dashboard_url=link)
        self.broadcast("action", {"incident_id": incident_id, "action": action, "status": status})

    # -- operations driven from the dashboard -------------------------------

    async def approve_action(self, action_id: str, actor: str = "user") -> dict[str, Any]:
        action_row = await asyncio.to_thread(self.store.get_action, action_id)
        if not action_row:
            return {"ok": False, "error": "unknown action"}
        if action_row["status"] != "pending":
            return {"ok": False, "error": f"action is '{action_row['status']}', not pending"}

        incident = await asyncio.to_thread(self.store.get_incident, action_row["incident_id"])
        if not incident:
            return {"ok": False, "error": "unknown incident"}

        protected = self.policy.protected(incident["container_name"])
        if protected:
            await asyncio.to_thread(self.store.take_pending_action, action_id, "rejected", protected)
            return {"ok": False, "error": protected}

        # A person's approval overrides the circuit breaker and the cooldown:
        # those stop the doctor repeating itself, and "repeated failure needs
        # a human" is exactly this. The approver is recorded as the decider.
        taken = await asyncio.to_thread(
            self.store.take_pending_action, action_id, "executing", f"approved by {actor}",
            decided_by=f"user:{actor}")
        if not taken:
            return {"ok": False, "error": "action is no longer pending"}
        log.info("%s approved %s on %s", actor, action_row["action"], incident["container_name"])
        self._spawn_action(action_id, incident, incident.get("diagnosis"))
        return {"ok": True, "action": action_row["action"], "container": incident["container_name"]}

    async def reject_action(self, action_id: str, actor: str = "user") -> dict[str, Any]:
        action_row = await asyncio.to_thread(self.store.get_action, action_id)
        if not action_row or not await asyncio.to_thread(
                self.store.take_pending_action, action_id, "rejected", f"rejected by {actor}",
                decided_by=f"user:{actor}"):
            return {"ok": False, "error": "no pending action with that id"}
        await asyncio.to_thread(
            self.store.set_incident_status, action_row["incident_id"], "open"
        )
        self.broadcast("action", {"incident_id": action_row["incident_id"], "status": "rejected"})
        return {"ok": True}

    async def dismiss_incident(self, incident_id: str, actor: str = "user") -> dict[str, Any]:
        incident = await asyncio.to_thread(self.store.get_incident, incident_id)
        if not incident:
            return {"ok": False, "error": "unknown incident"}
        for action in await asyncio.to_thread(self.store.list_actions, incident_id=incident_id):
            if action["status"] == "pending":
                await asyncio.to_thread(
                    self.store.take_pending_action, action["id"], "rejected", f"dismissed by {actor}"
                )
        await asyncio.to_thread(self.store.set_incident_status, incident_id, "dismissed", resolved=True)
        self.broadcast("incident", {"id": incident_id, "status": "dismissed"})
        return {"ok": True}

    async def manual_action(self, container_name: str, action: str, actor: str) -> dict[str, Any]:
        """Operator-initiated action from the dashboard. Still audited, still guarded."""
        from .remediator import MUTATING_ACTIONS

        if action not in MUTATING_ACTIONS:
            return {"ok": False, "error": f"'{action}' is not an executable action"}
        protected = self.policy.protected(container_name)
        if protected:
            return {"ok": False, "error": protected}
        container = self.containers.get(container_name)
        if container is None and action != "prune_images":
            return {"ok": False, "error": "unknown container"}

        incident_id = await asyncio.to_thread(
            self.store.open_incident,
            container_id=(container or {}).get("id", ""),
            container_name=container_name,
            image=(container or {}).get("image", ""),
            kind=self.store.MANUAL,
            fingerprint=f"manual:{container_name}:{action}",
            severity="info",
            summary=f"{actor} requested {action.replace('_', ' ')} from the dashboard",
            evidence={"manual": True, "actor": actor},
            mode=self.settings.mode,
        )
        action_id = await asyncio.to_thread(
            self.store.record_action,
            incident_id=incident_id,
            container_name=container_name,
            action=action,
            status="pending",
            decided_by=f"user:{actor}",
            reason="manual request from the dashboard",
        )
        incident = await asyncio.to_thread(self.store.get_incident, incident_id)
        # Like an approval, a manual request is a person's decision: the
        # circuit breaker does not stand in its way.
        await asyncio.to_thread(self.store.take_pending_action, action_id, "executing",
                                f"requested by {actor}")
        self._spawn_action(action_id, incident, None)
        return {"ok": True, "incident_id": incident_id}

    def _spawn_action(self, action_id: str, incident: dict[str, Any],
                      diagnosis: dict[str, Any] | None) -> None:
        task = asyncio.create_task(self._run_action(action_id, incident, diagnosis))
        running = self.__dict__.setdefault("_action_tasks", set())
        running.add(task)  # held, or the loop may drop it half done
        task.add_done_callback(running.discard)

    # -- redeploying a new build ---------------------------------------------

    def _stack_members(self, stack: str) -> list[dict[str, Any]]:
        return [c for c in self.containers.values() if _stack_of(c)["stack"] == stack]

    DEPLOY_STACKS_SECONDS = 3600.0

    async def _refresh_deploy_stacks(self) -> None:
        """Which stacks the deploy workflow knows, read from the repository."""
        if not (self.settings.github_token and self.settings.deploy_repo):
            return
        loop = asyncio.get_running_loop()
        if loop.time() - self._deploy_stacks_read < self.DEPLOY_STACKS_SECONDS:
            return
        self._deploy_stacks_read = loop.time()
        settings = self.settings
        async with httpx.AsyncClient(timeout=20) as client:
            token = await github_deploy.check_token(
                client, settings.github_token, settings.deploy_repo, settings.deploy_workflow)
            found, why = await github_deploy.stacks(
                client, settings.github_token, settings.deploy_repo, settings.deploy_ref)
        token["permissions"]["contents_read"] = (
            "granted" if not why else "missing" if "HTTP 403" in why else "unknown")
        self._github = {**token, "checked_at": utcnow(), "error": token["error"] or why,
                        "workflow": token["workflow"] or self._github.get("workflow")}
        if why:
            log.warning("could not list the stacks in %s: %s", settings.deploy_repo, why)
            return  # keep the last answer, or none
        self._deploy_stacks = found

    # What each permission is for, in the order the token page lists them.
    TOKEN_PERMISSIONS = (
        ("metadata", "Metadata read", "always granted with repository access"),
        ("contents_read", "Contents read", "lists the stacks"),
        ("actions_read", "Actions read", "reads the workflow and follows its runs"),
        ("actions_write", "Actions write", "starts the workflow"),
    )

    def _token_rows(self, state: dict[str, Any]) -> list[tuple[str, str, str]]:
        kind = state.get("kind") or "other"
        rows = [("Token type", kind,
                 "a fine-grained token limited to this repository is the narrow choice"
                 if kind != "fine-grained" else "")]
        expires = _parse_iso(state.get("expires_at"))
        if expires is None:
            rows.append(("Expires", "never",
                         "a token without an expiry works until revoked - GitHub "
                         "recommends one; the doctor warns 14 days before"))
        else:
            days = (expires - datetime.now(timezone.utc)).total_seconds() / 86400
            rows.append(("Expires", f"{expires:%Y-%m-%d} ({_days(days)})",
                         "the dashboard warns 14 days before"))
        granted = state.get("permissions") or {}
        words = {"granted": "yes", "missing": "MISSING", "unknown": "not checked"}
        for key, label, use in self.TOKEN_PERMISSIONS:
            rows.append((label, words.get(granted.get(key, "unknown"), "not checked"), use))
        if state.get("scopes") is not None:
            rows.append(("Classic scopes", ", ".join(state["scopes"]) or "none",
                         "a classic token reaches every repository you can - "
                         "a fine-grained one is narrower"))
        return rows

    def _redeployable(self) -> list[str] | None:
        """The repository's stacks, less the doctor's own; None if not read."""
        if self._deploy_stacks is None:
            return None
        own = {_stack_of(c)["stack"] for c in self.containers.values()
               if c["name"] == self.settings.self_container_name}
        return [stack for stack in self._deploy_stacks if stack not in own]

    TOKEN_EXPIRY_WARN_DAYS = 14

    def github_warning(self) -> str:
        """A sentence for the dashboard's notices when the token is about to stop."""
        expires = _parse_iso(self._github.get("expires_at"))
        if not self.settings.github_token or expires is None:
            return ""
        days = (expires - datetime.now(timezone.utc)).total_seconds() / 86400
        if days < 0:
            return ("DOCTOR_GITHUB_TOKEN has expired, so Redeploy no longer works. "
                    "Regenerate it on GitHub and update it in the stack environment.")
        if days < self.TOKEN_EXPIRY_WARN_DAYS:
            return (f"DOCTOR_GITHUB_TOKEN expires on {expires:%Y-%m-%d} "
                    f"({_days(days)}). Regenerate it before then, or Redeploy stops working.")
        return ""

    def github_status(self) -> list[tuple[str, str, str]]:
        """What the doctor last heard from GitHub, as Settings rows."""
        if not self.settings.github_token:
            return []
        state = self._github
        flow = state.get("workflow") or {}
        rows: list[tuple[str, str, str]] = [
            ("Connection",
             "not checked yet" if not state.get("checked_at")
             else f"error: {state['error']}" if state.get("error") else "ok",
             "read with the image check, at most once an hour"),
            ("Last checked", _when(state.get("checked_at")), ""),
        ]
        if state.get("checked_at"):
            rows.extend(self._token_rows(state))
        if flow:
            rows.append(("Workflow on GitHub", f"{flow['name']} ({flow['state']})",
                         flow.get("html_url") or ""))
        stacks = self._redeployable()
        rows.append(("Stacks it can redeploy",
                     ", ".join(stacks) if stacks else
                     "none found" if stacks is not None else "not read yet - all may try",
                     "top-level folders with a compose file, less the doctor's own; "
                     "the Redeploy button shows only for these"))
        busy = ", ".join(sorted(self._redeploying))
        if busy:
            rows.append(("Redeploying now", busy, ""))
        last = next((a for a in self.store.list_actions(limit=200)
                     if a["action"] == "redeploy_stack"), None)
        rows.append(("Last redeploy",
                     f"{last['container_name']}: {last['status']}, {_when(last['created_at'])}"
                     if last else "none yet",
                     (last.get("result") or "")[:300] if last else ""))
        return rows

    def redeploy_refusal(self, stack: str) -> str | None:
        """Why this stack cannot be redeployed from here, or None."""
        if not stack:
            return "not started by Compose, so there is no stack to redeploy"
        if not (self.settings.github_token and self.settings.deploy_repo):
            return ("set DOCTOR_GITHUB_TOKEN and DOCTOR_DEPLOY_REPO to redeploy through "
                    "the deploy workflow")
        if self._deploy_stacks is not None and stack not in self._deploy_stacks:
            return (f"{self.settings.deploy_repo} has no stack folder named {stack} - "
                    "the deploy workflow redeploys stacks by folder name")
        for member in self._stack_members(stack):
            # The doctor's own stack would recreate the doctor mid-run, and
            # with it the record of how the redeploy went.
            if member["name"] == self.settings.self_container_name:
                return "the doctor does not redeploy its own stack - its image workflow does"
        if stack in self._redeploying:
            return f"{stack} is already being redeployed"
        return None

    async def redeploy(self, container_name: str, actor: str) -> dict[str, Any]:
        """Redeploy the stack a container belongs to, through the deploy workflow.

        Asked for from the dashboard, never decided by the doctor. Audited like
        a manual action, and verified afterwards: a green run says the stack's
        webhook accepted the deploy, not that the new build is what is running now.
        """
        container = self.containers.get(container_name)
        if container is None:
            return {"ok": False, "error": "unknown container"}
        stack = _stack_of(container)["stack"]
        try:
            await self._refresh_deploy_stacks()
        except Exception:
            log.exception("listing the deploy workflow's stacks failed")
        refusal = self.redeploy_refusal(stack)
        if refusal:
            return {"ok": False, "error": refusal}
        members = sorted(c["name"] for c in self._stack_members(stack))
        self._redeploying[stack] = {"by": actor, "at": utcnow(), "run_url": ""}
        try:
            incident_id = await asyncio.to_thread(
                self.store.open_incident,
                container_id=container.get("id", ""),
                container_name=container_name,
                image=container.get("image", ""),
                kind=self.store.MANUAL,
                fingerprint=f"manual:{stack}:redeploy_stack",
                severity="info",
                summary=f"{actor} requested a redeploy of stack {stack} from the dashboard",
                evidence={"manual": True, "actor": actor, "stack": stack,
                          "containers": members},
                mode=self.settings.mode,
            )
            action_id = await asyncio.to_thread(
                self.store.record_action,
                incident_id=incident_id,
                container_name=container_name,
                action="redeploy_stack",
                status="executing",
                decided_by=f"user:{actor}",
                reason=f"redeploy of stack {stack} ({', '.join(members)}) "
                       "requested from the dashboard",
            )
        except Exception:
            self._redeploying.pop(stack, None)
            raise
        log.info("%s asked to redeploy stack %s", actor, stack)
        self.broadcast("state", self.snapshot())
        asyncio.create_task(self._run_redeploy(stack, incident_id, action_id),
                            name=f"redeploy-{stack}")
        return {"ok": True, "stack": stack, "containers": members, "incident_id": incident_id}

    async def _run_redeploy(self, stack: str, incident_id: str, action_id: str) -> None:
        before = {c["name"]: c.get("image_id", "") for c in self._stack_members(stack)}
        flagged = sorted({c.get("image", "") for c in self._stack_members(stack)} & {
            e["image"] for e in self.image_updates.get("images", []) if e.get("state") == "update"})
        settings = self.settings
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                run, why = await github_deploy.dispatch(
                    client, settings.github_token, settings.deploy_repo,
                    settings.deploy_workflow, settings.deploy_ref, stack,
                    marker=f"container-doctor {action_id[:8]}")
                conclusion, url = "", ""
                if run:
                    url = run.get("html_url") or ""
                    self._redeploying[stack]["run_url"] = url
                    self.broadcast("state", self.snapshot())
                    conclusion, why = await github_deploy.wait(
                        client, settings.github_token, settings.deploy_repo, run["id"])
            link = f" ({url})" if run and url else ""
            if conclusion == "success":
                ok, said = await self._verify_redeploy(
                    stack, before, flagged, f"the deploy workflow redeployed {stack}{link}")
            else:
                ok = False
                # A name that is not a stack folder fails the run's plan job.
                said = (why or f"the deploy run for {stack} ended {conclusion} - its log "
                        "says why; the workflow redeploys stacks by folder name") + (
                    link if run else "")
            status = "succeeded" if ok else "failed"
            await asyncio.to_thread(self.store.finish_action, action_id, status, said)
            await asyncio.to_thread(
                self.store.set_incident_status, incident_id,
                "resolved" if ok else "failed", resolved=ok)
            log.info("redeploy of %s %s", stack, status)
            await self.notifier.notify_action(
                stack, "redeploy_stack", ok, said, dashboard_url=self.activity_url())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.exception("redeploy of %s failed", stack)
            await asyncio.to_thread(self.store.finish_action, action_id, "failed",
                                    f"redeploy failed ({error.__class__.__name__})")
            await asyncio.to_thread(self.store.set_incident_status, incident_id, "failed")
        finally:
            self._redeploying.pop(stack, None)
            self.broadcast("action", {"incident_id": incident_id, "action": "redeploy_stack"})
            self.broadcast("state", self.snapshot())

    async def _verify_redeploy(
        self, stack: str, before: dict[str, str], flagged: list[str], said: str
    ) -> tuple[bool, str]:
        """Read Docker and the registries again: is the new build running now?"""
        await self._fresh_poll()
        members = self._stack_members(stack)
        replaced = sorted(c["name"] for c in members
                          if before.get(c["name"]) and c.get("image_id") != before[c["name"]])
        if not flagged:
            moved = f"; {', '.join(replaced)} now on a new image" if replaced else ""
            return True, f"{said}{moved}"
        await self._recheck_images(flagged)
        entries = {e["image"]: e for e in self.image_updates.get("images", [])}
        behind = [image for image in flagged if entries.get(image, {}).get("state") == "update"]
        if not behind:
            return True, f"{said}; now running the new build of {', '.join(flagged)}"
        return False, (
            f"{said}, but {', '.join(behind)} still runs the old build. "
            f"{_stale_build_hint(self.settings.docker_manager)} If the run's summary "
            "says the stack's webhook secret is not set, nothing was deployed at all")

    async def _fresh_poll(self, timeout: float = 120.0) -> bool:
        """Wait for a poll that started after now, so the containers are current."""
        loop = asyncio.get_running_loop()
        needed = 2 if self._polling else 1  # the one running started too early
        seen = self.last_poll_at
        self._poll_now.set()
        deadline = loop.time() + timeout
        while needed and loop.time() < deadline:
            await asyncio.sleep(1)
            if self.last_poll_at != seen:
                seen = self.last_poll_at
                needed -= 1
        return needed == 0

    async def _recheck_images(self, images: list[str]) -> None:
        """Check a few images again and fold the answers into the last report."""
        # A full check running now may have read these before the redeploy;
        # let it land first, then ask again on top of it.
        for _ in range(300):
            if not self._image_checking:
                break
            await asyncio.sleep(1)
        async with httpx.AsyncClient(timeout=20) as client:
            fresh = {}
            for image in images:
                entry, _ = await self._check_image(client, image)
                fresh[image] = entry
        report = self.image_updates
        results = [fresh.pop(e["image"], e) for e in report.get("images", [])]
        results.extend(fresh.values())
        self.image_updates = self._image_report(
            results, report.get("checked_at"), bool(report.get("needs_images_permission")))

    # -- release notes, on request ------------------------------------------

    NOTES_CACHE_SECONDS = 3600.0

    async def image_release_notes(self, image: str, tag: str) -> dict[str, Any]:
        """The notes of every release between an image's pin and `tag`.

        Only for a tag the last check named, newer or cooling down - not a
        way to make the doctor fetch arbitrary repositories.
        """
        entry = next((e for e in self.image_updates.get("images", [])
                      if e.get("image") == image), None)
        if entry is None:
            return {"ok": False, "error": "not an image the last check looked at"}
        offered = {(entry.get("newer") or {}).get("tag"), (entry.get("cooling") or {}).get("tag")}
        if not tag or tag not in offered:
            return {"ok": False, "error": "not a version the last check found"}
        repository = entry.get("source") or ""
        if not self.settings.release_notes:
            return {"ok": False, "error": "release notes are off (DOCTOR_RELEASE_NOTES)"}
        if not repository:
            return {"ok": False, "error": "the image does not name its source repository"}
        reference = parse_reference(image)
        if reference is None:
            return {"ok": False, "error": "unreadable image reference"}
        key = (repository, reference.tag, tag)
        loop = asyncio.get_running_loop()
        cached = self._notes_cache.get(key)
        if cached and loop.time() - cached[0] < self.NOTES_CACHE_SECONDS:
            return {"ok": True, **cached[1]}
        async with httpx.AsyncClient(timeout=20) as client:
            notes = await release_list(client, repository, reference.tag, tag)
        notes = {**notes, "repository": repository, "current": reference.tag, "target": tag}
        if not notes.get("detail") or notes.get("releases"):
            self._notes_cache[key] = (loop.time(), notes)
        return {"ok": True, **notes}

    async def rediagnose(
        self, incident_id: str, context: str = "", actor: str = ""
    ) -> dict[str, Any]:
        """Diagnose again, optionally with what the operator knows.

        The context goes through the same redaction as the logs - people paste
        log lines, and log lines carry tokens - and is kept on the diagnosis it
        produced, so the dashboard can show what the answer was based on.
        """
        incident = await asyncio.to_thread(self.store.get_incident, incident_id)
        if not incident:
            return {"ok": False, "error": "unknown incident"}
        if not self.diagnoser.enabled:
            return {"ok": False, "error": "no LLM configured"}
        note = redact((context or "").strip())[:OPERATOR_CONTEXT_CHARS]
        spent = self._budget_spent(await asyncio.to_thread(self.store.usage_today))
        if spent:
            return {"ok": False, "error": spent}
        if not await asyncio.to_thread(self.store.reserve_call, self.settings.daily_call_limit):
            return {"ok": False, "error": "daily LLM call limit reached"}
        evidence = dict(incident.get("evidence") or {})
        if note:
            evidence["operator_context"] = note
        try:
            diagnosis, call_usage = await self.diagnoser.diagnose(evidence, ref=incident_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        await asyncio.to_thread(
            self.store.add_usage,
            input_tokens=call_usage["input_tokens"],
            output_tokens=call_usage["output_tokens"],
            cached_tokens=call_usage["cached_tokens"],
            cost_usd=call_usage["cost_usd"],
        )
        if note:
            diagnosis["operator_context"] = {"text": note, "by": actor or "operator", "at": utcnow()}
        await asyncio.to_thread(
            self.store.set_diagnosis, incident_id, diagnosis, diagnosis.get("severity")
        )
        # Not cached when it rests on a note: "I am restoring a backup right
        # now" explains today's failure, and would be wrong about next week's
        # identical one if the cache handed it on.
        if not note:
            await asyncio.to_thread(self.store.cache_diagnosis, incident["fingerprint"], diagnosis)
        self.broadcast("diagnosis", {"incident_id": incident_id, "summary": diagnosis["summary"]})
        return {"ok": True, "diagnosis": diagnosis}

    # -- log views, scoped to an incident -----------------------------------
    #
    # Deliberately not a general log browser: that is Dozzle's job, and a live
    # unredacted feed of every container would undo the redaction this whole
    # design rests on. These answer the two questions a frozen tail cannot -
    # "what is it doing now?" and "what else broke at the same moment?" -
    # and everything they return goes through redaction first.

    async def container_logs(
        self,
        container_name: str,
        lines: int = 200,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        container = self.containers.get(container_name)
        if container is None:
            containers = await self.docker.list_containers()
            container = next((c for c in containers if c["name"] == container_name), None)
        if container is None:
            return {"ok": False, "error": "unknown container"}

        policy = ContainerPolicy.from_labels(container.get("labels") or {})
        if not policy.enabled or container_name in self.settings.ignore_containers:
            # doctor.enable=false means "do not look at this container" - which
            # has to include looking on request, or the label means nothing.
            return {"ok": False, "error": "container is excluded via doctor.enable=false"}

        raw = await self.docker.logs(container["id"], lines, since=since, until=until)
        return {
            "ok": True,
            "container": container_name,
            "lines": lines,
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "logs": redact(raw)[-40000:],
        }

    async def incident_context(self, incident_id: str, minutes: int = 5) -> dict[str, Any]:
        """The window around a detection: the container's own logs, plus what
        else was failing at the same moment."""
        incident = await asyncio.to_thread(self.store.get_incident, incident_id)
        if not incident:
            return {"ok": False, "error": "unknown incident"}

        detected = _parse_iso(incident["opened_at"])
        if detected is None:
            return {"ok": False, "error": "incident has no usable timestamp"}
        since = detected - timedelta(minutes=minutes)
        until = detected + timedelta(minutes=minutes)

        own: dict[str, Any] = {}
        if incident["container_id"]:
            own = await self.container_logs(
                incident["container_name"], lines=400, since=since, until=until
            )

        # What else was logging errors in the same window. Explicit action
        # only - this is one log read per watched container.
        pattern = self.settings.log_error_regex
        neighbours: list[dict[str, Any]] = []
        for container in self.containers.values():
            if container["name"] == incident["container_name"] or not container.get("watched"):
                continue
            if container.get("state") != "running":
                continue
            raw = await self.docker.logs(container["id"], 200, since=since, until=until)
            hits = error_lines(raw, pattern, limit=4)
            if hits:
                neighbours.append({
                    "container": container["name"],
                    "matches": len(hits),
                    "sample": hits,  # error_lines redacts at source
                })
        neighbours.sort(key=lambda n: -n["matches"])

        nearby = [
            {
                "id": item["id"],
                "container_name": item["container_name"],
                "kind": item["kind"],
                "summary": item["summary"],
                "opened_at": item["opened_at"],
            }
            for item in await asyncio.to_thread(self.store.list_incidents, limit=200)
            if item["id"] != incident_id
            and (parsed := _parse_iso(item["opened_at"])) is not None
            and since <= parsed <= until
        ]

        return {
            "ok": True,
            "window_minutes": minutes,
            "since": since.isoformat(timespec="seconds"),
            "until": until.isoformat(timespec="seconds"),
            "logs": own.get("logs", ""),
            "neighbours": neighbours,
            "nearby_incidents": nearby,
        }

    # -- read models for the API -------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        containers = []
        for container in self.containers.values():
            containers.append(
                {
                    "name": container["name"],
                    "image": container.get("image", ""),
                    "state": container.get("state"),
                    "health": container.get("health"),
                    "restart_count": container.get("restart_count", 0),
                    "restart_policy": container.get("restart_policy", ""),
                    "exit_code": container.get("exit_code"),
                    "watched": container.get("watched", False),
                    "protected_reason": container.get("protected_reason"),
                    "mode": (container.get("policy") or {}).get("mode"),
                    "stats": container.get("stats") or {},
                    **_stack_of(container),
                }
            )
        containers.sort(key=lambda c: (_state_rank(c), c["name"]))
        return {
            "containers": containers,
            "disk": self.disk,
            "image_updates": {**self.image_updates, "checking": self._image_checking,
                              "progress": self._image_progress,
                              "partial": list(self._image_partial),
                              # Which stacks a new build can be redeployed in.
                              # Whether a new build can be redeployed from here,
                              # and in which stacks (null: not known, so all).
                              "redeploy": bool(self.settings.github_token
                                               and self.settings.deploy_repo),
                              "redeploy_stacks": self._redeployable(),
                              "redeploying": dict(self._redeploying)},
            "raid": self.raid,
            "host": self.host,
            "connectivity": self.net.view() if self.net is not None else None,
            "smart": self.smart,
            "last_poll_at": self.last_poll_at,
            "last_poll_seconds": self.last_poll_seconds,
            "poll_interval_seconds": self.settings.poll_interval_seconds,
            "polling": self._polling,
            "poll_progress": self._poll_progress,
            "docker_connected": self.docker_connected,
        }

    def network_report(self) -> dict[str, Any]:
        report = networks.report(list(self.containers.values()), self.network_inventory)
        report["last_poll_at"] = self.last_poll_at
        return report

    def container_detail(self, name: str) -> dict[str, Any] | None:
        """Everything the dashboard knows about one container, in one answer."""
        container = self.containers.get(name)
        if container is None:
            return None
        summary = next((c for c in self.snapshot()["containers"] if c["name"] == name), {})
        labels = container.get("labels") or {}
        image = container.get("image", "")
        return {
            "container": summary,
            # doctor.* is the configuration; the compose labels say where it
            # came from. Everything else - Traefik rules, Homepage widget keys -
            # is left out: it is not the doctor's to show, and some carry keys.
            "labels": {k: v for k, v in sorted(labels.items())
                       if k.startswith("doctor.")
                       or (k.startswith("com.docker.compose.")
                           and k.rsplit(".", 1)[-1] in ("project", "service", "depends_on"))},
            "stack": relatives(container, list(self.containers.values())),
            "siblings": sorted(
                ({"name": other["name"], "service": _stack_of(other)["service"],
                  "state": other.get("state"), "health": other.get("health")}
                 for other in self.containers.values()
                 if other["name"] != name and _stack_of(container)["stack"]
                 and _stack_of(other)["stack"] == _stack_of(container)["stack"]),
                key=lambda other: other["name"],
            ),
            "image": next((entry for entry in self.image_updates.get("images", [])
                           if entry.get("image") == image), None),
            "incidents": self.store.list_incidents(limit=10, container_name=name),
            "actions": self.store.list_actions(limit=10, container_name=name),
            "history": [
                {"at": at.isoformat(timespec="seconds"), "cpu": cpu, "mem_pct": mem,
                 "mem_bytes": mem_bytes}
                # list() copies in one step under the GIL; iterating the deque
                # itself from this worker thread races the poll appending to it.
                for at, cpu, mem, mem_bytes in list(self.history.get(name, ()))
            ],
            "history_step_seconds": HISTORY_STEP_SECONDS,
            "changes": self.store.list_changes(container_name=name, limit=15),
            "memory": next(iter(self.memory_report(name)), None),
        }

    # -- the dashboard briefing ----------------------------------------------

    def _briefing_facts(self) -> dict[str, briefing.Facts]:
        open_incidents = self.store.list_incidents(limit=50, status=self.store.ACTIVE)
        pending = {a["incident_id"] for a in self.store.pending_actions()}
        return {
            "incidents": briefing.incident_facts(open_incidents, pending),
            "containers": briefing.container_facts(
                self.snapshot()["containers"],
                memory=self.memory_report(),
                changes=self.store.list_changes(
                    since=datetime.now(timezone.utc) - timedelta(days=1), limit=20),
            ),
            "storage": briefing.storage_facts(
                self.disk, self.raid.get("arrays") or [],
                {"disk": self.settings.disk_threshold_pct,
                 "disk_critical": self.settings.disk_critical_pct},
                self.settings.disk_forecast_days, self.settings.disk_forecast_critical_days,
                self.raid.get("pools") or [], self.raid.get("drives") or []),
            "updates": briefing.update_facts(self.image_updates),
        }

    async def _briefing_call(self, system, prompt, schema, name):
        # Low effort: three lines of summary do not need the reasoning a
        # diagnosis gets.
        return await self.diagnoser.structured(
            system, prompt, schema, name,
            model=self.settings.briefing_model or self.settings.model,
            effort="low", max_tokens=2000,
        )

    def _budget_spent(self, usage: dict[str, Any]) -> str:
        """Why no more diagnosis calls today, or ''. Every diagnosis - an
        incident's own, or one asked for from the dashboard - asks here.

        A threshold on what has been recorded, not a reservation: calls
        already in flight finish and are counted after, so the day can end
        a call or two over. A provider-side credit limit is the absolute one.
        """
        cap = self.settings.daily_usd_budget
        if cap > 0 and usage["cost_usd"] >= cap:
            return (f"daily LLM budget of ${cap:.2f} is spent "
                    f"(${usage['cost_usd']:.2f} today)")
        return ""

    def _briefing_budget(self) -> str:
        """'' when a briefing call may be spent, else why not.

        The briefing stops at 80% of either cap, so the last of the day's
        budget is always there for diagnosing an incident.
        """
        usage = self.store.usage_today()
        cap = self.settings.daily_usd_budget
        if cap > 0 and usage["cost_usd"] >= cap * 0.8:
            return "Summary paused: most of today's LLM budget is spent, and the rest is kept for diagnoses."
        limit = self.settings.daily_call_limit
        if limit > 0 and usage["calls"] >= int(limit * 0.8):
            return "Summary paused: most of today's LLM calls are used, and the rest are kept for diagnoses."
        if not self.store.reserve_call(limit):
            return "Summary paused: today's LLM call limit is reached."
        return ""

    def _record_usage(self, usage: dict[str, Any]) -> None:
        self.store.add_usage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cached_tokens=usage.get("cached_tokens", 0),
            cost_usd=usage.get("cost_usd", 0.0),
        )

    def incident_handoff(self, incident_id: str) -> str | None:
        """The incident as a Markdown brief for a local LLM agent, or None."""
        incident = self.store.get_incident(incident_id)
        if incident is None:
            return None
        name = incident["container_name"]
        current = next((c for c in self.snapshot()["containers"] if c["name"] == name), None)
        image = next((entry for entry in self.image_updates.get("images", [])
                      if entry.get("image") == incident.get("image")), None)
        return handoff.render(
            incident,
            self.store.list_actions(incident_id=incident_id),
            current=current,
            image=image,
            link=self.incident_url(incident_id),
            model=self.settings.model,
            provider=self.settings.llm_provider,
            manager=self.settings.docker_manager,
            method=self.settings.deploy_method,
            host=self.describe_host(),
        )

    def classifier_status(self, limit: int = 30) -> dict[str, Any]:
        """What the classifier has been saying, for the dashboard."""
        s = self.settings
        since = datetime.now(timezone.utc) - timedelta(days=7)
        return {
            "mode": s.classifier_mode,
            "enabled": self.classifier.enabled,
            "provider": s.classifier_provider,
            "model": s.classifier_model if s.classifier_provider == "openrouter" else s.model,
            "calibrated": bool(self.classifier.model and self.classifier.model.calibrated),
            "suppress_at": s.classifier_suppress_at,
            "daily_calls": s.classifier_daily_calls,
            "daily_usd": s.classifier_daily_usd,
            "today": self.store.classifier_usage_today(),
            "last_error": self.classifier.last_error,
            "week": self.store.classification_counts(since),
            "recent": self.store.list_classifications(limit=limit),
        }

    def llm_spend(self, days: int) -> dict[str, Any]:
        """Daily spend for the chart, each day split by what it paid for.

        The totals come from llm_usage, which is kept as long as everything
        else; the split from the call log, which is kept for less. A day
        older than the log keeps its total as "earlier" - unitemized, not lost.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days - 1)
        totals = {row["day"]: row for row in self.store.usage_since(since)}
        split: dict[str, dict[str, dict[str, Any]]] = {}
        for row in self.store.llm_spend_by_purpose(since):
            bucket = split.setdefault(row["day"], {}).setdefault(
                _purpose_group(row["purpose"]), {"calls": 0, "cost_usd": 0.0})
            bucket["calls"] += int(row["calls"] or 0)
            bucket["cost_usd"] += float(row["cost_usd"] or 0.0)
        out = []
        for offset in range(days):
            day = (since + timedelta(days=offset)).date().isoformat()
            total = totals.get(day) or {}
            parts = split.get(day, {})
            itemized = sum(p["cost_usd"] for p in parts.values())
            cost = float(total.get("cost_usd") or 0.0)
            if cost - itemized > 1e-6:
                parts["earlier"] = {"calls": 0, "cost_usd": cost - itemized}
            out.append({
                "day": day,
                "cost_usd": round(max(cost, itemized), 6),
                "calls": int(total.get("calls") or 0),
                "input_tokens": int(total.get("input_tokens") or 0),
                "output_tokens": int(total.get("output_tokens") or 0),
                "by_purpose": {k: {"calls": v["calls"], "cost_usd": round(v["cost_usd"], 6)}
                               for k, v in parts.items()},
            })
        return {
            "days": out,
            "budget_usd": self.settings.daily_usd_budget,
            "call_limit": self.settings.daily_call_limit,
            "log_days": self.settings.llm_log_days,
        }

    def disk_history(self, days: int) -> list[dict[str, Any]]:
        """Each watched path's samples, for the forecast chart."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        out = []
        for usage in self.disk:
            samples = self.store.disk_history(usage["path"], since)
            # Capacity as the percentage counts it, used + free, at the time of
            # each sample: a volume that grew would otherwise read as fuller
            # back then than it was. Rows from before free space was kept
            # scale their filesystem total by today's share of it that is
            # usable, the root reserve being a fixed fraction.
            usable = ((usage["used_bytes"] + usage["free_bytes"]) / usage["total_bytes"]
                      if usage["total_bytes"] else 1.0)
            out.append({
                "path": usage["path"],
                "total_bytes": usage["total_bytes"],
                "used_bytes": usage["used_bytes"],
                "free_bytes": usage["free_bytes"],
                "trend": usage.get("trend"),
                "samples": [
                    [row["at"], row["used_bytes"],
                     row["used_bytes"] + row["free_bytes"] if row["free_bytes"] is not None
                     else round(row["total_bytes"] * usable)]
                    for row in samples
                ],
            })
        return out

    def status(self) -> dict[str, Any]:
        usage = self.store.usage_today()
        day_ago = datetime.now(timezone.utc) - timedelta(days=1)
        return {
            "mode": self.settings.mode,
            "build": self.settings.build,
            "version": self.settings.version,
            "model": self.settings.model,
            "provider": self.settings.llm_provider,
            "llm_enabled": self.diagnoser.enabled,
            "notifications_enabled": self.notifier.enabled,
            "auto_actions": self.settings.auto_actions,
            "min_confidence": self.settings.min_confidence,
            "docker_connected": self.docker_connected,
            "docker_host": self.settings.docker_host,
            "docker_access": self.docker_access,
            # A name for labels on the page; empty when none is set.
            "docker_manager": self.settings.docker_manager,
            # What the host is, for its labels: synology or generic, the
            # model where the kernel names it, and the name it was given.
            "host_kind": self.platform.kind,
            "host_model": self.platform.model,
            "host_name": self.settings.host_name,
            "last_poll_at": self.last_poll_at,
            "poll_interval_seconds": self.settings.poll_interval_seconds,
            "warnings": self.startup_warnings + (
                [self.github_warning()] if self.github_warning() else []),
            "budget": {
                "daily_usd": self.settings.daily_usd_budget,
                "spent_usd": round(usage["cost_usd"], 4),
                "daily_call_limit": self.settings.daily_call_limit,
                "calls": usage["calls"],
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cached_tokens": usage["cached_tokens"],
            },
            "incidents_24h": self.store.count_incidents_since(day_ago),
            # The number worth acting on, next to the number that happened.
            "open_incidents": self.store.count_incidents(self.store.ACTIVE),
            # The dashboard's meters colour themselves from these rather than
            # from numbers of their own, so raising a threshold moves the bar
            # as well as the rule.
            "thresholds": {
                "cpu": self.settings.cpu_threshold_pct,
                "memory": self.settings.mem_threshold_pct,
                "disk": self.settings.disk_threshold_pct,
                "disk_critical": self.settings.disk_critical_pct,
                "host_mem_available": self.settings.host_mem_available_pct,
                "host_iowait": self.settings.host_iowait_pct,
                "host_load_per_core": self.settings.host_load_per_core,
                "host_temp": self.settings.host_temp_c,
                "drive_temp": self.settings.smart_temp_c,
                "drive_nvme_temp": self.settings.smart_nvme_temp_c,
                "host_swap_in": self.settings.host_swap_in_per_s,
            },
            "pending_approvals": len(self.store.pending_actions()),
        }



def _sighting_lines(signal: Signal) -> list[str]:
    """The log lines one sighting fired on, for the incident's history.

    Redacted again although the detector already did: this is stored and
    served, and a future rule that forgets must not leak through here.
    """
    details = signal.details or {}
    lines = details.get("matched_lines") or details.get("fatal_lines") or []
    return [redact(str(line))[:500] for line in lines[:20]]


def _parse_iso(value: str | None) -> datetime | None:
    """Stored timestamps are ISO-8601 UTC; be forgiving about anything else."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _exit_code(attributes: dict[str, Any]) -> int:
    try:
        return int(attributes.get("exitCode") or 0)
    except (TypeError, ValueError):
        return 0


def _purpose_group(purpose: str) -> str:
    """container_diagnosis -> diagnosis, briefing_section -> briefing, ..."""
    for group in ("diagnosis", "briefing", "classifier"):
        if group in purpose:
            return group
    return "other"


def _days(days: float) -> str:
    """"in 12 days", "today", "3 days ago"."""
    whole = int(days)  # towards zero: 0.5 days either way is "today"
    if whole == 0:
        return "today" if days >= 0 else "expired today"
    if whole > 0:
        return f"in {whole} day{'s' if whole != 1 else ''}"
    return f"{-whole} day{'s' if whole != -1 else ''} ago"


def _when(iso: str | None) -> str:
    """A stored timestamp the way a person reads it, in UTC."""
    moment = _parse_iso(iso)
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else (iso or "never")


def _stack_of(container: dict[str, Any]) -> dict[str, str]:
    info = compose_info(container.get("labels"))
    # "stack" is set by the Docker client for the whole Compose project when
    # any of its containers carries the stack label (see stacks.resolve_stacks).
    return {"stack": container.get("stack") or info["stack"], "service": info["service"]}


def _stale_build_hint(manager: str) -> str:
    """Why a redeploy can run and still leave the old build, for the manager
    DOCTOR_DOCKER_MANAGER names - generic when it is one without known quirks."""
    if "portainer" in manager.lower():
        return ("Portainer redeploys a git stack only when its commit changed, unless "
                "Force redeployment is on in the stack's GitOps settings, and fetches a "
                "moving tag again only with pull_policy: always on the service (or Re-pull "
                "image on Business Edition).")
    return ("A redeploy fetches a moving tag again only when the deploy pulls images: "
            "pull_policy: always on the service does it with any manager, or the "
            "manager's own re-pull option where it has one.")


def _state_rank(container: dict[str, Any]) -> int:
    if container.get("state") != "running":
        return 0
    if container.get("health") == "unhealthy":
        return 1
    return 2


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def drive_key(drive: dict[str, Any]) -> str:
    """What a drive's stored history is filed under: its serial, which
    follows it to another bay, or its device where SMART gave none."""
    return drive.get("serial") or drive["device"]


def smart_signal(drive: dict[str, Any]) -> Signal:
    """One incident per drive, however its numbers move; critical opens a fresh
    one, as any escalation does."""
    what = f"{drive['label']} ({drive['model']})" if drive.get("model") else drive["label"]
    return Signal(
        kind="smart_health",
        severity=drive["severity"],
        summary=f"{what}: {'; '.join(drive['problems'])}",
        container={"id": "", "name": f"host:{drive['label']}", "image": "", "labels": {}},
        details={k: drive.get(k) for k in (
            "device", "label", "model", "serial", "family", "passed", "temperature",
            "power_on_hours", "counters", "crc_errors", "nvme", "problems")},
        signature_parts=["smart", drive.get("serial") or drive["device"]],
    )
