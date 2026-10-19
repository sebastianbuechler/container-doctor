"""The model's side of the demo: canned answers in the shape the real one
gives, so diagnoses, the briefing, the call log and the spend meter all fill
in through the doctor's own code."""

from __future__ import annotations

import asyncio
import json
import random
import re

DIAGNOSES = {
    ("paperless-db", "unhealthy"): {
        "summary": "Postgres for paperless keeps failing its health check after being "
                   "killed at its 256 MiB memory limit; a restart clears the stuck recovery.",
        "root_cause": "paperless-db runs with mem_limit 256m and sits at 218 MiB, peaking at "
                      "254 MiB every night at 02:00. The log shows "
                      "\"database system was not properly shut down; automatic recovery in "
                      "progress\" after the last restart: the kernel killed postgres at the "
                      "limit during the nightly index rebuild, and recovery has stalled since "
                      "the replay needs more shared buffers than the limit leaves.",
        "confidence": 0.86, "severity": "critical", "is_transient": False,
        "recommended_action": "restart_container",
        "action_rationale": "A clean restart finishes the recovery with no other load on the "
                            "database; paperless reconnects on its own. It does not fix the "
                            "limit, so it will happen again on the next rebuild.",
        "human_fix_steps": [
            "Approve the restart of paperless-db to get paperless back now.",
            "Raise mem_limit for the db service in the paperless stack to 384m, as the "
            "Memory page suggests from two weeks of peaks.",
            "Redeploy the paperless stack once the limit is changed.",
        ],
        "evidence_used": [
            "health: unhealthy, failing streak 5",
            "memory: 85% of 256 MiB, 254 MiB peak at 02:00",
            "LOG:  database system was not properly shut down; automatic recovery in progress",
        ],
    },
    ("paperless", "restart_loop"): {
        "summary": "paperless restarts because its database is unhealthy; the database is the "
                   "thing to fix.",
        "root_cause": "Every restart ends in \"connection to server at db port 5432 failed: the "
                      "database system is in recovery mode\". paperless itself is fine; it "
                      "depends on db with condition service_healthy and exits when it cannot "
                      "connect.",
        "confidence": 0.91, "severity": "warning", "is_transient": False,
        "recommended_action": "none",
        "action_rationale": "Restarting paperless cannot help while paperless-db is in recovery; "
                            "the proposed restart of paperless-db addresses the cause.",
        "human_fix_steps": ["Resolve the paperless-db incident first; paperless recovers by "
                            "itself once the database accepts connections."],
        "evidence_used": [
            "OperationalError: ... the database system is in recovery mode",
            "depends_on: db (service_healthy)",
            "restart count rose from 4 to 10",
        ],
    },
    ("sonarr", "log_errors"): {
        "summary": "Sonarr's indexer rejects its API key, so no new episodes are found.",
        "root_cause": "Every RSS sync and search fails with HTTP 401 from the Newznab indexer: "
                      "\"API key is invalid or revoked\". Sonarr itself is healthy; the key "
                      "stored in its indexer settings no longer works - indexers revoke keys "
                      "after a password change or an expired membership.",
        "confidence": 0.93, "severity": "warning", "is_transient": False,
        "recommended_action": "notify_only",
        "action_rationale": "A restart keeps the same key and changes nothing; the key has to "
                            "be renewed by hand.",
        "human_fix_steps": [
            "Log in to the indexer and copy a fresh API key.",
            "Paste it in Sonarr under Settings > Indexers, then press Test.",
            "Check Prowlarr if it syncs indexers to Sonarr - update the key there instead.",
        ],
        "evidence_used": [
            "[Error] Newznab: Indexer returned HTTP 401 Unauthorized: API key is invalid or revoked",
            "62 of the last 120 lines at ERROR level",
        ],
    },
    ("host:/srv/data", "disk_filling"): {
        "summary": "The data volume grows about 54 GiB a day and is full in under three weeks.",
        "root_cause": "Growth has been steady for 14 days, matching sabnzbd's completed "
                      "downloads; nothing is cleaning up /srv/data/media/downloads/complete "
                      "after import.",
        "confidence": 0.78, "severity": "warning", "is_transient": False,
        "recommended_action": "notify_only",
        "action_rationale": "Deleting data is never the doctor's call; pruning images frees "
                            "space on / only.",
        "human_fix_steps": [
            "Turn on \"Remove completed downloads\" in Sonarr and Radarr's download client settings.",
            "Clear /srv/data/media/downloads/complete of what has been imported.",
        ],
        "evidence_used": ["54.0 GiB/day over 14 days", "87% used, 940 GiB free"],
    },
    ("host:sdb", "smart_health"): {
        "summary": "sdb has remapped 24 sectors; it still works, but watch it.",
        "root_cause": "SMART attribute 5 (Reallocated_Sector_Ct) is 24 on sdb and 0 on its mirror "
                      "partner sda. A small, stable count is common; a growing one predicts "
                      "failure.",
        "confidence": 0.7, "severity": "warning", "is_transient": False,
        "recommended_action": "notify_only",
        "action_rationale": "A drive is replaced on the host, not by the doctor.",
        "human_fix_steps": ["Check the count again next week; replace the drive if it grows.",
                            "Make sure the backup of /srv/data is current."],
        "evidence_used": ["Reallocated_Sector_Ct raw 24 on sdb"],
    },
}

GENERIC = {
    "summary": "A short-lived problem that cleared on its own.",
    "root_cause": "The evidence shows a brief failure followed by normal operation.",
    "confidence": 0.62, "severity": "info", "is_transient": True,
    "recommended_action": "none", "action_rationale": "Nothing is wrong now.",
    "human_fix_steps": [], "evidence_used": [],
}

SECTIONS = {
    "incidents": {"headline": "",
                  "points": ["paperless-db fails its health check after hitting its memory limit",
                             "A restart of paperless-db is waiting for your approval",
                             "Sonarr's indexer rejects its API key"],
                  "level": "critical"},
    "containers": {"headline": "",
                   "points": ["paperless restarts until its database is back",
                              "paperless-db peaks at its 256 MiB limit every night"],
                   "level": "attention"},
    "storage": {"headline": "/srv/data full in about 17 days",
                "points": ["Growing 54 GiB a day, 940 GiB left",
                           "sdb has 24 reallocated sectors; the mirror is healthy"],
                "level": "attention"},
    "updates": {"headline": "9 images have a newer build or version",
                "points": ["postgres 17.6 is a new major - 16.10 is the safe step",
                           "Jellyfin 10.11 migrates its database: back up first",
                           "Sonarr and Prowlarr have new builds of latest"],
                "level": "info"},
}


def _answer(name: str, prompt: str) -> dict:
    if name == "container_diagnosis":
        container = re.search(r"^name: (.+)$", prompt, re.M)
        kind = re.search(r"^kind: (.+)$", prompt, re.M)
        key = (container.group(1).strip() if container else "",
               kind.group(1).strip() if kind else "")
        return DIAGNOSES.get(key, GENERIC)
    if name == "briefing_section":
        section = re.search(r"^Section: (\w+)", prompt, re.M)
        answer = dict(SECTIONS.get(section.group(1) if section else "", SECTIONS["containers"]))
        first = re.search(r"^Facts:\n- (.+)$", prompt, re.M)
        if first and section and section.group(1) in ("incidents", "containers"):
            answer["headline"] = first.group(1)  # the counts, as the rules worked them out
        return answer
    if name == "briefing_overall":
        refs = dict(re.findall(r"^- ([\w:-]+): (.+)$", prompt.split("## References")[-1], re.M))

        def ref(*words: str) -> str:
            return next((r for r, label in refs.items()
                         if all(w in label.lower() for w in words)), "")
        return {"headline": "Paperless is down until you approve a database restart",
                "level": "critical",
                "priorities": [
                    {"text": "Approve the paperless-db restart", "ref": ref("paperless-db")},
                    {"text": "Renew Sonarr's indexer API key", "ref": ref("sonarr")},
                    {"text": "Clean up completed downloads on /srv/data", "ref": ref("srv")},
                ]}
    return {}


# The classifier's answers, in the Decisions API's shape (choice, confidence
# and probabilities per question; `noul` is the chance a flag is true), keyed
# like the diagnoses. One of each outcome: hidden, raised, lowered and kept.
def _choice(choice: str, probabilities: dict[str, float]) -> dict:
    return {"choice": choice, "confidence": probabilities[choice], "probabilities": probabilities}


CLASSIFICATIONS = {
    # The rules see a burst of ERROR lines; they are sites Uptime Kuma watches
    # being down, which is its job, not a fault of Uptime Kuma. Hidden.
    ("uptime-kuma", "log_errors"): {
        "verdict": _choice("benign_noise", {"benign_noise": 0.94, "expected_transient": 0.03,
                                            "real_fault": 0.02, "unclear": 0.01}),
        "severity": _choice("info", {"info": 0.88, "warning": 0.11, "critical": 0.01}),
        "cause": _choice("network", {"network": 0.71, "dependency": 0.22, "unknown": 0.07}),
        "self_heals": {"noul": 0.83},
    },
    # A database stuck in recovery: worse than the rule's warning. Raised.
    ("paperless-db", "unhealthy"): {
        "verdict": _choice("real_fault", {"real_fault": 0.93, "expected_transient": 0.05,
                                          "benign_noise": 0.01, "unclear": 0.01}),
        "severity": _choice("critical", {"critical": 0.81, "warning": 0.18, "info": 0.01}),
        "cause": _choice("resources", {"resources": 0.68, "storage": 0.14, "config": 0.1,
                                       "unknown": 0.08}),
        "self_heals": {"noul": 0.06},
    },
    # Real, but the restarts follow from the database: lowered to warning.
    ("paperless", "restart_loop"): {
        "verdict": _choice("real_fault", {"real_fault": 0.88, "expected_transient": 0.09,
                                          "benign_noise": 0.02, "unclear": 0.01}),
        "severity": _choice("warning", {"warning": 0.92, "critical": 0.07, "info": 0.01}),
        "cause": _choice("dependency", {"dependency": 0.9, "config": 0.05, "unknown": 0.05}),
        "self_heals": {"noul": 0.31},
    },
    # Real, and as serious as the rule said. Kept as it is.
    ("sonarr", "log_errors"): {
        "verdict": _choice("real_fault", {"real_fault": 0.86, "benign_noise": 0.08,
                                          "expected_transient": 0.04, "unclear": 0.02}),
        "severity": _choice("warning", {"warning": 0.79, "critical": 0.12, "info": 0.09}),
        "cause": _choice("auth", {"auth": 0.87, "config": 0.08, "unknown": 0.05}),
        "self_heals": {"noul": 0.04},
    },
}

UNCLEAR = {
    "verdict": _choice("unclear", {"unclear": 0.55, "real_fault": 0.3, "benign_noise": 0.15}),
    "severity": _choice("warning", {"warning": 0.6, "info": 0.3, "critical": 0.1}),
    "cause": _choice("unknown", {"unknown": 0.8, "config": 0.2}),
    "self_heals": {"noul": 0.5},
}


def classification(state: str) -> dict:
    container = re.search(r"^name: (.+)$", state, re.M)
    kind = re.search(r"^kind: (.+)$", state, re.M)
    key = (container.group(1).strip() if container else "", kind.group(1).strip() if kind else "")
    return CLASSIFICATIONS.get(key, UNCLEAR)


def patch_classifier(decision_module) -> None:
    """The decision model's endpoint: everything before and after the HTTP
    call - the questions, the guard rails, the call log - is the doctor's."""
    async def post(self, body):
        await asyncio.sleep(random.uniform(0.3, 0.9))
        tokens_in = len(body["state"]) // 4 + 600
        return {"model": self.name, "answers": classification(body["state"]),
                "usage": {"input_tokens": tokens_in, "output_tokens": 24,
                          "cost": tokens_in * self.price_in / 1_000_000}}

    decision_module.OpenRouterDecisions._post = post


def patch(diagnoser_module) -> None:
    async def structured_anthropic(self, *, system, prompt, schema, name, model, effort,
                                   max_tokens):
        await asyncio.sleep(random.uniform(0.6, 1.8))
        result = _answer(name, prompt)
        input_tokens = (len(system) + len(prompt)) // 4
        output_tokens = len(json.dumps(result)) // 4 + random.randint(200, 900)
        cached = len(system) // 4 if random.random() < 0.6 else 0
        return result, {
            "input_tokens": input_tokens, "output_tokens": output_tokens, "cached_tokens": cached,
            "cost_usd": diagnoser_module._cost(model, input_tokens, output_tokens, cached,
                                               self.settings.custom_pricing),
            "model": model,
        }

    diagnoser_module.Diagnoser._structured_anthropic = structured_anthropic
