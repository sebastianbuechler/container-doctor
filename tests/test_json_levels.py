"""Severity read from the level field of JSON log lines.

Pocket ID logs JSON (`LOG_JSON=true`), where the level is a quoted value -
`"level":"ERROR"` - that the plain-text scan never recognised, so its errors
counted as nothing at all.
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import Settings  # noqa: E402
from app.detector import Detector  # noqa: E402
from app.redact import json_level, leveled_lines  # noqa: E402

# Docker's own timestamp comes first, since the doctor asks for timestamps.
TS = "2026-09-24T16:08:22.123456789Z "
SLOG = {
    "error": TS + '{"time":"2026-09-24T16:08:22Z","level":"ERROR","msg":"Failed to send email","error":"dial tcp: i/o timeout"}',
    "warning": TS + '{"time":"2026-09-24T16:08:22Z","level":"WARN","msg":"GeoLite2 database missing"}',
    "quiet": TS + '{"time":"2026-09-24T16:08:22Z","level":"INFO","msg":"Server listening"}',
}
PINO = {
    "fatal": '{"level":60,"time":1790000000000,"msg":"cannot bind"}',
    "error": '{"level":50,"time":1790000000000,"msg":"request failed"}',
    "quiet": '{"level":30,"time":1790000000000,"msg":"started"}',
}
ZERO = {"fatal": 0, "error": 0, "warning": 0}


def detect(log: str):
    container = {"id": "c", "name": "pocket-id", "image": "pocket-id", "state": "running",
                 "health": "healthy", "exit_code": 0, "restart_count": 0, "labels": {}}
    stats = {"cpu_pct": 0.1, "cpu_host_pct": 0.1, "online_cpus": 4,
             "mem_usage_bytes": 1, "mem_limit_bytes": 0, "mem_pct": 0.1}
    return Detector(Settings()).from_poll(container, stats, log)


class JsonLevels(unittest.TestCase):
    def test_slog_levels_are_counted(self):
        for level in ("error", "warning"):
            with self.subTest(level):
                counts, samples = leveled_lines(SLOG[level])
                self.assertEqual(counts[level], 1, counts)
                self.assertEqual(len(samples[level]), 1)

    def test_numeric_levels_are_counted(self):
        for level in ("fatal", "error"):
            with self.subTest(level):
                counts, _ = leveled_lines(PINO[level])
                self.assertEqual(counts[level], 1, counts)

    def test_info_lines_stay_quiet(self):
        self.assertEqual(leveled_lines("\n".join([SLOG["quiet"], PINO["quiet"]] * 20))[0], ZERO)

    def test_the_field_wins_over_words_in_the_message(self):
        line = '{"level":"INFO","msg":"retrying after ERROR: upstream FATAL"}'
        self.assertEqual(leveled_lines(line)[0], ZERO)

    def test_a_level_word_in_another_field_is_not_the_level(self):
        # The `error` field holds the cause; only `level` says how bad it is.
        self.assertEqual(json_level('{"level":"info","error":"none"}'), "info")

    def test_non_json_falls_back_to_the_text_scan(self):
        self.assertIsNone(json_level("[16:09:01] [FTL] [3] Main: {broken}"))
        counts, _ = leveled_lines("[16:09:01] [FTL] [3] Main: {broken}")
        self.assertEqual(counts["fatal"], 1)

    def test_json_without_a_level_falls_back_to_the_text_scan(self):
        self.assertIsNone(json_level('{"msg":"hello"}'))
        self.assertEqual(leveled_lines('{"msg":"hello"}')[0], ZERO)

    def test_secrets_in_json_samples_are_redacted(self):
        line = '{"level":"ERROR","msg":"login failed","token":"abcdefghijklmnop"}'
        _, samples = leveled_lines(line)
        self.assertNotIn("abcdefghijklmnop", samples["error"][0])

    def test_a_burst_of_json_errors_opens_an_incident(self):
        signals = detect("\n".join([SLOG["error"]] * 12))
        self.assertEqual([s.kind for s in signals], ["log_errors"])
        self.assertEqual(signals[0].details["source"], "level")

    def test_one_json_fatal_is_enough(self):
        self.assertEqual([s.kind for s in detect(PINO["fatal"])], ["log_fatal"])


if __name__ == "__main__":
    unittest.main()
