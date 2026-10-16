"""The dashboard's own files must never outlive an upgrade in a browser cache.

A new index.html next to a cached old app.js drew the compact-rows switch and
left it dead: the old script had no handler for it.
"""

import os
import re
import tempfile
import unittest

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover - the image always has it
    HAVE_FASTAPI = False


class _StubSupervisor:
    def __init__(self, settings):
        from app.store import Store

        self.settings = settings
        self.store = Store(os.path.join(tempfile.mkdtemp(), "doctor.db"))
        self.docker_connected = False


def _client():
    previous = dict(os.environ)
    os.environ.update({"DOCTOR_DATA_DIR": tempfile.mkdtemp(), "DOCKER_HOST": "tcp://127.0.0.1:1"})
    try:
        from app.config import Settings
        from app.web.api import build_app

        settings = Settings()
        return TestClient(build_app(_StubSupervisor(settings), settings))
    finally:
        os.environ.clear()
        os.environ.update(previous)


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class StaticCaching(unittest.TestCase):
    def setUp(self):
        self.client = _client()

    def test_the_page_names_its_script_and_stylesheet_by_content(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("no-cache", page.headers["cache-control"])
        for name in ("app.js", "styles.css"):
            match = re.search(rf'/static/{re.escape(name)}\?v=([0-9a-f]{{12}})"', page.text)
            self.assertIsNotNone(match, f"{name} is not versioned")
            # The versioned URL is the file itself.
            asset = self.client.get(f"/static/{name}?v={match.group(1)}")
            self.assertEqual(asset.status_code, 200)

    def test_a_changed_file_gets_a_new_url(self):
        from app.web import api

        first = re.search(r"app\.js\?v=(\w+)", api._index_html()).group(1)
        real = api.STATIC_DIR
        with tempfile.TemporaryDirectory() as copy:
            for name in ("index.html", "app.js", "styles.css"):
                with open(real / name, "rb") as src, open(os.path.join(copy, name), "wb") as dst:
                    dst.write(src.read())
            with open(os.path.join(copy, "app.js"), "a", encoding="utf-8") as script:
                script.write("\n// a new build\n")
            from pathlib import Path

            api.STATIC_DIR = Path(copy)
            try:
                second = re.search(r"app\.js\?v=(\w+)", api._index_html()).group(1)
            finally:
                api.STATIC_DIR = real
        self.assertNotEqual(first, second)

    def test_static_files_are_revalidated_not_reused_blind(self):
        response = self.client.get("/static/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-cache")
        etag = response.headers["etag"]
        again = self.client.get("/static/app.js", headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class OfflineFallback(unittest.TestCase):
    """Opening the app off the VPN shows a page of ours, not the browser's."""

    def setUp(self):
        self.client = _client()

    def test_the_worker_is_served_from_the_root_for_the_whole_app(self):
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-cache")

    def test_the_worker_is_named_after_the_offline_page_it_serves(self):
        script = self.client.get("/sw.js").text
        self.assertNotIn("__VERSION__", script)
        self.assertRegex(script, r'doctor-offline-[0-9a-f]{12}"')
        self.assertIn('"/static/offline.html"', script)

    def test_the_worker_only_steps_in_for_page_loads(self):
        # Data and assets must never come from a cache it keeps.
        script = self.client.get("/sw.js").text
        self.assertIn('event.request.mode !== "navigate"', script)

    def test_the_offline_page_exists_and_stands_alone(self):
        page = self.client.get("/static/offline.html")
        self.assertEqual(page.status_code, 200)
        # Nothing else can be loaded when it is shown.
        self.assertNotIn('rel="stylesheet"', page.text)
        self.assertNotIn("<script src", page.text)

    def test_the_page_registers_the_worker_and_has_the_banner(self):
        script = self.client.get("/static/app.js").text
        self.assertIn('serviceWorker.register("/sw.js")', script)
        self.assertIn('id="offline"', self.client.get("/").text)


if __name__ == "__main__":
    unittest.main()
