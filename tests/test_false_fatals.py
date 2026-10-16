"""Two containers opened a `log_fatal` incident every poll, neither in trouble.

Both came from reading a log line's level wrong, in different ways:

  Stirling-PDF  an INFO line whose *message* is "Resource status changed from
                OK to CRITICAL". The scan knew nothing of INFO, read straight
                past it and found CRITICAL in the text.

  Docmost-DB    genuine PostgreSQL FATAL lines - from 2025, still sitting in
                the tail of a container that logs almost nothing, and read as
                a live failure by a poll in 2026.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import Settings  # noqa: E402
from app.detector import Detector  # noqa: E402
from app.redact import leveled_lines  # noqa: E402

STIRLING_INFO = (
    "2026-09-24 16:05:58.123  INFO 1 --- [   scheduling-1] "
    "s.s.c.u.ResourceMonitor  : Resource status changed from OK to CRITICAL "
    "(CPU: 103.9%)"
)
POSTGRES_FATAL = "2025-11-26 10:00:00.000 UTC [1] FATAL:  the database system is starting up"

# One line per app, in the format each actually emits.
REAL_FAILURES = {
    "jellyfin": "[16:08:22] [FTL] [3] Main: Unable to start Kestrel",
    "sonarr": "2026-09-24 16:08:22.1|Fatal|Source|the database went away",
    "sabnzbd": "2026-09-24 16:08:22,527::ERROR::[mod:55] download failed",
    "seerr": "2026-09-24 16:08:22 error: request failed",
    "postgres": POSTGRES_FATAL,
}
BENIGN = {
    "jellyfin": "[16:08:23] [INF] [12] LibraryManager: Loading library",
    "sonarr": "2026-09-24 16:08:22.1|Info|Source|scan complete",
    "spring": STIRLING_INFO,
    "debug": "2026-09-24 16:08:22 debug: cache hit",
    "info about an error": "2026-09-24 10:00:00  INFO  retrying after ERROR from upstream",
}


def detect(log: str, name: str = "stirling-pdf"):
    container = {"id": "c", "name": name, "image": "img", "state": "running",
                 "health": "healthy", "exit_code": 0, "restart_count": 0, "labels": {}}
    stats = {"cpu_pct": 0.1, "cpu_host_pct": 0.1, "online_cpus": 4,
             "mem_usage_bytes": 1, "mem_limit_bytes": 0, "mem_pct": 0.18}
    return Detector(Settings()).from_poll(container, stats, log)


class TheLinesOwnLevelWins(unittest.TestCase):
    def test_a_level_word_in_the_message_is_not_the_level(self):
        counts, _ = leveled_lines(STIRLING_INFO)
        self.assertEqual(counts, {"fatal": 0, "error": 0, "warning": 0})

    def test_no_incident_from_a_tail_of_those_lines(self):
        self.assertEqual(detect("\n".join([STIRLING_INFO] * 30)), [])

    def test_every_benign_level_stays_quiet(self):
        for label, line in BENIGN.items():
            with self.subTest(label):
                counts, _ = leveled_lines("\n".join([line] * 20))
                self.assertEqual(counts, {"fatal": 0, "error": 0, "warning": 0})

    def test_real_failures_are_still_caught(self):
        for app, line in REAL_FAILURES.items():
            with self.subTest(app):
                counts, _ = leveled_lines(line)
                self.assertEqual(sum(counts.values()), 1, counts)

    def test_a_fatal_among_benign_chatter_still_counts(self):
        noise = [STIRLING_INFO, BENIGN["jellyfin"]] * 20
        counts, samples = leveled_lines("\n".join(noise + [REAL_FAILURES["jellyfin"]]))
        self.assertEqual(counts["fatal"], 1)
        self.assertIn("Unable to start Kestrel", samples["fatal"][0])


class OldLinesAreNotNews(unittest.TestCase):
    """The tail of a quiet container reaches back as far as it has run."""

    def test_the_poll_asks_the_daemon_for_recent_lines_only(self):
        import asyncio
        import json
        import threading
        import urllib.parse
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen: list[dict] = []
        inspect = {"Id": "abc", "Name": "/x",
                   "State": {"Status": "running", "ExitCode": 0, "OOMKilled": False,
                             "StartedAt": "2025-01-01T00:00:00Z", "FinishedAt": ""},
                   "Config": {"Image": "i", "Labels": {}, "Tty": False},
                   "HostConfig": {"RestartPolicy": {"Name": "no"}}, "RestartCount": 0}

        class Daemon(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if "/logs" in parsed.path:
                    seen.append(urllib.parse.parse_qs(parsed.query))
                    body = POSTGRES_FATAL.encode()
                elif parsed.path.endswith("/json"):
                    body = json.dumps(inspect).encode()
                else:
                    body = json.dumps({"Version": "27.0", "ApiVersion": "1.45"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Daemon)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        from app.docker_client import DockerClient

        client = DockerClient(f"tcp://127.0.0.1:{server.server_address[1]}")
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=60)
        asyncio.run(client.logs("abc", 120, since=cutoff))

        self.assertEqual(seen[0]["tail"], ["120"])
        sent = int(seen[0]["since"][0])
        self.assertAlmostEqual(sent, int(cutoff.timestamp()), delta=5)

    def test_the_cutoff_follows_the_setting(self):
        from app.supervisor import Supervisor

        cutoff = Supervisor._log_cutoff.__get__(_FakeSupervisor(60))()
        age = (datetime.now(timezone.utc) - cutoff).total_seconds()
        self.assertAlmostEqual(age, 3600, delta=30)

    def test_zero_means_read_the_whole_tail(self):
        from app.supervisor import Supervisor

        self.assertIsNone(Supervisor._log_cutoff.__get__(_FakeSupervisor(0))())


class _FakeSupervisor:
    def __init__(self, minutes: int) -> None:
        self.settings = type("S", (), {"log_max_age_minutes": minutes})()


if __name__ == "__main__":
    unittest.main()
