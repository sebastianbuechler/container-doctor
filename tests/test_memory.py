"""Memory-limit suggestions: which verdict, and what number."""

from __future__ import annotations

import unittest

from app import memory

MIB = memory.MIB
GIB = memory.GIB


def hours(peak, n=48, mean=None):
    return [{"bucket": f"h{i}", "max": peak if i == n // 2 else int(peak * 0.6),
             "mean": mean or int(peak * 0.5)} for i in range(n)]


class Verdicts(unittest.TestCase):
    def test_too_little_history_says_so(self):
        verdict = memory.suggest("app", hours(500 * MIB, n=10), limit=GIB)
        self.assertEqual(verdict["verdict"], "collecting")

    def test_no_limit_suggests_one(self):
        verdict = memory.suggest("app", hours(500 * MIB), limit=0)
        self.assertEqual(verdict["verdict"], "unlimited")
        # 500 MiB x 1.3 = 650, up to the next 64 MiB step
        self.assertEqual(verdict["suggested_bytes"], 704 * MIB)
        self.assertEqual(verdict["compose"], "mem_limit: 704m")

    def test_a_peak_at_the_limit_must_rise(self):
        verdict = memory.suggest("app", hours(int(0.95 * GIB)), limit=GIB)
        self.assertEqual(verdict["verdict"], "raise")
        self.assertGreater(verdict["suggested_bytes"], GIB)

    def test_an_oom_kill_must_rise_whatever_the_history(self):
        verdict = memory.suggest("app", hours(300 * MIB), limit=GIB, oom_killed=True)
        self.assertEqual(verdict["verdict"], "raise")

    def test_tight_is_never_told_to_go_lower(self):
        verdict = memory.suggest("app", hours(int(0.8 * GIB)), limit=GIB)
        self.assertEqual(verdict["verdict"], "tight")
        self.assertGreater(verdict["suggested_bytes"], GIB)

    def test_loose_offers_a_tighter_cap(self):
        verdict = memory.suggest("app", hours(200 * MIB), limit=4 * GIB)
        self.assertEqual(verdict["verdict"], "loose")
        self.assertLess(verdict["suggested_bytes"], GIB)

    def test_comfortable_is_ok(self):
        verdict = memory.suggest("app", hours(600 * MIB), limit=GIB)
        self.assertEqual(verdict["verdict"], "ok")


class Numbers(unittest.TestCase):
    def test_limits_are_ones_a_person_would_write(self):
        self.assertEqual(memory.nice(10 * MIB), 64 * MIB)
        self.assertEqual(memory.nice(1.1 * GIB), 1280 * MIB)
        self.assertEqual(memory.nice(5.2 * GIB), 6 * GIB)
        self.assertEqual(memory.compose_value(6 * GIB), "6g")
        self.assertEqual(memory.compose_value(1280 * MIB), "1280m")

    def test_ranked_most_urgent_first(self):
        ranked = memory.rank([
            memory.suggest("a", hours(600 * MIB), limit=GIB),
            memory.suggest("b", hours(int(0.95 * GIB)), limit=GIB),
            memory.suggest("c", hours(500 * MIB), limit=0),
        ])
        self.assertEqual([r["name"] for r in ranked], ["b", "c", "a"])


class InTheBriefing(unittest.TestCase):
    def test_a_limit_about_to_bite_is_worth_attention(self):
        from app.briefing import container_facts

        containers = [{"name": "sonarr", "watched": True, "state": "running", "health": "healthy"}]
        squeezed = memory.suggest("sonarr", hours(int(0.95 * GIB)), limit=GIB)
        facts = container_facts(containers, memory=[squeezed], changes=[])
        self.assertEqual(facts.level, "attention")
        self.assertTrue(any("memory peaks at" in line for line in facts.lines))
        self.assertIn("near its memory limit", facts.headline)

    def test_a_recreate_in_the_last_day_is_mentioned(self):
        from app.briefing import container_facts

        containers = [{"name": "sonarr", "watched": True, "state": "running", "health": "healthy"}]
        facts = container_facts(containers, memory=[], changes=[
            {"id": 7, "kind": "recreated", "summary": "sonarr was recreated on a new build"}])
        self.assertEqual(facts.level, "info")
        self.assertTrue(any("new build" in line for line in facts.lines))


if __name__ == "__main__":
    unittest.main()
