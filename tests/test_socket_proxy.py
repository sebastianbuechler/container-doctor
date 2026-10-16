"""Prove the socket proxy's shipped configuration against the real image.

The settings are read out of compose.example.yaml rather than restated here,
so this tests the configuration the doctor ships with and cannot drift from it.

The property that matters: the doctor can restart a container, and cannot
create one. `POST /containers/create` takes arbitrary host bind mounts, so an
open create endpoint is a container escape - and on the common socket proxies
`CONTAINERS=1` plus a global `POST=1` opens exactly that. LinuxServer's fork
evaluates its per-route ALLOW_* rules *before* the global POST gate, which is
why `ALLOW_RESTARTS=1` with `POST=0` gives restart without create.

Run with DOCTOR_PROXY_INTEGRATION=1 and a running Docker daemon. No host Docker
socket is exposed to any test container.
"""
import os
from pathlib import Path
import re
import subprocess
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "compose.example.yaml"


def proxy_service() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["doctor-socket-proxy"]


_SUBSTITUTION = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*):-(.*)\}$")


def resolve(value: str) -> str:
    """What compose would substitute, so the tests see the deployed value."""
    match = _SUBSTITUTION.match(value.strip())
    if not match:
        return value
    name, default = match.groups()
    return os.environ.get(name) or default


def proxy_env() -> dict:
    return {
        key: resolve(value)
        for key, value in (
            entry.split("=", 1) for entry in proxy_service()["environment"]
        )
    }


class ShippedConfigTests(unittest.TestCase):
    """Cheap checks on the compose file - no daemon needed."""

    def setUp(self):
        self.service = proxy_service()
        self.env = proxy_env()

    def test_the_global_write_gate_stays_shut(self):
        # POST=1 would reach /containers/create, /build and /images/create.
        self.assertEqual(self.env["POST"], "0")

    def test_restarts_are_granted_by_route_not_by_method(self):
        self.assertIn("ALLOW_RESTARTS", self.env)
        self.assertNotIn("CONTAINERS=1, POST=1", str(self.env))

    def test_the_reads_the_doctor_needs_are_on(self):
        for key in ("CONTAINERS", "EVENTS", "PING", "VERSION"):
            self.assertEqual(self.env[key], "1", key)
        # /containers/<id>/logs is denied by the image's defaults.
        self.assertEqual(self.env["ALLOW_LOGS"], "1")

    def test_images_are_readable_for_the_update_check(self):
        """A read grant. POST=0 is what keeps it from meaning pull or prune."""
        self.assertEqual(self.env["IMAGES"], "1")
        self.assertEqual(self.env["POST"], "0")

    def test_networks_are_readable_for_the_network_map(self):
        """A read grant. POST=0 is what keeps it from meaning create,
        connect or remove."""
        self.assertEqual(self.env["NETWORKS"], "1")
        self.assertEqual(self.env["POST"], "0")

    def test_the_dangerous_sections_are_explicitly_off(self):
        for key in ("EXEC", "VOLUMES", "SECRETS", "SWARM", "INFO",
                    "ALLOW_START", "ALLOW_ARCHIVE", "ALLOW_EXPORT", "ALLOW_CHANGES"):
            self.assertEqual(self.env[key], "0", key)

    def test_nothing_needs_building_or_mounting(self):
        # A git deploy (Portainer's, for one) runs `compose pull`; a build never
        # runs, and a relative bind mount resolves on the host, not in its clone.
        self.assertNotIn("build", self.service)
        self.assertEqual(
            self.service["volumes"], ["/var/run/docker.sock:/var/run/docker.sock:ro"]
        )

    def test_the_image_is_pinned(self):
        self.assertNotIn(":latest", self.service["image"])


@unittest.skipUnless(os.environ.get("DOCTOR_PROXY_INTEGRATION") == "1",
                     "requires Docker integration opt-in")
class SocketProxyTests(unittest.TestCase):
    @staticmethod
    def docker(*args):
        return subprocess.check_output(["docker", *args], text=True).strip()

    @classmethod
    def pull(cls, image, attempts=6):
        """Pull once, up front, retrying a registry that rate-limits. Left to
        each `docker run`, every test pulled on its own, and lscr.io answering
        `toomanyrequests` to one of them failed it before it had tested
        anything."""
        for attempt in range(attempts):
            try:
                cls.docker("pull", "--quiet", image)
                return
            except subprocess.CalledProcessError:
                if attempt == attempts - 1:
                    raise
                time.sleep(2 ** attempt)

    @classmethod
    def setUpClass(cls):
        cls.service = proxy_service()
        cls.image = cls.service["image"]
        cls.pull(cls.image)
        cls.pull("python:3.12-slim")
        cls.network = "doctor-proxy-test-" + uuid.uuid4().hex[:12]
        cls.docker("network", "create", cls.network)
        cls.addClassCleanup(cls.docker, "network", "rm", cls.network)
        upstream = cls.network + "-upstream"
        script = """
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def respond(self):
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()
    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = respond
HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
"""
        cls.addClassCleanup(cls.docker, "rm", "-f", upstream)
        cls.docker(
            "run", "-d", "--name", upstream, "--network", cls.network,
            "--network-alias", "upstream", "python:3.12-slim",
            "python", "-B", "-u", "-c", script,
        )

    @staticmethod
    def request(base, method, path):
        try:
            with urlopen(Request(base + path, method=method), timeout=2) as response:
                return response.status
        except HTTPError as exc:
            return exc.code

    def start_proxy(self, allow_restarts):
        """Run the proxy with the compose settings, overriding ALLOW_RESTARTS."""
        env = proxy_env()
        env["ALLOW_RESTARTS"] = str(allow_restarts)
        env["SOCKET_PATH"] = "upstream:8080"

        name = f"{self.network}-r{allow_restarts}"
        self.addCleanup(self.docker, "rm", "-f", name)
        args = ["run", "-d", "--name", name, "--network", self.network,
                "--read-only", "--tmpfs", "/run", "--tmpfs", "/tmp",
                "--security-opt", "no-new-privileges:true",
                "-p", "127.0.0.1::2375"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        self.docker(*args, self.image)

        base = "http://" + self.docker("port", name, "2375/tcp").splitlines()[0]
        for _ in range(60):
            try:
                if self.request(base, "GET", "/_ping") == 200:
                    return base
            except (URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(0.5)
        self.fail("Proxy startup failed: " + self.docker("logs", name))

    def test_reads_the_doctor_needs_are_permitted(self):
        base = self.start_proxy(1)
        for prefix in ("", "/v1.47"):
            for path in ("/_ping", "/version", "/events", "/containers/json?all=1",
                         "/containers/abc123/json", "/containers/abc123/logs?tail=10",
                         "/containers/abc123/stats?stream=false",
                         # the image update check
                         "/images/json", "/images/nginx:latest/json",
                         # the network map
                         "/networks", "/networks/abc123"):
                for method in ("GET", "HEAD"):
                    self.assertEqual(self.request(base, method, prefix + path), 200,
                                     (method, prefix + path))

    def test_restart_is_permitted_but_create_is_not(self):
        """The whole point: a write path narrow enough to be worth having."""
        base = self.start_proxy(1)
        for prefix in ("", "/v1.47"):
            for operation in ("restart", "stop"):
                self.assertEqual(
                    self.request(base, "POST", prefix + f"/containers/abc123/{operation}?t=20"),
                    200, operation,
                )
            # POST /containers/create mounts arbitrary host paths. Never.
            self.assertEqual(self.request(base, "POST", prefix + "/containers/create"), 403)

    def test_every_other_write_is_denied(self):
        base = self.start_proxy(1)
        for prefix in ("", "/v1.47"):
            for method, path in (
                ("POST", "/containers/create"), ("POST", "/containers/abc123/start"),
                ("POST", "/containers/abc123/exec"), ("POST", "/containers/abc123/update"),
                ("POST", "/containers/abc123/pause"), ("POST", "/containers/abc123/unpause"),
                ("DELETE", "/containers/abc123"), ("PUT", "/containers/abc123/archive"),
                ("GET", "/containers/abc123/archive"), ("GET", "/containers/abc123/export"),
                ("GET", "/containers/abc123/top"), ("GET", "/containers/abc123/changes"),
                ("POST", "/images/create"), ("POST", "/images/prune"), ("POST", "/build"),
                ("POST", "/volumes/create"), ("GET", "/volumes"), ("GET", "/secrets"),
                ("GET", "/swarm"), ("GET", "/info"),
                # The network map reads; it must never rewire anything.
                ("POST", "/networks/create"), ("POST", "/networks/prune"),
                ("POST", "/networks/abc123/connect"), ("POST", "/networks/abc123/disconnect"),
                ("DELETE", "/networks/abc123"),
                ("POST", "/exec/abc123/start"),
                ("POST", "/containers/abc123%2f..%2fcreate/restart"),
            ):
                self.assertEqual(self.request(base, method, prefix + path), 403,
                                 (method, prefix + path))

    def test_allow_restarts_zero_leaves_no_write_path_at_all(self):
        base = self.start_proxy(0)
        for operation in ("restart", "stop", "kill"):
            self.assertEqual(
                self.request(base, "POST", f"/containers/abc123/{operation}"), 403, operation,
            )
        # Reads still work, so observe mode is fully functional.
        self.assertEqual(self.request(base, "GET", "/containers/json"), 200)
