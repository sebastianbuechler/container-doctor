"""A reverse proxy's access log is about its clients, not about itself.

Traefik sitting in front of a log viewer records the viewer's own queries -
`?levels=error&levels=fatal&levels=trace` - verbatim in the request URI. Those
words are the client asking for log levels, not Traefik reporting a fault, and
counting them raised an incident against a proxy that was serving 200s.

The lines below are real, taken from the deployed stack.
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import Settings  # noqa: E402
from app.detector import Detector  # noqa: E402
from app.redact import access_log_status, error_lines, leveled_lines  # noqa: E402

DOZZLE_QUERY = (
    '2026-09-24T16:05:58.984040005Z 192.168.2.2 - - [24/Sep/2026:16:05:58 +0000] '
    '"GET /api/hosts/017aa90a-2199-4e96-9631-845fe801a539/containers/dbc3d6c5fc92/logs'
    "?stdout=1&stderr=1&levels=info&levels=debug&levels=warn&levels=error&levels=fatal"
    "&levels=trace&levels=unknown&from=2026-09-08T22%3A42%3A53.266Z"
    '&to=2026-09-08T22%3A43%3A53.266Z&min=4 HTTP/2.0" 200 433 "-" "-" 82698 '
    '"dozzle@docker" "http://172.29.0.2:8080" 71ms'
)
REDIRECT = (
    '2026-09-24T16:06:25.268608982Z 192.168.1.34 - - [24/Sep/2026:16:06:25 +0000] '
    '"GET / HTTP/1.1" 302 0 "-" "-" 82713 "jellyfin@docker" '
    '"http://172.29.0.18:8096" 0ms'
)
SERVER_ERROR = (
    '2026-09-24T16:06:01.146035683Z 192.168.2.2 - - [24/Sep/2026:16:06:01 +0000] '
    '"GET /api/v3/command HTTP/2.0" 502 6 "-" "-" 82706 "radarr@docker" '
    '"http://172.29.0.14:7878" 6ms'
)
# The same requests with `--accesslog.format=json`.
JSON_DOZZLE_QUERY = (
    '2026-09-24T16:05:58.984040005Z {"ClientHost":"192.168.2.2","DownstreamContentSize":433,'
    '"DownstreamStatus":200,"Duration":71000000,"RequestMethod":"GET",'
    '"RequestPath":"/api/hosts/017aa90a/containers/dbc3d6c5fc92/logs?stdout=1&levels=error'
    '&levels=fatal&levels=trace","RequestProtocol":"HTTP/2.0","RouterName":"dozzle@docker",'
    '"ServiceURL":"http://172.29.0.2:8080","level":"info","msg":"","time":"2026-09-24T16:05:58Z"}'
)
JSON_SERVER_ERROR = (
    '2026-09-24T16:06:01.146035683Z {"ClientHost":"192.168.2.2","DownstreamContentSize":6,'
    '"DownstreamStatus":502,"Duration":6000000,"RequestMethod":"GET",'
    '"RequestPath":"/api/v3/command","RequestProtocol":"HTTP/2.0","RouterName":"radarr@docker",'
    '"ServiceURL":"http://172.29.0.14:7878","level":"info","msg":"","time":"2026-09-24T16:06:01Z"}'
)
JSON_APP_ERROR = (
    '2026-09-24T16:08:22.1Z {"level":"error","error":"dial tcp: i/o timeout",'
    '"msg":"Provider error","providerName":"docker","time":"2026-09-24T16:08:22Z"}'
)
REAL_FAILURE = "2026-09-24 16:08:22.1|Error|SomeService|the database went away"


def traefik(log: str):
    container = {"id": "t", "name": "traefik", "image": "traefik:v3", "state": "running",
                 "health": "none", "exit_code": 0, "restart_count": 0, "labels": {}}
    stats = {"cpu_pct": 1.2, "cpu_host_pct": 0.3, "online_cpus": 4,
             "mem_usage_bytes": 1, "mem_limit_bytes": 0, "mem_pct": 0.18}
    return Detector(Settings()).from_poll(container, stats, log)


class AccessLogStatus(unittest.TestCase):
    def test_reads_the_status_code(self):
        self.assertEqual(access_log_status(DOZZLE_QUERY), 200)
        self.assertEqual(access_log_status(REDIRECT), 302)
        self.assertEqual(access_log_status(SERVER_ERROR), 502)

    def test_an_application_log_line_is_not_an_access_log(self):
        self.assertIsNone(access_log_status(REAL_FAILURE))
        self.assertIsNone(access_log_status("GET /thing failed: connection refused"))


class KeywordsInsideARequestUri(unittest.TestCase):
    """The bug as reported: 20 successful requests, one incident."""

    def test_no_incident_from_a_tail_of_successful_requests(self):
        self.assertEqual(traefik("\n".join([DOZZLE_QUERY] * 20)), [])

    def test_the_keyword_scan_skips_them(self):
        import re

        pattern = re.compile(r"(?i)\b(fatal|panic|no space left)\b")
        self.assertEqual(error_lines("\n".join([DOZZLE_QUERY] * 20), pattern), [])

    def test_the_level_scan_skips_them(self):
        counts, _ = leveled_lines("\n".join([DOZZLE_QUERY] * 20))
        self.assertEqual(counts, {"fatal": 0, "error": 0, "warning": 0})


class StatusCodesStillCount(unittest.TestCase):
    def test_a_burst_of_5xx_is_an_incident(self):
        signals = traefik("\n".join([SERVER_ERROR] * 12))
        self.assertEqual([s.kind for s in signals], ["log_errors"])
        self.assertEqual(signals[0].details["source"], "level")

    def test_a_few_5xx_are_not(self):
        self.assertEqual(traefik("\n".join([SERVER_ERROR] * 3)), [])

    def test_an_application_error_is_still_counted_among_access_lines(self):
        noise = [DOZZLE_QUERY, REDIRECT] * 30
        counts, samples = leveled_lines("\n".join(noise + [REAL_FAILURE]))
        self.assertEqual(counts["error"], 1)
        self.assertIn("the database went away", samples["error"][0])



class JsonAccessLog(unittest.TestCase):
    """Traefik runs with `--accesslog.format=json` and `--log.format=json`."""

    def test_reads_the_downstream_status(self):
        self.assertEqual(access_log_status(JSON_DOZZLE_QUERY), 200)
        self.assertEqual(access_log_status(JSON_SERVER_ERROR), 502)

    def test_a_json_application_log_line_is_not_an_access_log(self):
        self.assertIsNone(access_log_status(JSON_APP_ERROR))

    def test_keywords_in_the_request_path_do_not_count(self):
        import re

        pattern = re.compile(r"(?i)\b(fatal|panic|no space left)\b")
        self.assertEqual(error_lines("\n".join([JSON_DOZZLE_QUERY] * 20), pattern), [])
        self.assertEqual(traefik("\n".join([JSON_DOZZLE_QUERY] * 20)), [])

    def test_5xx_count_despite_the_info_level(self):
        counts, _ = leveled_lines("\n".join([JSON_SERVER_ERROR] * 3))
        self.assertEqual(counts["error"], 3)
        signals = traefik("\n".join([JSON_SERVER_ERROR] * 12))
        self.assertEqual([s.kind for s in signals], ["log_errors"])

    def test_an_application_error_is_still_counted(self):
        counts, samples = leveled_lines("\n".join([JSON_DOZZLE_QUERY] * 30 + [JSON_APP_ERROR]))
        self.assertEqual(counts["error"], 1)
        self.assertIn("Provider error", samples["error"][0])


if __name__ == "__main__":
    unittest.main()
