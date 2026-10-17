---
title: "Investigating an incident"
description: "Add what the logs cannot show, read the logs around it, and hand it to your own agent."
---

A diagnosis is one automated pass with no follow-up questions. When it is not
enough, you can tell the model what the logs cannot show, read the logs around
the incident yourself, or hand the whole incident to your own agent.

## Telling the model what the logs cannot

**Re-diagnose** opens a box for *additional context*, optional: what you know
that no log line says. *"Migrating postgres 15 to 16 until 15:00, the app is
read-only meanwhile"*, *"I changed the reverse proxy an hour ago"*, *"a restart
did not help"*. The model is told to weigh it over inference and to say in the
root cause when it changed its reading, and the same enum and policy apply as
ever: the note informs the diagnosis, it cannot widen what may be proposed or
run.

The note is shown above the diagnosis it produced — *Context from alice · 2m
ago* — and the incident's header line says *with your context*, so nobody
reads "expected during a migration" without seeing where that came from.
Re-diagnosing again starts from the last note.

It goes through the same redaction as the logs before it is stored or sent,
since people paste log lines and log lines carry tokens, and it is kept to the
first 2000 characters. A diagnosis that rests on a note is **not cached**:
"I am restoring a backup right now" explains today's failure, and would be
wrong about next week's identical one.

## Logs, scoped to an incident

The tail captured at detection is frozen, which answers "what did it say?" but
not "what is it doing now?" or "what else broke at that moment?". Two buttons on
each incident:

- **Fetch live tail** — the container's last 300 lines, read now.
- **±5 min around detection** — the same container's logs bounded to the window
  around the incident, *plus* every other watched container that logged an error
  in that window, and any other incidents opened in it. That correlation is the
  useful part: "memos went unhealthy" reads very differently when postgres was
  throwing connection errors in the same minute.

Both go through the same redaction as everything else, and both are refused
outright unless `DOCTOR_WEB_PASSWORD` is set — the read-only endpoints fall
through without auth, live log content must not. `doctor.enable=false` excludes
a container here too; the label would mean little if it only applied to
automatic reads.

**This is not a log browser, deliberately.** For browsing, searching and
tailing everything, run [Dozzle](https://dozzle.dev/) — it does regex search,
multi-container merge, split view and SQL queries in under 15MB, and it does
them better than a side feature here would. The reason to keep it out of the
doctor is not effort but exposure: a live unredacted feed of every container on
a public hostname undoes the one thing this design is careful about, and the
stacks in `tools/` pass bearer tokens and app keys as environment variables.
Dozzle keeps that decision in its own service, with its own auth, made on
purpose.

## Taking it further with your own agent

The doctor's diagnosis is one shot: one prompt, one structured answer, no way
to ask what it would want to see next. That is the right shape for deciding
whether to restart something unattended, and the wrong one for digging in. So
every incident has **Copy for an LLM**, which puts the whole case on the
clipboard as Markdown for an agent you run yourself — Claude Code, a local
model, whatever has your shell — and **Download .md** for one that reads files.

What it carries:

- **A brief for the agent**: what the host is (see [Which host](/container-doctor/getting-started/installation/#which-host)), Compose stacks - managed and
  deployed as `DOCTOR_DOCKER_MANAGER` and `DOCTOR_DEPLOY_METHOD` say - so fixes
  are compose-file edits; treat the diagnosis as a hypothesis; conclude from the
  evidence when it is enough; read the repository and run read-only commands
  itself where it can, and ask you only for what it cannot reach and would change
  its answer; nothing destructive without a yes.
- **The incident** — container, rule, when, how often — and a link back to it.
- **The doctor's diagnosis** in full, and your note if you added one.
- **What was decided and done**, with every reason.
- **The container now**, and whether its image is behind.
- **The evidence exactly as the doctor's model was shown it** — the same text,
  your note put back in — and the instructions that model worked under, marked
  as not applying to the agent.

It is built from what the doctor already holds, so it is behind the same check
as reading the incident, and it contains nothing that has not been through
redaction on the way into the database: no live log is read. The brief still
says to read it through before pasting it anywhere outside your network,
because pattern redaction is a strong default, not a guarantee.
