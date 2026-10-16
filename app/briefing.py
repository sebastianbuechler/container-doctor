"""The briefing at the top of the dashboard: what is going on, and what to do.

Built in two layers, so it can be cheap and still be right:

1. **Facts** - deterministic, per section of the app (incidents, containers,
   storage, updates). Plain lines and a level, computed from what the doctor
   already holds. They are always there, with or without a model.

2. **Summaries** - one small model call per section, then one that reads the
   section summaries and picks the priorities. Each is cached against a
   fingerprint of the facts it was written from, so a reload shows the same
   words, and a section is only rewritten when its facts actually change.

The fingerprint is built only from facts that mean something - which
incidents are open and how bad, which containers are down, a disk's fill level
to the nearest 5% - and never from CPU percentages or "seen 47 times"
counters, which move every poll without anything having happened.

A changed fingerprint is not rewritten on the spot, either: a section is
rewritten at most every DOCTOR_BRIEFING_REFRESH_MINUTES, unless its level
changed (a new critical incident does not wait), or someone presses refresh.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

SECTIONS = ("incidents", "containers", "storage", "updates")
LEVELS = ("ok", "info", "attention", "critical")
RANK = {level: index for index, level in enumerate(LEVELS)}

# Room for the model to list what matters, not to narrate.
MAX_POINTS = 3
MAX_PRIORITIES = 3


@dataclass
class Facts:
    section: str
    level: str
    lines: list[str]
    # What a summary may link to: {"id": "incident:ab12", "label": ..., "href": ...}
    refs: list[dict[str, str]] = field(default_factory=list)
    # The stable part, hashed into the fingerprint.
    key: Any = None
    # A few words for when there is no summary: the section's own headline.
    headline: str = ""

    @property
    def fingerprint(self) -> str:
        raw = json.dumps([self.level, self.key], sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# -- facts --------------------------------------------------------------------

def _worst(*levels: str) -> str:
    return max(levels or ("ok",), key=lambda level: RANK.get(level, 0))


def incident_facts(open_incidents: list[dict[str, Any]], pending_ids: set[str]) -> Facts:
    """What is open, how bad, and what waits on a person."""
    rows = sorted(open_incidents, key=lambda i: (
        -RANK.get(_incident_level(i), 0), i.get("opened_at") or ""))
    waiting = [i for i in rows if i["id"] in pending_ids]
    level = _worst(*(_incident_level(i) for i in rows)) if rows else "ok"
    lines = [f"{len(rows)} open incident{'s' if len(rows) != 1 else ''}"
             + (f", {len(waiting)} awaiting approval" if waiting else "")]
    refs = []
    for incident in rows[:10]:
        summary = (incident.get("diagnosis") or {}).get("summary") or incident.get("summary", "")
        tags = [str(incident.get("kind", "")).replace("_", " "),
                incident.get("rule_severity") or incident.get("severity") or ""]
        if incident["id"] in pending_ids:
            tags.append("awaiting approval")
        if incident.get("status") == "failed":
            tags.append("remediation failed")
        lines.append(f"{incident['container_name']}: {summary} ({', '.join(t for t in tags if t)})")
        refs.append({"id": f"incident:{incident['id']}",
                     "label": f"{incident['container_name']} - {tags[0]}",
                     "href": f"#incident/{incident['id']}"})
    if len(rows) > 10:
        lines.append(f"and {len(rows) - 10} more")
    refs.append({"id": "view:incidents", "label": "Incidents", "href": "#/incidents"})
    key = sorted(
        (i["id"], i.get("status"), i.get("rule_severity") or i.get("severity"),
         (i.get("diagnosis") or {}).get("summary") or i.get("summary"), i["id"] in pending_ids)
        for i in rows
    )
    if not rows:
        headline = "No open incidents"
    else:
        headline = f"{len(rows)} open incident{'s' if len(rows) != 1 else ''}" + (
            f", {len(waiting)} awaiting approval" if waiting else "")
    return Facts("incidents", level, lines, refs, key, headline)


def _incident_level(incident: dict[str, Any]) -> str:
    if incident.get("status") == "failed":
        return "critical"
    severity = incident.get("rule_severity") or incident.get("severity")
    return "critical" if severity == "critical" else "attention"


def _restarts_bucket(count: int) -> str:
    return "0" if count <= 0 else "1-2" if count <= 2 else "3+"


def container_facts(containers: list[dict[str, Any]],
                    memory: list[dict[str, Any]] | None = None,
                    changes: list[dict[str, Any]] | None = None) -> Facts:
    watched = [c for c in containers if c.get("watched")]
    troubled = []
    for c in watched:
        health = c.get("health") if c.get("health") not in (None, "none") else ""
        if c.get("state") != "running":
            what = f"{c.get('state') or 'stopped'}" + (
                f" (exit {c['exit_code']})" if c.get("exit_code") not in (None, 0) else "")
        elif health in ("unhealthy", "starting"):
            what = health
        else:
            continue
        troubled.append((c, what))
    lines = [f"{len(watched)} watched container{'s' if len(watched) != 1 else ''}, "
             f"{len(watched) - len(troubled)} running normally"]
    refs = []
    for c, what in troubled[:10]:
        stack = f" in stack {c['stack']}" if c.get("stack") else ""
        restarts = int(c.get("restart_count") or 0)
        lines.append(f"{c['name']}{stack}: {what}"
                     + (f", restarted {restarts} times" if restarts >= 3 else ""))
        refs.append({"id": f"container:{c['name']}", "label": c["name"],
                     "href": f"#container/{c['name']}"})
    # Memory caps about to bite, and what changed in the last day: neither is
    # a failure, both are what the next failure is likely to be about.
    squeezed = [m for m in memory or [] if m.get("verdict") in ("raise", "tight")]
    for m in squeezed[:5]:
        lines.append(f"{m['name']}: memory peaks at {_pct(m.get('peak_bytes'), m.get('limit_bytes'))} "
                     f"of its limit; suggested {m.get('compose', '')}")
        refs.append({"id": f"container:{m['name']}", "label": m["name"],
                     "href": f"#container/{m['name']}"})
    recreated = [c for c in changes or [] if c.get("kind") == "recreated"]
    for change in recreated[:5]:
        lines.append(f"changed in the last day: {change['summary']}")
    refs.append({"id": "view:containers", "label": "Containers", "href": "#/containers"})
    stopped = any(c.get("state") != "running" and c.get("exit_code") not in (None, 0, 143)
                  for c, _ in troubled)
    raise_now = any(m["verdict"] == "raise" for m in squeezed)
    level = ("critical" if stopped else "attention" if troubled or raise_now
             else "info" if squeezed or recreated else "ok")
    key = [len(watched), sorted(
        (c["name"], c.get("state"), c.get("health"), c.get("exit_code"),
         _restarts_bucket(int(c.get("restart_count") or 0)))
        for c, _ in troubled),
        sorted((m["name"], m["verdict"]) for m in squeezed),
        sorted(c.get("id", 0) for c in recreated)]
    headline = (f"{len(troubled)} of {len(watched)} containers need a look" if troubled
                else f"All {len(watched)} running; {len(squeezed)} near "
                     f"{'its' if len(squeezed) == 1 else 'their'} memory limit" if squeezed
                else f"All {len(watched)} containers running normally")
    return Facts("containers", level, lines, refs, key, headline)


def _pct(part: Any, whole: Any) -> str:
    try:
        return f"{float(part) / float(whole) * 100:.0f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return "?"


def _days_bucket(days: float | None) -> str:
    if days is None:
        return "not filling"
    for limit, label in ((2, "<2d"), (7, "<1w"), (21, "<3w"), (90, "<3mo"), (365, "<1y")):
        if days < limit:
            return label
    return ">1y"


def storage_facts(disks: list[dict[str, Any]], arrays: list[dict[str, Any]],
                  thresholds: dict[str, float], forecast_days: int,
                  critical_days: int, pools: list[dict[str, Any]] | None = None,
                  drives: list[dict[str, Any]] | None = None) -> Facts:
    lines, levels, key = [], [], []
    # The short version of each problem, in step with its level, for the headline.
    shorts: list[str] = []
    levels_by_item: list[str] = []
    warn = float(thresholds.get("disk") or 90)
    crit = float(thresholds.get("disk_critical") or 95)
    for disk in disks:
        pct = float(disk.get("used_pct") or 0)
        trend = disk.get("trend") or {}
        days = trend.get("days_until_full")
        line = f"{disk['path']}: {pct:.0f}% used"
        if days is not None:
            line += f", filling at {trend.get('rate')}, full in {trend.get('until_full')}"
        else:
            line += ", not filling"
        lines.append(line)
        name = disk["path"].rstrip("/").rsplit("/", 1)[-1] or disk["path"]
        if pct >= crit or (days is not None and days <= critical_days):
            levels.append("critical")
        elif pct >= warn or (days is not None and days <= forecast_days):
            levels.append("attention")
        else:
            continue
        levels_by_item.append(levels[-1])
        shorts.append(f"{name} is {pct:.0f}% full" if pct >= warn
                      else f"{name} full in {trend.get('until_full')}")
        key.append((disk["path"], int(round(pct / 5) * 5), _days_bucket(days)))
    for array in arrays:
        for warning in array.get("warnings") or []:
            lines.append(warning["summary"])
            levels.append("attention")
            levels_by_item.append("attention")
            shorts.append(f"{array['name']} scrub found mismatches")
            key.append((array["name"], warning["key"]))
        if array.get("healthy"):
            if array.get("operation") in ("check", "repair"):
                lines.append(f"Disk array {array['name']}: scrub running")
            continue
        lines.append(array.get("summary") or f"Disk array {array['name']} is degraded")
        rebuilding = array.get("operation") in ("recovery", "reshape")
        levels.append("attention" if rebuilding else "critical")
        levels_by_item.append(levels[-1])
        shorts.append(f"{array['name']} is {'rebuilding' if rebuilding else 'degraded'}")
        progress = array.get("progress")
        key.append((array["name"], array.get("operation"),
                    None if progress is None else int(progress // 10) * 10))
    for drive in drives or []:
        if not drive.get("summary"):
            continue
        lines.append(drive["summary"])
        levels.append("critical" if drive.get("severity") == "critical" else "attention")
        levels_by_item.append(levels[-1])
        shorts.append(f"{drive['label']} has read errors")
        # Bucketed so each new error does not rewrite the briefing.
        key.append((drive["disk"], drive.get("severity"), len(str(drive.get("seen") or 0))))
    # How the arrays add up, so several raid5 arrays on one volume read as the
    # SHR layers they are rather than as volumes nobody created.
    for pool in pools or []:
        if len(pool.get("arrays") or []) < 2:
            continue
        names = ", ".join(v["name"] + (f" ({', '.join(v['paths'])})" if v.get("paths") else "")
                          for v in pool.get("volumes") or []) or "One volume"
        lines.append(f"{names} is built from {' + '.join(pool['arrays'])}, "
                     f"joined into one storage pool")
    if arrays and all(a.get("healthy") and not a.get("warnings") for a in arrays) \
            and not any(d.get("summary") for d in drives or []):
        lines.append(f"All {len(arrays)} disk arrays healthy")
    refs = [{"id": "view:storage", "label": "Storage", "href": "#/storage"}]
    headline = next((short for short, level in zip(shorts, levels_by_item)
                     if level == _worst(*levels)), "Storage healthy") if levels else "Storage healthy"
    return Facts("storage", _worst(*levels) if levels else "ok", lines, refs, key, headline)


def update_facts(report: dict[str, Any]) -> Facts:
    images = report.get("images") or []
    newer = [i for i in images if i.get("newer")]
    moved = [i for i in images if i.get("state") == "update"]
    lines, refs, key = [], [], []
    if not report.get("checked_at"):
        lines.append("Image registries not checked yet")
    for entry in newer[:10]:
        n = entry["newer"]
        users = ", ".join(entry.get("containers") or [])
        line = f"{entry['image']} ({users}): {n['tag']} available, a {n['level']} step"
        if n.get("breaking"):
            line += f"; release notes mention: {n['breaking'][:120]}"
        lines.append(line)
        key.append((entry["image"], n.get("tag"), n.get("level"), bool(n.get("breaking"))))
    for entry in moved[:10]:
        lines.append(f"{entry['image']}: a newer build of the same tag is published")
        key.append((entry["image"], "moved"))
    if report.get("checked_at") and not newer and not moved:
        lines.append(f"All {len(images)} images current")
    refs.append({"id": "view:updates", "label": "Updates", "href": "#/updates"})
    level = "info" if newer or moved else "ok"
    if not report.get("checked_at"):
        headline = "Not checked yet"
    elif newer or moved:
        count = len({i["image"] for i in newer + moved})
        headline = f"{count} image{'s' if count != 1 else ''} behind"
    else:
        headline = "All images current"
    return Facts("updates", level, lines, refs, key, headline)


# -- the model's part ---------------------------------------------------------

SECTION_SYSTEM = """You write one section of the status briefing at the top of a \
home lab monitoring dashboard (Container Doctor, watching the Docker stacks on \
one host). The operator glances at it to decide what to work on next.

You get the facts for one area of the dashboard. Write:
- `headline`: at most 9 words, specific and plain. When nothing needs attention, \
say so plainly - "All 31 containers running normally".
- `points`: 0 to 3 short items, at most 16 words each, most important first. Only \
what needs a decision or is worth knowing today; nothing that repeats the headline. \
Group related facts rather than listing each one.
- `level`: ok, info, attention or critical. Keep the level you are given unless the \
facts plainly contradict it.

Use the names from the facts. Never invent a number, a name or a cause. No relative \
times, no greetings, no markdown."""

SECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}},
        "level": {"type": "string", "enum": list(LEVELS)},
    },
    "required": ["headline", "points", "level"],
    "additionalProperties": False,
}

OVERALL_SYSTEM = """You write the top line of a home lab monitoring dashboard \
(Container Doctor): the one thing the operator should know, and what to work on, \
in order.

You get each section's level and summary, and a list of references you may link to.
- `headline`: at most 12 words - the state of things in one breath. When everything \
is fine, say that.
- `priorities`: 0 to 3 items, most urgent first. `text` is an instruction of at most \
12 words ("Approve the sonarr restart", "Replace the failed disk in md2"). `ref` is \
the id of the most relevant reference from the list, or "" when none fits.
- `level`: the worst level that genuinely applies.

Only what the sections say. No relative times, no greetings, no markdown."""

OVERALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "level": {"type": "string", "enum": list(LEVELS)},
        "priorities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "ref": {"type": "string"}},
                "required": ["text", "ref"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "level", "priorities"],
    "additionalProperties": False,
}


def section_prompt(facts: Facts) -> str:
    lines = "\n".join(f"- {line}" for line in facts.lines)
    return f"Section: {facts.section}\nLevel from the rules: {facts.level}\n\nFacts:\n{lines}"


def overall_prompt(sections: dict[str, dict[str, Any]], refs: list[dict[str, str]]) -> str:
    parts = []
    for name in SECTIONS:
        section = sections[name]
        summary = section.get("summary") or {}
        headline = summary.get("headline") or "; ".join(section["facts"][:2])
        points = summary.get("points") or section["facts"][1:4]
        parts.append(f"## {name} ({section['level']})\n{headline}"
                     + "".join(f"\n- {p}" for p in points))
    listed = "\n".join(f"- {r['id']}: {r['label']}" for r in refs)
    return "\n\n".join(parts) + f"\n\n## References\n{listed}"


def clean_section(raw: dict[str, Any], fallback_level: str) -> dict[str, Any]:
    level = raw.get("level") if raw.get("level") in LEVELS else fallback_level
    points = [str(p).strip()[:200] for p in (raw.get("points") or []) if str(p).strip()]
    return {"headline": str(raw.get("headline") or "").strip()[:140],
            "points": points[:MAX_POINTS], "level": level}


def clean_overall(raw: dict[str, Any], refs: dict[str, dict[str, str]],
                  fallback_level: str) -> dict[str, Any]:
    level = raw.get("level") if raw.get("level") in LEVELS else fallback_level
    priorities = []
    for item in raw.get("priorities") or []:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        ref = refs.get(str(item.get("ref") or ""))
        priorities.append({
            "text": str(item["text"]).strip()[:160],
            # A reference the model made up links nowhere, rather than to a 404.
            "href": ref["href"] if ref else "",
            "label": ref["label"] if ref else "",
        })
    return {"headline": str(raw.get("headline") or "").strip()[:160],
            "level": level, "priorities": priorities[:MAX_PRIORITIES]}


def fallback_headline(sections: dict[str, dict[str, Any]]) -> str:
    """The top line without a model: the headlines of what needs a look."""
    worst = sorted((s for s in SECTIONS if sections[s]["level"] in ("attention", "critical")),
                   key=lambda s: -RANK[sections[s]["level"]])
    if not worst:
        return "All clear - nothing needs you right now."
    return " \u00b7 ".join(sections[s]["headline"] for s in worst)


# -- the service ----------------------------------------------------------------

LlmCall = Callable[[str, str, dict[str, Any], str], Awaitable[tuple[dict[str, Any], dict[str, Any]]]]


class Briefing:
    """Holds the cache, decides what is stale, and rewrites it in the background.

    `collect` returns the current facts per section; `call` is the model
    (system, prompt, schema, name) -> (object, usage); `budget_ok` says whether
    a call may be spent; `record` books its usage; `notify` tells the
    dashboard something changed.
    """

    def __init__(self, *, store, enabled: bool, refresh_minutes: int, model: str,
                 collect: Callable[[], dict[str, Facts]], call: LlmCall | None,
                 budget_ok: Callable[[], str], record: Callable[[dict[str, Any]], None],
                 notify: Callable[[], None]) -> None:
        self.store = store
        self.enabled = enabled and call is not None
        self.refresh = timedelta(minutes=max(0, refresh_minutes))
        self.model = model
        self.collect = collect
        self.call = call
        self.budget_ok = budget_ok
        self.record = record
        self.notify = notify
        self.task: asyncio.Task | None = None
        self.last_error = ""
        self.pending: list[str] = []

    # What the dashboard reads. Never waits on a model.
    def view(self) -> dict[str, Any]:
        facts = self.collect()
        # Off means no model's words on the page at all - not last week's,
        # still sitting in the cache from before it was turned off.
        cached = self.store.briefing_entries() if self.enabled else {}
        sections: dict[str, dict[str, Any]] = {}
        for name in SECTIONS:
            f = facts[name]
            entry = cached.get(name)
            summary = entry["content"] if entry else None
            sections[name] = {
                "level": f.level,
                "headline": f.headline,
                "facts": f.lines,
                "summary": summary,
                "generated_at": entry["created_at"] if entry else None,
                # Written from other facts than the ones on screen now.
                "stale": bool(entry) and entry["fingerprint"] != f.fingerprint,
                "updating": name in self.pending,
            }
        overall_entry = cached.get("overall")
        refs = self._refs(facts)
        level = _worst(*(s["level"] for s in sections.values()))
        return {
            "enabled": self.enabled,
            "model": self.model if self.enabled else "",
            "generating": self.running,
            "error": self.last_error,
            "level": level,
            "fallback_headline": fallback_headline(sections),
            "overall": overall_entry["content"] if overall_entry else None,
            "overall_generated_at": overall_entry["created_at"] if overall_entry else None,
            "overall_stale": bool(overall_entry) and overall_entry["fingerprint"]
                              != self._overall_fingerprint(sections, refs),
            "sections": sections,
        }

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def ensure(self, force: bool = False) -> bool:
        """Start rewriting whatever is stale. Returns whether a run started.

        The facts come off the event loop: collecting them reads two weeks of
        memory samples, and on a cold disk that held every other request -
        the whole dashboard - for twenty seconds after a sign-in."""
        if not self.enabled or self.running:
            return False
        facts, cached = await asyncio.to_thread(self._facts_and_cache)
        if self.running:  # another request started one while this one read
            return False
        due = [name for name in SECTIONS if force or self._due(facts[name], cached.get(name))]
        overall = cached.get("overall")
        if not due and overall and not force:
            # The sections are fine; the top line may still predate them.
            if not self._overall_needed(overall, facts, cached):
                return False
        self.pending = due
        self.task = asyncio.create_task(self._run(due, force), name="briefing")
        return True

    def _facts_and_cache(self) -> tuple[dict[str, Facts], dict[str, dict[str, Any]]]:
        return self.collect(), self.store.briefing_entries()

    def _due(self, facts: Facts, entry: dict[str, Any] | None) -> bool:
        if entry is None:
            return True
        if entry["fingerprint"] == facts.fingerprint:
            return False
        if entry["level"] != facts.level:
            return True  # worse, or better: either way the old words are wrong
        return _age(entry["created_at"]) >= self.refresh

    def _overall_needed(self, overall, facts, cached) -> bool:
        sections = self._sections_for_overall(facts, cached)
        return overall["fingerprint"] != self._overall_fingerprint(sections, self._refs(facts))

    async def _run(self, due: list[str], force: bool) -> None:
        self.last_error = ""
        try:
            for name in due:
                reason = await asyncio.to_thread(self.budget_ok)
                if reason:
                    self.last_error = reason
                    return
                facts = (await asyncio.to_thread(self.collect))[name]
                raw, usage = await self.call(SECTION_SYSTEM, section_prompt(facts),
                                             SECTION_SCHEMA, "briefing_section")
                await asyncio.to_thread(self.record, usage)
                content = clean_section(raw, facts.level)
                await asyncio.to_thread(self.store.save_briefing, name, facts.fingerprint,
                                        facts.level, content)
                self.pending = [n for n in self.pending if n != name]
                self.notify()

            facts, cached = await asyncio.to_thread(self._facts_and_cache)
            sections = self._sections_for_overall(facts, cached)
            refs = self._refs(facts)
            fingerprint = self._overall_fingerprint(sections, refs)
            overall = cached.get("overall")
            if overall and overall["fingerprint"] == fingerprint and not force:
                return
            reason = await asyncio.to_thread(self.budget_ok)
            if reason:
                self.last_error = reason
                return
            level = _worst(*(s["level"] for s in sections.values()))
            raw, usage = await self.call(OVERALL_SYSTEM, overall_prompt(sections, refs),
                                         OVERALL_SCHEMA, "briefing_overall")
            await asyncio.to_thread(self.record, usage)
            content = clean_overall(raw, {r["id"]: r for r in refs}, level)
            await asyncio.to_thread(self.store.save_briefing, "overall", fingerprint,
                                    level, content)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # a briefing failure costs a summary, never a poll
            log.warning("briefing failed: %s", error)
            self.last_error = f"The summary could not be written: {error}"[:300]
        finally:
            self.pending = []
            self.notify()

    def _sections_for_overall(self, facts, cached) -> dict[str, dict[str, Any]]:
        return {name: {"level": facts[name].level, "facts": facts[name].lines,
                       "summary": (cached.get(name) or {}).get("content")}
                for name in SECTIONS}

    @staticmethod
    def _refs(facts: dict[str, Facts]) -> list[dict[str, str]]:
        return [ref for name in SECTIONS for ref in facts[name].refs]

    @staticmethod
    def _overall_fingerprint(sections, refs) -> str:
        # The sections as written, not the links on offer: a new incident
        # changes its section's summary when that is rewritten, and the top
        # line follows then - not a moment earlier, from the refs alone.
        raw = json.dumps([[sections[n]["level"], sections[n].get("summary")] for n in SECTIONS],
                         sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _age(iso: str | None) -> timedelta:
    try:
        then = datetime.fromisoformat(iso or "")
    except ValueError:
        return timedelta.max
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - then
