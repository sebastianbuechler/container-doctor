"""Is the image a container is running still the current one?

Thirty containers on `:latest` and nothing that says which of them are behind.
This answers that and stops: it reports, it never pulls. Updating a host
unattended is how you find out at 3am which service did not survive it.

The doctor cannot pull in any case - the socket proxy denies POST /images -
so the comparison is made against the registry directly, over HTTPS:

  local     the digest the running container's image carries
  remote    the digest the registry currently has for that tag
  differ    an update exists

Anonymous tokens only. A private repository answers 401 and is reported as
needing credentials rather than quietly counted as up to date.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_REGISTRY = "registry-1.docker.io"
DEFAULT_TAG = "latest"

# What a v2 registry may answer a manifest request with. Without the index
# types a multi-architecture image returns the wrong digest, or none.
MANIFEST_TYPES = ",".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


@dataclass(frozen=True)
class Reference:
    registry: str
    repository: str
    tag: str
    # `name:5.1.3@sha256:...` keeps both: the digest is what runs, the tag
    # still says which release line to look for newer versions in.
    digest: str = ""

    @property
    def pinned(self) -> bool:
        """A digest reference cannot drift - it names exactly one image."""
        return bool(self.digest)

    def __str__(self) -> str:
        tag = f":{self.tag}" if self.tag else ""
        digest = f"@{self.digest}" if self.digest else ""
        return f"{self.registry}/{self.repository}{tag}{digest}"


def parse_reference(image: str) -> Reference | None:
    """Split an image reference the way Docker does.

    The rule that decides whether the first element is a registry or a user
    name is the awkward part: `user/repo` is Docker Hub, `host.tld/repo` is
    not, and the difference is a dot, a colon, or the word localhost.
    """
    image = (image or "").strip()
    if not image:
        return None

    remainder, _, digest = image.partition("@")
    head, separator, candidate = remainder.rpartition(":")
    # A colon in the host part is a port, not a tag: registry:5000/repo
    if separator and "/" not in candidate:
        remainder, tag = head, candidate
    else:
        # A bare digest has no tag to speak of; without either, Docker pulls latest.
        tag = "" if digest else DEFAULT_TAG

    first, slash, rest = remainder.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, rest
    else:
        registry = DEFAULT_REGISTRY
        # Docker Hub keeps its own images under `library/`.
        repository = remainder if slash else f"library/{remainder}"

    if not repository:
        return None
    return Reference(registry=registry, repository=repository, tag=tag, digest=digest)


@dataclass(frozen=True)
class Status:
    """What we could work out about one image."""

    image: str
    state: str  # current | update | pinned | unknown | denied
    detail: str = ""
    local_digest: str = ""
    remote_digest: str = ""

    @property
    def update_available(self) -> bool:
        return self.state == "update"


async def remote_digest(client: Any, reference: Reference) -> tuple[str, str]:
    """The digest the registry has for this tag.

    Returns (digest, "") or ("", reason). A reason rather than an exception:
    every outcome here is something to report next to the image, not something
    to stop a poll over.
    """
    url = f"https://{reference.registry}/v2/{reference.repository}/manifests/{reference.tag}"
    headers = {"Accept": MANIFEST_TYPES}
    try:
        response = await client.head(url, headers=headers, follow_redirects=True)
        if response.status_code == 401:
            token, reason = await _anonymous_token(client, response, reference)
            if not token:
                return "", reason
            headers["Authorization"] = f"Bearer {token}"
            response = await client.head(url, headers=headers, follow_redirects=True)
    except Exception as error:
        return "", f"could not reach {reference.registry} ({error.__class__.__name__})"

    if response.status_code in (401, 403):
        return "", "needs credentials"
    if response.status_code == 404:
        return "", "not found in the registry"
    if response.status_code == 429:
        # Docker Hub meters manifest reads against the anonymous pull limit.
        return "", "registry rate limit reached"
    if response.status_code >= 400:
        return "", f"registry answered HTTP {response.status_code}"

    digest = response.headers.get("Docker-Content-Digest", "")
    return (digest, "") if digest else ("", "registry returned no digest")


async def _anonymous_token(client: Any, challenge: Any, reference: Reference):
    """Follow the Bearer challenge a v2 registry answers 401 with."""
    header = challenge.headers.get("WWW-Authenticate", "")
    if not header.lower().startswith("bearer "):
        return "", "needs credentials"

    fields: dict[str, str] = {}
    for part in header[7:].split(","):
        key, _, value = part.strip().partition("=")
        fields[key.strip()] = value.strip().strip('"')

    realm = fields.get("realm")
    if not realm:
        return "", "needs credentials"
    params = {k: v for k, v in fields.items() if k in ("service", "scope")}
    params.setdefault("scope", f"repository:{reference.repository}:pull")
    try:
        response = await client.get(realm, params=params)
        if response.status_code >= 400:
            return "", "needs credentials"
        body = response.json()
    except Exception as error:
        return "", f"could not get a token ({error.__class__.__name__})"
    return str(body.get("token") or body.get("access_token") or ""), ""


def compare(image: str, local_digests: list[str] | None, remote: str,
            reason: str) -> Status:
    """Judge one image from what we have."""
    reference = parse_reference(image)
    if reference is None:
        return Status(image=image, state="unknown", detail="unreadable image reference")
    if reference.pinned:
        return Status(image=image, state="pinned",
                      detail="pinned to a digest, so it cannot drift")
    if reason:
        state = "denied" if reason == "needs credentials" else "unknown"
        return Status(image=image, state=state, detail=reason)

    local = _digest_for(local_digests or [], reference)
    if not local:
        return Status(image=image, state="unknown", remote_digest=remote,
                      detail="the running image carries no registry digest")
    if local == remote:
        return Status(image=image, state="current", local_digest=local,
                      remote_digest=remote)
    return Status(image=image, state="update", local_digest=local,
                  remote_digest=remote, detail="a newer image is published")


def _digest_for(repo_digests: list[str], reference: Reference) -> str:
    """Pick the digest belonging to this repository.

    An image may carry several - one per repository it has been tagged into -
    and comparing against the wrong one reports an update forever.
    """
    for entry in repo_digests:
        repo, _, digest = entry.partition("@")
        if not digest:
            continue
        parsed = parse_reference(repo)
        if parsed and parsed.repository == reference.repository:
            return digest
    return ""
