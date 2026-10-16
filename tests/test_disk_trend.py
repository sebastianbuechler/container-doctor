"""How fast a disk is filling, rather than how full it is.

"92% full" is close to useless on a NAS: it can sit there for a year or be
gone by Friday and the number reads the same either way. These cover the fit,
the sampling that feeds it, and the rule that turns it into an incident.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import Settings  # noqa: E402
from app.detector import Detector  # noqa: E402
from app.forecast import MIN_SAMPLES, human_days, human_rate, trend  # noqa: E402
from app.store import Store  # noqa: E402

TOTAL = 40_000_000_000_000  # 40 TB, near enough to the reported NAS
GB = 1_000_000_000


def series(days: int, per_day: float, start_used: float, *, end: datetime | None = None):
    """One sample a day, filling at `per_day`."""
    end = end or datetime.now(timezone.utc)
    return [
        (end - timedelta(days=days - n), int(start_used + per_day * n))
        for n in range(days + 1)
    ]


class FittingTheLine(unittest.TestCase):
    def test_a_steady_fill_gives_the_rate_and_the_date(self):
        fitted = trend(series(14, 4 * GB, TOTAL * 0.90), TOTAL)
        self.assertAlmostEqual(fitted.bytes_per_day, 4 * GB, delta=0.05 * GB)
        # 10% of 40TB left at 4GB/day
        self.assertAlmostEqual(fitted.days_until_full, 1000, delta=30)

    def test_a_disk_that_is_not_moving_has_no_date(self):
        fitted = trend(series(14, 0, TOTAL * 0.92), TOTAL)
        self.assertIsNone(fitted.days_until_full)
        self.assertFalse(fitted.filling)

    def test_a_disk_being_emptied_has_no_date(self):
        fitted = trend(series(14, -2 * GB, TOTAL * 0.92), TOTAL)
        self.assertLess(fitted.bytes_per_day, 0)
        self.assertIsNone(fitted.days_until_full)

    def test_one_restart_freeing_space_bends_the_line_rather_than_resetting_it(self):
        """Least squares over the window, not first-to-last."""
        points = series(14, 4 * GB, TOTAL * 0.90)
        # a cleanup on day 7 frees 20GB, then filling resumes
        points = [(when, used - 20 * GB if n >= 7 else used)
                  for n, (when, used) in enumerate(points)]
        fitted = trend(points, TOTAL)
        self.assertGreater(fitted.bytes_per_day, 0)
        self.assertIsNotNone(fitted.days_until_full)

    def test_a_trickle_is_not_a_trend(self):
        """A few bytes a day extrapolates to a date centuries out."""
        fitted = trend(series(14, 100, TOTAL * 0.92), TOTAL)
        self.assertIsNone(fitted.days_until_full)

    def test_too_few_samples_to_draw_a_line_through(self):
        self.assertIsNone(trend(series(MIN_SAMPLES - 2, 4 * GB, 0), TOTAL))

    def test_samples_all_at_one_instant(self):
        now = datetime.now(timezone.utc)
        self.assertIsNone(trend([(now, 1), (now, 2), (now, 3), (now, 4)], TOTAL))

    def test_order_does_not_matter(self):
        points = series(14, 4 * GB, TOTAL * 0.90)
        self.assertAlmostEqual(
            trend(points, TOTAL).bytes_per_day,
            trend(list(reversed(points)), TOTAL).bytes_per_day,
            delta=1,
        )

    def test_it_projects_from_the_latest_reading_not_the_fitted_line(self):
        """The question is how long from now."""
        points = series(14, 1 * GB, TOTAL - 10 * GB)
        fitted = trend(points, TOTAL)
        self.assertLess(fitted.days_until_full, 12)


class SayingItInWords(unittest.TestCase):
    def test_rates(self):
        self.assertEqual(human_rate(4 * 1024 ** 3), "4.0 GiB/day")
        self.assertEqual(human_rate(512 * 1024), "512.0 KiB/day")

    def test_durations(self):
        self.assertEqual(human_days(0.5), "under a day")
        self.assertEqual(human_days(31), "31 days")
        self.assertIn("months", human_days(120))
        self.assertIn("years", human_days(900))


class Sampling(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.addCleanup(self.store.close)

    def test_many_polls_in_one_bucket_keep_one_row(self):
        """Polling every 30s must not become 2880 rows a day."""
        for used in (100, 200, 300):
            self.store.record_disk_sample("/data", used, TOTAL, bucket_minutes=60)
        since = datetime.now(timezone.utc) - timedelta(days=1)
        samples = self.store.disk_samples("/data", since)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0][1], 300)  # the latest reading wins

    def test_paths_are_kept_apart(self):
        self.store.record_disk_sample("/data", 1, TOTAL)
        self.store.record_disk_sample("/other", 2, TOTAL)
        since = datetime.now(timezone.utc) - timedelta(days=1)
        self.assertEqual(len(self.store.disk_samples("/data", since)), 1)
        self.assertEqual(self.store.disk_samples("/other", since)[0][1], 2)

    def test_samples_outside_the_window_are_not_returned(self):
        self.store.record_disk_sample("/data", 1, TOTAL)
        future = datetime.now(timezone.utc) + timedelta(days=1)
        self.assertEqual(self.store.disk_samples("/data", future), [])


class TheRule(unittest.TestCase):
    def detector(self, **env):
        previous = dict(os.environ)
        os.environ.update({"DOCTOR_DATA_DIR": tempfile.mkdtemp(), **env})
        try:
            return Detector(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def usage(self, days_left, pct=60.0):
        return [{
            "path": "/data", "used_pct": pct, "free_bytes": 100,
            "total_bytes": TOTAL, "used_bytes": 1,
            "trend": {
                "bytes_per_day": 4 * GB, "days_until_full": days_left,
                "samples": 20, "span_days": 14.0,
                "rate": "3.7 GiB/day", "until_full": f"{days_left:.0f} days",
            },
        }]

    def test_a_disk_filling_soon_is_an_incident_even_at_60_percent(self):
        """The whole point: full-ness and filling are different questions."""
        signals = self.detector().check_disk_trend(self.usage(10, pct=60.0))
        self.assertEqual([s.kind for s in signals], ["disk_filling"])
        self.assertEqual(signals[0].severity, "warning")

    def test_days_away_is_critical(self):
        signals = self.detector().check_disk_trend(self.usage(2))
        self.assertEqual(signals[0].severity, "critical")

    def test_a_distant_date_is_not_worth_saying(self):
        self.assertEqual(self.detector().check_disk_trend(self.usage(400)), [])

    def test_no_trend_yet_is_not_an_incident(self):
        usage = self.usage(1)
        usage[0]["trend"] = None
        self.assertEqual(self.detector().check_disk_trend(usage), [])

    def test_a_disk_that_is_not_filling_is_not_an_incident(self):
        usage = self.usage(1)
        usage[0]["trend"]["days_until_full"] = None
        self.assertEqual(self.detector().check_disk_trend(usage), [])

    def test_the_horizon_is_configurable(self):
        detector = self.detector(DOCTOR_DISK_FORECAST_DAYS="60")
        self.assertEqual(len(detector.check_disk_trend(self.usage(45))), 1)

    def test_it_fingerprints_apart_from_the_fullness_rule(self):
        filling = self.detector().check_disk_trend(self.usage(10))[0]
        self.assertIn("filling", filling.signature_parts)
        self.assertEqual(filling.container["name"], "host:/data")


if __name__ == "__main__":
    unittest.main()
