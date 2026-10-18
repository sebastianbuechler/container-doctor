"""A log line is news once, on the poll that first reads it.

authentik-server logged one gunicorn `WORKER TIMEOUT` at level critical. The
log tail is re-read every poll for an hour, so that one line was "seen 78
times", and after the incident was closed it was still in the window to open
another. Only lines written since the last poll can fire a rule now.
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import Settings  # noqa: E402
from app.detector import Detector, _lines_after, log_stamp  # noqa: E402
from app.store import Store  # noqa: E402

TIMEOUT = ('2026-10-08T01:27:45.687889411Z {"event": "WORKER TIMEOUT (pid:167)", '
           '"level": "critical", "logger": "gunicorn.error"}')
HEALTH = '{ts} {{"event": "/health/live/", "level": "info", "status": 200}}'
ERROR = '{ts} {{"event": "upstream refused", "level": "error"}}'

CONTAINER = {"id": "c", "name": "authentik-server", "image": "img", "state": "running",
             "health": "healthy", "exit_code": 0, "restart_count": 0, "labels": {}}


def kinds(detector: Detector, log: str) -> list[str]:
    return [s.kind for s in detector.from_poll(dict(CONTAINER), None, log)]


class AFatalCountsOnce(unittest.TestCase):
    def test_the_same_tail_read_again_is_not_another_sighting(self):
        detector = Detector(Settings())
        log = "\n".join([TIMEOUT, HEALTH.format(ts="2026-10-08T01:28:02.394309104Z")])
        self.assertEqual(kinds(detector, log), ["log_fatal"])
        self.assertEqual(kinds(detector, log), [])

    def test_new_quiet_lines_after_it_do_not_bring_it_back(self):
        detector = Detector(Settings())
        log = TIMEOUT
        kinds(detector, log)
        log += "\n" + HEALTH.format(ts="2026-10-08T01:28:30.1Z")
        self.assertEqual(kinds(detector, log), [])

    def test_a_second_fatal_is_a_sighting(self):
        detector = Detector(Settings())
        kinds(detector, TIMEOUT)
        again = TIMEOUT + "\n" + TIMEOUT.replace("01:27:45.687889411", "01:58:00.5")
        self.assertEqual(kinds(detector, again), ["log_fatal"])

    def test_a_restart_of_the_doctor_remembers_what_it_read(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.addCleanup(store.close)
        first = Detector(Settings())
        kinds(first, TIMEOUT)
        store.save_log_marks(first.drain_log_marks())

        second = Detector(Settings())
        second.load_log_marks(store.log_marks())
        self.assertEqual(kinds(second, TIMEOUT), [])

    def test_only_moved_marks_are_drained(self):
        detector = Detector(Settings())
        kinds(detector, TIMEOUT)
        self.assertEqual(set(detector.drain_log_marks()), {"authentik-server"})
        kinds(detector, TIMEOUT)
        self.assertEqual(detector.drain_log_marks(), {})


class ABurstNeedsANewLine(unittest.TestCase):
    def burst(self, count: int, start_minute: int = 10) -> list[str]:
        return [ERROR.format(ts=f"2026-10-08T01:{start_minute + i:02d}:00Z")
                for i in range(count)]

    def test_a_burst_fires_once_and_not_again_on_the_same_lines(self):
        detector = Detector(Settings())
        settings = detector.settings
        log = "\n".join(self.burst(settings.log_level_error_burst))
        self.assertEqual(kinds(detector, log), ["log_errors"])
        self.assertEqual(kinds(detector, log), [])

    def test_one_more_error_in_a_window_already_over_the_burst_is_a_sighting(self):
        detector = Detector(Settings())
        count = detector.settings.log_level_error_burst
        lines = self.burst(count)
        kinds(detector, "\n".join(lines))
        lines.append(ERROR.format(ts="2026-10-08T01:59:00Z"))
        self.assertEqual(kinds(detector, "\n".join(lines)), ["log_errors"])

    def test_a_burst_still_adds_up_across_polls(self):
        detector = Detector(Settings())
        count = detector.settings.log_level_error_burst
        lines = self.burst(count)
        self.assertEqual(kinds(detector, "\n".join(lines[:-1])), [])
        self.assertEqual(kinds(detector, "\n".join(lines)), ["log_errors"])


class Timestamps(unittest.TestCase):
    def test_trailing_zeros_docker_drops_do_not_break_the_order(self):
        # RFC3339Nano writes .5 for .500000000: as text ".5Z" > ".487Z" but
        # ".5Z" < ".52Z" would compare wrongly without the padding.
        self.assertLess(log_stamp("2026-10-08T01:00:00.5Z x"),
                        log_stamp("2026-10-08T01:00:00.52Z x"))
        self.assertLess(log_stamp("2026-10-08T01:00:00Z x"),
                        log_stamp("2026-10-08T01:00:00.000000001Z x"))

    def test_a_continuation_line_goes_with_the_line_before_it(self):
        log = "\n".join([
            "2026-10-08T01:00:00Z old",
            "    at old.frame",
            "2026-10-08T01:05:00Z new",
            "    at new.frame",
        ])
        mark = log_stamp("2026-10-08T01:00:00Z x")
        self.assertEqual(_lines_after(log, mark).splitlines(),
                         ["2026-10-08T01:05:00Z new", "    at new.frame"])

    def test_without_a_mark_every_line_is_new(self):
        self.assertEqual(_lines_after("a\nb", None), "a\nb")


if __name__ == "__main__":
    unittest.main()
