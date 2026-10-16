"""Redeploying a stack through a workflow in the repository it is deployed from.

The doctor cannot pull an image or create a container - the socket proxy
denies both, and that is the point of it. The repository the stacks deploy
from (DOCTOR_DEPLOY_REPO) has a workflow that redeploys a stack, run by hand
with its name - docs/redeploy-workflow.yml says what it must accept. The
doctor starts that run, so the webhooks live in one place and the doctor
holds one token that can do no more than run the repository's workflows and
read its files.

The token is a credential, so it never appears in a message or a log line.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

API = "https://api.github.com"
# A stack is a top-level directory with a compose file - the workflow's rule.
_COMPOSE = re.compile(r"^([^/]+)/(?:docker-compose|compose)\.ya?ml$")
POLL_SECONDS = 10.0
# The deploy jobs time out at five minutes each; a queued run waits its turn.
RUN_TIMEOUT_SECONDS = 20 * 60.0


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _refused(response: Any, what: str) -> str:
    status = response.status_code
    if status in (401, 403):
        return (f"GitHub refused {what} (HTTP {status}) - check DOCTOR_GITHUB_TOKEN: "
                "it needs Actions read and write, and Contents read, on the repository")
    if status == 404:
        return (f"GitHub found nothing to {what} (HTTP 404) - check DOCTOR_DEPLOY_REPO, "
                "DOCTOR_DEPLOY_WORKFLOW, and that the token can see the repository")
    if status == 422:
        return f"GitHub rejected {what} (HTTP 422) - is DOCTOR_DEPLOY_REF a branch?"
    return f"GitHub refused {what} (HTTP {status})"


def _unreachable(error: Exception) -> str:
    # The exception text can carry request details; its class name cannot.
    return f"could not reach GitHub ({error.__class__.__name__})"


async def stacks(client: Any, token: str, repo: str, ref: str) -> tuple[list[str], str]:
    """The repository's stacks at `ref`, or ([], why not)."""
    try:
        response = await client.get(f"{API}/repos/{repo}/git/trees/{ref}",
                                    params={"recursive": "1"}, headers=_headers(token))
    except Exception as error:
        return [], _unreachable(error)
    if response.status_code >= 400:
        return [], _refused(response, "the repository listing")
    found = {match.group(1) for item in (response.json().get("tree") or [])
             if item.get("type") == "blob" and (match := _COMPOSE.match(item.get("path", "")))}
    return sorted(found), ""


def token_kind(token: str) -> str:
    """What sort of token this is, from its public prefix."""
    if token.startswith("github_pat_"):
        return "fine-grained"
    if token.startswith("ghp_"):
        return "classic"
    return "other"


def _expiry(response: Any) -> str | None:
    """The token's expiry as GitHub reports it on every answer, ISO, or None.

    GitHub sends `github-authentication-token-expiration` only for a token
    that expires, as "2026-11-01 00:00:00 UTC" (or with a numeric offset).
    """
    raw = (response.headers.get("github-authentication-token-expiration") or "").strip()
    if not raw:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            moment = datetime.strptime(raw, pattern)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
    return raw


async def check_token(client: Any, token: str, repo: str,
                      workflow_file: str) -> dict[str, Any]:
    """What the token may do here, and when it stops working.

    A fine-grained token cannot be asked for its permissions, so each one the
    doctor needs is tried: reading the repository (metadata), reading the
    workflow (Actions read), and starting it with no branch named (Actions
    write). That last request cannot start a run - GitHub checks the
    permission first, then refuses the missing branch with 422 - so 422 means
    granted and 403 means not. Contents read is tried by the stack listing.
    """
    result: dict[str, Any] = {
        "kind": token_kind(token), "expires_at": None, "scopes": None,
        "permissions": {"metadata": "unknown", "actions_read": "unknown",
                        "actions_write": "unknown"},
        "workflow": None, "error": "",
    }
    permissions = result["permissions"]
    try:
        repository = await client.get(f"{API}/repos/{repo}", headers=_headers(token))
    except Exception as error:
        result["error"] = _unreachable(error)
        return result
    result["expires_at"] = _expiry(repository)
    scopes = repository.headers.get("x-oauth-scopes")
    if scopes is not None:  # classic tokens only
        result["scopes"] = [s.strip() for s in scopes.split(",") if s.strip()]
    if repository.status_code >= 400:
        permissions["metadata"] = "missing"
        result["error"] = _refused(repository, "the repository")
        return result
    permissions["metadata"] = "granted"

    flow, why = await workflow(client, token, repo, workflow_file)
    if flow:
        permissions["actions_read"] = "granted"
        result["workflow"] = flow
    else:
        permissions["actions_read"] = "missing" if "HTTP 403" in why else "unknown"
        result["error"] = why
        return result

    try:
        probe = await client.post(
            f"{API}/repos/{repo}/actions/workflows/{workflow_file}/dispatches",
            headers=_headers(token), json={})
    except Exception as error:
        result["error"] = _unreachable(error)
        return result
    if probe.status_code == 422:
        permissions["actions_write"] = "granted"
    elif probe.status_code in (401, 403):
        permissions["actions_write"] = "missing"
        result["error"] = ("the token cannot start workflows - give it Actions: "
                           "read and write")
    return result


async def workflow(client: Any, token: str, repo: str,
                   workflow_file: str) -> tuple[dict[str, Any] | None, str]:
    """The workflow as GitHub has it - name, state, link - or (None, why not).

    Needs Actions read, so it doubles as a check of the token.
    """
    try:
        response = await client.get(f"{API}/repos/{repo}/actions/workflows/{workflow_file}",
                                    headers=_headers(token))
    except Exception as error:
        return None, _unreachable(error)
    if response.status_code >= 400:
        return None, _refused(response, "the workflow")
    body = response.json()
    return {"name": str(body.get("name") or workflow_file),
            "state": str(body.get("state") or "unknown"),
            "html_url": str(body.get("html_url") or "")}, ""


async def dispatch(client: Any, token: str, repo: str, workflow: str, ref: str,
                   stack: str, marker: str) -> tuple[dict[str, Any] | None, str]:
    """Start the workflow for one stack. Returns (run, "") or (None, why not).

    `marker` goes into the run's name, so the run can be found again when
    GitHub does not say which run it started.
    """
    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    try:
        response = await client.post(
            f"{API}/repos/{repo}/actions/workflows/{workflow}/dispatches",
            headers=_headers(token),
            json={"ref": ref, "inputs": {"stacks": stack, "requested_by": marker},
                  "return_run_details": True},
        )
    except Exception as error:
        return None, _unreachable(error)
    if response.status_code >= 400:
        return None, _refused(response, "the workflow run")
    if response.status_code == 200:
        body = response.json() if response.content else {}
        if body.get("workflow_run_id"):
            return {"id": body["workflow_run_id"], "html_url": body.get("html_url", "")}, ""
    # 204: started, without saying which run. It is listed within seconds.
    for _ in range(12):
        await asyncio.sleep(POLL_SECONDS / 2)
        run, why = await _find_run(client, token, repo, workflow, marker, started)
        if run or why:
            return run, why
    return None, "the workflow was started, but its run did not appear on GitHub"


async def _find_run(client: Any, token: str, repo: str, workflow: str, marker: str,
                    since: datetime) -> tuple[dict[str, Any] | None, str]:
    try:
        response = await client.get(
            f"{API}/repos/{repo}/actions/workflows/{workflow}/runs",
            params={"event": "workflow_dispatch", "per_page": 20,
                    "created": f">={since.strftime('%Y-%m-%dT%H:%M:%SZ')}"},
            headers=_headers(token),
        )
    except Exception as error:
        return None, _unreachable(error)
    if response.status_code >= 400:
        return None, _refused(response, "the run list")
    for run in response.json().get("workflow_runs") or []:
        if marker in str(run.get("display_title") or ""):
            return {"id": run["id"], "html_url": run.get("html_url", "")}, ""
    return None, ""


async def wait(client: Any, token: str, repo: str, run_id: int,
               timeout: float = RUN_TIMEOUT_SECONDS,
               poll: float = POLL_SECONDS) -> tuple[str, str]:
    """Follow a run to its end. Returns (conclusion, "") or ("", why not)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    failures = 0
    while loop.time() < deadline:
        await asyncio.sleep(poll)
        try:
            response = await client.get(f"{API}/repos/{repo}/actions/runs/{run_id}",
                                        headers=_headers(token))
        except Exception as error:
            failures += 1
            if failures >= 5:
                return "", _unreachable(error)
            continue
        if response.status_code >= 400:
            return "", _refused(response, "the run")
        run = response.json()
        if run.get("status") == "completed":
            return str(run.get("conclusion") or "unknown"), ""
    return "", f"the run did not finish within {timeout / 60:.0f} minutes"
