"""What the repository says about a stack, for a diagnosis to read.

The doctor sees a container change - recreated, on another image reference -
but not why. The repository it is deployed from knows: the commit that did it,
its message, the exact lines it touched, and the service's definition with
the comments that explain it. authentik was diagnosed as "the image changed,
roll it back" when the commit had only pinned the same tag to its digest;
the diff says so in one line.

Read with the token the redeploy already holds (Contents read), only when one
is configured, and never in the way of an incident: anything GitHub refuses
or takes too long over is left out, with the reason.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .changes import _iso
from .github_deploy import API, _headers, _refused, _unreachable

log = logging.getLogger(__name__)

COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
# How far back, and how much of it: a few commits' diffs are what a diagnosis
# can use; a stack's whole history is not.
HISTORY = timedelta(days=14)
MAX_COMMITS = 4
PATCH_CHARS = 1500
SERVICE_CHARS = 2500


async def stack_context(client: Any, token: str, repo: str, ref: str, stack: str,
                        service: str) -> dict[str, Any]:
    """The service's definition and the stack folder's recent commits.

    Returns {"repo", "ref", "stack", "service", "compose_path", "definition",
    "commits": [...], "error"} - any part may be empty.
    """
    out: dict[str, Any] = {"repo": repo, "ref": ref, "stack": stack, "service": service,
                           "compose_path": "", "definition": "", "commits": [], "error": ""}
    try:
        await _definition(client, token, repo, ref, stack, service, out)
        await _commits(client, token, repo, ref, stack, out)
    except Exception as error:  # never let the repository break an incident
        out["error"] = out["error"] or _unreachable(error)
    return out


async def _definition(client: Any, token: str, repo: str, ref: str, stack: str,
                      service: str, out: dict[str, Any]) -> None:
    headers = {**_headers(token), "Accept": "application/vnd.github.raw+json"}
    for name in COMPOSE_NAMES:
        path = f"{stack}/{name}"
        response = await client.get(f"{API}/repos/{repo}/contents/{path}",
                                    params={"ref": ref}, headers=headers)
        if response.status_code == 404:
            continue
        if response.status_code >= 400:
            out["error"] = _refused(response, "the compose file")
            return
        out["compose_path"] = path
        out["definition"] = service_block(response.text, service)[:SERVICE_CHARS]
        return


async def _commits(client: Any, token: str, repo: str, ref: str, stack: str,
                   out: dict[str, Any]) -> None:
    since = (datetime.now(timezone.utc) - HISTORY).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = await client.get(
        f"{API}/repos/{repo}/commits",
        params={"sha": ref, "path": stack, "since": since, "per_page": MAX_COMMITS},
        headers=_headers(token))
    if response.status_code >= 400:
        out["error"] = out["error"] or _refused(response, "the commit history")
        return
    for item in (response.json() or [])[:MAX_COMMITS]:
        sha = str(item.get("sha") or "")
        commit = item.get("commit") or {}
        entry = {
            "sha": sha[:7],
            "at": str((commit.get("committer") or {}).get("date") or ""),
            "message": str(commit.get("message") or "").strip()[:600],
            "url": str(item.get("html_url") or ""),
            "files": [],
        }
        detail = await client.get(f"{API}/repos/{repo}/commits/{sha}", headers=_headers(token))
        if detail.status_code < 400:
            for changed in (detail.json() or {}).get("files") or []:
                filename = str(changed.get("filename") or "")
                if not filename.startswith(f"{stack}/"):
                    continue  # the same commit's other stacks are theirs
                entry["files"].append({
                    "path": filename,
                    "patch": str(changed.get("patch") or "")[:PATCH_CHARS],
                })
        out["commits"].append(entry)


def service_block(compose_text: str, service: str) -> str:
    """The service's lines in the compose file, comments included.

    Cut out of the text rather than parsed: the comments are why a setting is
    what it is, and YAML parsing drops them.
    """
    lines = compose_text.splitlines()
    start = None
    indent = 0
    header = re.compile(rf"^(\s+){re.escape(service)}:\s*(#.*)?$")
    for index, line in enumerate(lines):
        match = header.match(line)
        if match:
            start, indent = index, len(match.group(1))
            break
    if start is None:
        return ""
    block = [lines[start]]
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped and len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    while block and not block[-1].strip():
        block.pop()
    return "\n".join(block)


def deployed(commit_at: str, container_created: str) -> bool | None:
    """Whether a commit is older than the container - in what it runs, if the
    stack was redeployed from the branch - or None when either time is missing."""
    try:
        commit = datetime.fromisoformat(_iso(commit_at))
        created = datetime.fromisoformat(_iso(container_created))
    except (TypeError, ValueError):
        return None
    return commit <= created
