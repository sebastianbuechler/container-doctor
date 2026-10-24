"""Configuration for Container Doctor.

Everything is driven by environment variables so the whole stack can be
configured from the stack environment of whatever deploys it (Portainer,
Coolify, a .env file next to the compose file...). Per-container overrides
come from Docker labels (see `ContainerPolicy.from_labels`).
"""

from __future__ import annotations

import contextvars
import os
import re
from dataclasses import dataclass, field, fields
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

# A setting with a closed set of values is an enum, and the enum is the one
# list of them: validation reads it, and so does the Settings page, which shows
# every member with the one in use highlighted (CHOICES_FOR). A member added
# here appears there with nothing else to change.


class Mode(StrEnum):
    OBSERVE = "observe"
    SUGGEST = "suggest"
    AUTO = "auto"


class Action(StrEnum):
    """The LLM may only pick from these; anything else is coerced to notify_only."""
    NONE = "none"
    NOTIFY_ONLY = "notify_only"
    RESTART_CONTAINER = "restart_container"
    STOP_CONTAINER = "stop_container"
    PRUNE_IMAGES = "prune_images"


class HostKind(StrEnum):
    """What the doctor runs on; auto reads it from the kernel (app/hostkind.py)."""
    AUTO = "auto"
    SYNOLOGY = "synology"
    GENERIC = "generic"


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class Effort(StrEnum):
    """How hard an Anthropic model thinks per call."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ClassifierMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


class ClassifierProvider(StrEnum):
    OPENROUTER = "openrouter"
    CHAT = "chat"


class Switch(StrEnum):
    """How a plain on/off setting reads on the Settings page."""
    ON = "on"
    OFF = "off"


def _values(enum: type[StrEnum]) -> tuple[str, ...]:
    return tuple(member.value for member in enum)


def _or_list(enum: type[StrEnum]) -> str:
    """"a, b or c", for a warning that names the values allowed."""
    *head, last = _values(enum)
    return f"{', '.join(head)} or {last}" if head else last


VALID_MODES: tuple[str, ...] = _values(Mode)
# A multiple-choice row with nothing chosen.
NOTHING = "nothing"
VALID_ACTIONS: tuple[str, ...] = _values(Action)

# Actions that are safe enough to run unattended once the policy engine agrees.
DEFAULT_AUTO_ACTIONS: tuple[str, ...] = ("restart_container",)


# Values saved from the Settings page, while Settings.resolve() builds one:
# read where the environment has nothing for a setting.
_SAVED: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "saved", default=MappingProxyType({}))


def _env(name: str, default: str = "") -> str:
    """The environment's value, else a saved one, else `default`.

    Empty is unset: a compose file passing every setting writes ${X:-}.
    """
    return (os.environ.get(name, "").strip() or _SAVED.get().get(name, "").strip()
            or default)


# Values a person might plausibly type. Quotes are stripped because an
# environment variable set through a UI often keeps them, and `"true"` silently
# reading as false is the kind of thing nobody finds by looking at the config.
_TRUE_WORDS = frozenset({"1", "true", "yes", "y", "on", "enabled"})
_FALSE_WORDS = frozenset({"0", "false", "no", "n", "off", "disabled"})

def _bool_word(name: str) -> str:
    return _env(name).strip("\"'").strip().lower()


def _env_bool(name: str, default: bool) -> bool:
    word = _bool_word(name)
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    return default  # unset, or unreadable - validate() reports the latter


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _none_empty(items: list[str]) -> list[str]:
    """`none` empties a list setting that has a default."""
    return [] if [i.lower() for i in items] == ["none"] else items


def _env_list(name: str, default: tuple[str, ...] = ()) -> list[str]:
    raw = _env(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# Passwords nobody chose: what an example file says, or a guess. One of these
# is no sign-in at all, so it is treated as unset.
PLACEHOLDER_PASSWORDS = frozenset({
    "change-me", "changeme", "change_me", "password", "admin", "doctor", "secret", "123456",
})
SHORT_PASSWORD = 12


def _placeholder_password(value: str) -> bool:
    return (value.strip().lower() in PLACEHOLDER_PASSWORDS
            or value == _env("DOCTOR_WEB_USER", "doctor"))


def _refuse_placeholder(value: str) -> str:
    return "" if _placeholder_password(value) else value


def _validate_mode(value: str) -> Mode:
    value = value.lower()
    return Mode(value) if value in VALID_MODES else Mode.OBSERVE


# Words that turn off a setting whose default is on, where empty would not:
# empty is the same as unset, and a compose file may pass every variable.
_OFF_WORDS = frozenset({"off", "none"})


@dataclass(frozen=True)
class Spec:
    """One setting: the variable it is read from, its type and its default.

    Every Settings field is declared with setting(), which records this in the
    field's metadata - so the variable names, defaults, choices and secrets are
    listed once, next to the field, and everything else reads them from there
    (SETTINGS, SECRET_ENV, BOOLEAN_ENV_VARS, CHOICES_FOR).
    """
    env: str
    default: Any
    kind: str  # str, int, float, bool or list
    choices: type[StrEnum] | None = None
    secret: bool = False
    # A setting whose default is on, that "off" (or "none") turns off.
    can_be_off: bool = False
    # Read from the environment only, never saved from the page: secrets, and
    # what is needed before the database is open or the web server starts.
    env_only: bool = False
    # Read once, when the doctor starts - into a connection pool, a client,
    # sign-in, or whether a loop runs at all - so a change waits for a
    # restart. Every other setting applies the moment it is saved; see
    # Supervisor.apply_settings and tests/test_live_settings.py.
    restart: bool = False


def setting(env: str, default: Any, *, choices: type[StrEnum] | None = None,
            secret: bool = False, can_be_off: bool = False, env_only: bool = False,
            restart: bool = False, convert: Any = None) -> Any:
    """A Settings field read from `env`, falling back to `default`.

    Empty is unset: a compose file passing every variable writes `${X:-}`, so
    an empty value means nobody set it. A str setting with choices is read
    lowercased; a list is comma-separated, and "none" empties it. `convert`
    turns the value read into the field's value.
    """
    if isinstance(default, bool):
        kind = "bool"
    elif isinstance(default, (tuple, list)):
        kind = "list"
    else:
        kind = type(default).__name__
    spec = Spec(env=env, default=default, kind=kind, choices=choices, secret=secret,
                can_be_off=can_be_off, env_only=env_only or secret, restart=restart)

    def read() -> Any:
        if kind == "bool":
            value: Any = _env_bool(env, default)
        elif kind == "int":
            value = _env_int(env, default)
        elif kind == "float":
            value = _env_float(env, default)
        elif kind == "list":
            value = _none_empty(_env_list(env, tuple(default)))
        else:
            value = _env(env) or default
            if can_be_off and value.lower() in _OFF_WORDS:
                value = ""
            elif choices is not None:
                value = value.lower()
        return convert(value) if convert else value

    return field(default_factory=read, metadata={"spec": spec})


@dataclass(frozen=True)
class Settings:
    # The Docker endpoint: the socket proxy, never the raw socket. The compose
    # file sets it from DOCTOR_DOCKER_HOST.
    docker_host: str = setting("DOCKER_HOST", "tcp://doctor-socket-proxy:2375", env_only=True)

    mode: Mode = setting("DOCTOR_MODE", "observe", choices=Mode, convert=_validate_mode)
    require_opt_in: bool = setting("DOCTOR_REQUIRE_OPT_IN", False)
    ignore_containers: list[str] = setting("DOCTOR_IGNORE_CONTAINERS", ())
    auto_actions: list[str] = setting("DOCTOR_AUTO_ACTIONS", DEFAULT_AUTO_ACTIONS, choices=Action)
    min_confidence: float = setting("DOCTOR_MIN_CONFIDENCE", 0.75)

    poll_interval_seconds: int = setting("DOCTOR_POLL_INTERVAL", 30)
    # Containers read at once during a poll. `docker stats` takes a second or
    # two per container, so one at a time made a poll of 30 run over a minute.
    poll_concurrency: int = setting("DOCTOR_POLL_CONCURRENCY", 6, restart=True)
    # Share of the WHOLE host, not of a single core (see docker_client._parse_stats).
    cpu_threshold_pct: float = setting("DOCTOR_CPU_THRESHOLD", 85.0)
    mem_threshold_pct: float = setting("DOCTOR_MEM_THRESHOLD", 90.0)
    # Consecutive polls a breach must persist for, so one backup spike is quiet.
    resource_breach_polls: int = setting("DOCTOR_RESOURCE_BREACH_POLLS", 4)
    restart_loop_count: int = setting("DOCTOR_RESTART_LOOP_COUNT", 3)
    restart_loop_window_minutes: int = setting("DOCTOR_RESTART_LOOP_WINDOW_MINUTES", 15)
    log_lines: int = setting("DOCTOR_LOG_LINES", 120)
    # Ignore log lines older than this. A quiet container's `tail` reaches back
    # as far as it has been running, and a FATAL line from months ago would
    # otherwise read as a live failure.
    log_max_age_minutes: int = setting("DOCTOR_LOG_MAX_AGE_MINUTES", 60)
    # A log line matching this counts as an error, whatever its level says.
    log_error_pattern: str = setting(
        "DOCTOR_LOG_ERROR_PATTERN",
        r"(?i)\b(fatal|panic|traceback|segfault|out of memory|oomkill|"
        r"connection refused|no such host|permission denied|disk full|"
        r"no space left|deadlock|unhandled exception)\b",
    )
    log_error_burst: int = setting("DOCTOR_LOG_ERROR_BURST", 5)
    # Higher than the keyword burst: an ERROR line is weaker evidence than
    # "FATAL" or "no space left".
    log_level_error_burst: int = setting("DOCTOR_LOG_LEVEL_ERROR_BURST", 10)
    # A single FATAL/CRITICAL line is worth an incident on its own.
    log_fatal_threshold: int = setting("DOCTOR_LOG_FATAL_THRESHOLD", 1)
    # Global; for one container's noise prefer the doctor.ignore_log label.
    log_ignore_pattern: str = setting("DOCTOR_LOG_IGNORE_PATTERN", "")
    # After a redeploy - containers stopped on request, destroyed, created -
    # log lines and failing health checks from this long are not counted.
    deploy_settle_minutes: float = setting("DOCTOR_DEPLOY_SETTLE_MINUTES", 3.0)
    # A nonzero exit nobody asked for waits this long before it is a crash:
    # if the container was replaced meanwhile, it was a redeploy.
    die_grace_seconds: float = setting("DOCTOR_DIE_GRACE_SECONDS", 30.0)
    disk_paths: list[str] = setting("DOCTOR_DISK_PATHS", ())
    # A storage host that is simply well used sits high for years; 90% pestered about a
    # disk with terabytes free.
    disk_threshold_pct: float = setting("DOCTOR_DISK_THRESHOLD", 95.0)
    # How far back the fill-rate is fitted, and how close to full is worth
    # saying something about. A percentage says how full; these say whether it
    # is going anywhere.
    disk_trend_window_days: int = setting("DOCTOR_DISK_TREND_WINDOW_DAYS", 14)
    disk_forecast_days: int = setting("DOCTOR_DISK_FORECAST_DAYS", 21)
    disk_forecast_critical_days: int = setting("DOCTOR_DISK_FORECAST_CRITICAL_DAYS", 5)
    # How often a disk's free space is recorded for the fill forecast.
    disk_sample_minutes: int = setting("DOCTOR_DISK_SAMPLE_MINUTES", 60)
    # Where the same condition stops being a warning and becomes critical.
    disk_critical_pct: float = setting("DOCTOR_DISK_CRITICAL", 98.0)
    # How many newly corrected read errors in a day turn a drive's warning
    # critical. The first one is always a warning; this is "and it is getting
    # worse". md itself fails a member at 20 corrected errors that come
    # faster than its hourly halving drains them; 0 never escalates.
    raid_errors_per_day: int = setting("DOCTOR_RAID_ERRORS_PER_DAY", 10)
    # How long a drive's newly corrected read errors keep its warning up.
    # md's own count only goes back to 0 on a reboot, so without a window a
    # drive that had a bad day would warn until the next one.
    raid_errors_days: int = setting("DOCTOR_RAID_ERRORS_DAYS", 7, convert=lambda v: max(1, v))

    # Circuit breakers: at most this many actions on one container, and on the
    # whole host, in any hour. doctor.max_actions_per_hour tightens the first.
    max_actions_per_container_per_hour: int = setting(
        "DOCTOR_MAX_ACTIONS_PER_CONTAINER_PER_HOUR", 3)
    max_actions_global_per_hour: int = setting("DOCTOR_MAX_ACTIONS_GLOBAL_PER_HOUR", 10)
    verify_delay_seconds: int = setting("DOCTOR_VERIFY_DELAY", 45)
    # Blocks re-running the same action for the same fingerprint.
    action_cooldown_minutes: int = setting("DOCTOR_ACTION_COOLDOWN_MINUTES", 30)

    llm_provider: str = setting("DOCTOR_LLM_PROVIDER", "anthropic", choices=LLMProvider, restart=True)
    anthropic_api_key: str = setting("ANTHROPIC_API_KEY", "", secret=True)
    model: str = setting("DOCTOR_MODEL", "claude-opus-5-5")
    effort: str = setting("DOCTOR_EFFORT", "medium", choices=Effort)
    # Cap on the tokens one diagnosis may answer with.
    max_output_tokens: int = setting("DOCTOR_MAX_OUTPUT_TOKENS", 4000)
    # OpenAI-compatible endpoint (Ollama, LM Studio, OpenRouter, vLLM, ...).
    # Environment-only, like every URL a secret is sent to: a setting saved
    # from the page could otherwise send the key to a host of its choosing.
    openai_base_url: str = setting("DOCTOR_OPENAI_BASE_URL", "", env_only=True)
    openai_api_key: str = setting("DOCTOR_OPENAI_API_KEY", "not-needed", secret=True)
    daily_usd_budget: float = setting("DOCTOR_DAILY_USD_BUDGET", 1.0)
    # The cap that matters on a free tier, which meters requests not dollars.
    daily_call_limit: int = setting("DOCTOR_DAILY_CALL_LIMIT", 0)
    diagnosis_cache_hours: int = setting("DOCTOR_DIAGNOSIS_CACHE_HOURS", 6)
    # How long a recurrence folds into the open incident instead of opening a
    # new one. Measured from when it was last seen, so a condition that keeps
    # firing stays a single incident for as long as it lasts.
    incident_dedup_hours: int = setting("DOCTOR_INCIDENT_DEDUP_HOURS", 24)
    # USD per million tokens. Unset prices Anthropic models from a built-in
    # table and counts anything else as free - hence the call cap above.
    price_in_per_mtok: float = setting("DOCTOR_PRICE_IN_PER_MTOK", -1.0)
    price_out_per_mtok: float = setting("DOCTOR_PRICE_OUT_PER_MTOK", -1.0)
    # Free tiers rate-limit hard (Gemini free: 10/minute); retry rather than
    # lose the diagnosis.
    llm_max_retries: int = setting("DOCTOR_LLM_MAX_RETRIES", 3)
    llm_timeout_seconds: float = setting("DOCTOR_LLM_TIMEOUT", 180.0)
    # Negative never sends it, which reasoning models require (GPT-6 Luna
    # rejects it outright); the request path also drops it if a vendor objects.
    llm_temperature: float = setting("DOCTOR_LLM_TEMPERATURE", 0.2)

    # ntfy server to publish to, e.g. https://ntfy.sh or http://ntfy:80. Empty
    # sends no notifications.
    ntfy_url: str = setting("DOCTOR_NTFY_URL", "", env_only=True)
    # The ntfy topic notifications are published to. No default: a topic on a
    # public server is readable by anyone who guesses its name, so it has to
    # be chosen - long and random on ntfy.sh.
    ntfy_topic: str = setting("DOCTOR_NTFY_TOPIC", "")
    # Access token for a protected ntfy topic.
    ntfy_token: str = setting("DOCTOR_NTFY_TOKEN", "", secret=True)

    # Address and port the dashboard listens on inside the container.
    web_host: str = setting("DOCTOR_WEB_HOST", "0.0.0.0", env_only=True)
    web_port: int = setting("DOCTOR_WEB_PORT", 8080, env_only=True)
    # --- OIDC ---------------------------------------------------------------
    # Sign-in is environment-only throughout: whoever can save a setting must
    # not be able to decide who signs in, or where.
    # Issuer base URL, e.g. https://id.example.com. Empty disables OIDC.
    oidc_issuer: str = setting("DOCTOR_OIDC_ISSUER", "", env_only=True, convert=lambda v: v.rstrip("/"))
    # The client ID registered with the provider for the doctor.
    oidc_client_id: str = setting("DOCTOR_OIDC_CLIENT_ID", "", env_only=True)
    oidc_client_secret: str = setting("DOCTOR_OIDC_CLIENT_SECRET", "", secret=True)
    # The doctor's own public base URL; the redirect URI is derived from it and
    # must match what the provider has registered exactly.
    public_url: str = setting("DOCTOR_PUBLIC_URL", "", env_only=True, convert=lambda v: v.rstrip("/"))
    # Scopes requested at sign-in, space-separated.
    oidc_scopes: str = setting("DOCTOR_OIDC_SCOPES", "openid profile email groups", env_only=True)
    # The ID token claim holding the user's groups, for DOCTOR_OIDC_ALLOWED_GROUPS.
    oidc_groups_claim: str = setting("DOCTOR_OIDC_GROUPS_CLAIM", "groups", env_only=True)
    # Empty means any account the provider authenticates.
    oidc_allowed_groups: list[str] = setting("DOCTOR_OIDC_ALLOWED_GROUPS", (), env_only=True)
    # Signs the session cookie. Unset, one is generated once and kept in the
    # data directory (session_secret, mode 0600).
    session_secret: str = setting("DOCTOR_SESSION_SECRET", "", secret=True)
    # How long a session survives unused. In use, it lasts as long as the
    # provider keeps renewing its tokens.
    session_max_age_seconds: int = setting("DOCTOR_SESSION_HOURS", 12, env_only=True,
                                           convert=lambda v: v * 3600)
    # Once OIDC is configured the password is refused by default, so there is
    # one way in. Turn this on to keep it as a way back in when the provider
    # itself is the thing that is broken.
    password_fallback: bool = setting("DOCTOR_WEB_PASSWORD_FALLBACK", False, env_only=True)

    web_user: str = setting("DOCTOR_WEB_USER", "doctor", env_only=True)
    # Off makes the Settings page read-only again: nothing can be saved from
    # it, and the stack environment is the only way to change a setting.
    settings_editable: bool = setting("DOCTOR_SETTINGS_EDITABLE", True, env_only=True)
    web_password: str = setting("DOCTOR_WEB_PASSWORD", "", secret=True,
                                convert=_refuse_placeholder)

    # Where the database lives inside the container; mount a volume here.
    data_dir: str = setting("DOCTOR_DATA_DIR", "/data", env_only=True)
    # Resolve an incident whose condition has not re-fired for this long. 0 is
    # off. Kinds that suppress their own re-alerts are excluded - see
    # Store.auto_resolve_stale.
    auto_resolve_hours: int = setting("DOCTOR_AUTO_RESOLVE_HOURS", 12)
    # How often to ask the registries what they currently publish. The doctor
    # never pulls itself - the socket proxy denies POST /images - but a new
    # build can be redeployed from the dashboard through the deploy workflow.
    image_check_hours: int = setting("DOCTOR_IMAGE_CHECK_HOURS", 12, restart=True)
    # A newer version counts once it has been on Docker Hub this many days -
    # match a Dependabot cooldown (or Renovate's minimumReleaseAge), so the
    # dashboard lists what it will propose rather than running days ahead of
    # it. 0 reports every release at once.
    update_cooldown_days: int = setting("DOCTOR_UPDATE_COOLDOWN_DAYS", 3)
    # Redeploying a new build from the Updates page: the doctor starts the
    # repository's Deploy stacks workflow for the stack, which calls the
    # stack's redeploy webhook from its own secrets. A fine-grained token on
    # this one repository, with Actions read/write and Contents read. Unset,
    # the page only reports.
    github_token: str = setting("DOCTOR_GITHUB_TOKEN", "", secret=True)
    # owner/repository the stacks are deployed from; see examples/redeploy-workflow.yml.
    deploy_repo: str = setting("DOCTOR_DEPLOY_REPO", "",
                               convert=lambda v: v.strip("/"))
    deploy_workflow: str = setting("DOCTOR_DEPLOY_WORKFLOW", "deploy-stacks.yml")
    deploy_ref: str = setting("DOCTOR_DEPLOY_REF", "main")
    # What deploys the stacks, in words - for the diagnosis, the hand-off and
    # the labels on the Settings page. Neither changes what the doctor does:
    # it reads Compose's own labels whatever started a container, and
    # redeploys only through the workflow above. The manager is a name -
    # Portainer, Coolify, Komodo, Dockge - and empty names none. The method is
    # how a change reaches the host, e.g. "a push to main; a GitHub Actions
    # workflow calls each changed stack's webhook".
    docker_manager: str = setting("DOCTOR_DOCKER_MANAGER", "")
    deploy_method: str = setting("DOCTOR_DEPLOY_METHOD", "")
    # The machine itself. auto tells a Synology NAS from any other Linux by its
    # kernel; synology keeps DSM's md0/md1 system arrays from reading as
    # degraded and uses its names for drives and pools. The name is what the
    # host is called in diagnoses and on the Host page - empty says "the host".
    host_kind: str = setting("DOCTOR_HOST_KIND", "auto", choices=HostKind)
    host_name: str = setting("DOCTOR_HOST_NAME", "")
    # The summary at the top of the dashboard. Needs an LLM; off leaves the
    # deterministic facts in its place.
    briefing: bool = setting("DOCTOR_BRIEFING", True)
    # A smaller model is plenty for three lines of summary. Empty: DOCTOR_MODEL.
    briefing_model: str = setting("DOCTOR_BRIEFING_MODEL", "")
    # How often a section may be rewritten when its facts change without its
    # level changing. A new critical incident does not wait for this.
    briefing_refresh_minutes: int = setting("DOCTOR_BRIEFING_REFRESH_MINUTES", 15)
    # --- the classifier: a decision model's second opinion on a signal -------
    # off, shadow (record verdicts, change nothing) or on.
    classifier_mode: str = setting("DOCTOR_CLASSIFIER", "off", choices=ClassifierMode)
    # openrouter: a decision model through OpenRouter's Decisions API.
    # chat: the diagnosis's own chat model, which may never suppress.
    classifier_provider: str = setting("DOCTOR_CLASSIFIER_PROVIDER", "openrouter",
                                       choices=ClassifierProvider)
    # Pinned rather than -latest, so verdicts do not drift under you.
    classifier_model: str = setting("DOCTOR_CLASSIFIER_MODEL", "typesafe/jev-1.13")
    # The Decisions API endpoint the openrouter backend calls.
    classifier_url: str = setting(
        "DOCTOR_CLASSIFIER_URL", "https://openrouter.ai/api/alpha/decisions", env_only=True)
    # Unset falls back to the diagnosis's key when that is an OpenRouter one;
    # see classifier_key.
    classifier_api_key: str = setting("DOCTOR_CLASSIFIER_API_KEY", "", secret=True)
    # Probability of noise at or above which a signal opens no incident.
    classifier_suppress_at: float = setting("DOCTOR_CLASSIFIER_SUPPRESS_AT", 0.9)
    classifier_daily_calls: int = setting("DOCTOR_CLASSIFIER_DAILY_CALLS", 500)
    classifier_daily_usd: float = setting("DOCTOR_CLASSIFIER_DAILY_USD", 0.10)
    # A host coming back up fires everything at once; past this, the rules
    # decide alone until the minute is over.
    classifier_max_per_minute: int = setting("DOCTOR_CLASSIFIER_MAX_PER_MINUTE", 10)
    classifier_cache_hours: int = setting("DOCTOR_CLASSIFIER_CACHE_HOURS", 6)
    classifier_timeout_seconds: float = setting("DOCTOR_CLASSIFIER_TIMEOUT", 5.0)
    # Roughly 3,000 tokens: the evidence without the tail of a long log.
    classifier_max_state_chars: int = setting("DOCTOR_CLASSIFIER_MAX_STATE_CHARS", 12000)
    # Jev's input price; its output is free. Used when OpenRouter does not
    # report the cost itself.
    classifier_price_per_mtok: float = setting("DOCTOR_CLASSIFIER_PRICE_PER_MTOK", 0.042)
    # The kernel's account of every md array. off turns the RAID check off.
    mdstat_path: str = setting("DOCTOR_MDSTAT_PATH", "/proc/mdstat", can_be_off=True)
    # Where the arrays' layout, scrub results and per-disk errors are read
    # from. Docker mounts the host's /sys read-only; off skips it.
    sysfs_path: str = setting("DOCTOR_SYSFS_PATH", "/sys", can_be_off=True, restart=True)
    # The host's CPU, memory, load and disk counters. They are system-wide in
    # /proc, so a container reads the host's own numbers. off turns the Host
    # page and its rules off.
    proc_path: str = setting("DOCTOR_PROC_PATH", "/proc", can_be_off=True, restart=True)
    # Below this share of memory available (the kernel's MemAvailable, which
    # counts reclaimable cache) the host is about to swap or OOM-kill.
    host_mem_available_pct: float = setting("DOCTOR_HOST_MEM_AVAILABLE_PCT", 5.0)
    # The 5-minute load average per CPU. Linux load counts processes
    # waiting on disks as well as on CPU, so it reads high during a scrub.
    host_load_per_core: float = setting("DOCTOR_HOST_LOAD_PER_CORE", 2.0)
    # Share of CPU time spent waiting on disks.
    host_iowait_pct: float = setting("DOCTOR_HOST_IOWAIT_PCT", 40.0)
    # Pages read back from swap per second. Swap being used is normal - Linux,
    # DSM especially, parks idle pages there - swap being read back steadily is not.
    host_swap_in_per_s: float = setting("DOCTOR_HOST_SWAP_IN_PER_S", 256.0)
    host_temp_c: float = setting("DOCTOR_HOST_TEMP_C", 80.0)
    # Consecutive polls a host breach must last: ten is five minutes at the
    # default interval, so a backup's burst of I/O wait stays quiet.
    host_breach_polls: int = setting("DOCTOR_HOST_BREACH_POLLS", 10)
    # Internet and DNS, checked from the doctor's container every this many
    # seconds. 0 turns the checks off. "none" empties any of the lists.
    net_check_seconds: int = setting("DOCTOR_NET_CHECK_SECONDS", 60, restart=True)
    net_timeout_seconds: float = setting("DOCTOR_NET_TIMEOUT", 5.0)
    # Rounds in a row a check must fail before an incident opens, and pass
    # before it closes.
    net_fail_rounds: int = setting("DOCTOR_NET_FAIL_ROUNDS", 3)
    # Addresses, not names: reaching them says the line works, whatever DNS does.
    net_tcp_targets: list[str] = setting("DOCTOR_NET_TCP_TARGETS", ("1.1.1.1:443", "9.9.9.9:443"))
    # Resolved the way containers resolve names.
    net_dns_names: list[str] = setting("DOCTOR_NET_DNS_NAMES", ("cloudflare.com", "github.com"))
    # DNS servers asked directly - a Pi-hole, AdGuard, the router - as ip,
    # ip:port or a container name on a network the doctor shares. Empty by
    # default: which ones exist is the network's business.
    net_dns_servers: list[str] = setting("DOCTOR_NET_DNS_SERVERS", ())
    net_http_urls: list[str] = setting(
        "DOCTOR_NET_HTTP_URLS", ("https://www.cloudflare.com/cdn-cgi/trace",))
    # The SMART sidecar's metrics page. off turns drive health off.
    smart_url: str = setting("DOCTOR_SMART_URL", "http://doctor-smart:9633/metrics",
                             can_be_off=True)
    smart_temp_c: float = setting("DOCTOR_SMART_TEMP_C", 55.0)
    smart_nvme_temp_c: float = setting("DOCTOR_SMART_NVME_TEMP_C", 70.0)
    # Share of an SSD's rated write endurance used before it is worth a word.
    smart_wear_pct: float = setting("DOCTOR_SMART_WEAR_PCT", 90.0)
    # One stored point per this many minutes, for the Host page's charts and
    # each drive's temperature and sleep.
    host_sample_minutes: int = setting("DOCTOR_HOST_SAMPLE_MINUTES", 5)
    # Read GitHub release notes for images with a newer version, to link them
    # and flag the ones that mention breaking changes. One unauthenticated
    # request per such image per check.
    release_notes: bool = setting("DOCTOR_RELEASE_NOTES", True)
    retention_days: int = setting("DOCTOR_RETENTION_DAYS", 30)
    # Every request to a model, kept as it was sent, for this many days. 0
    # keeps no bodies at all; the daily totals are kept either way.
    llm_log_days: int = setting("DOCTOR_LLM_LOG_DAYS", 14)
    # Stamped into the image by CI. "dev" means a local build.
    build: str = setting("DOCTOR_BUILD", "dev", env_only=True)
    # The release, X.Y.Z, for an image the Release workflow built; empty otherwise.
    version: str = setting("DOCTOR_VERSION", "", env_only=True)
    log_level: str = setting("DOCTOR_LOG_LEVEL", "INFO", env_only=True, convert=lambda v: v.upper())
    # The doctor's own container name: it never diagnoses or acts on itself.
    self_container_name: str = setting("DOCTOR_SELF_NAME", "container-doctor")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "doctor.db")

    @property
    def log_error_regex(self) -> re.Pattern[str]:
        return re.compile(self.log_error_pattern)

    @property
    def log_ignore_regex(self) -> re.Pattern[str] | None:
        return _compile_or_none(self.log_ignore_pattern)

    @property
    def custom_pricing(self) -> tuple[float, float] | None:
        """Operator-supplied token prices, or None to use the built-in table."""
        if self.price_in_per_mtok < 0 and self.price_out_per_mtok < 0:
            return None
        return (max(self.price_in_per_mtok, 0.0), max(self.price_out_per_mtok, 0.0))

    @property
    def llm_enabled(self) -> bool:
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        if self.llm_provider == "openai":
            return bool(self.openai_base_url)
        return False

    @property
    def classifier_key(self) -> str:
        """The key the Decisions API is called with: the classifier's own, or
        else the diagnosis's when it already goes through OpenRouter. A key of
        its own is still the better choice - it can carry its own credit limit."""
        if self.classifier_api_key:
            return self.classifier_api_key
        if (self.llm_provider == "openai" and "openrouter.ai" in self.openai_base_url
                and self.openai_api_key not in ("", "not-needed")):
            return self.openai_api_key
        return ""

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.public_url)

    @property
    def oidc_discovery_url(self) -> str:
        return f"{self.oidc_issuer}/.well-known/openid-configuration"

    @property
    def oidc_redirect_url(self) -> str:
        return f"{self.public_url}/auth/callback"

    @property
    def password_login_allowed(self) -> bool:
        """The password is a fallback, not a second front door."""
        if not self.web_password:
            return False
        return self.password_fallback or not self.oidc_enabled

    def overview(self) -> list[dict[str, Any]]:
        """The effective configuration, grouped for the dashboard.

        Each section names the tab it sits under on the Settings page:
        general, detection, response or ai.

        An allowlist, deliberately: every value shown is named here, so a
        secret added to Settings later cannot appear by accident. Secrets are
        reported as set/not set and never by value.
        """

        def present(value: object) -> str:
            return "set" if value else "not set"

        def money(value: float) -> str:
            return "from the built-in table" if value < 0 else f"${value:.2f} / M tokens"

        return [
            {
                "title": "Build and mode",
                "group": "general",
                "items": [
                    ("Version", self.version or "unreleased", "the release this image was built for"),
                    ("Build", self.build, "the commit this image was built from"),
                    ("Mode", self.mode, "observe diagnoses only; suggest proposes; auto acts"),
                    ("Poll interval", f"{self.poll_interval_seconds}s", "from the start of one poll to the next"),
                    ("Poll concurrency", self.poll_concurrency, "containers read at once"),
                    ("Docker endpoint", self.docker_host, ""),
                    ("Opt-in only", Switch.ON if self.require_opt_in else Switch.OFF,
                     "on watches only containers labelled doctor.enable=true; off watches "
                     "every container except doctor.enable=false"),
                ],
            },
            {
                "title": "Deployment",
                "group": "general",
                "items": [
                    ("Docker manager", self.docker_manager or "not named",
                     "what deploys the stacks; named in diagnoses, hand-offs and on this page"),
                    ("Deploy method", self.deploy_method or "not described",
                     "how a change reaches the host, in words, for the diagnosis"),
                    ("Host kind", self.host_kind,
                     "auto reads it from the kernel; synology handles DSM's system arrays"),
                    ("Host name", self.host_name or "not named",
                     "what the host is called in diagnoses and on the Host page"),
                ],
            },
            {
                "title": "Model",
                "group": "ai",
                "items": [
                    ("Provider", self.llm_provider, ""),
                    ("Model", self.model, ""),
                    ("Endpoint", self.openai_base_url or "the provider's default", ""),
                    # Not the raw keys: openai_api_key defaults to a placeholder
                    # so local endpoints work without one, which would make this
                    # read "set" with nothing configured at all.
                    ("Credentials", "set" if self.llm_enabled else "not set",
                     "incidents are recorded but not diagnosed without them"),
                    ("Reasoning effort", self.effort, ""),
                    ("Temperature", "not sent" if self.llm_temperature < 0
                     else str(self.llm_temperature), "reasoning models reject it"),
                ],
            },
            {
                "title": "Cost",
                "group": "ai",
                "items": [
                    ("Input price", money(self.price_in_per_mtok), ""),
                    ("Output price", money(self.price_out_per_mtok), ""),
                    ("Daily budget", f"${self.daily_usd_budget:.2f}",
                     "detection continues after it is reached; diagnosis stops"),
                    ("Daily call cap", self.daily_call_limit or "none",
                     "what matters on a free tier, which meters requests not dollars"),
                    ("Reuse identical diagnoses for", f"{self.diagnosis_cache_hours}h", ""),
                    ("Keep each call's request and answer for",
                     f"{self.llm_log_days} days" if self.llm_log_days else "off",
                     "the LLM page's call log; daily totals are kept regardless"),
                ],
            },
            {
                "title": "Detection",
                "group": "detection",
                "items": [
                    ("CPU threshold", f"{self.cpu_threshold_pct:.0f}% of the host", ""),
                    ("Memory threshold", f"{self.mem_threshold_pct:.0f}%", ""),
                    ("Sustained for", f"{self.resource_breach_polls} polls", ""),
                    ("Restart loop", f"{self.restart_loop_count} in "
                     f"{self.restart_loop_window_minutes} min", ""),
                    ("Log tail", f"{self.log_lines} lines, "
                     f"{self.log_max_age_minutes} min max age", ""),
                    ("Error burst", f"{self.log_error_burst} by keyword, "
                     f"{self.log_level_error_burst} by level", ""),
                    ("Fatal threshold", f"{self.log_fatal_threshold} line", ""),
                    ("Redeploy settle window", f"{self.deploy_settle_minutes:g} min",
                     "log errors and failing health checks from a redeploy do not count"),
                    ("Crash grace", f"{self.die_grace_seconds:g}s",
                     "an exit nobody asked for waits this long to see if it was replaced"),
                    ("Global log ignore", self.log_ignore_pattern or "none",
                     "per container, prefer the doctor.ignore_log label"),
                    ("Disk", f"{', '.join(self.disk_paths) or 'none'} at "
                     f"{self.disk_threshold_pct:.0f}%",
                     f"critical from {self.disk_critical_pct:.0f}%"),
                    ("Fill forecast", f"warn {self.disk_forecast_days}d, "
                     f"critical {self.disk_forecast_critical_days}d",
                     f"fitted over the last {self.disk_trend_window_days} days"),
                    ("RAID", self.mdstat_path or "off",
                     "the kernel's md array status - software RAID, and every DSM volume"),
                    ("RAID detail", self.sysfs_path or "off",
                     "which arrays make up each volume, scrub results, per-disk errors"),
                    ("Read errors", f"critical from {self.raid_errors_per_day} a day"
                     if self.raid_errors_per_day else "always a warning",
                     f"a drive with corrected read errors in the last "
                     f"{self.raid_errors_days} days is a warning"),
                ],
            },
            {
                "title": "Host",
                "group": "detection",
                "items": [
                    ("Read from", self.proc_path or "off",
                     "the kernel's system-wide counters; no extra access needed"),
                    ("Memory available below", f"{self.host_mem_available_pct:g}%", ""),
                    ("Swap read back above", f"{self.host_swap_in_per_s:g} pages/s",
                     "swap merely in use is normal"),
                    ("Load above", f"{self.host_load_per_core:g} per CPU",
                     "the 5-minute average"),
                    ("I/O wait above", f"{self.host_iowait_pct:g}%", ""),
                    ("Temperature above", f"{self.host_temp_c:g} °C",
                     "only where the host exposes its sensors"),
                    ("Sustained for", f"{self.host_breach_polls} polls", ""),
                    ("Chart resolution", f"{self.host_sample_minutes} min", ""),
                ],
            },
            {
                "title": "Internet and DNS",
                "group": "detection",
                "items": [
                    ("Checked every", f"{self.net_check_seconds}s" if self.net_check_seconds
                     else "off", "from the doctor's own container"),
                    ("Reach", ", ".join(self.net_tcp_targets) or "none",
                     "addresses, so the line is tested apart from DNS"),
                    ("Resolve", ", ".join(self.net_dns_names) or "none", ""),
                    ("Ask directly", ", ".join(self.net_dns_servers) or "none",
                     "DNS servers on the network, each checked on its own"),
                    ("Fetch", ", ".join(self.net_http_urls) or "none", ""),
                    ("Incident after", f"{self.net_fail_rounds} failed rounds",
                     f"each check times out after {self.net_timeout_seconds:g}s"),
                ],
            },
            {
                "title": "Drive health",
                "group": "detection",
                "items": [
                    ("SMART from", self.smart_url or "off",
                     "the doctor-smart sidecar; the doctor itself never touches a disk"),
                    ("Hard disk too warm at", f"{self.smart_temp_c:g} °C", ""),
                    ("NVMe too warm at", f"{self.smart_nvme_temp_c:g} °C", ""),
                    ("SSD wear warning at", f"{self.smart_wear_pct:g}% used", ""),
                ],
            },
            {
                "title": "Remediation",
                "group": "response",
                "items": [
                    ("Auto-applies", ", ".join(self.auto_actions) or NOTHING,
                     "only in auto mode"),
                    ("Minimum confidence", f"{self.min_confidence:.2f}", ""),
                    ("Rate limit", f"{self.max_actions_per_container_per_hour}/container/h, "
                     f"{self.max_actions_global_per_hour}/h overall", ""),
                    ("Cooldown", f"{self.action_cooldown_minutes} min",
                     "per container and failure fingerprint"),
                    ("Verify after", f"{self.verify_delay_seconds}s", ""),
                ],
            },
            {
                "title": "Incident lifecycle",
                "group": "detection",
                "items": [
                    ("Fold recurrences for", f"{self.incident_dedup_hours}h",
                     "measured from when it was last seen"),
                    ("Auto-resolve after", f"{self.auto_resolve_hours}h"
                     if self.auto_resolve_hours else "never",
                     "disk pressure and anything awaiting approval are exempt"),
                    ("Keep history for", f"{self.retention_days} days", ""),
                ],
            },
            {
                "title": "Access",
                "group": "general",
                "items": [
                    ("Sign-in", "OIDC" if self.oidc_enabled else
                     ("password" if self.web_password else "none"), ""),
                    ("Issuer", self.oidc_issuer or "-", ""),
                    ("Client ID", self.oidc_client_id or "-", ""),
                    ("Client secret", present(self.oidc_client_secret),
                     "empty means a public client, which relies on PKCE alone"),
                    ("Restricted to groups", ", ".join(self.oidc_allowed_groups)
                     or "any account the provider authenticates", ""),
                    ("Session", f"{self.session_max_age_seconds // 3600}h",
                     "idle limit; within it, the provider's token lifetimes decide"),
                    ("Session secret", present(self.session_secret),
                     "unset means a new one per restart, so restarts sign you out"),
                    ("Password fallback", Switch.ON if self.password_fallback else Switch.OFF,
                     "keeps the password working while OIDC is configured"),
                ],
            },
            {
                "title": "Briefing",
                "group": "ai",
                "items": [
                    ("Summary", Switch.ON if self.briefing else Switch.OFF,
                     "an LLM summary per section, cached until its facts change"
                     + ("" if self.llm_enabled else " - but no LLM is configured, "
                        "so the facts show without one")),
                    ("Model", self.briefing_model or self.model, ""),
                    ("Rewrite at most every", f"{self.briefing_refresh_minutes} min",
                     "per section, unless its level changes or you refresh"),
                ],
            },
            {
                "title": "Classifier",
                "group": "ai",
                "items": [
                    ("Classifier", self.classifier_mode,
                     "shadow records verdicts and changes nothing; on may hide noise"),
                    ("Backend", self.classifier_provider, ""),
                    ("Decision model", self.classifier_model
                     if self.classifier_provider == "openrouter" else self.model, ""),
                    ("Decisions endpoint", self.classifier_url
                     if self.classifier_provider == "openrouter" else "the diagnosis's", ""),
                    ("Classifier API key", present(self.classifier_key)
                     if self.classifier_provider == "openrouter" else "-",
                     "the diagnosis's OpenRouter key" if self.classifier_provider == "openrouter"
                     and self.classifier_key and not self.classifier_api_key else ""),
                    ("Hides a signal from", f"{self.classifier_suppress_at:.0%} likely noise",
                     "calibrated models only, and never for exits, OOM kills, disks or arrays"),
                    ("Daily caps", f"{self.classifier_daily_calls} calls, "
                     f"${self.classifier_daily_usd:.2f}", "separate from the diagnosis's"),
                    ("Burst cap", f"{self.classifier_max_per_minute} per minute", ""),
                    ("Reuse a verdict for", f"{self.classifier_cache_hours}h", ""),
                    ("Timeout", f"{self.classifier_timeout_seconds:g}s",
                     "then the rules decide alone"),
                ],
            },
            {
                "title": "Updates",
                "group": "detection",
                "items": [
                    ("Registry check", f"every {self.image_check_hours}h"
                     if self.image_check_hours else "off",
                     "reports only - the doctor never pulls an image"),
                    ("Update cooldown", f"{self.update_cooldown_days} days"
                     if self.update_cooldown_days else "off",
                     "a release counts once it is this old on Docker Hub, as Dependabot waits"),
                    ("Release notes", Switch.ON if self.release_notes else Switch.OFF,
                     "links newer versions to their notes and flags breaking changes"),
                    ("Redeploy from the dashboard",
                     "through GitHub" if self.github_token else "off",
                     "runs the repository's deploy workflow for a stack with a new build; "
                     "set DOCTOR_GITHUB_TOKEN to turn it on"),
                ],
            },
            *([{
                "id": "github",
                "title": "GitHub",
                "group": "response",
                "items": [
                    ("Repository", self.deploy_repo, "DOCTOR_DEPLOY_REPO"),
                    ("Workflow", self.deploy_workflow,
                     "DOCTOR_DEPLOY_WORKFLOW - run by hand with the stack's name"),
                    ("Branch", self.deploy_ref, "DOCTOR_DEPLOY_REF - the workflow runs from it"),
                    ("Token", present(self.github_token),
                     "DOCTOR_GITHUB_TOKEN - fine-grained, this repository only: "
                     "Actions read and write, Contents read"),
                ],
            }] if self.github_token else []),
            {
                "title": "Notifications",
                "group": "response",
                "items": [
                    ("ntfy", self.ntfy_url or "not configured", ""),
                    ("Topic", self.ntfy_topic or "-", ""),
                    ("Token", present(self.ntfy_token), ""),
                ],
            },
        ]

    def validate(self) -> list[str]:
        """Return a list of human-readable configuration warnings."""
        problems: list[str] = []
        raw_password = _env("DOCTOR_WEB_PASSWORD")
        if raw_password and _placeholder_password(raw_password):
            problems.append(
                "DOCTOR_WEB_PASSWORD is a placeholder or the user name, so it is ignored. "
                "Choose a long random one, e.g. `openssl rand -base64 24`."
            )
        elif self.web_password and len(self.web_password) < SHORT_PASSWORD:
            problems.append(
                f"DOCTOR_WEB_PASSWORD is shorter than {SHORT_PASSWORD} characters; "
                "anyone who can reach the dashboard can guess at it."
            )
        if not (self.web_password or self.oidc_enabled):
            problems.append(
                "No sign-in configured: set DOCTOR_WEB_PASSWORD or DOCTOR_OIDC_*. "
                "Until then the dashboard and its API refuse every request."
            )
        if self.ntfy_url and not self.ntfy_topic:
            problems.append(
                "DOCTOR_NTFY_URL is set but DOCTOR_NTFY_TOPIC is not, so nothing is "
                "sent. On a public server, pick a long random topic."
            )
        if self.oidc_issuer and not self.oidc_enabled:
            missing = [
                name for name, value in (
                    ("DOCTOR_OIDC_CLIENT_ID", self.oidc_client_id),
                    ("DOCTOR_PUBLIC_URL", self.public_url),
                )
                if not value
            ]
            problems.append(
                f"DOCTOR_OIDC_ISSUER is set but OIDC is off: {', '.join(missing)} missing."
            )
        if self.oidc_enabled and not self.session_secret:
            problems.append(
                "DOCTOR_SESSION_SECRET is unset: one is generated and kept in the "
                "data directory. Set it to rotate it, or when the data directory "
                "is not private."
            )
        for name in BOOLEAN_ENV_VARS:
            raw = _env(name)
            if raw and _bool_word(name) not in _TRUE_WORDS | _FALSE_WORDS:
                problems.append(
                    f"{name}={raw!r} is not a yes/no value, so its default is being "
                    "used. Write it as true or false, without quotes."
                )
        if self.oidc_enabled and self.web_password and not self.password_fallback:
            raw = _env("DOCTOR_WEB_PASSWORD_FALLBACK")
            problems.append(
                "DOCTOR_WEB_PASSWORD is set but ignored while OIDC is on "
                f"(DOCTOR_WEB_PASSWORD_FALLBACK reads as {raw or 'unset'!r}). Set it "
                "to true to keep the password as a way in when the provider is "
                "unreachable."
            )
        if not self.llm_enabled:
            problems.append(
                f"No LLM credentials for provider '{self.llm_provider}': incidents will be "
                "detected and recorded but not diagnosed."
            )
        if self.llm_provider not in _values(LLMProvider):
            problems.append(
                f"DOCTOR_LLM_PROVIDER={self.llm_provider!r} is not {_or_list(LLMProvider)}.")
        if self.llm_provider == LLMProvider.ANTHROPIC and self.effort not in _values(Effort):
            problems.append(
                f"DOCTOR_EFFORT={self.effort!r} is not {_or_list(Effort)}; "
                "the model will reject it.")
        if self.classifier_mode not in _values(ClassifierMode):
            problems.append(
                f"DOCTOR_CLASSIFIER={self.classifier_mode!r} is not "
                f"{_or_list(ClassifierMode)}; the classifier is off.")
        elif self.classifier_mode != "off":
            if self.classifier_provider == "openrouter" and not self.classifier_key:
                problems.append(
                    "DOCTOR_CLASSIFIER is on but DOCTOR_CLASSIFIER_API_KEY is unset and the "
                    "diagnosis does not go through OpenRouter; the classifier is off.")
            elif self.classifier_provider == "chat" and not self.llm_enabled:
                problems.append(
                    "DOCTOR_CLASSIFIER_PROVIDER=chat needs the diagnosis model configured; "
                    "the classifier is off.")
            elif self.classifier_provider not in _values(ClassifierProvider):
                problems.append(
                    f"DOCTOR_CLASSIFIER_PROVIDER={self.classifier_provider!r} is not "
                    f"{_or_list(ClassifierProvider)}; the classifier is off.")
        if self.github_token and not self.deploy_repo:
            problems.append(
                "DOCTOR_GITHUB_TOKEN is set but DOCTOR_DEPLOY_REPO is not: set it to the "
                "owner/repository the stacks are deployed from, or nothing uses the token.")
        elif self.github_token and not re.match(r"^[\w.-]+/[\w.-]+$", self.deploy_repo):
            problems.append(
                f"DOCTOR_DEPLOY_REPO={self.deploy_repo!r} is not owner/repository; "
                "redeploying from the dashboard will fail.")
        unknown = [a for a in self.auto_actions if a not in VALID_ACTIONS]
        if unknown:
            problems.append(f"DOCTOR_AUTO_ACTIONS contains unknown actions: {', '.join(unknown)}")
        if not 0.0 <= self.min_confidence <= 1.0:
            problems.append("DOCTOR_MIN_CONFIDENCE must be between 0 and 1.")
        if self.llm_provider == "openai" and self.daily_call_limit <= 0:
            problems.append(
                "DOCTOR_DAILY_CALL_LIMIT is unset. On a free tier the quota is counted in "
                "requests per day, not dollars, so the USD budget will not stop anything."
            )
        return problems


# Every setting, by the variable it is read from, and by its field's name.
SETTINGS: dict[str, Spec] = {f.metadata["spec"].env: f.metadata["spec"]
                             for f in fields(Settings)}
FIELD_SPECS: dict[str, Spec] = {f.name: f.metadata["spec"] for f in fields(Settings)}


def storable(saved: dict[str, str]) -> dict[str, str]:
    """The saved values a Settings may read: known settings, not env-only."""
    return {env: value for env, value in saved.items()
            if env in SETTINGS and not SETTINGS[env].env_only}


def resolve(saved: dict[str, str]) -> Settings:
    """Settings from, in order of precedence: the environment, the values
    saved from the Settings page, the code's defaults.

    The environment wins so the stack environment stays the way back:
    whatever is saved, a variable set there overrides it.
    """
    token = _SAVED.set(storable(saved))
    try:
        return Settings()
    finally:
        _SAVED.reset(token)


def source(env: str, saved: dict[str, str]) -> str:
    """Where a setting's value comes from: "env" (the stack environment),
    "saved" (the Settings page) or "default" (the code)."""
    if os.environ.get(env, "").strip():
        return "env"
    if env in storable(saved) and saved[env].strip():
        return "saved"
    return "default"


def shown(spec: Spec, value: Any) -> str:
    """A setting's value written the way the environment would hold it."""
    if spec.kind == "bool":
        return Switch.ON if value else Switch.OFF
    if spec.kind == "list":
        return ", ".join(str(v) for v in value)
    if spec.kind == "float":
        return f"{value:g}"
    return str(value)


def current(env: str, saved: dict[str, str]) -> str:
    """What a setting holds now, as it would be typed: the environment's
    value, else the saved one, else the default. Never a secret's."""
    spec = SETTINGS[env]
    if spec.secret:
        return ""
    raw = os.environ.get(env, "").strip() or storable(saved).get(env, "").strip()
    return raw or shown(spec, spec.default)


def editable(env: str) -> bool:
    """Whether the Settings page may save this setting: a known one, not
    environment-only, and not set in the environment, which would override it."""
    spec = SETTINGS.get(env)
    return bool(spec and not spec.env_only and not os.environ.get(env, "").strip())


def _unusable(env: str) -> str | None:
    """Why the page cannot save `env` at all, or None."""
    spec = SETTINGS.get(env)
    if spec is None:
        return f"{env} is not a setting."
    if spec.env_only:
        return f"{env} is read from the environment only."
    if os.environ.get(env, "").strip():
        return f"{env} is set in the stack environment, which overrides this page."
    return None


def _unreadable(env: str, value: str) -> str | None:
    """Why `value` does not read as `env`'s type, or None. An unreadable
    value would silently fall back to the default, so it is refused."""
    spec = SETTINGS[env]
    value = value.strip()
    if not value:
        return None
    word = value.strip("\"'").lower()
    if spec.kind == "int":
        try:
            int(value)
        except ValueError:
            return f"{env} must be a whole number."
    elif spec.kind == "float":
        try:
            float(value)
        except ValueError:
            return f"{env} must be a number."
    elif spec.kind == "bool" and word not in _TRUE_WORDS | _FALSE_WORDS:
        return f"{env} must be on or off."
    elif spec.choices is not None:
        options = _values(spec.choices)
        picked = ([v.strip().lower() for v in value.split(",") if v.strip()]
                  if spec.kind == "list" else [value.lower()])
        if spec.kind == "list" and picked == ["none"]:
            picked = []
        wrong = [v for v in picked if v not in options]
        if wrong:
            return f"{env} must be one of {', '.join(options)}; not {', '.join(wrong)}."
    return None


def check_values(values: dict[str, str], saved: dict[str, str], *,
                 replace: bool = False) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Check a batch of values to save, as a whole.

    Returns (to_save, skipped, errors): the values that would be written
    (with replace, an empty one for every saved setting the batch leaves
    out); the variables passed over, each with why - not a setting, read
    from the environment only, or set in the stack environment, which would override
    it; and why the batch cannot be saved. A batch is saved whole or not at
    all: one unreadable value, or one new configuration problem from the
    batch together, refuses it.
    """
    to_save: dict[str, str] = {}
    skipped: dict[str, str] = {}
    errors: list[str] = []
    wanted = {env: str(value).strip() for env, value in values.items()}
    if replace:
        for env in storable(saved):
            wanted.setdefault(env, "")
    for env, value in wanted.items():
        reason = _unusable(env)
        if reason:
            skipped[env] = reason
            continue
        problem = _unreadable(env, value)
        if problem:
            errors.append(problem)
            continue
        to_save[env] = value
    if not errors:
        after_saved = {env: value for env, value in {**saved, **to_save}.items() if value}
        before = set(resolve(saved).validate())
        errors = [p for p in resolve(after_saved).validate() if p not in before]
    return to_save, skipped, errors


def check_value(env: str, value: str, saved: dict[str, str]) -> str | None:
    """Why `value` cannot be saved for `env`, or None when it can."""
    reason = _unusable(env)
    if reason:
        return reason
    _, _, errors = check_values({env: value}, saved)
    return " ".join(errors) or None


def load_settings() -> Settings:
    """Settings for the running doctor: the environment over what the
    Settings page saved in the database. The database's path is itself a
    setting, read from the environment alone."""
    boot = Settings()
    if not os.path.exists(boot.db_path):
        return boot
    from .store import Store

    store = Store(boot.db_path)
    try:
        return resolve(store.saved_settings())
    finally:
        store.close()

# Every boolean setting, so validate() can tell you when one is unreadable.
BOOLEAN_ENV_VARS: tuple[str, ...] = tuple(
    spec.env for spec in SETTINGS.values() if spec.kind == "bool")


# Which environment variables decide each row of Settings.overview(), keyed by
# (section title, row label). A row missing here is not a setting - the build,
# or a fact derived from several. tests/test_settings_env.py keeps it complete.
ENV_FOR: dict[tuple[str, str], tuple[str, ...]] = {
    ("Build and mode", "Mode"): ("DOCTOR_MODE",),
    ("Build and mode", "Poll interval"): ("DOCTOR_POLL_INTERVAL",),
    ("Build and mode", "Poll concurrency"): ("DOCTOR_POLL_CONCURRENCY",),
    ("Build and mode", "Docker endpoint"): ("DOCKER_HOST",),
    ("Build and mode", "Opt-in only"): ("DOCTOR_REQUIRE_OPT_IN",),
    ("Deployment", "Docker manager"): ("DOCTOR_DOCKER_MANAGER",),
    ("Deployment", "Deploy method"): ("DOCTOR_DEPLOY_METHOD",),
    ("Deployment", "Host kind"): ("DOCTOR_HOST_KIND",),
    ("Deployment", "Host name"): ("DOCTOR_HOST_NAME",),
    ("Model", "Provider"): ("DOCTOR_LLM_PROVIDER",),
    ("Model", "Model"): ("DOCTOR_MODEL",),
    ("Model", "Endpoint"): ("DOCTOR_OPENAI_BASE_URL",),
    ("Model", "Credentials"): ("ANTHROPIC_API_KEY", "DOCTOR_OPENAI_API_KEY",
                               "DOCTOR_OPENAI_BASE_URL", "DOCTOR_LLM_PROVIDER"),
    ("Model", "Reasoning effort"): ("DOCTOR_EFFORT",),
    ("Model", "Temperature"): ("DOCTOR_LLM_TEMPERATURE",),
    ("Cost", "Input price"): ("DOCTOR_PRICE_IN_PER_MTOK",),
    ("Cost", "Output price"): ("DOCTOR_PRICE_OUT_PER_MTOK",),
    ("Cost", "Daily budget"): ("DOCTOR_DAILY_USD_BUDGET",),
    ("Cost", "Daily call cap"): ("DOCTOR_DAILY_CALL_LIMIT",),
    ("Cost", "Reuse identical diagnoses for"): ("DOCTOR_DIAGNOSIS_CACHE_HOURS",),
    ("Cost", "Keep each call's request and answer for"): ("DOCTOR_LLM_LOG_DAYS",),
    ("Detection", "CPU threshold"): ("DOCTOR_CPU_THRESHOLD",),
    ("Detection", "Memory threshold"): ("DOCTOR_MEM_THRESHOLD",),
    ("Detection", "Sustained for"): ("DOCTOR_RESOURCE_BREACH_POLLS",),
    ("Detection", "Restart loop"): ("DOCTOR_RESTART_LOOP_COUNT",
                                    "DOCTOR_RESTART_LOOP_WINDOW_MINUTES"),
    ("Detection", "Log tail"): ("DOCTOR_LOG_LINES", "DOCTOR_LOG_MAX_AGE_MINUTES"),
    ("Detection", "Error burst"): ("DOCTOR_LOG_ERROR_BURST", "DOCTOR_LOG_LEVEL_ERROR_BURST"),
    ("Detection", "Fatal threshold"): ("DOCTOR_LOG_FATAL_THRESHOLD",),
    ("Detection", "Redeploy settle window"): ("DOCTOR_DEPLOY_SETTLE_MINUTES",),
    ("Detection", "Crash grace"): ("DOCTOR_DIE_GRACE_SECONDS",),
    ("Detection", "Global log ignore"): ("DOCTOR_LOG_IGNORE_PATTERN",),
    ("Detection", "Disk"): ("DOCTOR_DISK_PATHS", "DOCTOR_DISK_THRESHOLD",
                            "DOCTOR_DISK_CRITICAL"),
    ("Detection", "Fill forecast"): ("DOCTOR_DISK_FORECAST_DAYS",
                                     "DOCTOR_DISK_FORECAST_CRITICAL_DAYS",
                                     "DOCTOR_DISK_TREND_WINDOW_DAYS"),
    ("Detection", "RAID"): ("DOCTOR_MDSTAT_PATH",),
    ("Detection", "RAID detail"): ("DOCTOR_SYSFS_PATH",),
    ("Detection", "Read errors"): ("DOCTOR_RAID_ERRORS_PER_DAY", "DOCTOR_RAID_ERRORS_DAYS"),
    ("Host", "Read from"): ("DOCTOR_PROC_PATH",),
    ("Host", "Memory available below"): ("DOCTOR_HOST_MEM_AVAILABLE_PCT",),
    ("Host", "Swap read back above"): ("DOCTOR_HOST_SWAP_IN_PER_S",),
    ("Host", "Load above"): ("DOCTOR_HOST_LOAD_PER_CORE",),
    ("Host", "I/O wait above"): ("DOCTOR_HOST_IOWAIT_PCT",),
    ("Host", "Temperature above"): ("DOCTOR_HOST_TEMP_C",),
    ("Host", "Sustained for"): ("DOCTOR_HOST_BREACH_POLLS",),
    ("Host", "Chart resolution"): ("DOCTOR_HOST_SAMPLE_MINUTES",),
    ("Internet and DNS", "Checked every"): ("DOCTOR_NET_CHECK_SECONDS",),
    ("Internet and DNS", "Reach"): ("DOCTOR_NET_TCP_TARGETS",),
    ("Internet and DNS", "Resolve"): ("DOCTOR_NET_DNS_NAMES",),
    ("Internet and DNS", "Ask directly"): ("DOCTOR_NET_DNS_SERVERS",),
    ("Internet and DNS", "Fetch"): ("DOCTOR_NET_HTTP_URLS",),
    ("Internet and DNS", "Incident after"): ("DOCTOR_NET_FAIL_ROUNDS", "DOCTOR_NET_TIMEOUT"),
    ("Drive health", "SMART from"): ("DOCTOR_SMART_URL",),
    ("Drive health", "Hard disk too warm at"): ("DOCTOR_SMART_TEMP_C",),
    ("Drive health", "NVMe too warm at"): ("DOCTOR_SMART_NVME_TEMP_C",),
    ("Drive health", "SSD wear warning at"): ("DOCTOR_SMART_WEAR_PCT",),
    ("Remediation", "Auto-applies"): ("DOCTOR_AUTO_ACTIONS",),
    ("Remediation", "Minimum confidence"): ("DOCTOR_MIN_CONFIDENCE",),
    ("Remediation", "Rate limit"): ("DOCTOR_MAX_ACTIONS_PER_CONTAINER_PER_HOUR",
                                    "DOCTOR_MAX_ACTIONS_GLOBAL_PER_HOUR"),
    ("Remediation", "Cooldown"): ("DOCTOR_ACTION_COOLDOWN_MINUTES",),
    ("Remediation", "Verify after"): ("DOCTOR_VERIFY_DELAY",),
    ("Incident lifecycle", "Fold recurrences for"): ("DOCTOR_INCIDENT_DEDUP_HOURS",),
    ("Incident lifecycle", "Auto-resolve after"): ("DOCTOR_AUTO_RESOLVE_HOURS",),
    ("Incident lifecycle", "Keep history for"): ("DOCTOR_RETENTION_DAYS",),
    ("Access", "Sign-in"): ("DOCTOR_OIDC_ISSUER", "DOCTOR_OIDC_CLIENT_ID",
                            "DOCTOR_PUBLIC_URL", "DOCTOR_WEB_PASSWORD"),
    ("Access", "Issuer"): ("DOCTOR_OIDC_ISSUER",),
    ("Access", "Client ID"): ("DOCTOR_OIDC_CLIENT_ID",),
    ("Access", "Client secret"): ("DOCTOR_OIDC_CLIENT_SECRET",),
    ("Access", "Restricted to groups"): ("DOCTOR_OIDC_ALLOWED_GROUPS",),
    ("Access", "Session"): ("DOCTOR_SESSION_HOURS",),
    ("Access", "Session secret"): ("DOCTOR_SESSION_SECRET",),
    ("Access", "Password fallback"): ("DOCTOR_WEB_PASSWORD_FALLBACK",),
    ("Briefing", "Summary"): ("DOCTOR_BRIEFING",),
    ("Briefing", "Model"): ("DOCTOR_BRIEFING_MODEL", "DOCTOR_MODEL"),
    ("Briefing", "Rewrite at most every"): ("DOCTOR_BRIEFING_REFRESH_MINUTES",),
    ("Classifier", "Classifier"): ("DOCTOR_CLASSIFIER",),
    ("Classifier", "Backend"): ("DOCTOR_CLASSIFIER_PROVIDER",),
    ("Classifier", "Decision model"): ("DOCTOR_CLASSIFIER_MODEL", "DOCTOR_CLASSIFIER_PROVIDER",
                                       "DOCTOR_MODEL"),
    ("Classifier", "Decisions endpoint"): ("DOCTOR_CLASSIFIER_URL", "DOCTOR_CLASSIFIER_PROVIDER"),
    ("Classifier", "Classifier API key"): ("DOCTOR_CLASSIFIER_API_KEY",
                                           "DOCTOR_CLASSIFIER_PROVIDER",
                                           "DOCTOR_LLM_PROVIDER", "DOCTOR_OPENAI_BASE_URL",
                                           "DOCTOR_OPENAI_API_KEY"),
    ("Classifier", "Hides a signal from"): ("DOCTOR_CLASSIFIER_SUPPRESS_AT",),
    ("Classifier", "Daily caps"): ("DOCTOR_CLASSIFIER_DAILY_CALLS",
                                   "DOCTOR_CLASSIFIER_DAILY_USD"),
    ("Classifier", "Burst cap"): ("DOCTOR_CLASSIFIER_MAX_PER_MINUTE",),
    ("Classifier", "Reuse a verdict for"): ("DOCTOR_CLASSIFIER_CACHE_HOURS",),
    ("Classifier", "Timeout"): ("DOCTOR_CLASSIFIER_TIMEOUT",),
    ("Updates", "Registry check"): ("DOCTOR_IMAGE_CHECK_HOURS",),
    ("Updates", "Update cooldown"): ("DOCTOR_UPDATE_COOLDOWN_DAYS",),
    ("Updates", "Release notes"): ("DOCTOR_RELEASE_NOTES",),
    ("Updates", "Redeploy from the dashboard"): ("DOCTOR_GITHUB_TOKEN",),
    ("GitHub", "Repository"): ("DOCTOR_DEPLOY_REPO",),
    ("GitHub", "Workflow"): ("DOCTOR_DEPLOY_WORKFLOW",),
    ("GitHub", "Branch"): ("DOCTOR_DEPLOY_REF",),
    ("GitHub", "Token"): ("DOCTOR_GITHUB_TOKEN",),
    ("Notifications", "ntfy"): ("DOCTOR_NTFY_URL",),
    ("Notifications", "Topic"): ("DOCTOR_NTFY_TOPIC",),
    ("Notifications", "Token"): ("DOCTOR_NTFY_TOKEN",),
}


# The rows whose value is one of a closed set, and that set: the Settings page
# shows every choice and highlights the one in use. A row has choices when it
# is set by one variable whose setting has them - a boolean's are on and off.
# A multiple-choice row's value is its choices joined by ", ".
# tests/test_settings_env.py checks each reads as one of its choices.
def _row_choices(names: tuple[str, ...]) -> tuple[type[StrEnum], bool] | None:
    if len(names) != 1 or names[0] not in SETTINGS:
        return None
    spec = SETTINGS[names[0]]
    if spec.kind == "bool":
        return Switch, False
    return (spec.choices, spec.kind == "list") if spec.choices else None


CHOICES_FOR: dict[tuple[str, str], tuple[type[StrEnum], bool]] = {
    row: entry for row, names in ENV_FOR.items() if (entry := _row_choices(names))}


def choices(section: str, label: str, value: str) -> dict[str, Any] | None:
    """A row's choices for the Settings page - every option, which are in use,
    and any value in use that is not an option - or None for a free-form row."""
    entry = CHOICES_FOR.get((section, label))
    if entry is None:
        return None
    enum, multiple = entry
    options = list(_values(enum))
    if multiple:
        picked = [] if value == NOTHING else [v for v in value.split(", ") if v]
    else:
        picked = [value]
    return {"options": options, "multiple": multiple,
            "selected": [v for v in picked if v in options],
            "unknown": [v for v in picked if v not in options]}

# Never shown by value, the default included.
SECRET_ENV = frozenset(spec.env for spec in SETTINGS.values() if spec.secret)

def passed_env(environ: Any = None) -> frozenset[str] | None:
    """The settings the deployment passes to the container, read from the
    environment the doctor was started with - which the stack environment can
    set, as opposed to fixed ones it never reaches.

    A compose file that passes its settings through writes `- X=${X:-}`, so
    every one it passes is there, empty when unset. An empty one is that
    pattern's mark; without any, the deployment sets only what it sets
    (`docker run -e`, say) and the answer is None: unknown, not "none".
    """
    environ = os.environ if environ is None else environ
    names = set(SETTINGS) | {name for env in ENV_FOR.values() for name in env}
    passed = frozenset(name for name in names if name in environ)
    if not any(not str(environ[name]).strip() for name in passed):
        return None
    return passed


def default_values() -> dict[tuple[str, str], str]:
    """Each row's value as it reads when the stack sets nothing, by (section, label):
    Settings built with no setting in the environment. Built by swapping the
    process environment for a moment, so call it once, at start."""
    names = {name for env in ENV_FOR.values() for name in env}
    saved = dict(os.environ)
    try:
        for name in list(os.environ):
            if name.startswith("DOCTOR_") or name in names:
                del os.environ[name]
        if "DOCTOR_DATA_DIR" in saved:  # where it lives is not a setting here
            os.environ["DOCTOR_DATA_DIR"] = saved["DOCTOR_DATA_DIR"]
        defaults = Settings().overview()
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return {(section["title"], label): str(value)
            for section in defaults for label, value, _ in section["items"]}


def _compile_or_none(pattern: str) -> re.Pattern[str] | None:
    """A bad regex in config must not take the doctor down with it."""
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


@dataclass(frozen=True)
class ContainerPolicy:
    """Per-container overrides, read from Docker labels.

    Supported labels::

        doctor.enable=false          # never touch this container
        doctor.mode=observe          # override the global mode for it
        doctor.actions=restart_container,stop_container
        doctor.max_actions_per_hour=1
        doctor.critical=true         # always notify, even for low severity
        doctor.ignore_log=StudiosImageProvider|TrailerProvider
                                     # log lines this container emits
                                     # harmlessly; never counted as errors
    """

    enabled: bool = True
    mode: Mode | None = None
    actions: list[str] | None = None
    max_actions_per_hour: int | None = None
    critical: bool = False
    ignore_log: str = ""

    @classmethod
    def from_labels(cls, labels: dict[str, str]) -> "ContainerPolicy":
        def label(name: str) -> str:
            return (labels.get(f"doctor.{name}") or "").strip()

        enable_raw = label("enable")
        actions_raw = label("actions")
        mode_raw = label("mode").lower()
        max_raw = label("max_actions_per_hour")

        try:
            max_actions = int(max_raw) if max_raw else None
        except ValueError:
            max_actions = None

        return cls(
            enabled=enable_raw.lower() not in ("false", "0", "no", "off") if enable_raw else True,
            mode=Mode(mode_raw) if mode_raw in VALID_MODES else None,
            actions=[a.strip() for a in actions_raw.split(",") if a.strip()] if actions_raw else None,
            max_actions_per_hour=max_actions,
            critical=label("critical").lower() in ("1", "true", "yes", "on"),
            ignore_log=label("ignore_log"),
        )

    @property
    def ignore_log_regex(self) -> re.Pattern[str] | None:
        return _compile_or_none(self.ignore_log)


settings = Settings()
