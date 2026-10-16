"""Severity taken from the log's own level marker, not just keywords.

The apps in arr-stack cannot be made to log JSON - Sonarr/Radarr have an open
feature request for it, SABnzbd and seerr do not offer it - but every one of
them puts a level on the line. Parsing that is what lets the doctor tell a
fatal apart from chatter without asking the apps to change format.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import ContainerPolicy, Settings  # noqa: E402
from app.detector import Detector  # noqa: E402
from app.redact import leveled_lines  # noqa: E402

# One line per level, in the format each app actually emits.
FORMATS = {
    "jellyfin": {
        "error": "[16:08:22] [ERR] [54] ProviderManager: StudiosImageProvider failed",
        "fatal": "[16:09:01] [FTL] [3] Main: Unable to start Kestrel",
        "warning": "[16:08:24] [WRN] [9] Api: Slow response",
        "quiet": "[16:08:23] [INF] [12] LibraryManager: Loading library",
    },
    "sonarr": {
        "error": "2026-09-24 16:08:23.4|Error|DownloadService|Couldn't add report",
        "fatal": "2026-09-24 16:08:25.2|Fatal|Bootstrap|EPIC FAIL",
        "warning": "2026-09-24 16:08:24.0|Warn|IndexerProxy|Indexer slow",
        "quiet": "2026-09-24 16:08:22.1|Info|SeriesService|Updating series",
    },
    "sabnzbd": {
        "error": "2026-09-24 16:08:23,529::ERROR::[misc:522] Cannot create directory",
        "fatal": "2026-09-24 16:08:25,001::CRITICAL::[__init__:90] Fatal error",
        "warning": "2026-09-24 16:08:24,100::WARNING::[nzbqueue:88] Job failed",
        "quiet": "2026-09-24 16:08:22,527::INFO::[_cplogging:55] Serving on 0.0.0.0",
    },
    "seerr": {
        "error": "2026-09-24 16:08:23 error: Failed to fetch from Sonarr",
        "fatal": "2026-09-24 16:08:25 fatal: Unrecoverable startup failure",
        "warning": "2026-09-24 16:08:24 warn: Slow response",
        "quiet": "2026-09-24 16:08:22 debug: cache hit",
    },
}


def make_settings(**overrides: str) -> Settings:
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        return Settings()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class LevelParsingTests(unittest.TestCase):
    def test_every_app_format_is_understood(self):
        for app, lines in FORMATS.items():
            with self.subTest(app=app):
                counts, _ = leveled_lines("\n".join(lines.values()))
                self.assertEqual(counts["error"], 1, f"{app} error")
                self.assertEqual(counts["fatal"], 1, f"{app} fatal")
                self.assertEqual(counts["warning"], 1, f"{app} warning")

    def test_info_and_debug_never_count(self):
        quiet = "\n".join(lines["quiet"] for lines in FORMATS.values())
        counts, _ = leveled_lines(quiet)
        self.assertEqual(counts, {"fatal": 0, "error": 0, "warning": 0})

    def test_a_word_containing_error_is_not_a_level(self):
        # "ErrorHandler" or "terror" must not read as an ERROR line.
        counts, _ = leveled_lines("[16:00:00] [INF] [1] ErrorHandler: registered\n"
                                  "2026-01-01 16:00:00 info: no terrors here")
        self.assertEqual(counts["error"], 0)

    def test_samples_are_redacted(self):
        counts, samples = leveled_lines(
            "2026-09-24 16:08:23 error: auth failed token=sk-ant-supersecretvalue123"
        )
        self.assertEqual(counts["error"], 1)
        self.assertNotIn("supersecretvalue", samples["error"][0])

    def test_ignored_lines_are_not_counted(self):
        text = "\n".join([FORMATS["jellyfin"]["error"]] * 5)
        self.assertEqual(leveled_lines(text)[0]["error"], 5)
        ignored = leveled_lines(text, ignore=re.compile("StudiosImageProvider"))[0]
        self.assertEqual(ignored["error"], 0)


class DetectorTierTests(unittest.TestCase):
    """A fatal is worth an incident alone; errors only in volume."""

    def container(self, **labels):
        return {"id": "c", "name": "jellyfin", "image": "jellyfin/jellyfin:12.1",
                "state": "running", "health": "none", "exit_code": 0,
                "restart_count": 0, "labels": labels}

    def detect(self, log, settings=None, **labels):
        detector = Detector(settings or make_settings())
        stats = {"cpu_pct": 5.0, "cpu_host_pct": 1.0, "online_cpus": 4,
                 "mem_usage_bytes": 1, "mem_limit_bytes": 0, "mem_pct": 0.0}
        return detector.from_poll(self.container(**labels), stats, log)

    def test_one_fatal_line_is_enough(self):
        signals = self.detect(FORMATS["jellyfin"]["fatal"])
        self.assertEqual([s.kind for s in signals], ["log_fatal"])
        self.assertEqual(signals[0].severity, "critical")

    def test_a_handful_of_errors_is_not_enough(self):
        signals = self.detect("\n".join([FORMATS["jellyfin"]["error"]] * 3))
        self.assertEqual(signals, [])

    def test_a_burst_of_errors_opens_a_warning(self):
        settings = make_settings(DOCTOR_LOG_LEVEL_ERROR_BURST="10")
        signals = self.detect("\n".join([FORMATS["jellyfin"]["error"]] * 12),
                              settings=settings)
        self.assertEqual([s.kind for s in signals], ["log_errors"])
        self.assertEqual(signals[0].severity, "warning")
        self.assertEqual(signals[0].details["source"], "level")

    def test_a_fatal_supersedes_the_error_burst(self):
        # One incident, not two, and the one that matters.
        log = "\n".join([FORMATS["sonarr"]["error"]] * 12 + [FORMATS["sonarr"]["fatal"]])
        signals = self.detect(log)
        self.assertEqual([s.kind for s in signals], ["log_fatal"])

    def test_the_keyword_rule_still_catches_unleveled_logs(self):
        # A container that prints no level at all must still be covered.
        log = "\n".join(["something went wrong: no space left on device"] * 6)
        signals = self.detect(log)
        self.assertEqual([s.kind for s in signals], ["log_errors"])
        self.assertEqual(signals[0].details["source"], "keyword")

    def test_ignore_label_silences_the_burst(self):
        log = "\n".join([FORMATS["jellyfin"]["error"]] * 12)
        self.assertEqual(
            self.detect(log, **{"doctor.ignore_log": "StudiosImageProvider"}), []
        )

    def test_ignore_label_does_not_silence_a_fatal(self):
        """The property that makes suppression safe to hand out."""
        log = "\n".join([FORMATS["jellyfin"]["error"]] * 12 + [FORMATS["jellyfin"]["fatal"]])
        signals = self.detect(log, **{"doctor.ignore_log": "StudiosImageProvider"})
        self.assertEqual([s.kind for s in signals], ["log_fatal"])

    def test_ignore_label_also_applies_to_the_keyword_rule(self):
        log = "\n".join(["backup job: no space left on device (scratch volume)"] * 8)
        self.assertEqual(self.detect(log, **{"doctor.ignore_log": "scratch volume"}), [])

    def test_a_broken_ignore_regex_is_ignored_not_fatal(self):
        log = "\n".join([FORMATS["jellyfin"]["error"]] * 12)
        signals = self.detect(log, **{"doctor.ignore_log": "([unclosed"})
        self.assertEqual([s.kind for s in signals], ["log_errors"])

    def test_the_real_jellyfin_log_is_now_caught(self):
        """The log that prompted this: previously matched nothing at all."""
        block = (
            "[16:08:22] [ERR] [54] MediaBrowser.Providers.Manager.ProviderManager: "
            "StudiosImageProvider failed in GetImageInfos for type Studio\n"
            "System.Net.Http.HttpRequestException: Resource temporarily unavailable "
            "(raw.githubusercontent.com:443)\n"
        )
        signals = self.detect(block * 12)
        self.assertEqual([s.kind for s in signals], ["log_errors"])


class LibraryScanFingerprintTests(unittest.TestCase):
    """Each scan names different broken movies; it is still one incident."""

    def detect(self, log):
        settings = make_settings(DOCTOR_LOG_LEVEL_ERROR_BURST="5")
        return DetectorTierTests.detect(DetectorTierTests(), log, settings=settings)

    @staticmethod
    def scan(movies, clock="16:08"):
        lines = []
        for n, movie in enumerate(movies):
            lines.append(
                f"[{clock}:{n:02d}] [ERR] [{40 + n}] MediaBrowser.Providers.MediaInfo."
                f"FFProbeProvider: Error probing /media/movies/{movie}/{movie}.mkv"
            )
            lines.append(
                f"[{clock}:{n:02d}] [ERR] [{40 + n}] MediaBrowser.MediaEncoding.Probing: "
                f"/media/movies/{movie}/{movie}.mkv: Invalid data found when processing input"
            )
        return "\n".join(lines)

    def test_different_movies_give_the_same_fingerprint(self):
        first = self.detect(self.scan(["Heat (1995)", "Alien (1979)", "Up (2009)"]))
        second = self.detect(self.scan(
            ["Crouching Tiger, Hidden Dragon (2000)", "Brazil (1985)", "Ran (1985)"],
            clock="17:41"))
        self.assertEqual([s.kind for s in first], ["log_errors"])
        self.assertEqual(first[0].fingerprint, second[0].fingerprint)

    def test_iso_images_in_the_mix_do_not_split_it(self):
        base = self.scan(["Heat (1995)", "Alien (1979)", "Up (2009)"])
        iso = ("[16:09:00] [ERR] [7] MediaBrowser.MediaEncoding.Probing: "
               "/media/movies/Ran (1985)/Ran.iso: Invalid data found when processing input")
        self.assertEqual(self.detect(base)[0].fingerprint,
                         self.detect(base + "\n" + iso)[0].fingerprint)

    def test_a_different_failure_still_gets_its_own(self):
        probe = self.detect(self.scan(["Heat (1995)", "Alien (1979)", "Up (2009)"]))
        db = self.detect("\n".join(
            [f"[16:08:{n:02d}] [ERR] [3] Jellyfin.Database: SQLite Error 5: 'database is locked'"
             for n in range(8)]))
        self.assertNotEqual(probe[0].fingerprint, db[0].fingerprint)


class PolicyLabelTests(unittest.TestCase):
    def test_ignore_log_label_compiles(self):
        policy = ContainerPolicy.from_labels({"doctor.ignore_log": "Studios|Trailer"})
        self.assertIsNotNone(policy.ignore_log_regex)
        self.assertTrue(policy.ignore_log_regex.search("StudiosImageProvider failed"))

    def test_absent_label_means_no_suppression(self):
        self.assertIsNone(ContainerPolicy.from_labels({}).ignore_log_regex)

    def test_an_invalid_regex_does_not_raise(self):
        self.assertIsNone(ContainerPolicy.from_labels({"doctor.ignore_log": "(["}).ignore_log_regex)


if __name__ == "__main__":
    unittest.main()
