"""A poll reads stats and logs for several containers at once. With docker-py's
default pool of 10 connections, every request past the tenth threw its
connection away afterwards and urllib3 logged "Connection pool is full,
discarding connection" - two warnings every poll, all day."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import Settings  # noqa: E402
from app.docker_client import DockerClient  # noqa: E402
from app.supervisor import docker_pool_size  # noqa: E402


class Daemon(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.endswith("/version"):
            body = json.dumps({"Version": "27.0", "ApiVersion": "1.45"}).encode()
        else:
            # Slow enough that every request is in flight at the same time.
            time.sleep(0.2)
            body = json.dumps({"Id": "abc", "Name": "/x", "State": {}, "Config": {}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PoolSizeTests(unittest.TestCase):
    def setUp(self):
        # Another suite switches warnings off for the whole run, which would
        # let the no-warning check pass without proving anything.
        self.addCleanup(logging.disable, logging.root.manager.disable)
        logging.disable(logging.NOTSET)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Daemon)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"tcp://127.0.0.1:{server.server_address[1]}"

    def read_at_once(self, client: DockerClient, count: int) -> None:
        async def main():
            # As many threads as requests, as on a host with enough CPUs:
            # asyncio's default pool is sized from the CPU count.
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(count))
            await asyncio.gather(*(client.inspect("abc") for _ in range(count)))

        asyncio.run(main())

    def test_a_poll_of_concurrent_reads_keeps_every_connection(self):
        settings = Settings()
        concurrent = 2 * settings.poll_concurrency
        client = DockerClient(self.url, max_pool_size=docker_pool_size(settings))
        self.addCleanup(client.close)
        with self.assertNoLogs("urllib3.connectionpool", level="WARNING"):
            self.read_at_once(client, concurrent)

    def test_the_default_pool_is_what_overflowed(self):
        client = DockerClient(self.url)
        self.addCleanup(client.close)
        with self.assertLogs("urllib3.connectionpool", level="WARNING") as logs:
            self.read_at_once(client, 12)
        self.assertIn("Connection pool is full", logs.output[0])

    def test_the_pool_grows_with_the_poll_concurrency(self):
        small = dataclasses.replace(Settings(), poll_concurrency=2)
        large = dataclasses.replace(Settings(), poll_concurrency=12)
        self.assertGreater(docker_pool_size(large), 2 * large.poll_concurrency)
        self.assertGreater(docker_pool_size(small), 2 * small.poll_concurrency)


if __name__ == "__main__":
    unittest.main()
