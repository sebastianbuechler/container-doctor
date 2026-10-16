"""The repository's account of a stack, read into a diagnosis.

authentik-server was diagnosed as "the image changed, roll it back" after a
commit that only pinned the same tag to its digest. With the commit's diff in
front of it, the diagnosis can see that.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import repo_context  # noqa: E402
from app.diagnoser import _render_incident  # noqa: E402

COMPOSE = """services:
  authentik-postgresql:
    image: postgres:18.6-alpine

  authentik-server:
    image: ghcr.io/goauthentik/server:2026.8.3@sha256:ab9b
    restart: unless-stopped
    # No healthcheck here: the image ships one (ak healthcheck).
    depends_on:
      authentik-postgresql:
        condition: service_healthy

  authentik-worker:
    image: ghcr.io/goauthentik/server:2026.8.3@sha256:ab9b
"""

PIN = """@@ -83,7 +83,7 @@
   authentik-server:
-    image: ghcr.io/goauthentik/server:2026.8.3
+    image: ghcr.io/goauthentik/server:2026.8.3@sha256:ab9b"""


class FakeGitHub:
    def __init__(self, status: dict[str, int] | None = None) -> None:
        self.status = status or {}
        self.calls: list[str] = []

    async def get(self, url: str, params=None, headers=None):
        self.calls.append(url)
        path = url.split("/repos/o/r/", 1)[1]
        code = self.status.get(path.split("/")[0], 200)
        if path == "contents/infrastructure/docker-compose.yml":
            return SimpleNamespace(status_code=code, text=COMPOSE, json=lambda: {})
        if path.startswith("contents/"):
            return SimpleNamespace(status_code=404, text="", json=lambda: {})
        if path == "commits":
            return SimpleNamespace(status_code=code, json=lambda: [{
                "sha": "f207dfc1234567", "html_url": "https://github.com/o/r/commit/f207dfc",
                "commit": {"message": "Pin every compose image to its digest\n\nSame tags.",
                           "committer": {"date": "2026-10-07T17:46:55Z"}}}])
        if path == "commits/f207dfc1234567":
            return SimpleNamespace(status_code=code, json=lambda: {"files": [
                {"filename": "infrastructure/docker-compose.yml", "patch": PIN},
                {"filename": "arr-stack/docker-compose.yml", "patch": "elsewhere"}]})
        raise AssertionError(url)


def read(client: FakeGitHub) -> dict:
    return asyncio.run(repo_context.stack_context(
        client, "token", "o/r", "main", "infrastructure", "authentik-server"))


class Reading(unittest.TestCase):
    def test_the_service_definition_keeps_its_comments_and_stops_at_the_next(self):
        context = read(FakeGitHub())
        self.assertEqual(context["compose_path"], "infrastructure/docker-compose.yml")
        self.assertIn("No healthcheck here", context["definition"])
        self.assertIn("condition: service_healthy", context["definition"])
        self.assertNotIn("authentik-worker", context["definition"])
        self.assertNotIn("postgres:18.6", context["definition"])

    def test_commits_carry_only_this_stacks_diffs(self):
        [commit] = read(FakeGitHub())["commits"]
        self.assertEqual(commit["sha"], "f207dfc")
        self.assertEqual([f["path"] for f in commit["files"]],
                         ["infrastructure/docker-compose.yml"])
        self.assertIn("@sha256:ab9b", commit["files"][0]["patch"])

    def test_a_refusal_is_said_not_raised(self):
        context = read(FakeGitHub({"commits": 403}))
        self.assertIn("HTTP 403", context["error"])
        self.assertEqual(context["commits"], [])
        self.assertIn("authentik-server", context["definition"])

    def test_github_unreachable_leaves_the_incident_alone(self):
        class Down:
            async def get(self, *args, **kwargs):
                raise ConnectionError("no route")
        context = read(Down())
        self.assertIn("could not reach GitHub", context["error"])

    def test_a_commit_is_placed_against_the_container(self):
        self.assertTrue(repo_context.deployed("2026-10-07T17:46:55Z",
                                              "2026-10-07T18:17:08.123456789Z"))
        self.assertFalse(repo_context.deployed("2026-10-07T19:00:00Z",
                                               "2026-10-07T18:17:08.123456789Z"))
        self.assertIsNone(repo_context.deployed("2026-10-07T19:00:00Z", ""))


class InThePrompt(unittest.TestCase):
    def test_the_diff_and_the_message_reach_the_diagnosis(self):
        context = read(FakeGitHub())
        context["commits"][0]["deployed"] = True
        prompt = _render_incident({"kind": "log_fatal",
                                   "detected_at": "2026-10-08T01:28:37+00:00",
                                   "container": {"name": "authentik-server"},
                                   "repo": context})
        self.assertIn("## Stack `infrastructure` in git (o/r, branch main)", prompt)
        self.assertIn("Pin every compose image to its digest", prompt)
        self.assertIn("8 h before detection", prompt)
        self.assertIn("in what it runs", prompt)
        self.assertIn("+    image: ghcr.io/goauthentik/server:2026.8.3@sha256:ab9b", prompt)
        self.assertIn("only pins an image to its digest", prompt)

    def test_nothing_read_is_nothing_shown(self):
        prompt = _render_incident({"kind": "log_fatal", "container": {"name": "x"},
                                   "repo": {"definition": "", "commits": [], "error": "x"}})
        self.assertNotIn("in git", prompt)


if __name__ == "__main__":
    unittest.main()
