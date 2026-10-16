"""The dashboard briefing: facts that do not move without cause, summaries that
are only rewritten when those facts change, and never at a diagnosis's expense."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import briefing  # noqa: E402
from app.store import Store  # noqa: E402

INCIDENT = {"id": "i1", "container_name": "sonarr", "kind": "unhealthy", "status": "open",
            "rule_severity": "warning", "summary": "sonarr is unhealthy", "diagnosis": None,
            "opened_at": "2026-09-26T08:00:00+00:00", "occurrences": 3}


def container(name, state="running", health="healthy", **extra):
    return {"name": name, "state": state, "health": health, "watched": True,
            "restart_count": 0, "exit_code": 0, "stack": "", **extra}


class IncidentFacts(unittest.TestCase):
    def test_nothing_open_is_ok(self):
        facts = briefing.incident_facts([], set())
        self.assertEqual(facts.level, "ok")
        self.assertEqual(facts.lines[0], "0 open incidents")

    def test_open_and_waiting(self):
        facts = briefing.incident_facts([INCIDENT], {"i1"})
        self.assertEqual(facts.level, "attention")
        self.assertIn("1 awaiting approval", facts.lines[0])
        self.assertIn("awaiting approval", facts.lines[1])
        self.assertEqual(facts.refs[0]["href"], "#incident/i1")

    def test_critical_and_failed_raise_the_level(self):
        for override in ({"rule_severity": "critical"}, {"status": "failed"}):
            with self.subTest(override):
                self.assertEqual(
                    briefing.incident_facts([{**INCIDENT, **override}], set()).level, "critical")

    def test_a_recurring_sighting_does_not_change_the_fingerprint(self):
        """'Seen 47 times' moves every poll; nothing has happened."""
        before = briefing.incident_facts([INCIDENT], set()).fingerprint
        after = briefing.incident_facts([{**INCIDENT, "occurrences": 47}], set()).fingerprint
        self.assertEqual(before, after)

    def test_a_new_diagnosis_does(self):
        before = briefing.incident_facts([INCIDENT], set()).fingerprint
        diagnosed = {**INCIDENT, "diagnosis": {"summary": "the database is down"}}
        self.assertNotEqual(before, briefing.incident_facts([diagnosed], set()).fingerprint)


class ContainerFacts(unittest.TestCase):
    def test_all_running_is_ok(self):
        facts = briefing.container_facts([container("a"), container("b")])
        self.assertEqual((facts.level, facts.lines), ("ok", ["2 watched containers, 2 running normally"]))

    def test_unhealthy_is_attention_crashed_is_critical(self):
        self.assertEqual(briefing.container_facts([container("a", health="unhealthy")]).level,
                         "attention")
        crashed = container("a", state="exited", exit_code=137)
        facts = briefing.container_facts([crashed])
        self.assertEqual(facts.level, "critical")
        self.assertIn("a: exited (exit 137)", facts.lines)

    def test_cpu_does_not_change_the_fingerprint(self):
        before = briefing.container_facts([container("a", stats={"cpu_pct": 1})]).fingerprint
        after = briefing.container_facts([container("a", stats={"cpu_pct": 90})]).fingerprint
        self.assertEqual(before, after)

    def test_unwatched_containers_are_left_out(self):
        facts = briefing.container_facts([container("a", state="exited", watched=False)])
        self.assertEqual(facts.level, "ok")


class StorageFacts(unittest.TestCase):
    TH = {"disk": 90, "disk_critical": 95}

    def disk(self, pct, days=None):
        trend = {"days_until_full": days, "rate": "1.0 GiB/day", "until_full": f"{days} days"} \
            if days is not None else None
        return {"path": "/data", "used_pct": pct, "trend": trend}

    def facts(self, disks, arrays=()):
        return briefing.storage_facts(disks, list(arrays), self.TH, 21, 5)

    def test_levels(self):
        self.assertEqual(self.facts([self.disk(50)]).level, "ok")
        self.assertEqual(self.facts([self.disk(91)]).level, "attention")
        self.assertEqual(self.facts([self.disk(96)]).level, "critical")
        self.assertEqual(self.facts([self.disk(50, days=14)]).level, "attention")
        self.assertEqual(self.facts([self.disk(50, days=3)]).level, "critical")

    def test_a_fraction_of_a_percent_is_not_news(self):
        self.assertEqual(self.facts([self.disk(61.2)]).fingerprint,
                         self.facts([self.disk(61.9)]).fingerprint)

    def test_arrays(self):
        degraded = {"name": "md2", "healthy": False, "operation": "", "summary": "md2 is degraded"}
        rebuilding = {**degraded, "operation": "recovery", "progress": 42.0}
        self.assertEqual(self.facts([], [degraded]).level, "critical")
        self.assertEqual(self.facts([], [rebuilding]).level, "attention")
        self.assertIn("All 1 disk arrays healthy", self.facts([], [{"name": "md2", "healthy": True}]).lines)

    def test_rebuild_progress_moves_the_fingerprint_in_tens(self):
        at = lambda p: self.facts([], [{"name": "md2", "healthy": False, "operation": "recovery",
                                        "progress": p}]).fingerprint  # noqa: E731
        self.assertEqual(at(41.0), at(48.9))
        self.assertNotEqual(at(48.9), at(51.0))


class UpdateFacts(unittest.TestCase):
    def test_not_checked_yet(self):
        self.assertIn("not checked yet", briefing.update_facts({}).lines[0])

    def test_newer_versions_are_info(self):
        report = {"checked_at": "x", "images": [
            {"image": "sonarr:4.0.20", "containers": ["sonarr"], "state": "current",
             "newer": {"tag": "4.1.0", "level": "minor", "breaking": "config moved"}}]}
        facts = briefing.update_facts(report)
        self.assertEqual(facts.level, "info")
        self.assertIn("4.1.0 available, a minor step", facts.lines[0])
        self.assertIn("config moved", facts.lines[0])


class Headlines(unittest.TestCase):
    """What shows when there is no summary: a few words per section."""

    def test_each_section_says_it_briefly(self):
        self.assertEqual(briefing.incident_facts([INCIDENT], {"i1"}).headline,
                         "1 open incident, 1 awaiting approval")
        self.assertEqual(briefing.container_facts([container("a", health="unhealthy"), container("b")]).headline,
                         "1 of 2 containers need a look")
        th = {"disk": 90, "disk_critical": 95}
        self.assertEqual(briefing.storage_facts(
            [{"path": "/data", "used_pct": 93, "trend": None}],
            [{"name": "md2", "healthy": False, "operation": "", "summary": "x"}], th, 21, 5).headline,
            "md2 is degraded")  # the worst of the two
        self.assertEqual(briefing.update_facts({"checked_at": "x", "images": []}).headline,
                         "All images current")

    def test_the_top_line_joins_only_what_needs_a_look(self):
        sections = {
            "incidents": {"level": "attention", "headline": "2 open incidents"},
            "containers": {"level": "ok", "headline": "All 3 containers running normally"},
            "storage": {"level": "critical", "headline": "md2 is degraded"},
            "updates": {"level": "info", "headline": "1 image behind"},
        }
        self.assertEqual(briefing.fallback_headline(sections), "md2 is degraded \u00b7 2 open incidents")


class Cleaning(unittest.TestCase):
    def test_a_section_is_trimmed_and_kept_in_bounds(self):
        clean = briefing.clean_section(
            {"headline": " x " * 100, "points": ["a", "", "b", "c", "d"], "level": "loud"}, "attention")
        self.assertLessEqual(len(clean["headline"]), 140)
        self.assertEqual(clean["points"], ["a", "b", "c"])
        self.assertEqual(clean["level"], "attention")

    def test_a_made_up_reference_links_nowhere(self):
        refs = {"incident:i1": {"id": "incident:i1", "label": "sonarr", "href": "#incident/i1"}}
        clean = briefing.clean_overall({"headline": "h", "level": "ok", "priorities": [
            {"text": "Look at sonarr", "ref": "incident:i1"},
            {"text": "Look at nothing", "ref": "incident:nope"},
        ]}, refs, "ok")
        self.assertEqual(clean["priorities"][0]["href"], "#incident/i1")
        self.assertEqual(clean["priorities"][1]["href"], "")


class TheService(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.addCleanup(self.store.close)
        self.incidents: list[dict] = []
        self.calls: list[str] = []
        self.budget = ""
        self.notified = 0

    def service(self, enabled=True, refresh=15):
        async def call(system, prompt, schema, name):
            self.calls.append(name)
            if name == "briefing_overall":
                return {"headline": "All good", "level": "ok",
                        "priorities": [{"text": "Check sonarr", "ref": "incident:i1"}]}, {}
            return {"headline": f"summary {len(self.calls)}", "points": ["p"], "level": "ok"}, {}

        def collect():
            return {
                "incidents": briefing.incident_facts(self.incidents, set()),
                "containers": briefing.container_facts([container("a")]),
                "storage": briefing.storage_facts([], [], {}, 21, 5),
                "updates": briefing.update_facts({"checked_at": "x", "images": []}),
            }

        def notify():
            self.notified += 1

        return briefing.Briefing(
            store=self.store, enabled=enabled, refresh_minutes=refresh, model="m",
            collect=collect, call=call, budget_ok=lambda: self.budget,
            record=lambda usage: None, notify=notify)

    def run_ensure(self, service, force=False):
        async def go():
            started = await service.ensure(force=force)
            if service.task:
                await service.task
            return started
        return asyncio.run(go())

    def test_first_open_writes_every_section_and_the_top_line(self):
        service = self.service()
        self.assertTrue(self.run_ensure(service))
        self.assertEqual(self.calls.count("briefing_section"), 4)
        self.assertEqual(self.calls.count("briefing_overall"), 1)
        view = service.view()
        self.assertEqual(view["overall"]["headline"], "All good")
        self.assertTrue(all(not s["stale"] for s in view["sections"].values()))
        self.assertGreater(self.notified, 0)

    def test_a_reload_costs_nothing(self):
        service = self.service()
        self.run_ensure(service)
        self.calls.clear()
        self.assertFalse(self.run_ensure(service))
        self.assertEqual(self.calls, [])

    def test_the_cache_survives_a_restart(self):
        self.run_ensure(self.service())
        self.calls.clear()
        self.assertFalse(self.run_ensure(self.service()))

    def test_a_changed_level_is_rewritten_at_once_the_rest_waits(self):
        service = self.service(refresh=60)
        self.run_ensure(service)
        self.calls.clear()
        self.incidents = [INCIDENT]  # incidents: ok -> attention
        self.run_ensure(service)
        self.assertEqual(self.calls, ["briefing_section", "briefing_overall"])

        self.calls.clear()
        self.incidents = [INCIDENT, {**INCIDENT, "id": "i2"}]  # same level, new facts
        self.assertFalse(self.run_ensure(service))
        view = service.view()
        self.assertTrue(view["sections"]["incidents"]["stale"])  # shown as such

    def test_the_throttle_lapses(self):
        service = self.service(refresh=15)
        self.incidents = [INCIDENT]
        self.run_ensure(service)
        self.incidents = [INCIDENT, {**INCIDENT, "id": "i2"}]
        old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(timespec="seconds")
        with self.store._lock:
            self.store._conn.execute("UPDATE briefing SET created_at=?", (old,))
            self.store._conn.commit()
        self.calls.clear()
        self.run_ensure(service)
        self.assertIn("briefing_section", self.calls)

    def test_refresh_rewrites_everything(self):
        service = self.service()
        self.run_ensure(service)
        self.calls.clear()
        self.run_ensure(service, force=True)
        self.assertEqual(self.calls.count("briefing_section"), 4)
        self.assertEqual(self.calls.count("briefing_overall"), 1)

    def test_off_means_no_calls_and_the_facts_still_show(self):
        service = self.service(enabled=False)
        self.assertFalse(self.run_ensure(service, force=True))
        self.assertEqual(self.calls, [])
        view = service.view()
        self.assertFalse(view["enabled"])
        self.assertEqual(view["sections"]["containers"]["facts"][0],
                         "1 watched container, 1 running normally")
        self.assertIn("All clear", view["fallback_headline"])

    def test_turning_it_off_hides_what_was_written_before(self):
        self.run_ensure(self.service())
        view = self.service(enabled=False).view()
        self.assertIsNone(view["overall"])
        self.assertTrue(all(s["summary"] is None for s in view["sections"].values()))

    def test_a_spent_budget_stops_it_and_says_why(self):
        self.budget = "Summary paused: budget"
        service = self.service()
        self.run_ensure(service)
        self.assertEqual(self.calls, [])
        self.assertEqual(service.view()["error"], "Summary paused: budget")

    def test_a_failing_model_keeps_the_last_words(self):
        service = self.service()
        self.run_ensure(service)

        async def broken(*args):
            raise RuntimeError("provider down")

        service.call = broken
        self.run_ensure(service, force=True)
        view = service.view()
        self.assertIn("provider down", view["error"])
        self.assertTrue(view["sections"]["containers"]["summary"])

    def test_collecting_facts_leaves_the_event_loop_free(self):
        # Collecting reads two weeks of memory samples; on a cold disk that
        # once held every request behind it for twenty seconds.
        import time

        service = self.service()
        collect = service.collect

        def slow():
            time.sleep(0.3)
            return collect()

        service.collect = slow

        async def go():
            ticks = 0

            async def tick():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1

            ticker = asyncio.create_task(tick())
            await service.ensure()
            ticker.cancel()
            return ticks

        self.assertGreater(asyncio.run(go()), 5)


class TheBudgetReserve(unittest.TestCase):
    """The briefing stops at 80% of a cap so diagnoses keep the last of it."""

    def supervisor(self, **env):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from app.config import Settings
        from app.supervisor import Supervisor

        class Daemon(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps({"Version": "27.0", "ApiVersion": "1.45"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Daemon)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        previous = dict(os.environ)
        os.environ.update(DOCTOR_DATA_DIR=tempfile.mkdtemp(), ANTHROPIC_API_KEY="sk-ant-x",
                          DOCKER_HOST=f"tcp://127.0.0.1:{server.server_address[1]}", **env)
        try:
            supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(supervisor.store.close)
        return supervisor

    def test_calls(self):
        supervisor = self.supervisor(DOCTOR_DAILY_CALL_LIMIT="10")
        for _ in range(7):
            supervisor.store.reserve_call(10)
        self.assertEqual(supervisor._briefing_budget(), "")  # the 8th call is fine
        self.assertIn("kept for diagnoses", supervisor._briefing_budget())

    def test_dollars(self):
        supervisor = self.supervisor(DOCTOR_DAILY_USD_BUDGET="1.00", DOCTOR_DAILY_CALL_LIMIT="0")
        supervisor.store.add_usage(input_tokens=0, output_tokens=0, cached_tokens=0, cost_usd=0.81)
        self.assertIn("budget", supervisor._briefing_budget())

    def test_off_is_off(self):
        self.assertFalse(self.supervisor(DOCTOR_BRIEFING="false").briefing.enabled)
        self.assertTrue(self.supervisor().briefing.enabled)


class ThroughTheApi(unittest.TestCase):
    def test_reading_is_open_refreshing_needs_a_login(self):
        from fastapi.testclient import TestClient

        from app.web.api import build_app
        from tests.test_incident_logs import StubSupervisor, make_settings

        class Stub(StubSupervisor):
            class briefing:  # noqa: N801 - stands in for the attribute
                forced = []

                @classmethod
                async def ensure(cls, force=False):
                    cls.forced.append(force)
                    return True

                @staticmethod
                def view():
                    return {"enabled": True, "sections": {}}

        client = TestClient(build_app(Stub(), make_settings(
            DOCTOR_WEB_PASSWORD="pw", DOCTOR_WEB_USER="doctor", DOCTOR_OIDC_ISSUER="")))
        self.assertEqual(client.get("/api/briefing", auth=("doctor", "pw")).status_code, 200)
        self.assertEqual(client.post("/api/briefing/refresh").status_code, 401)
        response = client.post("/api/briefing/refresh", auth=("doctor", "pw"))
        self.assertEqual(response.json()["started"], True)
        self.assertEqual(Stub.briefing.forced, [False, True])


if __name__ == "__main__":
    unittest.main()
