"""LLM diagnosis layer.

The model is *advisory only*: it explains an incident and proposes one action
from a closed list. Whether that action ever runs is decided by
`remediator.Policy`, in plain deterministic Python.

Two providers are supported: the Anthropic API (default) and any
OpenAI-compatible endpoint, so a local Ollama/LM Studio model can do the job
without anything leaving the house.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from .config import VALID_ACTIONS, Settings

log = logging.getLogger(__name__)

# USD per million tokens (input, output). Used for the budget meter only -
# unknown models simply record zero cost.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Models that take adaptive thinking + output_config.effort. Older/smaller
# models reject those fields, so we simply omit them.
_ADAPTIVE_THINKING_PREFIXES = (
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5",
)

DIAGNOSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One sentence a tired human can read on a phone at 2am.",
        },
        "root_cause": {
            "type": "string",
            "description": "What actually went wrong, grounded in the evidence provided.",
        },
        "confidence": {
            "type": "number",
            "description": "0.0-1.0. Be honest; below 0.7 nothing is auto-applied.",
        },
        "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
        "is_transient": {
            "type": "boolean",
            "description": "True if this is likely to clear on its own without action.",
        },
        "recommended_action": {"type": "string", "enum": list(VALID_ACTIONS)},
        "action_rationale": {
            "type": "string",
            "description": "Why that action, or why no automated action can help.",
        },
        "human_fix_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 5 concrete steps the operator should take (config edits, "
                           "compose changes, upstream checks). Empty if none are needed.",
        },
        "evidence_used": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The specific log lines or fields the conclusion rests on.",
        },
    },
    "required": [
        "summary", "root_cause", "confidence", "severity", "is_transient",
        "recommended_action", "action_rationale", "human_fix_steps", "evidence_used",
    ],
    "additionalProperties": False,
}

def deployment(manager: str = "", method: str = "") -> str:
    """How the containers got here, in one phrase, from DOCTOR_DOCKER_MANAGER
    and DOCTOR_DEPLOY_METHOD - both free text, both optional."""
    words = "Compose stacks"
    if manager:
        words += f" managed by {manager}"
    if method:
        words += f", deployed by: {method}"
    return words


def system_prompt(manager: str = "", method: str = "", host: str = "a Linux host") -> str:
    """The diagnosis's instructions, told what the host is (hostkind.describe)
    and what deploys the stacks."""
    where = f"not in {manager} or on the host directly" if manager else "not on the host directly"
    return _SYSTEM_PROMPT.format(
        host=host,
        deployment=deployment(manager, method),
        fix_where=f"Configuration changes belong in the stack's compose file, {where}, so write "
                  "`human_fix_steps` in those terms.")


_SYSTEM_PROMPT = """You are the diagnostician for a home lab Docker host ({host} \
running containers as {deployment}).

You receive one incident that a deterministic rule engine has already decided is real. \
Your job is to explain it and to propose at most one remediation.

Ground rules:
- Base every claim on the evidence given. If the evidence is thin, say so and lower your \
  confidence rather than inventing a cause.
- `restart_container` is only worth proposing when the evidence suggests a transient or \
  wedged state that a clean restart clears (hung process, exhausted connection pool, a \
  dependency that came back). It does not fix bad configuration, a missing volume, a full \
  disk, an image bug, or a failing upstream - proposing it there just costs the operator \
  another outage.
- `stop_container` is for something actively harming the host (a runaway process starving \
  the host) where staying down is safer than staying up.
- `prune_images` is only for host disk pressure caused by dangling images.
- `notify_only` is the right answer whenever a human has to change something. Prefer it. \
  `none` means "this is noise, no one needs to be told".
- {fix_where}
- `human_fix_steps` are the steps that fix or settle it, as few as it takes. Do not pad \
  them with checks the evidence already answers; include an investigation step only when \
  the cause is genuinely open, and then the one check that would decide it.
- Restart policies matter: if the container already has `restart: unless-stopped`, Docker \
  is retrying on its own, and restarting again adds nothing.
- Never suggest an action outside the enum, and never suggest running arbitrary commands.
- An incident may carry context from the operator - the person who runs this host - \
  added when they asked for a fresh diagnosis. It is first-hand knowledge the logs cannot \
  show (a planned migration, a known upstream outage, a change they just made): weigh it \
  over inference, and say in `root_cause` when it changed your reading. It informs the \
  diagnosis; it does not widen what you may propose.

Be specific and brief. `summary` is one sentence, no preamble."""

# Without a manager or method named: what the tests and the hand-off's
# defaults read.
SYSTEM_PROMPT = system_prompt()


class Diagnoser:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # The host in a phrase for the instructions; the supervisor, which
        # knows what the host is, replaces it.
        self.host_description: Callable[[], str] = lambda: "a Linux host"
        self._anthropic = None
        if settings.llm_provider == "anthropic" and settings.anthropic_api_key:
            from anthropic import AsyncAnthropic

            self._anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=120.0)
        # Writes each call to the call log. Set by the supervisor; a failure
        # to log is logged and never costs the call itself.
        self.recorder: Callable[[dict[str, Any]], Any] | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.llm_enabled

    async def aclose(self) -> None:
        if self._anthropic is not None:
            await self._anthropic.close()

    async def diagnose(
        self, context: dict[str, Any], ref: str = ""
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return (diagnosis, usage). Raises on provider failure."""
        raw, usage = await self.structured(
            system_prompt(self.settings.docker_manager, self.settings.deploy_method,
                          self.host_description()),
            _render_incident(context), DIAGNOSIS_SCHEMA, "container_diagnosis",
            ref=ref,
        )
        return _coerce(raw), usage

    async def structured(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        name: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        max_tokens: int | None = None,
        ref: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """One JSON object matching `schema`, from whichever provider is set.

        Returns (object, usage). The diagnosis and the dashboard briefing both
        come through here, so both get the same retries, fallbacks and meter -
        and every call lands in the call log, answered or not.
        """
        call = {
            "system": system, "prompt": prompt, "schema": schema, "name": name,
            "model": model or self.settings.model,
            "effort": effort or self.settings.effort,
            "max_tokens": max_tokens or self.settings.max_output_tokens,
        }
        openai = self.settings.llm_provider == "openai"
        started = time.monotonic()
        entry: dict[str, Any] = {
            "purpose": name, "ref": ref, "provider": self.settings.llm_provider,
            "model": call["model"],
            "endpoint": (f"{self.settings.openai_base_url.rstrip('/')}/chat/completions"
                         if openai else "https://api.anthropic.com/v1/messages"),
            "request": {"system": system, "prompt": prompt, "schema_name": name,
                        "schema": schema, "effort": call["effort"],
                        "max_tokens": call["max_tokens"]},
        }
        try:
            if openai:
                result, usage = await self._structured_openai(**call)
            else:
                result, usage = await self._structured_anthropic(**call)
        except Exception as error:
            entry.update(status="error", error=f"{error.__class__.__name__}: {error}")
            await self._record(entry, started)
            raise
        if usage.get("request_mode"):
            entry["request"]["sent_as"] = usage["request_mode"]
        entry.update(status="ok", response=json.dumps(result, indent=2, default=str),
                     **{k: usage.get(k, 0) for k in
                        ("input_tokens", "output_tokens", "cached_tokens", "cost_usd")})
        await self._record(entry, started)
        return result, usage

    async def _record(self, entry: dict[str, Any], started: float) -> None:
        if self.recorder is None:
            return
        entry["latency_ms"] = int((time.monotonic() - started) * 1000)
        try:
            await asyncio.to_thread(self.recorder, entry)
        except Exception:
            log.exception("could not write the LLM call log")

    # -- Anthropic ----------------------------------------------------------

    async def _structured_anthropic(
        self, *, system: str, prompt: str, schema: dict[str, Any], name: str,
        model: str, effort: str, max_tokens: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._anthropic is None:
            raise RuntimeError("Anthropic client is not configured")

        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": schema}
        }
        kwargs: dict[str, Any] = {}
        if model.startswith(_ADAPTIVE_THINKING_PREFIXES):
            kwargs["thinking"] = {"type": "adaptive"}
            output_config["effort"] = effort

        response = await self._anthropic.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    # Identical on every call, so bursts of incidents reuse it.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
            output_config=output_config,
            **kwargs,
        )

        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            raise RuntimeError(f"model declined to answer ({getattr(detail, 'category', 'unknown')})")

        text = next((b.text for b in response.content if b.type == "text"), "")
        result = json.loads(text)

        usage = response.usage
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        created = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        return result, {
            "input_tokens": input_tokens + created,
            "output_tokens": output_tokens,
            "cached_tokens": cached,
            "cost_usd": _cost(model, input_tokens + created, output_tokens, cached,
                              self.settings.custom_pricing),
            "model": model,
        }

    # -- OpenAI-compatible (Ollama, LM Studio, OpenRouter, vLLM, ...) --------

    async def _structured_openai(
        self, *, system: str, prompt: str, schema: dict[str, Any], name: str,
        model: str, effort: str, max_tokens: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        base = self.settings.openai_base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}

        def build(structured: bool, temperature: bool) -> dict[str, Any]:
            body: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
            }
            if temperature and self.settings.llm_temperature >= 0:
                body["temperature"] = self.settings.llm_temperature
            if structured:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": name,
                        "strict": True,
                        "schema": schema,
                    },
                }
            else:
                # Plain JSON mode, with the schema spelled out in the prompt.
                # Gemini, Groq and most local runtimes all accept this shape.
                body["response_format"] = {"type": "json_object"}
                body["messages"][1]["content"] = (
                    prompt
                    + "\n\nRespond with a single JSON object matching this schema exactly:\n"
                    + json.dumps(schema)
                )
            return body

        # Degrade one parameter at a time on a 400/422, so a vendor that rejects
        # only `temperature` (every reasoning model - GPT-6 Luna refuses it at
        # any reasoning effort above `none`) doesn't also lose schema
        # enforcement, and one that rejects only `json_schema` keeps its
        # sampling settings.
        attempts = [
            (True, True, "json_schema + temperature"),
            (True, False, "json_schema, no temperature"),
            (False, False, "plain JSON mode, no temperature"),
        ]
        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            response = None
            described = ""
            for index, (structured, temperature, described) in enumerate(attempts):
                response = await self._post_with_retries(
                    client, f"{base}/chat/completions",
                    build(structured, temperature), headers,
                )
                if response.status_code not in (400, 404, 422):
                    if index:
                        log.info("%s accepted %s", model, described)
                    break
                if index + 1 < len(attempts):
                    log.info(
                        "%s rejected %s (HTTP %s); retrying with %s",
                        model, described, response.status_code, attempts[index + 1][2],
                    )
            assert response is not None
            if response.status_code >= 400:
                # Surface the vendor's own message - it names the bad parameter.
                raise RuntimeError(
                    f"{model} rejected the request (HTTP {response.status_code}): "
                    f"{response.text[:300]}"
                )
            body = response.json()

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected response shape from {model}: {exc}") from exc
        result = _loads_lenient(content)

        usage = body.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        return result, {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": 0,
            "cost_usd": _cost(model, input_tokens, output_tokens, 0, self.settings.custom_pricing),
            "model": model,
            "request_mode": described,
        }

    async def _post_with_retries(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> httpx.Response:
        """POST, retrying rate limits and transient server errors.

        Free tiers meter tightly - Gemini's is 10 requests a minute - and a
        burst of incidents will hit that. Waiting is fine here: nothing is
        blocked on the answer, and the alternative is an undiagnosed incident.
        """
        delay = 2.0
        last: httpx.Response | None = None
        for attempt in range(self.settings.llm_max_retries + 1):
            try:
                last = await client.post(url, json=payload, headers=headers)
            except httpx.RequestError as exc:
                if attempt >= self.settings.llm_max_retries:
                    raise
                log.warning("LLM request failed (%s); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue

            if last.status_code != 429 and last.status_code < 500:
                return last
            if attempt >= self.settings.llm_max_retries:
                return last

            wait = _retry_after(last) or delay
            log.warning(
                "LLM returned %s; retrying in %.0fs (attempt %d/%d)",
                last.status_code, wait, attempt + 1, self.settings.llm_max_retries,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, 60.0)
        assert last is not None
        return last


def _cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    override: tuple[float, float] | None = None,
) -> float:
    """USD for one call. `override` is DOCTOR_PRICE_*_PER_MTOK, which is how a
    non-Anthropic vendor gets a working spend meter."""
    price = override or MODEL_PRICES.get(model)
    if not price:
        return 0.0  # unknown or free model - the daily call cap is the guard
    input_price, output_price = price
    # Cache reads bill at roughly a tenth of the input rate.
    return (
        input_tokens * input_price / 1_000_000
        + cached_tokens * input_price * 0.1 / 1_000_000
        + output_tokens * output_price / 1_000_000
    )


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from a Retry-After header, when the vendor sends one."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), 120.0))
    except ValueError:
        return None  # HTTP-date form; the caller's backoff covers it


def _loads_lenient(content: str) -> dict[str, Any]:
    """Parse JSON that a model wrapped in prose or a ```json fence.

    Strict json_schema mode makes this unnecessary; JSON mode on a small or
    local model does not.
    """
    content = (content or "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.S)
    if fenced:
        return json.loads(fenced.group(1))
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        return json.loads(content[start : end + 1])
    raise ValueError("model did not return JSON")


def _coerce(raw: dict[str, Any]) -> dict[str, Any]:
    """Defend against a model that ignores the schema."""
    action = str(raw.get("recommended_action", "notify_only"))
    if action not in VALID_ACTIONS:
        log.warning("model proposed unknown action %r; coercing to notify_only", action)
        action = "notify_only"

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if not math.isfinite(confidence):  # "NaN" compares false with everything
        confidence = 0.0

    severity = str(raw.get("severity", "warning"))
    if severity not in ("info", "warning", "critical"):
        severity = "warning"

    def string_list(key: str) -> list[str]:
        value = raw.get(key) or []
        if isinstance(value, str):
            value = [value]
        return [str(v)[:500] for v in value][:8]

    return {
        "summary": str(raw.get("summary", ""))[:500],
        "root_cause": str(raw.get("root_cause", ""))[:2000],
        "confidence": min(max(confidence, 0.0), 1.0),
        "severity": severity,
        "is_transient": bool(raw.get("is_transient", False)),
        "recommended_action": action,
        "action_rationale": str(raw.get("action_rationale", ""))[:2000],
        "human_fix_steps": string_list("human_fix_steps"),
        "evidence_used": string_list("evidence_used"),
        "diagnosed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _render_incident(ctx: dict[str, Any], closing: bool = True) -> str:
    """Flatten the evidence bundle into the prompt body.

    `closing` adds the diagnosis's final instruction; the classifier reads the
    same evidence with questions of its own instead.
    """
    container = ctx.get("container", {})
    lines = [
        "## Incident",
        f"kind: {ctx.get('kind')}",
        f"detected: {ctx.get('detected_at')}",
        f"rule summary: {ctx.get('summary')}",
        "",
        "## Container",
        f"name: {container.get('name')}",
        f"image: {container.get('image')}",
        f"state: {container.get('state')} (health: {container.get('health')})",
        f"restart policy: {container.get('restart_policy') or 'none'}",
        f"restart count: {container.get('restart_count')}",
        f"exit code: {container.get('exit_code')}",
        f"oom killed: {container.get('oom_killed')}",
        f"started at: {container.get('started_at')}",
    ]

    if ctx.get("stats"):
        stats = ctx["stats"]
        lines += [
            "",
            "## Resource usage at detection",
            f"cpu: {stats.get('cpu_pct')}%",
            f"memory: {stats.get('mem_pct')}% "
            f"({stats.get('mem_usage_bytes')} / {stats.get('mem_limit_bytes')} bytes)",
        ]

    details = dict(ctx.get("details") or {})
    # Out of the JSON blob and into their own block: these are the lines the
    # rule actually fired on, and json.dumps + a length cap used to escape and
    # then truncate them, leaving the diagnosis to guess at its own evidence.
    matched = [str(line) for line in details.pop("matched_lines", []) or []]
    if matched:
        lines += [
            "",
            f"## The {len(matched)} line{'s' if len(matched) != 1 else ''} "
            "that triggered the rule",
            "```",
            *[line[:400] for line in matched[:20]],
            "```",
        ]
    if details:
        lines += ["", "## Rule details", json.dumps(details, indent=2, default=str)[:2000]]

    stack = ctx.get("stack")
    if stack:
        lines += ["", f"## Stack `{stack.get('project')}` (service `{stack.get('service')}`)"]
        for label, key in (
            ("depends on", "depends_on"),
            ("depended on by", "dependents"),
            ("other unhealthy services", "unhealthy_siblings"),
        ):
            entries = stack.get(key) or []
            if entries:
                described = ", ".join(f"{e['service']} ({e['status']})" for e in entries)
                lines += [f"{label}: {described}"]
        root = ctx.get("likely_root_cause")
        if root:
            lines += [
                f"NOTE: this container depends on {root['service']}, which is itself "
                f"{root['status']}. Say so if the evidence fits, and prefer a fix "
                f"there over restarting this one."
            ]

    if ctx.get("health_log"):
        lines += ["", "## Last health check output", ctx["health_log"][:1500]]

    if ctx.get("disk"):
        lines += ["", "## Host disk", json.dumps(ctx["disk"], indent=2, default=str)[:1000]]

    if ctx.get("smart"):
        lines += ["", "## Each drive's SMART health",
                  json.dumps(ctx["smart"], default=str)[:2000]]
    if ctx.get("host"):
        # The host as a whole. A container OOM-killed while the host has memory
        # to spare hit its own limit; on a host out of memory it was a victim.
        lines += ["", "## The host as a whole at detection",
                  json.dumps(ctx["host"], default=str)[:1200]]
    if ctx.get("connectivity"):
        lines += ["", "## Internet and DNS from the host, last check",
                  json.dumps(ctx["connectivity"], default=str)[:800]]
    if ctx.get("top_containers"):
        lines += ["", "## Heaviest containers on the host right now",
                  json.dumps(ctx["top_containers"], default=str)[:1200]]

    if ctx.get("changes"):
        lines += ["", "## Recent changes to this container (newest first)"]
        detected = _parse_time(ctx.get("detected_at"))
        for change in ctx["changes"][:8]:
            when = _parse_time(change.get("at"))
            before = ""
            if detected and when:
                minutes = (detected - when).total_seconds() / 60
                before = (f" ({_span(minutes)} before detection)" if minutes >= 0
                          else f" ({_span(-minutes)} after detection)")
            times = f" ({change['count']} times)" if int(change.get("count") or 1) > 1 else ""
            lines.append(f"- {change.get('at')}{before}: {change.get('summary')}{times}")
            # The full list; the summary above names the first few.
            for item in (change.get("config_changes") or [])[:20]:
                lines.append(f"    - {item}")
        lines.append(_changes_hint(ctx["changes"][:8]))

    if ctx.get("repo"):
        lines += _render_repo(ctx["repo"], _parse_time(ctx.get("detected_at")))

    verdict = ctx.get("classification") or {}
    if verdict.get("verdict"):
        p = (verdict.get("probabilities") or {}).get(verdict["verdict"])
        lines += ["", "## A decision model's first read of this signal",
                  f"{verdict['verdict'].replace('_', ' ')}"
                  + (f" ({p:.0%})" if isinstance(p, (int, float)) else "")
                  + f", severity {verdict.get('severity')}, likely cause {verdict.get('cause') or 'unknown'}. "
                  "A quick classification, not a diagnosis: overrule it when the evidence says otherwise."]

    if ctx.get("history"):
        lines += ["", "## Recent history for this container", ctx["history"][:1500]]

    if ctx.get("operator_context"):
        # Last before the logs: it is what the operator wants read against them.
        lines += ["", "## Context from the operator", ctx["operator_context"][:2000]]

    if ctx.get("logs"):
        lines += ["", "## Container log tail (secrets already redacted)", "```", ctx["logs"], "```"]

    if not closing:
        return "\n".join(lines)
    lines += [
        "",
        "Diagnose this incident. Remember that `restart_container` is only appropriate for "
        "transient or wedged state, and that anything requiring a config change is "
        "`notify_only` with concrete steps.",
    ]
    return "\n".join(lines)


def _render_repo(repo: dict[str, Any], detected: datetime | None) -> list[str]:
    """The stack as the repository has it: the service's definition, and the
    recent commits to its folder with what they changed and why."""
    if not (repo.get("definition") or repo.get("commits")):
        return []
    lines = ["", f"## Stack `{repo.get('stack')}` in git ({repo.get('repo')}, "
                 f"branch {repo.get('ref')})"]
    if repo.get("definition"):
        lines += [f"Service `{repo.get('service')}` in {repo.get('compose_path')}, "
                  "comments included:", "```yaml", repo["definition"], "```"]
    commits = repo.get("commits") or []
    if commits:
        lines += ["", "Recent commits to the stack's folder (newest first):"]
    for commit in commits:
        when = _parse_time(commit.get("at"))
        ago = ""
        if detected and when:
            minutes = (detected - when).total_seconds() / 60
            ago = (f" ({_span(minutes)} before detection)" if minutes >= 0
                   else f" ({_span(-minutes)} after detection)")
        state = {True: "older than this container, so in what it runs if the stack was "
                       "deployed from the branch",
                 False: "newer than this container: not running yet",
                 None: ""}[commit.get("deployed")]
        message = (commit.get("message") or "").splitlines()
        lines.append(f"- {commit.get('sha')} {commit.get('at')}{ago}: "
                     f"{message[0] if message else ''}" + (f" [{state}]" if state else ""))
        body = " ".join(line.strip() for line in message[1:] if line.strip())
        if body:
            lines.append(f"    {body[:400]}")
        for changed in commit.get("files") or []:
            if changed.get("patch"):
                lines += [f"    {changed.get('path')}:", "```diff", changed["patch"], "```"]
    lines.append(
        "These diffs are what was actually changed, and the messages why: read them before "
        "blaming a change. A commit that only pins an image to its digest, or touches "
        "another service, does not change what this container runs. Propose fixes as "
        f"edits to {repo.get('compose_path') or 'the compose file'}.")
    return lines


def _changes_hint(changes: list[dict[str, Any]]) -> str:
    """What the timeline above does and does not support.

    A fixed "a recreate is the likeliest cause, name the image to go back to"
    sent the model hunting for a previous image after a rename on the same
    one - and there was none to name. So the hint follows what changed: only
    a moved image makes a rollback a question at all.
    """
    moved = [c for c in changes if c.get("image_change") in ("version", "build")]
    replaced = [c for c in changes if c.get("kind") in ("recreated", "renamed")]
    appeared = [c for c in changes if c.get("kind") == "appeared"]
    reconfigured = [c for c in replaced if c.get("config_changes")]
    hints: list[str] = []
    if moved:
        hints.append(
            "The image changed shortly before the failure: that is the likeliest cause when "
            "the evidence fits. Say so, and name the previous image or build shown above "
            "to go back to. Some env, cmd or label differences listed with it may come from "
            "the new image itself rather than from anyone's configuration.")
    if reconfigured:
        hints.append(
            "The configuration it runs with changed (listed under the change): read those "
            "differences against the errors, and when one explains them, say which and "
            "propose changing it back in the compose file.")
    if hints:
        return " ".join(hints)
    if replaced:
        compared = [c for c in replaced if c.get("config_changes") is not None]
        unchanged = (" Its run configuration was compared and did not change either, so "
                     "what is left is the workload, its data or something outside the "
                     "container." if compared and len(compared) == len(replaced) else
                     " A recreate or rename on the same image points at configuration "
                     "or at the workload.")
        return ("None of these changes moved the image: the container was replaced on the "
                "same one, so an image regression is not what they show and there is no "
                "earlier image to go back to." + unchanged)
    if appeared:
        return ("The container appeared new, with no earlier image recorded here: do not "
                "propose rolling back to one.")
    return ("None of these changes replaced the container or its image.")


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _span(minutes: float) -> str:
    if minutes < 90:
        return f"{max(round(minutes), 0)} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} h"
    return f"{minutes / 1440:.0f} days"
