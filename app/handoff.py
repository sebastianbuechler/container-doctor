"""One incident as a Markdown brief for an LLM agent the operator runs locally.

The doctor's own diagnosis is a single shot: one prompt, one structured answer,
an action enum. That is the right shape for deciding whether to restart
something unattended, and the wrong one for "I want to dig into this" - which
needs a conversation, the operator's shell, and a model that can ask for the
next piece of evidence. This hands the case over to one.

What goes in is what the doctor already holds, and nothing it does not:
the evidence exactly as its model was shown it, what the model concluded, what
was decided and done, and how the container looks now. Every part of it has
been through redaction on the way into the database; no live log is read here.
"""

from __future__ import annotations

import re
from typing import Any

from .diagnoser import _render_incident, deployment, system_prompt

ACTION_WORDS = {
    "none": "no action",
    "notify_only": "notify only",
    "restart_container": "restart the container",
    "stop_container": "stop the container",
    "prune_images": "prune dangling images",
}

_BRIEF = """You are helping the operator of a home lab investigate an incident \
that Container Doctor - an automated monitor on their Docker host, {host} - \
detected and diagnosed. The containers run under Docker as {deployment}, so \
configuration changes belong in the stacks' compose files rather than \
{elsewhere}on the host directly.

The doctor's diagnosis below came from a single automated pass with no way to \
ask a follow-up question. Treat it as a hypothesis to test, not a conclusion:

- Start from the evidence below. When it already settles the question, say so \
and give your conclusion - do not ask for more.
- Gather what you can yourself. If you can read the repository, read the \
compose files and their git history directly; if you have a shell on the host \
or Docker access, run the read-only commands (`docker logs --since`, \
`docker inspect`) rather than asking for their output.
- Ask the operator only for what you cannot reach and what would change your \
conclusion - usually one thing, rarely more than two - and say what each answer \
would decide.
- Say plainly when the evidence is too thin to decide.
- Propose changes as edits to the compose file or the app's configuration, and \
say what could go wrong with each.
- Prefer what can be undone. Anything that deletes data, volumes or images \
needs the operator's explicit go-ahead."""


def brief(manager: str = "", method: str = "", host: str = "a Linux host") -> str:
    """How to help, told what the host is and what deploys the stacks (see
    diagnoser.deployment)."""
    return _BRIEF.format(host=host, deployment=deployment(manager, method),
                         elsewhere=f"in {manager} or " if manager else "")


def _fence(text: str, language: str = "text") -> str:
    """A code fence longer than any backtick run inside the text.

    The evidence carries its own ``` fences around the log tail, and a log
    line can contain anything; a fixed fence would end the block early.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{language}\n{text.rstrip()}\n{ticks}"


def _line(label: str, value: Any) -> str:
    return f"- **{label}:** {value}" if value not in (None, "", []) else ""


def render(
    incident: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    current: dict[str, Any] | None = None,
    image: dict[str, Any] | None = None,
    link: str = "",
    model: str = "",
    provider: str = "",
    manager: str = "",
    method: str = "",
    host: str = "a Linux host",
) -> str:
    """The whole brief. Sections with nothing to say are left out."""
    diagnosis = incident.get("diagnosis") or {}
    evidence = incident.get("evidence") or {}
    title = diagnosis.get("summary") or incident.get("summary") or incident.get("kind", "")
    parts: list[str] = [
        f"# Incident: {title}",
        "",
        "> Handed over from Container Doctor for an interactive investigation. "
        "Secrets were redacted by pattern before any of this was stored, which is "
        "a strong default rather than a guarantee - read it through before pasting "
        "it anywhere outside your own network.",
        "",
        "## How to help",
        "",
        brief(manager, method, host),
        "",
        "## The incident",
        "",
    ]
    parts += [line for line in (
        _line("Container", f"`{incident.get('container_name', '')}`"),
        _line("Image", f"`{incident['image']}`" if incident.get("image") else ""),
        _line("Rule", str(incident.get("kind", "")).replace("_", " ")),
        _line("What the rule saw", incident.get("summary")),
        _line("Rule severity", incident.get("rule_severity") or incident.get("severity")),
        _line("Status", str(incident.get("status", "")).replace("_", " ")),
        _line("Opened", incident.get("opened_at")),
        _line("Last seen", incident.get("last_seen_at")
              if (incident.get("occurrences") or 1) > 1 else ""),
        _line("Seen", f"{incident['occurrences']} times"
              if (incident.get("occurrences") or 1) > 1 else ""),
        _line("Dashboard", link),
    ) if line]

    note = diagnosis.get("operator_context") or {}
    if note.get("text"):
        parts += [
            "",
            "## What the operator told the doctor",
            "",
            f"From {note.get('by') or 'the operator'}"
            + (f", {note['at']}" if note.get("at") else "") + ":",
            "",
            "\n".join(f"> {line}" for line in str(note["text"]).splitlines() or [""]),
        ]

    parts += ["", "## The doctor's diagnosis", ""]
    if diagnosis:
        who = " ".join(bit for bit in (provider, model) if bit)
        if who:
            parts += [f"_From the configured model, {who}"
                      f"{', reused from an identical earlier incident' if diagnosis.get('from_cache') else ''}._", ""]
        confidence = diagnosis.get("confidence")
        parts += [line for line in (
            _line("Summary", diagnosis.get("summary")),
            _line("Root cause", diagnosis.get("root_cause")),
            _line("Severity", diagnosis.get("severity")),
            _line("Confidence", f"{float(confidence) * 100:.0f}%" if confidence is not None else ""),
            _line("Transient", {True: "likely clears on its own", False: "needs attention"}
                  .get(diagnosis.get("is_transient"), "")),
            _line("Proposed", ACTION_WORDS.get(str(diagnosis.get("recommended_action")),
                                               diagnosis.get("recommended_action"))),
            _line("Why", diagnosis.get("action_rationale")),
        ) if line]
        steps = diagnosis.get("human_fix_steps") or []
        if steps:
            parts += ["", "Steps it suggested:", ""]
            parts += [f"{index}. {step}" for index, step in enumerate(steps, 1)]
        used = diagnosis.get("evidence_used") or []
        if used:
            parts += ["", "Evidence it cited:", "", _fence("\n".join(str(item) for item in used))]
    else:
        parts += ["No diagnosis was recorded - no model was configured, the daily budget was "
                  "spent, or the call failed. The evidence below is everything the doctor had."]

    if actions:
        parts += ["", "## What was decided and done", ""]
        for action in sorted(actions, key=lambda a: a.get("created_at") or ""):
            what = ACTION_WORDS.get(str(action.get("action")), action.get("action"))
            line = (f"- {action.get('created_at', '')}: **{what}** - {action.get('status', '')}, "
                    f"decided by {action.get('decided_by', '')}")
            detail = "; ".join(bit for bit in (action.get("reason"), action.get("result")) if bit)
            parts.append(f"{line}. {detail}" if detail else line)

    if current:
        stats = current.get("stats") or {}
        parts += ["", "## The container now", ""]
        parts += [line for line in (
            _line("State", current.get("state")),
            _line("Health", current.get("health") if current.get("health") not in (None, "none") else ""),
            _line("Restarts", current.get("restart_count")),
            _line("Restart policy", current.get("restart_policy")),
            _line("CPU", f"{float(stats['cpu_host_pct']):.1f}% of the host"
                  if stats.get("cpu_host_pct") is not None else ""),
            _line("Memory", f"{float(stats['mem_pct']):.0f}% of its limit"
                  if stats.get("mem_limit_bytes") else ""),
            _line("Stack", f"`{current['stack']}`, service `{current.get('service', '')}`"
                  if current.get("stack") else ""),
        ) if line]

    newer = (image or {}).get("newer") or {}
    if image and (image.get("state") == "update" or newer):
        parts += ["", "## Its image", ""]
        if image.get("state") == "update":
            parts.append("- A newer build of this tag is published and not yet pulled.")
        if newer:
            parts.append(f"- A newer version exists: `{newer.get('tag')}` ({newer.get('level')})"
                         + (f", release notes at {newer['notes_url']}" if newer.get("notes_url") else ""))
            if newer.get("breaking"):
                parts.append(f"- Its release notes mention: {newer['breaking']}")

    # The note lives on the diagnosis, not in the stored evidence; put it back
    # so this is the prompt as it was actually sent.
    shown = dict(evidence, operator_context=note["text"]) if note.get("text") else evidence
    prompt = _render_incident(shown) if shown else ""
    if prompt:
        parts += [
            "",
            "## The evidence, exactly as the doctor's model was shown it",
            "",
            _fence(prompt, "markdown"),
        ]

    parts += [
        "",
        "## The instructions the doctor's model worked under",
        "",
        "For reference: these constrain the automated pass to one action from a fixed "
        "list. They do not apply to you.",
        "",
        _fence(system_prompt(manager, method, host)),
        "",
    ]
    return "\n".join(parts)
