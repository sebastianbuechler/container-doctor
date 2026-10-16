"""A second opinion on a signal, before it becomes an incident.

The rules are exact about facts - a container exited 137, a disk dropped out
of an array - and blunt about judgement: five lines matching `error` in a log
tail may be a failing database or an app that logs its retries at ERROR. That
judgement is what a decision model is for, so it is asked there and only
there. The rules still decide what a signal *is*; the classifier only says
whether this one looks real.

Guard rails, all of them deliberate:

- Only the judgement kinds are ever asked about (ELIGIBLE). The rest - exits,
  OOM kills, disks, arrays - open an incident whatever a model thinks.
- Suppressing needs a calibrated model and a probability of noise at or above
  DOCTOR_CLASSIFIER_SUPPRESS_AT. A chat model may raise a severity, never
  lower one or hide an incident.
- `shadow` (the way to start) records every verdict and changes nothing, so
  the verdicts can be read against what happened before any are trusted.
- A verdict is cached against the signal - its kind, container and the
  normalised lines it fired on - and re-asked when the volume doubles, so a
  recurring burst costs one call, not one per poll.
- Its own daily call cap and dollar cap, a per-minute cap for bursts, a
  timeout, and a breaker: three failures in a row and it sits out ten
  minutes. Whenever it cannot answer, the rules' verdict stands.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from .decision import Choice, DecisionModel, Flag

log = logging.getLogger(__name__)

# The kinds a rule decides by judgement rather than by fact.
ELIGIBLE = frozenset({"log_errors", "log_fatal", "unhealthy", "high_cpu", "high_memory",
                      "restart_loop"})

SEVERITIES = ("info", "warning", "critical")

QUESTIONS = {
    "verdict": Choice(
        "A monitoring rule fired on this container. Is it a real problem someone should look at?",
        {
            "real_fault": "Something is broken or degrading and needs a person: errors that "
                          "stop the service working, a dependency down, data at risk.",
            "benign_noise": "The lines or numbers look alarming but are normal for this app: "
                            "retries that succeed, errors it logs as routine, expected load.",
            "expected_transient": "A real but passing state that clears on its own: a "
                                  "restart or upgrade in progress, a scheduled job, a warm-up.",
            "unclear": "The evidence is too thin to tell.",
        },
    ),
    "severity": Choice(
        "How serious is it, if it is real?",
        {
            "info": "Worth knowing, nothing to do.",
            "warning": "Needs a look soon; the service still mostly works.",
            "critical": "The service is down or data is at risk; act now.",
        },
    ),
    "cause": Choice(
        "What is the most likely cause?",
        {
            "config": "A configuration mistake in the app or its compose file.",
            "dependency": "Something it depends on is down or unreachable.",
            "resources": "Out of memory, CPU or connections.",
            "storage": "A full, missing or read-only volume.",
            "network": "DNS, TLS or connectivity.",
            "auth": "Credentials, tokens or permissions.",
            "image": "A bug or breaking change in the image version.",
            "upgrade": "An upgrade, migration or redeploy in progress.",
            "unknown": "None of these, or not enough evidence.",
        },
    ),
    "self_heals": Flag(
        "Will this clear on its own within about ten minutes, without anyone acting?",
        "Yes, it is likely to clear by itself.",
        "No, it needs someone to act.",
    ),
}

NOISE = ("benign_noise", "expected_transient")


@dataclass
class Verdict:
    verdict: str            # the chosen option, or "" when there was no answer
    severity: str           # the classifier's severity (the rule's, if unanswered)
    action: str             # open | suppress | downgrade | escalate
    applied: bool           # whether the pipeline acted on it (mode on)
    mode: str
    model: str
    calibrated: bool
    p_noise: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    cause: str = ""
    self_heals: float | None = None
    reason: str = ""
    from_cache: bool = False
    cost_usd: float = 0.0
    # The rest is for showing the working, not for deciding: every answer's
    # spread, the bar a suppression has to clear, and what the call cost.
    rule_severity: str = ""
    suppress_at: float | None = None
    confidence: float | None = None
    severity_confidence: float | None = None
    severity_probabilities: dict[str, float] = field(default_factory=dict)
    cause_probabilities: dict[str, float] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def cache_key(signal) -> str:
    """The signal, minus what changes every poll.

    Timestamps and numbers are stripped from the lines it fired on, so the
    same retry logged a minute later is the same question. The volume goes in
    as a power of two: twice as many errors is worth asking again.
    """
    details = signal.details or {}
    lines = details.get("matched_lines") or details.get("fatal_lines") or []
    normal = sorted({re.sub(r"\d+", "#", str(line))[:200] for line in lines})
    volume = int(details.get("match_count") or len(lines) or 0)
    bucket = int(math.log2(volume)) if volume > 0 else 0
    raw = "|".join([signal.container.get("name", ""), signal.kind, str(bucket), *normal])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class Classifier:
    def __init__(self, settings, model: DecisionModel | None, store) -> None:
        self.settings = settings
        self.model = model
        self.store = store
        self._recent: deque[float] = deque()
        # Up to three signals are processed at once, each checking the caps
        # from a worker thread: the check and the claim have to be one step.
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return self.settings.classifier_mode in ("shadow", "on") and self.model is not None

    def eligible(self, signal) -> bool:
        return self.enabled and signal.kind in ELIGIBLE

    # -- the budget and the breaker ---------------------------------------------

    def _blocked(self) -> str:
        """'' when a call may be made now, else why not."""
        with self._lock:
            return self._blocked_locked()

    def _blocked_locked(self) -> str:
        now = time.monotonic()
        if now < self._open_until:
            return f"paused for {int(self._open_until - now)}s after repeated failures"
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= self.settings.classifier_max_per_minute > 0:
            return "per-minute cap reached"
        usage = self.store.classifier_usage_today()
        cap = self.settings.classifier_daily_usd
        if cap > 0 and usage["cost_usd"] >= cap:
            return f"daily classifier budget of ${cap:.2f} spent"
        if not self.store.reserve_classifier_call(self.settings.classifier_daily_calls):
            return f"daily cap of {self.settings.classifier_daily_calls} classifier calls reached"
        self._recent.append(now)
        return ""

    # -- the review --------------------------------------------------------------

    def cached(self, signal) -> Verdict | None:
        """A verdict already given for this signal, still fresh."""
        row = self.store.cached_classification(cache_key(signal),
                                               self.settings.classifier_cache_hours)
        if not row:
            return None
        verdict = Verdict(**{k: row[k] for k in Verdict.__dataclass_fields__ if k in row})
        verdict.from_cache = True
        verdict.cost_usd = 0.0
        # Recomputed against today's settings: a mode switched off in the
        # meantime must not keep suppressing from yesterday's cache.
        verdict.mode = self.settings.classifier_mode
        verdict.applied = verdict.mode == "on"
        return verdict

    async def review(self, signal, state: str, *, ref: str = "") -> Verdict | None:
        """The classifier's verdict on one signal, or None when it was not asked.

        Never raises: a classifier that cannot answer leaves the rules alone.
        """
        if not self.eligible(signal):
            return None
        hit = await asyncio.to_thread(self.cached, signal)
        if hit is not None:
            return hit
        blocked = await asyncio.to_thread(self._blocked)
        if blocked:
            self.last_error = blocked
            log.info("classifier skipped %s for %s: %s", signal.kind,
                     signal.container.get("name"), blocked)
            return None
        try:
            decision = await asyncio.wait_for(
                self.model.decide(state[: self.settings.classifier_max_state_chars], QUESTIONS,
                                  purpose="classifier_signal", ref=ref),
                timeout=self.settings.classifier_timeout_seconds,
            )
        except Exception as error:
            self._failures += 1
            self.last_error = f"{error.__class__.__name__}: {error}"[:300]
            if self._failures >= 3:
                self._open_until = time.monotonic() + 600
                self._failures = 0
                log.warning("classifier failing (%s); pausing for ten minutes", self.last_error)
            else:
                log.info("classifier failed (%s); the rules decide", self.last_error)
            return None
        self._failures = 0
        self.last_error = ""
        await asyncio.to_thread(self.store.add_classifier_usage, decision.cost_usd,
                                decision.input_tokens, decision.output_tokens)
        verdict = self.judge(signal, decision)
        await asyncio.to_thread(self.store.record_classification, cache_key(signal),
                                signal.fingerprint, signal.container.get("name", ""),
                                signal.kind, signal.severity, verdict.to_dict())
        return verdict

    def judge(self, signal, decision) -> Verdict:
        """From answers to an action, under the guard rails."""
        answers = decision.answers
        choice = answers.get("verdict")
        severity_answer = answers.get("severity")
        heals = answers.get("self_heals")
        cause = answers.get("cause")
        probabilities = dict(choice.probabilities) if choice else {}
        p_noise = (sum(probabilities.get(k, 0.0) for k in NOISE)
                   if decision.calibrated and probabilities else None)
        rule = signal.severity if signal.severity in SEVERITIES else "warning"
        severity = severity_answer.value if severity_answer else rule
        mode = self.settings.classifier_mode
        threshold = self.settings.classifier_suppress_at

        action, reason = "open", "The rule stands."
        if choice is None:
            reason = "No usable answer; the rule stands."
        elif decision.calibrated and p_noise is not None and p_noise >= threshold:
            action = "suppress"
            reason = f"{p_noise:.0%} likely {choice.value.replace('_', ' ')}, at or above {threshold:.0%}."
        elif SEVERITIES.index(severity) > SEVERITIES.index(rule):
            action = "escalate"
            reason = f"Rated {severity}, above the rule's {rule}."
        elif (decision.calibrated and SEVERITIES.index(severity) < SEVERITIES.index(rule)
              and (severity_answer.confidence or 0) >= threshold):
            action = "downgrade"
            reason = f"Rated {severity} with {severity_answer.confidence:.0%} confidence."
        elif not decision.calibrated and choice.value in NOISE:
            reason = "Looks like noise, but an uncalibrated model may not hide an incident."

        return Verdict(
            verdict=choice.value if choice else "", severity=severity, action=action,
            applied=mode == "on", mode=mode, model=decision.model,
            calibrated=decision.calibrated, p_noise=p_noise, probabilities=probabilities,
            cause=cause.value if cause else "",
            self_heals=(heals.probabilities.get("true") if heals and heals.probabilities
                        else (1.0 if heals and heals.value else 0.0) if heals else None),
            reason=reason, cost_usd=decision.cost_usd,
            rule_severity=rule, suppress_at=threshold,
            confidence=choice.confidence if choice else None,
            severity_confidence=severity_answer.confidence if severity_answer else None,
            severity_probabilities=dict(severity_answer.probabilities) if severity_answer else {},
            cause_probabilities=dict(cause.probabilities) if cause else {},
            input_tokens=decision.input_tokens, output_tokens=decision.output_tokens,
            latency_ms=decision.latency_ms,
        )
