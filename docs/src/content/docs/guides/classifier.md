---
title: "The classifier"
description: "A second opinion on noisy signals before they become incidents."
---

The rules are exact about facts and blunt about judgement. An exit code of 137
is a fact. "Five lines matching `error`" may be a database failing, or an app
that logs its retries at ERROR. The classifier asks a decision model about the
judgement cases only, before an incident opens.

**What it looks at:** error bursts, fatal lines, failed health checks,
sustained CPU and memory, and restart loops. Never exits, OOM kills, disks or
arrays: those open an incident whatever a model thinks.

**What it asks**, in one call and from a closed list of answers:

- is this a real fault, noise, a passing state, or unclear;
- how severe it is;
- the likely cause;
- whether it will clear on its own.

The evidence is the same the diagnosis would see (redacted, including the
change timeline), capped at about 3,000 tokens.

**What it may do:**

| Mode | Effect |
|---|---|
| `off` | nothing (the default) |
| `shadow` | records every verdict and changes nothing; each incident shows what the classifier would have done |
| `on` | hides a signal when a calibrated model puts noise at `DOCTOR_CLASSIFIER_SUPPRESS_AT` (0.9) or above; may raise a severity, or lower one it is at least that sure of |

The policy engine still decides every action, and the diagnosis still runs on
whatever opens. It is shown the classifier's read as a hint it may overrule.

**Setting it up.** The backend is a decision model on OpenRouter's Decisions
API, TypeSafe's Jev by default (`typesafe/jev-1.13`, pinned so verdicts don't
drift):

```
DOCTOR_CLASSIFIER=shadow
DOCTOR_CLASSIFIER_API_KEY=sk-or-...
```

If the diagnosis already goes through OpenRouter (`DOCTOR_LLM_PROVIDER=openai`
with `DOCTOR_OPENAI_BASE_URL=https://openrouter.ai/api/v1`), the key can be
left out and the classifier uses `DOCTOR_OPENAI_API_KEY`. A key of its own is
still better: it can carry its own credit limit on openrouter.ai, so a runaway
classifier can't spend the diagnosis's money.

Run it in `shadow` for a week or two, and read its verdicts on the LLM page
against what actually happened before you switch to `on`.
`DOCTOR_CLASSIFIER_PROVIDER=chat` uses the diagnosis's own chat model instead.
A chat model has no real probabilities, so it's treated as uncalibrated: it may
annotate and raise a severity, never hide or lower one. Any other decision
model is one class in `app/decision.py` behind the same `DecisionModel`
protocol.

**Cost, and the brakes on it.** Jev bills input at $0.042 per million tokens,
and its output is free (as of October 2026 — check the model's page, prices
change often; `DOCTOR_CLASSIFIER_PRICE_PER_MTOK` follows it). At about 3,000
tokens a call, a call costs roughly
$0.00013; 200 calls a day is under $0.03. The brakes:

- A verdict is cached against the signal for `DOCTOR_CLASSIFIER_CACHE_HOURS`
  (6): the lines it fired on with numbers stripped, and the volume as a power
  of two. A burst repeating every poll is one call, and twice the volume is
  asked again. A signal judged noise isn't even read again while its verdict
  holds.
- It has its own caps, separate from the diagnosis's: `DOCTOR_CLASSIFIER_DAILY_CALLS`
  (500) and `DOCTOR_CLASSIFIER_DAILY_USD` ($0.10). Its dollars still count
  toward the day's total spend.
- `DOCTOR_CLASSIFIER_MAX_PER_MINUTE` (10) holds back a host coming back up with
  everything firing at once.
- A timeout of `DOCTOR_CLASSIFIER_TIMEOUT` (5 s), and a breaker: three failures
  in a row pause it for ten minutes.
- Whenever it can't answer, the rule decides alone. The failure mode is the
  doctor as it was without a classifier.

For a hard ceiling outside the doctor, give the OpenRouter key a credit limit
of its own and no auto top-up.

**Prompt injection.** A hostile log line can only push the answer toward one
of the listed options. An answer outside them is dropped. Facts are never
asked about, a hidden signal is re-asked when its verdict expires or its volume
doubles, and hiding needs 90%.
