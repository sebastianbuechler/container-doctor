"""Send one synthetic incident to the configured model and print the result.

The point is to make swapping vendors a 30-second loop instead of "change the
config, redeploy, wait for something to actually break":

    docker compose exec container-doctor python -m app.selftest

It uses the same prompt, schema and coercion as a real diagnosis, so if this
prints a sensible answer the provider is wired up correctly. It does not touch
Docker, the database or the spend meter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

from .config import load_settings
from .diagnoser import Diagnoser
from .redact import redact_obj

# A container failing its health check because a dependency is down. The right
# answer is notify_only, or a restart with a caveat; a model that confidently
# says `prune_images` is telling you something useful about itself.
SAMPLE_INCIDENT = {
    "kind": "unhealthy",
    "summary": "memos has been failing its health check for two checks running",
    "detected_at": "2026-09-24T09:14:03+00:00",
    "details": {"source": "poll", "consecutive_polls": 2},
    "container": {
        "name": "memos",
        "image": "neosmemo/memos:stable",
        "state": "running",
        "health": "unhealthy",
        "exit_code": 0,
        "restart_count": 2,
        "oom_killed": False,
        "started_at": "2026-09-24T08:02:11Z",
        "finished_at": "",
        "restart_policy": "unless-stopped",
    },
    "stats": {"cpu_pct": 4.2, "cpu_host_pct": 1.1, "mem_pct": 31.0,
              "mem_usage_bytes": 330_000_000, "mem_limit_bytes": 1_073_741_824},
    "health_log": "exit=1 output=curl: (7) Failed to connect to localhost port 5230",
    "history": "2026-09-24T08:02:11+00:00 restart_loop: memos restarted 3 times in 15 minutes",
    "logs": (
        "2026-09-24T09:13:58Z INFO  starting memos v0.24.1\n"
        "2026-09-24T09:13:59Z ERROR failed to open database: connection refused (db:5432)\n"
        "2026-09-24T09:14:00Z ERROR retrying in 5s (attempt 4)\n"
        "2026-09-24T09:14:01Z ERROR failed to open database: connection refused (db:5432)\n"
        "2026-09-24T09:14:02Z FATAL giving up after 5 attempts\n"
    ),
}


async def main() -> int:
    settings = load_settings()
    # Which parameters the vendor refused, and what it finally accepted.
    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    print(f"provider : {settings.llm_provider}")
    print(f"model    : {settings.model}")
    if settings.llm_provider == "openai":
        print(f"base_url : {settings.openai_base_url or '(unset)'}")
    print()

    if not settings.llm_enabled:
        print("FAIL: no credentials configured for this provider.")
        if settings.llm_provider == "anthropic":
            print("      Set ANTHROPIC_API_KEY.")
        else:
            print("      Set DOCTOR_OPENAI_BASE_URL (and DOCTOR_OPENAI_API_KEY).")
        return 2

    diagnoser = Diagnoser(settings)
    started = time.monotonic()
    try:
        # redact_obj mirrors what the supervisor does before anything is sent.
        diagnosis, usage = await diagnoser.diagnose(redact_obj(SAMPLE_INCIDENT))
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        await diagnoser.aclose()
    elapsed = time.monotonic() - started

    print(json.dumps(diagnosis, indent=2))
    print()
    print(f"latency       : {elapsed:.1f}s")
    print(f"tokens        : {usage['input_tokens']} in / {usage['output_tokens']} out"
          + (f" ({usage['cached_tokens']} cached)" if usage["cached_tokens"] else ""))
    if usage["cost_usd"]:
        print(f"cost          : ${usage['cost_usd']:.5f} per incident")
        print(f"                ~${usage['cost_usd'] * 30:.2f} for 30 incidents")
    else:
        print("cost          : $0.00 (free tier, or no price configured)")
        print("                set DOCTOR_PRICE_IN_PER_MTOK / _OUT_PER_MTOK to meter a paid model")

    problems = []
    if not diagnosis["root_cause"]:
        problems.append("empty root_cause")
    if diagnosis["confidence"] <= 0:
        problems.append("zero confidence")
    if diagnosis["recommended_action"] == "prune_images":
        problems.append("proposed prune_images for a dependency failure, which is nonsense")
    if not diagnosis["human_fix_steps"] and diagnosis["recommended_action"] == "notify_only":
        problems.append("notify_only with no steps for the operator")

    print()
    if problems:
        print("USABLE? questionable - " + "; ".join(problems))
        print("This model answers, but read a few real diagnoses before trusting it.")
    else:
        print("USABLE? looks good - schema honoured, reasoning grounded in the evidence.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
