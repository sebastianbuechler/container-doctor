"""One problem, not two incidents.

When Docmost-DB goes unhealthy Docmost follows, and both get diagnosed in
isolation without the one fact that explains them. Compose already records the
project, the service and the declared dependencies on every container, so the
relationship is there to be read.
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.stacks import compose_info, likely_root_cause, relatives, resolve_stacks  # noqa: E402


def container(name, service, project="docmost", depends="", state="running",
              health="healthy"):
    labels = {
        "com.docker.compose.project": project,
        "com.docker.compose.service": service,
    }
    if depends:
        labels["com.docker.compose.depends_on"] = depends
    return {"name": name, "state": state, "health": health, "labels": labels}


class ReadingTheLabels(unittest.TestCase):
    def test_the_long_form_compose_writes(self):
        info = compose_info({
            "com.docker.compose.project": "docmost",
            "com.docker.compose.service": "app",
            "com.docker.compose.depends_on": "db:service_healthy:false,redis:service_started:false",
        })
        self.assertEqual(info["project"], "docmost")
        self.assertEqual(info["depends_on"], ["db", "redis"])

    def test_a_bare_service_name_reads_the_same(self):
        """The label's shape has changed between versions; do not bet on it."""
        info = compose_info({"com.docker.compose.depends_on": "db,redis"})
        self.assertEqual(info["depends_on"], ["db", "redis"])

    def test_duplicates_and_whitespace_are_tidied(self):
        info = compose_info({"com.docker.compose.depends_on": " db:x , db:y ,, redis "})
        self.assertEqual(info["depends_on"], ["db", "redis"])

    def test_a_container_compose_did_not_start(self):
        self.assertEqual(compose_info({})["project"], "")
        self.assertEqual(compose_info(None)["depends_on"], [])


class NamingTheStack(unittest.TestCase):
    """The stack is the repository folder the deploy workflow redeploys."""

    def test_it_is_the_project_by_default(self):
        members = [container("docmost", "app"), container("docmost-db", "db")]
        resolve_stacks(members)
        self.assertEqual([c["stack"] for c in members], ["docmost", "docmost"])

    def test_one_labelled_service_names_its_whole_project(self):
        members = [container("docmost", "app", project="k3x9"),
                   container("docmost-db", "db", project="k3x9"),
                   container("dozzle", "dozzle", project="tools")]
        members[1]["labels"]["doctor.stack"] = "productivity"
        resolve_stacks(members)
        self.assertEqual([c["stack"] for c in members], ["productivity", "productivity", "tools"])

    def test_outside_compose_the_label_is_all_there_is(self):
        loose = {"name": "x", "labels": {"doctor.stack": "tools"}}
        bare = {"name": "y", "labels": {}}
        resolve_stacks([loose, bare])
        self.assertEqual((loose["stack"], bare["stack"]), ("tools", ""))


class WhatIsRelated(unittest.TestCase):
    def setUp(self):
        self.db = container("Docmost-DB", "db", health="unhealthy")
        self.app = container("Docmost", "app", depends="db:service_healthy:false")
        self.cache = container("Docmost-Redis", "redis")
        self.elsewhere = container("sonarr", "sonarr", project="arr-stack")
        self.everything = [self.db, self.app, self.cache, self.elsewhere]

    def test_it_finds_what_the_container_depends_on(self):
        relations = relatives(self.app, self.everything)
        self.assertEqual(
            relations["depends_on"], [{"service": "db", "name": "Docmost-DB",
                                       "status": "unhealthy"}])

    def test_it_finds_what_depends_on_the_container(self):
        relations = relatives(self.db, self.everything)
        self.assertEqual([d["service"] for d in relations["dependents"]], ["app"])

    def test_another_project_is_not_a_relative(self):
        relations = relatives(self.app, self.everything)
        names = [e["name"] for group in ("depends_on", "dependents", "unhealthy_siblings")
                 for e in relations[group]]
        self.assertNotIn("sonarr", names)

    def test_a_healthy_sibling_is_not_worth_the_tokens(self):
        relations = relatives(self.app, self.everything)
        self.assertEqual(relations["unhealthy_siblings"], [])

    def test_an_unhealthy_sibling_without_a_declared_dependency_still_counts(self):
        """Plenty of stacks never declare depends_on."""
        self.cache["health"] = "unhealthy"
        relations = relatives(self.app, self.everything)
        self.assertEqual([s["service"] for s in relations["unhealthy_siblings"]], ["redis"])

    def test_a_container_outside_any_project_has_no_relatives(self):
        self.assertEqual(relatives({"name": "x", "labels": {}}, self.everything), {})

    def test_a_stopped_dependency_reports_its_state(self):
        self.db["state"] = "exited"
        relations = relatives(self.app, self.everything)
        self.assertEqual(relations["depends_on"][0]["status"], "exited")

    def test_a_container_with_no_healthcheck_reads_as_running(self):
        self.db["health"] = "none"
        relations = relatives(self.app, self.everything)
        self.assertEqual(relations["depends_on"][0]["status"], "running")


class TheHint(unittest.TestCase):
    def setUp(self):
        self.db = container("Docmost-DB", "db", health="unhealthy")
        self.app = container("Docmost", "app", depends="db")
        self.everything = [self.db, self.app]

    def test_an_unwell_dependency_is_named(self):
        root = likely_root_cause(relatives(self.app, self.everything))
        self.assertEqual(root["service"], "db")

    def test_a_healthy_dependency_is_not(self):
        self.db["health"] = "healthy"
        self.assertIsNone(likely_root_cause(relatives(self.app, self.everything)))

    def test_the_database_itself_gets_no_hint(self):
        """It is the root; there is nothing below it to point at."""
        self.assertIsNone(likely_root_cause(relatives(self.db, self.everything)))


class InThePrompt(unittest.TestCase):
    def render(self, evidence):
        from app.diagnoser import _render_incident

        return _render_incident({"kind": "unhealthy", "summary": "s",
                                 "container": {"name": "Docmost"}, **evidence})

    def test_the_stack_is_described(self):
        prompt = self.render({
            "stack": {
                "project": "docmost", "service": "app",
                "depends_on": [{"service": "db", "name": "Docmost-DB",
                                "status": "unhealthy"}],
                "dependents": [], "unhealthy_siblings": [],
            },
            "likely_root_cause": {"service": "db", "name": "Docmost-DB",
                                  "status": "unhealthy"},
        })
        self.assertIn("depends on: db (unhealthy)", prompt)
        self.assertIn("prefer a fix there over restarting this one", prompt)

    def test_nothing_is_said_when_there_is_nothing_to_say(self):
        self.assertNotIn("## Stack", self.render({}))


if __name__ == "__main__":
    unittest.main()
