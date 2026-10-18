"""Redeploying a new build from the dashboard, and reading release notes on request."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import httpx

from app import github_deploy
from app.versions import release_list

TOKEN = "github_pat_secretvalue"


def run(coro):
    return asyncio.run(coro)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class StartingTheWorkflow(unittest.TestCase):
    def dispatch(self, handler):
        async def go():
            async with client_for(handler) as client:
                with mock.patch.object(github_deploy, "POLL_SECONDS", 0):
                    return await github_deploy.dispatch(
                        client, TOKEN, "o/homelab", "deploy-stacks.yml", "main",
                        "arr-stack", marker="container-doctor abcd1234")
        return run(go())

    def test_one_stack_by_name_and_the_run_github_names(self):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            self.assertEqual(request.headers["Authorization"], f"Bearer {TOKEN}")
            self.assertTrue(request.url.path.endswith("/workflows/deploy-stacks.yml/dispatches"))
            return httpx.Response(200, json={"workflow_run_id": 42,
                                             "html_url": "https://github.com/o/homelab/actions/runs/42"})

        found, why = self.dispatch(handler)
        self.assertEqual((found["id"], why), (42, ""))
        self.assertEqual(seen[0]["ref"], "main")
        self.assertEqual(seen[0]["inputs"], {"stacks": "arr-stack",
                                             "requested_by": "container-doctor abcd1234"})

    def test_without_run_details_the_run_is_found_by_its_name(self):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(204)
            return httpx.Response(200, json={"workflow_runs": [
                {"id": 7, "display_title": "Deploy tools - container-doctor ffff0000"},
                {"id": 8, "display_title": "Deploy arr-stack - container-doctor abcd1234",
                 "html_url": "https://github.com/o/homelab/actions/runs/8"},
            ]})

        found, why = self.dispatch(handler)
        self.assertEqual((found["id"], why), (8, ""))

    def test_a_refusal_says_what_to_check_and_never_the_token(self):
        found, why = self.dispatch(lambda request: httpx.Response(403))
        self.assertIsNone(found)
        self.assertIn("DOCTOR_GITHUB_TOKEN", why)
        self.assertNotIn(TOKEN, why)

    def test_an_unreachable_github_never_repeats_the_request(self):
        def handler(request):
            raise httpx.ConnectError(f"failed with {TOKEN}")

        found, why = self.dispatch(handler)
        self.assertIsNone(found)
        self.assertNotIn(TOKEN, why)


class ListingTheStacks(unittest.TestCase):
    def test_top_level_folders_with_a_compose_file(self):
        def handler(request):
            self.assertTrue(request.url.path.endswith("/git/trees/main"))
            return httpx.Response(200, json={"tree": [
                {"path": "arr-stack", "type": "tree"},
                {"path": "arr-stack/docker-compose.yml", "type": "blob"},
                {"path": "tools/compose.yaml", "type": "blob"},
                {"path": "docs/README.md", "type": "blob"},
                {"path": "tools/nested/docker-compose.yml", "type": "blob"},
                {"path": "docker-compose.yml", "type": "blob"},
            ]})

        async def go():
            async with client_for(handler) as client:
                return await github_deploy.stacks(client, TOKEN, "o/homelab", "main")

        self.assertEqual(run(go()), (["arr-stack", "tools"], ""))

    def test_a_token_without_contents_read_says_so(self):
        async def go():
            async with client_for(lambda request: httpx.Response(403)) as client:
                return await github_deploy.stacks(client, TOKEN, "o/homelab", "main")

        found, why = run(go())
        self.assertEqual(found, [])
        self.assertIn("Contents read", why)


class ReadingTheWorkflow(unittest.TestCase):
    def test_name_state_and_link(self):
        def handler(request):
            self.assertTrue(request.url.path.endswith("/actions/workflows/deploy-stacks.yml"))
            return httpx.Response(200, json={
                "name": "Deploy stacks", "state": "active",
                "html_url": "https://github.com/o/homelab/blob/main/.github/workflows/deploy-stacks.yml"})

        async def go():
            async with client_for(handler) as client:
                return await github_deploy.workflow(client, TOKEN, "o/homelab", "deploy-stacks.yml")

        flow, why = run(go())
        self.assertEqual((flow["name"], flow["state"], why), ("Deploy stacks", "active", ""))

    def test_a_missing_workflow_says_which_setting_to_check(self):
        async def go():
            async with client_for(lambda request: httpx.Response(404)) as client:
                return await github_deploy.workflow(client, TOKEN, "o/homelab", "nope.yml")

        flow, why = run(go())
        self.assertIsNone(flow)
        self.assertIn("DOCTOR_DEPLOY_WORKFLOW", why)


class CheckingTheToken(unittest.TestCase):
    def check(self, *, write=422, read=200, repo=200, expiry="2026-11-01 00:00:00 UTC",
              scopes=None, token=TOKEN):
        def handler(request):
            path = request.url.path
            headers = {}
            if expiry:
                headers["github-authentication-token-expiration"] = expiry
            if scopes is not None:
                headers["x-oauth-scopes"] = scopes
            if request.method == "POST":
                self.assertEqual(json.loads(request.content), {})  # no branch: no run
                return httpx.Response(write, headers=headers)
            if path.endswith("/actions/workflows/deploy-stacks.yml"):
                return httpx.Response(read, headers=headers,
                                      json={"name": "Deploy stacks", "state": "active"})
            return httpx.Response(repo, headers=headers, json={})

        async def go():
            async with client_for(handler) as client:
                return await github_deploy.check_token(client, token, "o/homelab",
                                                       "deploy-stacks.yml")
        return run(go())

    def test_a_working_fine_grained_token(self):
        result = self.check()
        self.assertEqual(result["kind"], "fine-grained")
        self.assertEqual(result["expires_at"], "2026-11-01T00:00:00+00:00")
        self.assertEqual(result["permissions"], {
            "metadata": "granted", "actions_read": "granted", "actions_write": "granted"})
        self.assertEqual(result["error"], "")
        self.assertIsNone(result["scopes"])

    def test_read_only_actions_is_caught_without_starting_a_run(self):
        result = self.check(write=403)
        self.assertEqual(result["permissions"]["actions_write"], "missing")
        self.assertIn("Actions: read and write", result["error"])

    def test_no_actions_at_all(self):
        result = self.check(read=403)
        self.assertEqual(result["permissions"]["actions_read"], "missing")
        self.assertEqual(result["permissions"]["actions_write"], "unknown")

    def test_a_token_without_an_expiry(self):
        self.assertIsNone(self.check(expiry="")["expires_at"])

    def test_an_offset_expiry_is_read_as_utc(self):
        self.assertEqual(self.check(expiry="2026-11-01 02:00:00 +0200")["expires_at"],
                         "2026-11-01T00:00:00+00:00")

    def test_a_classic_token_says_its_scopes(self):
        result = self.check(token="ghp_classic", scopes="repo, workflow")
        self.assertEqual((result["kind"], result["scopes"]), ("classic", ["repo", "workflow"]))


class GitHubInSettings(unittest.TestCase):
    def overview(self, **env):
        from app.config import Settings

        previous = dict(os.environ)
        os.environ.pop("DOCTOR_GITHUB_TOKEN", None)
        os.environ.update(env)
        try:
            return Settings().overview()
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def test_its_own_section_only_when_configured(self):
        self.assertNotIn("GitHub", [s["title"] for s in self.overview()])
        section = next(s for s in self.overview(DOCTOR_GITHUB_TOKEN=TOKEN,
                                                DOCTOR_DEPLOY_REPO="owner/homelab")
                       if s["title"] == "GitHub")
        values = {label: value for label, value, _ in section["items"]}
        self.assertEqual(values["Repository"], "owner/homelab")
        self.assertEqual(values["Workflow"], "deploy-stacks.yml")
        self.assertEqual(values["Token"], "set")
        self.assertNotIn(TOKEN, json.dumps(section))


class FollowingTheRun(unittest.TestCase):
    def wait(self, answers, timeout=5.0):
        answers = list(answers)

        def handler(request):
            return httpx.Response(200, json=answers.pop(0) if len(answers) > 1 else answers[0])

        async def go():
            async with client_for(handler) as client:
                return await github_deploy.wait(client, TOKEN, "o/homelab", 42,
                                                timeout=timeout, poll=0)
        return run(go())

    def test_until_it_completes(self):
        conclusion, why = self.wait([{"status": "queued"}, {"status": "in_progress"},
                                     {"status": "completed", "conclusion": "success"}])
        self.assertEqual((conclusion, why), ("success", ""))

    def test_a_failed_run_is_its_conclusion(self):
        conclusion, _ = self.wait([{"status": "completed", "conclusion": "failure"}])
        self.assertEqual(conclusion, "failure")

    def test_a_run_that_never_ends_times_out(self):
        conclusion, why = self.wait([{"status": "queued"}], timeout=0.05)
        self.assertEqual(conclusion, "")
        self.assertIn("did not finish", why)


class _VersionOnly(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"Version": "27.0", "ApiVersion": "1.45"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def container(name, image, stack, image_id="sha256:old"):
    return {"name": name, "id": name, "image": image, "image_id": image_id,
            "labels": {"com.docker.compose.project": stack,
                       "com.docker.compose.service": name}}


class Redeploying(unittest.TestCase):
    def setUp(self):
        from app.config import Settings
        from app.supervisor import Supervisor

        server = HTTPServer(("127.0.0.1", 0), _VersionOnly)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        previous = dict(os.environ)
        os.environ.update(DOCTOR_DATA_DIR=tempfile.mkdtemp(), ANTHROPIC_API_KEY="",
                          DOCKER_HOST=f"tcp://127.0.0.1:{server.server_address[1]}",
                          DOCTOR_GITHUB_TOKEN=TOKEN, DOCTOR_DEPLOY_REPO="owner/homelab")
        try:
            self.supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(self.supervisor.store.close)
        # The repository's stacks, as if already read: no GitHub in a test.
        self.supervisor._deploy_stacks = ["arr-stack", "container-doctor", "productivity"]
        self.supervisor._deploy_stacks_read = float("inf")
        self.supervisor.containers = {c["name"]: c for c in (
            container("radarr", "linuxserver/radarr:6.4.4", "arr-stack"),
            container("sonarr", "linuxserver/sonarr:4.0.20", "arr-stack"),
            container("dozzle", "amir20/dozzle:v11.2.0", "tools"),
            container("container-doctor", "ghcr.io/x/container-doctor:latest", "container-doctor"),
        )}
        self.supervisor.image_updates = {
            "checked_at": "2026-10-07T10:00:00+00:00",
            "images": [{"image": "linuxserver/radarr:6.4.4", "state": "update",
                        "containers": ["radarr"]}],
        }

    def finish(self, conclusion, still_behind, dispatched=True):
        """Run a redeploy with GitHub and the registries stood in for."""
        sup = self.supervisor

        async def fresh_poll(timeout=120.0):
            if not still_behind:
                sup.containers["radarr"] = {**sup.containers["radarr"], "image_id": "sha256:new"}
            return True

        async def recheck(images):
            sup.image_updates["images"] = [
                {**e, "state": "update" if still_behind else "current"}
                for e in sup.image_updates["images"]]

        async def go():
            started = (({"id": 42, "html_url": "https://github.com/o/r/actions/runs/42"}, "")
                       if dispatched else (None, "GitHub refused the workflow run (HTTP 403)"))
            with mock.patch.object(github_deploy, "dispatch", mock.AsyncMock(return_value=started)), \
                    mock.patch.object(github_deploy, "wait",
                                      mock.AsyncMock(return_value=(conclusion, ""))), \
                    mock.patch.object(sup, "_fresh_poll", fresh_poll), \
                    mock.patch.object(sup, "_recheck_images", recheck):
                result = await sup.redeploy("radarr", actor="alex")
                during = dict(sup.snapshot()["image_updates"]["redeploying"])
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if not sup._redeploying:
                        break
                return result, during

        result, during = run(go())
        actions = sup.store.list_actions(limit=5, container_name="radarr")
        return result, during, actions

    def test_the_whole_stack_is_named_and_the_new_build_verified(self):
        result, during, actions = self.finish("success", still_behind=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["containers"], ["radarr", "sonarr"])
        self.assertIn("arr-stack", during)
        self.assertEqual(actions[0]["action"], "redeploy_stack")
        self.assertEqual(actions[0]["status"], "succeeded")
        self.assertIn("now running the new build", actions[0]["result"])
        self.assertIn("actions/runs/42", actions[0]["result"])
        self.assertEqual(actions[0]["decided_by"], "user:alex")
        self.assertEqual(self.supervisor.snapshot()["image_updates"]["redeploying"], {})

    def test_a_redeploy_that_left_the_old_build_running_is_a_failure(self):
        _, _, actions = self.finish("success", still_behind=True)
        self.assertEqual(actions[0]["status"], "failed")
        self.assertIn("still runs the old build", actions[0]["result"])
        self.assertIn("pull_policy", actions[0]["result"])

    def test_without_a_manager_named_the_stale_build_hint_is_generic(self):
        _, _, actions = self.finish("success", still_behind=True)
        self.assertNotIn("Portainer", actions[0]["result"])

    def test_the_stale_build_hint_is_the_named_managers(self):
        import dataclasses

        sup = self.supervisor
        sup.settings = dataclasses.replace(sup.settings, docker_manager="Portainer")
        _, _, actions = self.finish("success", still_behind=True)
        self.assertIn("Force redeployment", actions[0]["result"])

    def test_the_stack_label_names_the_folder_for_a_differently_named_project(self):
        from app.stacks import resolve_stacks

        # A manager that names projects itself; one service says which folder.
        members = [container("memos", "neosmemo/memos:0.31.0", "x7f3k2"),
                   container("memos-db", "postgres:17", "x7f3k2")]
        members[1]["labels"]["doctor.stack"] = "productivity"
        resolve_stacks(members)
        self.supervisor.containers.update({c["name"]: c for c in members})
        self.assertIsNone(self.supervisor.redeploy_refusal("productivity"))
        self.assertEqual(sorted(c["name"] for c in self.supervisor._stack_members("productivity")),
                         ["memos", "memos-db"])

    def test_a_failed_run_is_a_failure_with_its_link(self):
        _, _, actions = self.finish("failure", still_behind=True)
        self.assertEqual(actions[0]["status"], "failed")
        self.assertIn("ended failure", actions[0]["result"])
        self.assertIn("actions/runs/42", actions[0]["result"])

    def test_github_refusing_is_a_failure(self):
        _, _, actions = self.finish("", still_behind=True, dispatched=False)
        self.assertEqual(actions[0]["status"], "failed")
        self.assertIn("403", actions[0]["result"])

    def test_without_a_token_nothing_is_redeployed(self):
        import dataclasses
        self.supervisor.settings = dataclasses.replace(self.supervisor.settings, github_token="")
        result = run(self.supervisor.redeploy("radarr", actor="x"))
        self.assertFalse(result["ok"])
        self.assertIn("DOCTOR_GITHUB_TOKEN", result["error"])
        self.assertFalse(self.supervisor.snapshot()["image_updates"]["redeploy"])

    def test_the_doctor_does_not_redeploy_itself(self):
        result = run(self.supervisor.redeploy("container-doctor", actor="x"))
        self.assertFalse(result["ok"])
        self.assertIn("own stack", result["error"])

    def test_the_snapshot_says_redeploying_is_on_and_where(self):
        updates = self.supervisor.snapshot()["image_updates"]
        self.assertTrue(updates["redeploy"])
        self.assertEqual(updates["redeploy_stacks"], ["arr-stack", "productivity"])

    def test_a_stack_the_repository_does_not_have_is_refused(self):
        result = run(self.supervisor.redeploy("dozzle", actor="x"))
        self.assertFalse(result["ok"])
        self.assertIn("no stack folder named tools", result["error"])

    def test_a_token_without_a_repository_redeploys_nothing(self):
        import dataclasses

        self.supervisor.settings = dataclasses.replace(self.supervisor.settings, deploy_repo="")
        self.assertIn("DOCTOR_DEPLOY_REPO", self.supervisor.redeploy_refusal("arr-stack"))
        self.assertTrue(any("DOCTOR_DEPLOY_REPO is not" in p
                            for p in self.supervisor.settings.validate()))

    def test_before_the_list_is_read_every_stack_may_try(self):
        self.supervisor._deploy_stacks = None
        self.assertIsNone(self.supervisor.redeploy_refusal("tools"))

    def test_settings_say_what_github_last_answered(self):
        sup = self.supervisor
        sup._github = {"checked_at": "2026-10-07T15:00:00+00:00", "error": "",
                       "workflow": {"name": "Deploy stacks", "state": "active", "html_url": ""}}
        rows = {label: value for label, value, _ in sup.github_status()}
        self.assertEqual(rows["Connection"], "ok")
        self.assertEqual(rows["Last checked"], "2026-10-07 15:00 UTC")
        self.assertEqual(rows["Workflow on GitHub"], "Deploy stacks (active)")
        self.assertEqual(rows["Stacks it can redeploy"], "arr-stack, productivity")
        self.assertEqual(rows["Last redeploy"], "none yet")

        self.finish("success", still_behind=False)
        sup._github["error"] = "GitHub refused the repository listing (HTTP 403)"
        rows = {label: value for label, value, _ in sup.github_status()}
        self.assertTrue(rows["Connection"].startswith("error: GitHub refused"))
        self.assertTrue(rows["Last redeploy"].startswith("radarr: succeeded, "))

    def test_the_token_rows_and_the_expiry_warning(self):
        from datetime import datetime, timedelta, timezone

        sup = self.supervisor
        soon = (datetime.now(timezone.utc) + timedelta(days=5, hours=1)).isoformat()
        sup._github = {"checked_at": "2026-10-07T15:00:00+00:00", "error": "", "workflow": None,
                       "kind": "fine-grained", "expires_at": soon, "scopes": None,
                       "permissions": {"metadata": "granted", "contents_read": "granted",
                                       "actions_read": "granted", "actions_write": "missing"}}
        rows = {label: value for label, value, _ in sup.github_status()}
        self.assertEqual(rows["Token type"], "fine-grained")
        self.assertTrue(rows["Expires"].endswith("(in 5 days)"))
        self.assertEqual(rows["Actions write"], "MISSING")
        self.assertEqual(rows["Contents read"], "yes")
        self.assertIn("expires on", sup.github_warning())
        self.assertIn(sup.github_warning(), sup.status()["warnings"])

        sup._github["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.assertIn("has expired", sup.github_warning())
        sup._github["expires_at"] = None
        self.assertEqual(sup.github_warning(), "")
        self.assertEqual({l: v for l, v, _ in sup.github_status()}["Expires"], "never")

    def test_no_github_rows_without_a_token(self):
        import dataclasses
        self.supervisor.settings = dataclasses.replace(self.supervisor.settings, github_token="")
        self.assertEqual(self.supervisor.github_status(), [])

    def test_release_notes_only_for_a_version_the_check_found(self):
        self.supervisor.image_updates["images"] = [{
            "image": "linuxserver/sonarr:4.0.20", "state": "current", "containers": ["sonarr"],
            "source": "Sonarr/Sonarr", "newer": {"tag": "4.1.0"}, "cooling": None}]
        refused = run(self.supervisor.image_release_notes("linuxserver/sonarr:4.0.20", "9.9.9"))
        self.assertFalse(refused["ok"])
        unknown = run(self.supervisor.image_release_notes("someone/else:1.0", "1.1"))
        self.assertFalse(unknown["ok"])


class ListingReleases(unittest.TestCase):
    def test_newest_first_between_the_pin_and_the_target(self):
        releases = [
            {"tag_name": "4.2.0", "body": "not yet", "html_url": "https://github.com/o/r/4.2.0"},
            {"tag_name": "4.1.0.2300-ls301", "name": "4.1", "body": "Breaking: new queue",
             "html_url": "https://github.com/o/r/4.1.0", "published_at": "2026-10-01T00:00:00Z"},
            {"tag_name": "4.0.21", "body": "x" * 30000, "html_url": "https://github.com/o/r/4.0.21"},
            {"tag_name": "4.0.20", "body": "running", "html_url": "https://github.com/o/r/4.0.20"},
            {"tag_name": "4.1.0-rc1", "body": "pre", "prerelease": True},
        ]

        def handler(request):
            return httpx.Response(200, text=json.dumps(releases))

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await release_list(client, "o/r", "4.0.20", "4.1.0")

        notes = run(go())
        self.assertEqual([r["tag"] for r in notes["releases"]], ["4.1.0.2300-ls301", "4.0.21"])
        self.assertIn("new queue", notes["releases"][0]["breaking"])
        self.assertTrue(notes["releases"][1]["truncated"])
        self.assertEqual(len(notes["releases"][1]["body"]), 20000)

    def test_github_refusing_still_leaves_the_link(self):
        def handler(request):
            return httpx.Response(403)

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await release_list(client, "o/r", "1.0", "1.1")

        notes = run(go())
        self.assertEqual(notes["releases"], [])
        self.assertEqual(notes["url"], "https://github.com/o/r/releases")
        self.assertIn("403", notes["detail"])


if __name__ == "__main__":
    unittest.main()
