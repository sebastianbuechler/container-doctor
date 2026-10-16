"""Is there a newer release than the version a container is pinned to?

The digest check answers "has this tag moved?", which is the whole question
for `:latest` and none of it for `sonarr:4.0.20`: an exact version tag never
moves, so it reads as current forever while 4.1 ships. This answers the other
half by listing the repository's tags and finding newer ones in the same line.

"The same line" is the part that needs care. A repository's tags are a mix of
lines that must not be compared with each other:

  4.0.20            what is pinned
  4.0.20.2240-ls300 the same release, one of LinuxServer's build tags
  v2.16.0-distroless a different image variant
  0.31.0-rc.1       a pre-release
  2026092110        a nightly build stamp

A candidate counts only when it has the same prefix, the same number of
version components, and the same suffix once its numbers are set aside - so
`4.0.20` is only ever compared with `4.1.0`, never with `4.1.0.2300-ls301`.

A release is reported only once it has been out for a few days, the same
cooldown Dependabot keeps before it proposes one: a broken or compromised
release is usually pulled within that time, and a dashboard that is days
ahead of the pull requests only lists updates nobody can take yet.

Report only, like the digest check. The doctor never pulls or edits a pin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .registry import DEFAULT_REGISTRY, Reference, _anonymous_token

# Registries serve up to 1000 tags a page. LinuxServer repositories carry ten
# thousand or more, and ghcr.io projects that tag every commit and PR (immich,
# open-webui) over forty thousand. Past this many pages the answer is "too many
# to scan", not an unbounded crawl twice a day.
MAX_TAG_PAGES = 100
TOO_MANY_TAGS = "too many to scan"

_TAG = re.compile(
    r"^(?P<prefix>(?:[A-Za-z]+[-_]?)?)"
    r"(?P<numbers>\d+(?:\.\d+)*)"
    r"(?P<suffix>(?:[-_+~].*)?)$"
)
# A commit hash in a suffix changes with every build, so it is part of the
# line's shape only as "some hash": 2024.10.22-7ca5933 -> 2025.1.5-e1f00d2.
_HASH = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")
_DIGITS = re.compile(r"\d+")

# A four-digit first component is a year: 2024.10.22 to 2025.1.5 is a newer
# release, not a "major version" with everything that word implies.
_CALVER_FROM = 1900


@dataclass(frozen=True, order=False)
class Version:
    tag: str
    prefix: str
    numbers: tuple[int, ...]
    shape: str  # the suffix with its numbers and hashes set aside
    build: int  # the first number in the suffix, -1 when there is none

    @property
    def key(self) -> tuple[tuple[int, ...], int]:
        return (self.numbers, self.build)

    def same_line(self, other: "Version") -> bool:
        return (self.prefix == other.prefix
                and len(self.numbers) == len(other.numbers)
                and self.shape == other.shape)


def parse_tag(tag: str) -> Version | None:
    """A version, or None for tags such as `latest` and `stable`."""
    match = _TAG.match((tag or "").strip())
    if not match:
        return None
    suffix = match["suffix"]
    hashless = _HASH.sub("<hash>", suffix)
    build_digits = _DIGITS.search(hashless)
    return Version(
        tag=tag,
        prefix=match["prefix"],
        numbers=tuple(int(part) for part in match["numbers"].split(".")),
        shape=_DIGITS.sub("#", hashless),
        build=int(build_digits.group()) if build_digits else -1,
    )


def level(current: Version, newer: Version) -> str:
    """How big the step is: major, minor, patch, build - or release, for dates."""
    for index, (old, new) in enumerate(zip(current.numbers, newer.numbers)):
        if old != new:
            if index == 0:
                return "release" if current.numbers[0] >= _CALVER_FROM else "major"
            return "minor" if index == 1 else "patch"
    return "build"


@dataclass(frozen=True)
class Newer:
    current: str
    newest: str
    level: str
    behind: int  # distinct newer versions in the line
    # When the newest is a new major, the newest that is not: the upgrade that
    # is usually safe to take today, next to the one that needs reading first.
    newest_same_major: str = ""


def newest(current_tag: str, tags: list[str]) -> Newer | None:
    """The newest tag in the same line as `current_tag`, if it is newer."""
    current = parse_tag(current_tag)
    if current is None:
        return None
    line = [
        version for version in (parse_tag(tag) for tag in tags)
        if version is not None and current.same_line(version)
        and version.key > current.key
    ]
    if not line:
        return None
    best = max(line, key=lambda version: version.key)
    step = level(current, best)
    same_major = ""
    if step == "major":
        within = [v for v in line if v.numbers[0] == current.numbers[0]]
        if within:
            same_major = max(within, key=lambda version: version.key).tag
    return Newer(
        current=current_tag,
        newest=best.tag,
        level=step,
        behind=len({version.numbers for version in line}),
        newest_same_major=same_major,
    )


async def list_tags(client: Any, reference: Reference) -> tuple[list[str], str]:
    """Every tag in the repository. Returns (tags, "") or ([], reason)."""
    base = f"https://{reference.registry}"
    url = f"{base}/v2/{reference.repository}/tags/list?n=1000"
    headers: dict[str, str] = {}
    tags: list[str] = []
    try:
        response = await client.get(url, headers=headers, follow_redirects=True)
        if response.status_code == 401:
            token, reason = await _anonymous_token(client, response, reference)
            if not token:
                return [], reason
            headers["Authorization"] = f"Bearer {token}"
            response = await client.get(url, headers=headers, follow_redirects=True)
        for _ in range(MAX_TAG_PAGES):
            if response.status_code in (401, 403):
                return [], "needs credentials"
            if response.status_code == 429:
                return [], "registry rate limit reached"
            if response.status_code >= 400:
                return [], f"registry answered HTTP {response.status_code} listing tags"
            tags.extend(str(tag) for tag in (response.json().get("tags") or []))
            following = _next_page(response.headers.get("Link", ""))
            if not following:
                return tags, ""
            if following.startswith("/"):
                following = base + following
            response = await client.get(following, headers=headers, follow_redirects=True)
    except Exception as error:
        return [], f"could not list tags on {reference.registry} ({error.__class__.__name__})"
    return [], f"more than {MAX_TAG_PAGES * 1000} tags - {TOO_MANY_TAGS}"


# -- cooldown ---------------------------------------------------------------

# Docker Hub is the registry that says when a tag was pushed. Elsewhere there
# is no date to wait on, so the release counts at once - as Dependabot treats
# lscr.io and ghcr.io.
DOCKER_HUB_REGISTRIES = frozenset({
    DEFAULT_REGISTRY, "docker.io", "index.docker.io", "registry.hub.docker.com",
})
# Each held-back release costs a request for the next one down. A repository
# that published this many in one cooldown is not waited on any further.
MAX_COOLDOWN_LOOKUPS = 10


async def published_at(client: Any, reference: Reference, tag: str) -> datetime | None:
    """When Docker Hub last had `tag` pushed, or None when it cannot say."""
    if reference.registry not in DOCKER_HUB_REGISTRIES:
        return None
    namespace, _, name = reference.repository.partition("/")
    try:
        response = await client.get(
            f"https://hub.docker.com/v2/namespaces/{namespace}/repositories/{name}/tags/{tag}",
            follow_redirects=True,
        )
        if response.status_code >= 400:
            return None
        body = response.json()
        stamp = str(body.get("tag_last_pushed") or body.get("last_updated") or "")
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except Exception:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


async def settled(
    client: Any, reference: Reference, tags: list[str], days: int,
    now: datetime | None = None,
) -> tuple[Newer | None, dict[str, str] | None]:
    """The newest release that is out of its cooldown, and the one held back.

    Returns (newer, waiting): `newer` as `newest` would report it, counting
    only releases at least `days` old; `waiting` names the newest release
    still inside its cooldown, how big a step it is and when it comes out of
    it, or None. A release
    whose date cannot be read counts at once - an unreadable date should not
    hide an update.
    """
    found = newest(reference.tag, tags)
    if days <= 0 or found is None or reference.registry not in DOCKER_HUB_REGISTRIES:
        return found, None
    now = now or datetime.now(timezone.utc)
    cooldown = timedelta(days=days)
    remaining = list(tags)
    waiting: dict[str, str] | None = None
    for _ in range(MAX_COOLDOWN_LOOKUPS):
        pushed = await published_at(client, reference, found.newest)
        if pushed is None or now - pushed >= cooldown:
            return found, waiting
        if waiting is None:
            waiting = {"tag": found.newest, "level": found.level,
                       "ready_at": (pushed + cooldown).isoformat(timespec="seconds")}
        remaining = [tag for tag in remaining if tag != found.newest]
        found = newest(reference.tag, remaining)
        if found is None:
            return None, waiting
    return None, waiting


def _next_page(link: str) -> str:
    """The `rel="next"` target of an RFC 5988 Link header, as registries send it."""
    for part in link.split(","):
        target, _, params = part.partition(";")
        if 'rel="next"' in params.replace(" ", "") or "rel=next" in params.replace(" ", ""):
            return target.strip().strip("<>")
    return ""


# -- release notes ----------------------------------------------------------

_GITHUB = re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$")

# Wording that means "read this before upgrading". A hint, said as one: the
# dashboard reports that the notes *mention* it, never that the upgrade breaks.
_BREAKING = re.compile(
    r"breaking[ -]change|\bbreaking\b|backwards?[- ]incompatib|not backwards? compatible"
    r"|migration (?:is )?required|manual (?:migration|intervention)|action required",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"\bno(?:n|t)?[- ](?:\w+[- ])?$", re.IGNORECASE)


def source_repository(labels: dict[str, str] | None, reference: Reference) -> str:
    """owner/repo on GitHub, from the image's own label or its registry path."""
    source = str((labels or {}).get("org.opencontainers.image.source") or "").strip()
    match = _GITHUB.match(source)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    if reference.registry == "ghcr.io" and reference.repository.count("/") == 1:
        return reference.repository
    return ""


def breaking_line(body: str) -> str:
    """The first line of release notes that warns about upgrading, or ''."""
    for line in (body or "").splitlines():
        for match in _BREAKING.finditer(line):
            if _NEGATED.search(line[: match.start()]):
                continue  # "no breaking changes"
            text = line.strip().lstrip("#*->• ").strip()
            return text[:200]
    return ""


async def _fetch_releases(client: Any, repository: str) -> tuple[list[dict[str, Any]], str]:
    """The repository's latest hundred releases, or ([], why not)."""
    try:
        response = await client.get(
            f"https://api.github.com/repos/{repository}/releases",
            params={"per_page": 100},
            headers={"Accept": "application/vnd.github+json"},
            follow_redirects=True,
        )
        if response.status_code >= 400:
            return [], f"GitHub answered HTTP {response.status_code}"
        releases = response.json()
    except Exception as error:
        return [], f"could not reach GitHub ({error.__class__.__name__})"
    return (releases if isinstance(releases, list) else []), ""


def _between(
    releases: list[dict[str, Any]], current_tag: str, target_tag: str
) -> list[tuple[tuple[int, ...], dict[str, Any]]]:
    """The releases after the pin, up to and including the target, oldest first."""
    current = parse_tag(current_tag)
    target = parse_tag(target_tag)
    if current is None or target is None:
        return []
    width = len(current.numbers)
    between: list[tuple[tuple[int, ...], dict[str, Any]]] = []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        version = parse_tag(str(release.get("tag_name") or ""))
        if version is None or len(version.numbers) < width:
            continue
        # Compare at the pin's precision: LinuxServer tags its releases
        # 4.1.0.2300-ls301, which is 4.1.0 as far as a pin on 4.0.20 goes.
        numbers = version.numbers[:width]
        if current.numbers < numbers <= target.numbers[:width]:
            between.append((numbers, release))
    return sorted(between, key=lambda item: item[0])


async def release_notes(
    client: Any, repository: str, newer: Newer
) -> dict[str, Any] | None:
    """What GitHub says about the releases between the pin and the newest.

    One unauthenticated request per image per check. GitHub allows sixty an
    hour, which a twice-daily check of a homelab does not come near.
    """
    if not repository:
        return None
    if parse_tag(newer.current) is None or parse_tag(newer.newest) is None:
        return None
    url = f"https://github.com/{repository}/releases"
    releases, detail = await _fetch_releases(client, repository)
    if detail:
        return {"url": url, "breaking": "", "detail": detail}

    between = _between(releases, newer.current, newer.newest)
    target = parse_tag(newer.newest)
    if between and between[-1][0] == target.numbers[:len(between[-1][0])]:
        url = str(between[-1][1].get("html_url") or url)

    warning, warning_url = "", ""
    for _, release in between:
        warning = breaking_line(str(release.get("body") or ""))
        if warning:
            warning_url = str(release.get("html_url") or url)
            break
    return {
        "url": url,
        "releases": len(between),
        "breaking": warning,
        "breaking_url": warning_url,
        "detail": "",
    }


# Enough to read a long changelog; a release whose notes run past it links on.
NOTES_BODY_CHARS = 20000


async def release_list(
    client: Any, repository: str, current_tag: str, target_tag: str
) -> dict[str, Any]:
    """Each release between the pin and the target, newest first, notes and all.

    Read when someone asks to see them, not during the check: a check only
    needs to know whether the notes warn about something.
    """
    url = f"https://github.com/{repository}/releases"
    releases, detail = await _fetch_releases(client, repository)
    if detail:
        return {"url": url, "releases": [], "detail": detail}
    listed = []
    for _, release in reversed(_between(releases, current_tag, target_tag)):
        body = str(release.get("body") or "")
        listed.append({
            "tag": str(release.get("tag_name") or ""),
            "name": str(release.get("name") or ""),
            "published_at": str(release.get("published_at") or ""),
            "url": str(release.get("html_url") or url),
            "body": body[:NOTES_BODY_CHARS],
            "truncated": len(body) > NOTES_BODY_CHARS,
            "breaking": breaking_line(body),
        })
    return {"url": url, "releases": listed,
            "detail": "" if listed else "GitHub lists no release notes for these versions"}
