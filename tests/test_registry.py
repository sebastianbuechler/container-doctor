"""Which of thirty containers on `:latest` are actually behind.

Report only: the doctor never pulls. It could not if it wanted to - the socket
proxy denies POST /images - and updating a NAS unattended is how you find out
at 3am which service did not survive it.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

import httpx  # noqa: E402

from app.registry import (  # noqa: E402
    DEFAULT_REGISTRY,
    Reference,
    compare,
    parse_reference,
    remote_digest,
)

LOCAL = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
REMOTE = "sha256:2222222222222222222222222222222222222222222222222222222222222222"


class ReadingAnImageReference(unittest.TestCase):
    def test_a_bare_name_is_a_docker_hub_library_image(self):
        parsed = parse_reference("nginx")
        self.assertEqual(parsed.registry, DEFAULT_REGISTRY)
        self.assertEqual(parsed.repository, "library/nginx")
        self.assertEqual(parsed.tag, "latest")

    def test_a_user_image_keeps_its_namespace(self):
        self.assertEqual(parse_reference("docmost/docmost").repository, "docmost/docmost")

    def test_a_host_is_told_apart_from_a_user_by_its_dot(self):
        self.assertEqual(
            parse_reference("ghcr.io/sebastianbuechler/container-doctor:latest"),
            Reference("ghcr.io", "sebastianbuechler/container-doctor", "latest"),
        )

    def test_a_port_is_not_a_tag(self):
        """registry:5000/team/app:dev - the first colon is a port."""
        parsed = parse_reference("registry:5000/team/app:dev")
        self.assertEqual(parsed.registry, "registry:5000")
        self.assertEqual(parsed.repository, "team/app")
        self.assertEqual(parsed.tag, "dev")

    def test_localhost_counts_as_a_registry_without_a_dot(self):
        self.assertEqual(parse_reference("localhost/thing").registry, "localhost")

    def test_a_digest_reference_is_pinned(self):
        self.assertTrue(parse_reference(f"postgres@{LOCAL}").pinned)

    def test_a_tag_pinned_to_a_digest_keeps_its_tag(self):
        """How the compose files pin: the tag still names the release line."""
        parsed = parse_reference(f"linuxserver/sabnzbd:5.1.3@{LOCAL}")
        self.assertEqual(parsed, Reference(
            "registry-1.docker.io", "linuxserver/sabnzbd", "5.1.3", LOCAL))
        self.assertTrue(parsed.pinned)
        self.assertEqual(str(parsed), f"registry-1.docker.io/linuxserver/sabnzbd:5.1.3@{LOCAL}")

    def test_a_port_is_not_a_tag_in_front_of_a_digest_either(self):
        parsed = parse_reference(f"registry:5000/team/app@{LOCAL}")
        self.assertEqual((parsed.registry, parsed.repository, parsed.tag),
                         ("registry:5000", "team/app", ""))
        self.assertTrue(parsed.pinned)

    def test_a_tag_alone_is_not_pinned(self):
        self.assertFalse(parse_reference("linuxserver/sabnzbd:5.1.3").pinned)

    def test_nonsense_is_refused_rather_than_guessed_at(self):
        self.assertIsNone(parse_reference(""))
        self.assertIsNone(parse_reference("   "))


class JudgingWhatWeFound(unittest.TestCase):
    def digests(self, repo="library/nginx", digest=LOCAL):
        return [f"{repo}@{digest}"]

    def test_matching_digests_are_current(self):
        status = compare("nginx", self.digests(), LOCAL, "")
        self.assertEqual(status.state, "current")
        self.assertFalse(status.update_available)

    def test_differing_digests_are_an_update(self):
        status = compare("nginx", self.digests(), REMOTE, "")
        self.assertTrue(status.update_available)

    def test_a_pinned_image_is_never_chased(self):
        status = compare(f"postgres@{LOCAL}", [], "", "")
        self.assertEqual(status.state, "pinned")

    def test_a_private_repository_is_reported_not_assumed_current(self):
        status = compare("ghcr.io/me/app:latest", [], "", "needs credentials")
        self.assertEqual(status.state, "denied")
        self.assertFalse(status.update_available)

    def test_an_unreachable_registry_is_unknown(self):
        status = compare("nginx", self.digests(), "", "could not reach x (ConnectError)")
        self.assertEqual(status.state, "unknown")

    def test_an_image_never_pulled_from_a_registry_is_unknown(self):
        """A locally built image carries no RepoDigests."""
        status = compare("nginx", [], REMOTE, "")
        self.assertEqual(status.state, "unknown")
        self.assertIn("no registry digest", status.detail)

    def test_the_digest_for_the_right_repository_is_chosen(self):
        """An image tagged into several repositories carries several digests."""
        status = compare(
            "nginx",
            [f"other/thing@{REMOTE}", f"library/nginx@{LOCAL}"],
            LOCAL,
            "",
        )
        self.assertEqual(status.state, "current")


class StubRegistry:
    """A v2 registry, including the anonymous token dance."""

    def __init__(self, *, require_token=True, private=False, digest=REMOTE,
                 status=200):
        self.requests: list[str] = []
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _finish(self, status, headers=None, body=b""):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/token":
                    if private:
                        return self._finish(403)
                    body = json.dumps({"token": "t-1"}).encode()
                    return self._finish(200, {"Content-Type": "application/json"}, body)
                return self._finish(404)

            def do_HEAD(self):
                registry.requests.append(self.headers.get("Authorization", "anonymous"))
                authorised = self.headers.get("Authorization") == "Bearer t-1"
                if require_token and not authorised:
                    return self._finish(401, {
                        "WWW-Authenticate":
                            f'Bearer realm="http://127.0.0.1:{registry.port}/token",'
                            f'service="stub"',
                    })
                if status != 200:
                    return self._finish(status)
                return self._finish(200, {"Docker-Content-Digest": digest})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class AskingTheRegistry(unittest.TestCase):
    """`https://` is hard-coded, so point httpx at the stub over http."""

    def ask(self, registry, repository="team/app", tag="latest"):
        base = f"http://{registry.host}"

        async def run():
            async with httpx.AsyncClient(timeout=5) as client:
                # The module hard-codes https://; send it to the stub over http.
                secure = "https://" + registry.host
                original_head, original_get = client.head, client.get

                async def head(url, **kwargs):
                    return await original_head(url.replace(secure, base), **kwargs)

                async def get(url, **kwargs):
                    return await original_get(url.replace(secure, base), **kwargs)

                client.head, client.get = head, get
                return await remote_digest(
                    client, Reference(registry.host, repository, tag))

        return asyncio.run(run())

    def test_it_follows_the_bearer_challenge_and_returns_the_digest(self):
        registry = StubRegistry()
        self.addCleanup(registry.stop)
        digest, reason = self.ask(registry)
        self.assertEqual(digest, REMOTE)
        self.assertEqual(reason, "")
        # anonymous first, then with the token it was handed
        self.assertEqual(registry.requests, ["anonymous", "Bearer t-1"])

    def test_a_registry_that_wants_no_token_is_fine_too(self):
        registry = StubRegistry(require_token=False)
        self.addCleanup(registry.stop)
        digest, reason = self.ask(registry)
        self.assertEqual(digest, REMOTE)
        self.assertEqual(registry.requests, ["anonymous"])

    def test_a_private_repository_says_so(self):
        registry = StubRegistry(private=True)
        self.addCleanup(registry.stop)
        digest, reason = self.ask(registry)
        self.assertEqual(digest, "")
        self.assertEqual(reason, "needs credentials")

    def test_a_rate_limited_registry_says_so_in_words(self):
        """Docker Hub meters manifest reads; "HTTP 429" tells a user nothing."""
        registry = StubRegistry(require_token=False, status=429)
        self.addCleanup(registry.stop)
        digest, reason = self.ask(registry)
        self.assertEqual(digest, "")
        self.assertEqual(reason, "registry rate limit reached")

    def test_an_unreachable_registry_is_a_reason_not_an_exception(self):
        async def run():
            async with httpx.AsyncClient(timeout=2) as client:
                return await remote_digest(
                    client, Reference("127.0.0.1:9", "team/app", "latest"))

        digest, reason = asyncio.run(run())
        self.assertEqual(digest, "")
        self.assertIn("could not reach", reason)

    def test_it_asks_for_every_manifest_type(self):
        """A multi-architecture image answers the wrong digest otherwise."""
        from app.registry import MANIFEST_TYPES

        for wanted in ("oci.image.index", "manifest.list", "manifest.v2"):
            with self.subTest(wanted):
                self.assertIn(wanted, MANIFEST_TYPES)


class FakeDaemon(BaseHTTPRequestHandler):
    """Enough of the Docker API for a Supervisor to start and inspect images."""

    images: dict[str, list[str]] = {}
    permit_images = True
    asked: list[str] = []

    def log_message(self, *args):
        pass

    def _json(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if "/version" in path:
            return self._json(200, {"Version": "27.0", "ApiVersion": "1.45"})
        if "/images/" in path and path.endswith("/json"):
            FakeDaemon.asked.append(
                urllib.parse.unquote(path.split("/images/")[1][: -len("/json")]))
            if not FakeDaemon.permit_images:
                # What the socket proxy answers with IMAGES=0.
                return self._json(403, {"message": "forbidden"})
            name = urllib.parse.unquote(path.split("/images/")[1][: -len("/json")])
            digests = FakeDaemon.images.get(name)
            if digests is None:
                return self._json(404, {"message": "no such image"})
            return self._json(200, {"RepoDigests": digests})
        return self._json(200, [])


class ThroughTheSupervisor(unittest.TestCase):
    """The whole check, as it runs on the NAS."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeDaemon)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeDaemon.images = {}
        FakeDaemon.asked = []
        FakeDaemon.permit_images = True

    def supervisor(self, **env):
        from app.config import Settings
        from app.supervisor import Supervisor

        previous = dict(os.environ)
        os.environ.update(
            DOCTOR_DATA_DIR=tempfile.mkdtemp(),
            DOCTOR_MODE="observe",
            ANTHROPIC_API_KEY="",
            DOCKER_HOST=f"tcp://127.0.0.1:{self.port}",
            **env,
        )
        try:
            supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(supervisor.store.close)
        return supervisor

    def check(self, containers, **env):
        supervisor = self.supervisor(**env)
        supervisor.containers = {
            c["name"]: c for c in containers
        }
        asyncio.run(supervisor._check_image_updates())
        return supervisor.image_updates

    def test_a_denied_endpoint_says_so_once_rather_than_thirty_times(self):
        """The proxy ships with IMAGES=0; this is the default on the NAS."""
        FakeDaemon.permit_images = False
        result = self.check([
            {"name": "a", "image": "nginx"},
            {"name": "b", "image": "redis"},
        ])
        self.assertTrue(result["needs_images_permission"])
        self.assertEqual({r["state"] for r in result["images"]}, {"unknown"})
        self.assertIn("does not permit", result["images"][0]["detail"])
        self.assertEqual(result["available"], 0)

    def test_a_pinned_image_stays_pinned_even_when_images_are_denied(self):
        FakeDaemon.permit_images = False
        result = self.check([{"name": "a", "image": f"postgres@{LOCAL}"}])
        self.assertEqual(result["images"][0]["state"], "pinned")

    def test_a_pinned_image_costs_no_lookups_at_all(self):
        """No registry call and no daemon call: a digest names one image."""
        result = self.check([{"name": "a", "image": f"postgres@{LOCAL}"}])
        self.assertEqual(FakeDaemon.asked, [])
        self.assertEqual(result["images"][0]["state"], "pinned")
        self.assertFalse(result["needs_images_permission"])

    def test_a_version_pinned_to_a_digest_is_pinned_but_still_followed(self):
        """Its labels are read, for the release line - the registry digest is not."""
        image = f"127.0.0.1:9/team/app:1.2.0@{LOCAL}"
        result = self.check([{"name": "a", "image": image}])
        self.assertEqual(FakeDaemon.asked, [image])
        self.assertEqual(result["images"][0]["state"], "pinned")
        self.assertEqual(result["available"], 0)

    def test_a_tagged_image_is_looked_up(self):
        """The counterpart, so the test above is not vacuously true.

        An unroutable registry keeps this offline - the point here is that the
        daemon was asked, not what the registry said.
        """
        result = self.check([{"name": "a", "image": "127.0.0.1:9/team/app:v1"}])
        self.assertEqual(FakeDaemon.asked, ["127.0.0.1:9/team/app:v1"])
        self.assertEqual(result["images"][0]["state"], "unknown")

    def test_progress_is_announced_image_by_image(self):
        """The dashboard counts along instead of watching a bare spinner."""
        supervisor = self.supervisor()
        supervisor.containers = {
            "a": {"name": "a", "image": f"postgres@{LOCAL}"},
            "b": {"name": "b", "image": f"redis@{LOCAL}"},
        }
        queue = asyncio.Queue()
        supervisor._subscribers.add(queue)
        asyncio.run(supervisor._check_image_updates())
        steps = []
        while not queue.empty():
            message = queue.get_nowait()
            if message["type"] == "images":
                steps.append(message["payload"]["progress"])
        self.assertEqual(steps, [
            {"done": 0, "total": 2, "current": f"postgres@{LOCAL}"},
            {"done": 1, "total": 2, "current": f"redis@{LOCAL}"},
            {"done": 2, "total": 2, "current": None},
        ])

    def test_containers_are_listed_against_their_image(self):
        FakeDaemon.permit_images = False
        result = self.check([
            {"name": "sonarr", "image": "lscr.io/linuxserver/sonarr:latest"},
            {"name": "radarr", "image": "lscr.io/linuxserver/sonarr:latest"},
        ])
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["images"][0]["containers"], ["radarr", "sonarr"])

    def test_nothing_running_is_not_an_error(self):
        supervisor = self.supervisor()
        supervisor.containers = {}
        asyncio.run(supervisor._check_image_updates())
        self.assertEqual(supervisor.image_updates["images"], [])

    def test_the_check_can_be_turned_off(self):
        from app.config import Settings

        previous = dict(os.environ)
        os.environ.update(DOCTOR_DATA_DIR=tempfile.mkdtemp(),
                          DOCTOR_IMAGE_CHECK_HOURS="0")
        try:
            self.assertEqual(Settings().image_check_hours, 0)
        finally:
            os.environ.clear()
            os.environ.update(previous)


if __name__ == "__main__":
    unittest.main()
