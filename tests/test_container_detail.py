"""One container on one screen: what the detail view is given, and what not."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())


class VersionOnlyDaemon(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"Version": "27.0", "ApiVersion": "1.45"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def container(name, project="", service="", depends="", **extra):
    labels = {"traefik.http.routers.x.rule": "Host(`x`)",
              "homepage.widget.key": "secret-api-key"}
    if project:
        labels["com.docker.compose.project"] = project
        labels["com.docker.compose.service"] = service or name
    if depends:
        labels["com.docker.compose.depends_on"] = depends
    labels.update(extra.pop("labels", {}))
    return {"id": f"id-{name}", "name": name, "image": f"{name}:1.0", "state": "running",
            "health": "healthy", "labels": labels, "watched": True,
            "stats": {"cpu_host_pct": 3.0, "mem_pct": 40.0, "mem_usage_bytes": 1000}, **extra}


class Detail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), VersionOnlyDaemon)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        from app.config import Settings
        from app.supervisor import Supervisor

        previous = dict(os.environ)
        os.environ.update(DOCTOR_DATA_DIR=tempfile.mkdtemp(), ANTHROPIC_API_KEY="",
                          DOCKER_HOST=f"tcp://127.0.0.1:{self.server.server_address[1]}")
        try:
            self.supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(self.supervisor.store.close)
        self.supervisor.containers = {c["name"]: c for c in (
            container("docmost", "docmost", depends="db:service_healthy:false",
                      labels={"doctor.critical": "true"}),
            container("docmost-db", "docmost", service="db"),
            container("memos"),
        )}

    def test_only_doctor_and_compose_labels_are_shown(self):
        labels = self.supervisor.container_detail("docmost")["labels"]
        self.assertEqual(labels["doctor.critical"], "true")
        self.assertEqual(labels["com.docker.compose.project"], "docmost")
        self.assertNotIn("homepage.widget.key", labels)
        self.assertNotIn("traefik.http.routers.x.rule", labels)

    def test_the_stack_and_its_siblings(self):
        detail = self.supervisor.container_detail("docmost")
        self.assertEqual([s["name"] for s in detail["siblings"]], ["docmost-db"])
        self.assertEqual(detail["stack"]["depends_on"][0]["name"], "docmost-db")

    def test_a_standalone_container_has_no_siblings(self):
        self.assertEqual(self.supervisor.container_detail("memos")["siblings"], [])

    def test_unknown_is_none(self):
        self.assertIsNone(self.supervisor.container_detail("nope"))

    def test_history_is_a_point_a_minute(self):
        from app.supervisor import HISTORY_STEP_SECONDS

        self.supervisor._record_history(self.supervisor.containers)
        self.supervisor._record_history(self.supervisor.containers)  # same minute
        self.assertEqual(len(self.supervisor.container_detail("memos")["history"]), 1)
        points = self.supervisor.history["memos"]
        at, *rest = points[-1]
        points[-1] = (at - timedelta(seconds=HISTORY_STEP_SECONDS), *rest)
        self.supervisor._record_history(self.supervisor.containers)
        history = self.supervisor.container_detail("memos")["history"]
        self.assertEqual(len(history), 2)
        self.assertEqual((history[-1]["cpu"], history[-1]["mem_pct"]), (3.0, 40.0))

    def test_a_removed_container_forgets_its_history(self):
        self.supervisor._record_history(self.supervisor.containers)
        remaining = dict(self.supervisor.containers)
        remaining.pop("memos")
        self.supervisor._record_history(remaining)
        self.assertNotIn("memos", self.supervisor.history)

    def test_incidents_and_actions_are_this_containers_own(self):
        store = self.supervisor.store
        for name in ("docmost", "memos"):
            incident = store.open_incident(
                container_id="", container_name=name, image="", kind="unhealthy",
                fingerprint=f"fp-{name}", severity="warning", summary=name, evidence={},
                mode="observe")
            store.record_action(incident_id=incident, container_name=name,
                                action="notify_only", status="skipped", decided_by="policy")
        detail = self.supervisor.container_detail("docmost")
        self.assertEqual([i["container_name"] for i in detail["incidents"]], ["docmost"])
        self.assertEqual([a["container_name"] for a in detail["actions"]], ["docmost"])
        # the container filter combines with a status filter
        self.assertEqual(len(store.list_incidents(status="active", container_name="memos")), 1)

    def test_the_stack_rides_along_in_the_snapshot(self):
        by_name = {c["name"]: c for c in self.supervisor.snapshot()["containers"]}
        self.assertEqual((by_name["docmost-db"]["stack"], by_name["docmost-db"]["service"]),
                         ("docmost", "db"))
        self.assertEqual(by_name["memos"]["stack"], "")

    def test_disk_history_for_the_chart(self):
        supervisor = self.supervisor
        supervisor.disk = [{"path": "/data", "total_bytes": 100, "used_bytes": 60,
                            "free_bytes": 35, "used_pct": 63.2, "trend": None}]
        supervisor.store.record_disk_sample("/data", 60, 100, free_bytes=35)
        disks = supervisor.disk_history(14)
        self.assertEqual(disks[0]["path"], "/data")
        self.assertEqual(disks[0]["free_bytes"], 35)
        # Each sample carries the capacity it had then: used + free.
        self.assertEqual([s[1:] for s in disks[0]["samples"]], [[60, 95]])

    def test_disk_history_capacity_for_samples_without_free_space(self):
        supervisor = self.supervisor
        # The volume has since grown from 100 to 200; 5% is reserved for root.
        supervisor.disk = [{"path": "/data", "total_bytes": 200, "used_bytes": 60,
                            "free_bytes": 130, "used_pct": 31.6, "trend": None}]
        supervisor.store.record_disk_sample("/data", 60, 100)
        disks = supervisor.disk_history(14)
        self.assertEqual([s[1:] for s in disks[0]["samples"]], [[60, 95]])


if __name__ == "__main__":
    unittest.main()
