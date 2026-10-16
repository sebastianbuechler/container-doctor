"""Scrub secrets out of anything that leaves the host, and normalise log noise.

Homelab container logs routinely carry bearer tokens, DSN strings and API keys
(Speedtest Tracker, for one, takes its bearer token as an env var),
so redaction runs before evidence is persisted *or* sent to an LLM.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REDACTED = "[redacted]"

# Names that mean "this value is a secret" when they appear as key=value,
# "key": "value", key: value or --key value.
_SECRET_NAME = (
    r"(?:pass(?:word|wd)?|secret|token|api[_-]?key|apikey|auth|credential|"
    r"private[_-]?key|access[_-]?key|client[_-]?secret|session|cookie|bearer|"
    r"dsn|connection[_-]?string|salt|signature)"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # key=value / key: value / "key": "value" / --key value
    (re.compile(rf'(?i)\b({_SECRET_NAME})(\s*[:=]\s*|"\s*:\s*"|\s+)([^\s,;"\'}}\]]{{4,}})'),
     rf"\g<1>\g<2>{REDACTED}"),
    # Authorization headers
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/=]{8,}"), rf"\g<1> {REDACTED}"),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"), REDACTED),
    # Credentials embedded in URLs
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@"), rf"\g<1>\g<2>:{REDACTED}@"),
    # Common vendor key shapes
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}\b"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), REDACTED),
    # PEM blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), REDACTED),
)

# Env var names that are safe (and useful) to show a diagnostician.
_ENV_ALLOWLIST = re.compile(
    r"(?i)^(PATH|HOME|HOSTNAME|LANG|LC_[A-Z_]+|TZ|PUID|PGID|UMASK|"
    r"NODE_ENV|PYTHONUNBUFFERED|.*_PORT|.*_HOST|.*_URL|.*_DRIVER|.*_LEVEL|"
    r".*_TIMEOUT|.*_INTERVAL|.*_SCHEDULE|.*_ENABLED?)$"
)


def redact(text: str) -> str:
    """Remove secret-looking material from a blob of text."""
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_obj(value: Any) -> Any:
    """Recursively redact every string in a nested structure.

    This is the choke point: the whole evidence bundle goes through it before
    it is stored or sent anywhere, so a future detector that stashes raw log
    text in `details` cannot quietly reintroduce a leak.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_obj(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(item) for item in value]
    return value


def env_value_shown(name: str) -> bool:
    """Whether an env var's value is one worth showing (after `redact`)."""
    return bool(_ENV_ALLOWLIST.match(name))


def secret_name(name: str) -> bool:
    """Whether a key's name says its value is a secret - `DB_PASSWORD`, or a
    label like `traefik.http.middlewares.x.basicauth.users`."""
    return bool(re.search(_SECRET_NAME, name, re.I)) or name.lower().endswith(".users")


def redact_env(env: list[str] | None) -> list[str]:
    """Keep only allowlisted env vars; mask the value of everything else."""
    out: list[str] = []
    for entry in env or []:
        name, _, value = entry.partition("=")
        if _ENV_ALLOWLIST.match(name):
            out.append(f"{name}={redact(value)}")
        else:
            out.append(f"{name}={REDACTED}" if value else name)
    return out



_NOISE: tuple[tuple[re.Pattern[str], str], ...] = (
    # ISO-ish timestamps, syslog timestamps, and bare clock times
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"), "<ts>"),
    # UUIDs and long hex blobs (container ids, request ids)
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{12,64}\b", re.I), "<hex>"),
    # IP:port and bare numbers
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h|MB|GB|KB|B)\b"), "<n>"),
    (re.compile(r"\b\d+\b"), "<n>"),
)


def normalize(text: str) -> str:
    """Collapse per-run variance so repeats of one failure hash identically."""
    for pattern, replacement in _NOISE:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text).strip()


# What a failure is *about* varies per line even when the failure is the same:
# Jellyfin's library scan logs one "Error probing <file>" per broken movie, and
# which movies land in the log tail changes from poll to poll. Hashing those
# names gave every scan a fresh fingerprint - 60-odd open incidents for one
# condition. These are stripped before a line joins a fingerprint.
_SUBJECTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I), "<url>"),
    # Quoted first: a quoted path may contain spaces.
    (re.compile(r'"[^"]*"'), "<str>"),
    # Not an apostrophe: "Couldn't add report" is the message itself.
    (re.compile(r"(?<!\w)'[^']*'"), "<str>"),
    # An unquoted absolute path, spaces and all, up to the ": " that
    # ffprobe puts after it or the end of the line - movie names hold commas
    # and parentheses, so nothing short of that is a safe place to stop.
    (re.compile(r"(?<![\w/:.])/[^\s/]+/.*?(?=:\s|$)", re.M), "<path>"),
    (re.compile(r"\b[A-Za-z]:\\.*?(?=:\s|$)", re.M), "<path>"),
)


def line_template(line: str) -> str:
    """A log line with its timestamps, ids, numbers and subjects taken out:
    what kind of line it is, not which file or request it was about."""
    for pattern, replacement in _SUBJECTS:
        line = pattern.sub(replacement, line)
    return normalize(line)


def dominant_templates(lines: list[str], limit: int = 1) -> list[str]:
    """The most common line templates, most common first.

    A fingerprint built from the first few lines of a tail changes whenever
    the mix of lines does; the commonest shape of line barely moves.
    """
    counts: dict[str, int] = {}
    for line in lines:
        template = line_template(line)
        if template:
            counts[template] = counts.get(template, 0) + 1
    ranked = sorted(counts, key=lambda t: (-counts[t], t))
    return ranked[:limit]


def fingerprint(*parts: str) -> str:
    """Stable id for 'this kind of failure on this container'."""
    joined = "␟".join(normalize(p or "") for p in parts)
    return hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()[:20]


# None of these apps can be made to log JSON, but all put a level on the line.
# Keyed on the delimiters rather than each layout - note SABnzbd's `::` and
# Serilog's three-letter abbreviations:
#
#   jellyfin (Serilog) [16:08:22] [ERR] [54] Source: message
#   sonarr/radarr      2026-09-24 16:08:22.1|Error|Source|message
#   sabnzbd            2026-09-24 16:08:22,527::ERROR::[mod:55] message
#   seerr (winston)    2026-09-24 16:08:22 error: message
# The benign levels are listed too, and the FIRST level token on the line wins.
# Without them the scan reads straight past `INFO` and finds a level word in the
# message instead - Stirling-PDF's "Resource status changed from OK to CRITICAL"
# is an INFO line, and was being counted as a fatal every single poll.
_LEVEL_RE = re.compile(
    r"(?:^|[\s|\[:])"
    r"(?P<level>FATAL|CRITICAL|CRIT|FTL|ERROR|ERR|WARNING|WARN|WRN"
    r"|INFO|INF|NOTICE|DEBUG|DBG|DBUG|TRACE|TRC|VERBOSE|FINE|LOG|STATUS)"
    r"(?=[\]|:\s]|$)",
    re.IGNORECASE | re.MULTILINE,
)

_FATAL_LEVELS = {"fatal", "critical", "crit", "ftl", "panic", "emergency", "alert"}
_ERROR_LEVELS = {"error", "err"}
_WARNING_LEVELS = {"warning", "warn", "wrn"}

# Apps that do log JSON carry the level as a field, where the scan above never
# sees it: the value is quoted, so `"level":"ERROR"` has no delimiter it knows.
#
#   pocket-id (slog)   {"time":"...","level":"ERROR","msg":"..."}
#   pino               {"level":50,"time":...,"msg":"..."}
#   zap / ECS          {"L":"ERROR",...} / {"log.level":"error",...}
_JSON_LEVEL_KEYS = ("level", "lvl", "severity", "log.level", "levelname", "L")
# pino and bunyan number their levels; 50 is error, 60 fatal.
_NUMERIC_LEVELS = ((60, "fatal"), (50, "error"), (40, "warning"))


def _json_record(line: str) -> dict[str, Any] | None:
    """The JSON object a log line carries, or None if it is not one.

    Docker's timestamp comes first, so the object starts at the first brace.
    """
    start = line.find("{")
    if start < 0 or not line.endswith("}"):
        return None
    try:
        record = json.loads(line[start:])
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def json_level(line: str) -> str | None:
    """The level field of a JSON log line, lowercased, or None if it has none."""
    record = _json_record(line)
    if record is None:
        return None
    for key in _JSON_LEVEL_KEYS:
        value = record.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return next((name for floor, name in _NUMERIC_LEVELS if value >= floor), "info")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None

# A request line and the status code that follows it, as Traefik, nginx and
# Caddy all emit. Matching this matters because an access log quotes the
# client's URL, and a proxy in front of a log viewer will faithfully record
# `?levels=error&levels=fatal` as part of the request - words that say nothing
# about the proxy's own health. The status code is the only part that does.
_ACCESS_LOG_RE = re.compile(
    r"\"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|CONNECT|TRACE) "
    r"[^\"]*HTTP/[\d.]+\"\s+(?P<status>\d{3})(?:\s|$)"
)


def access_log_status(line: str) -> int | None:
    """The HTTP status of an access-log line, or None if it is not one.

    Traefik's JSON access log carries the status as `DownstreamStatus`, next
    to a `"level": "info"` that says nothing about the response.
    """
    match = _ACCESS_LOG_RE.search(line)
    if match:
        return int(match.group("status"))
    if '"DownstreamStatus"' not in line:
        return None
    record = _json_record(line)
    status = record.get("DownstreamStatus") if record else None
    if isinstance(status, int) and not isinstance(status, bool) and "RequestMethod" in record:
        return status
    return None


def leveled_lines(
    log_text: str,
    ignore: re.Pattern[str] | None = None,
    limit: int = 6,
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Count log lines by severity, and keep a few samples of each.

    `ignore` drops lines a container is known to emit harmlessly - Jellyfin's
    studio-thumbnail fetcher, say - before they are counted, so suppressing
    noise does not also suppress the count that would page you.
    """
    counts = {"fatal": 0, "error": 0, "warning": 0}
    samples: dict[str, list[str]] = {"fatal": [], "error": [], "warning": []}

    for line in log_text.splitlines():
        stripped = line.strip()
        if not stripped or (ignore is not None and ignore.search(stripped)):
            continue
        status = access_log_status(stripped)
        if status is not None:
            # Judge a request by its status, never by words in the URL.
            if status < 500:
                continue
            bucket = "error"
        else:
            level = json_level(stripped)
            if level is None:
                match = _LEVEL_RE.search(stripped)
                if not match:
                    continue
                level = match.group("level").lower()
            if level in _FATAL_LEVELS:
                bucket = "fatal"
            elif level in _ERROR_LEVELS:
                bucket = "error"
            elif level in _WARNING_LEVELS:
                bucket = "warning"
            else:
                continue  # the line's own level is benign; the rest is message
        counts[bucket] += 1
        if len(samples[bucket]) < limit:
            samples[bucket].append(redact(stripped)[:500])
    return counts, samples


def error_lines(log_text: str, pattern: re.Pattern[str], limit: int = 12) -> list[str]:
    """Pull the lines that actually look like errors out of a log tail.

    Redacted here rather than later: these lines feed both the incident
    fingerprint and the evidence bundle, and a rotating token in an error line
    would otherwise give the same failure a different fingerprint every time.
    """
    hits: list[str] = []
    for line in log_text.splitlines():
        status = access_log_status(line)
        if status is not None and status < 500:
            continue  # the keywords are in the client's URL, not the app's
        if pattern.search(line):
            hits.append(redact(line.strip())[:500])
            if len(hits) >= limit:
                break
    return hits
