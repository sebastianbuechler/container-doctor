"""A pinned version never drifts, so "is there a newer one?" needs the tags.

The digest check reads `sonarr:4.0.20` as current forever. These cover the
other half: which tags count as newer, how big the step is, and what the
release notes say about it.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

import httpx  # noqa: E402

from app.registry import Reference, parse_reference  # noqa: E402
from app.versions import (  # noqa: E402
    MAX_TAG_PAGES,
    TOO_MANY_TAGS,
    Newer,
    breaking_line,
    level,
    list_tags,
    newest,
    parse_tag,
    published_at,
    release_notes,
    settled,
    source_repository,
)

# The shape of a LinuxServer repository: every release under several names.
SONARR_TAGS = [
    "latest", "develop", "v4", "4.0.19", "4.0.20", "4.0.21", "4.1.0",
    "4.0.20.2240-ls300", "4.1.0.2300-ls301", "version-4.1.0.2300",
    "4.2.0-rc1", "5.0.0.100-ls1", "2026092110", "78",
]


class ReadingATag(unittest.TestCase):
    def test_moving_tags_are_not_versions(self):
        for tag in ("latest", "stable", "develop", "preview", ""):
            with self.subTest(tag):
                self.assertIsNone(parse_tag(tag))

    def test_a_plain_version(self):
        version = parse_tag("4.0.20")
        self.assertEqual(version.numbers, (4, 0, 20))
        self.assertEqual((version.prefix, version.shape, version.build), ("", "", -1))

    def test_a_v_prefix_is_kept_apart_from_the_numbers(self):
        self.assertEqual(parse_tag("v2.16.0").prefix, "v")
        self.assertEqual(parse_tag("version-4.1.0.2300").prefix, "version-")

    def test_a_build_suffix_is_a_shape_and_a_number(self):
        version = parse_tag("4.0.20.2240-ls300")
        self.assertEqual(version.shape, "-ls#")
        self.assertEqual(version.build, 300)

    def test_a_commit_hash_is_any_hash(self):
        """IT Tools tags every release with its commit: 2024.10.22-7ca5933."""
        a, b = parse_tag("2024.10.22-7ca5933"), parse_tag("2025.1.5-e1f00d2")
        self.assertEqual(a.shape, b.shape)
        self.assertTrue(a.same_line(b))


class WhatCountsAsTheSameLine(unittest.TestCase):
    def line(self, a, b):
        return parse_tag(a).same_line(parse_tag(b))

    def test_same_precision_same_suffix(self):
        self.assertTrue(self.line("4.0.20", "4.1.0"))

    def test_a_build_tag_is_not_the_pinned_line(self):
        self.assertFalse(self.line("4.0.20", "4.1.0.2300-ls301"))

    def test_a_variant_is_not_the_pinned_line(self):
        self.assertFalse(self.line("v2.16.0", "v2.17.0-distroless"))

    def test_a_pre_release_is_not_the_pinned_line(self):
        self.assertFalse(self.line("4.0.20", "4.2.0-rc1"))

    def test_a_coarser_pin_is_compared_at_its_own_precision(self):
        """jellyfin:12.1 floats over 12.1.x; newer means 12.2, not 12.1.3."""
        self.assertFalse(self.line("12.1", "12.1.3"))
        self.assertTrue(self.line("12.1", "12.2"))

    def test_a_different_prefix_is_a_different_line(self):
        self.assertFalse(self.line("4.0.20", "v4.1.0"))


class FindingTheNewest(unittest.TestCase):
    def test_the_newest_in_line_wins(self):
        found = newest("4.0.20", SONARR_TAGS)
        self.assertEqual(found.newest, "4.1.0")
        self.assertEqual(found.level, "minor")
        self.assertEqual(found.behind, 2)  # 4.0.21 and 4.1.0

    def test_nothing_newer_is_none(self):
        self.assertIsNone(newest("4.1.0", SONARR_TAGS))

    def test_a_moving_tag_is_left_to_the_digest_check(self):
        self.assertIsNone(newest("latest", SONARR_TAGS))

    def test_numbers_compare_as_numbers_not_text(self):
        """Registries list tags alphabetically, where 1.9.0 sorts after 1.15.0."""
        self.assertEqual(newest("1.9.0", ["1.10.0", "1.15.0", "1.9.1"]).newest, "1.15.0")

    def test_a_build_tag_pin_follows_its_builds(self):
        found = newest("4.0.20.2240-ls300", ["4.0.20.2240-ls300", "4.0.20.2240-ls301"])
        self.assertEqual((found.newest, found.level), ("4.0.20.2240-ls301", "build"))

    def test_a_new_major_names_the_newest_that_is_not(self):
        found = newest("v1.6.0", ["v1.6.0", "v1.16.0", "v2.0.0", "v2.16.0"])
        self.assertEqual((found.newest, found.level), ("v2.16.0", "major"))
        self.assertEqual(found.newest_same_major, "v1.16.0")


class HowBigTheStepIs(unittest.TestCase):
    def step(self, a, b):
        return level(parse_tag(a), parse_tag(b))

    def test_semver_levels(self):
        self.assertEqual(self.step("4.0.20", "5.0.0"), "major")
        self.assertEqual(self.step("4.0.20", "4.1.0"), "minor")
        self.assertEqual(self.step("4.0.20", "4.0.21"), "patch")

    def test_a_date_version_is_a_release_not_a_major(self):
        self.assertEqual(self.step("2024.10.22-7ca5933", "2025.1.5-e1f00d2"), "release")


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


class ListingTags(unittest.TestCase):
    reference = Reference("registry.example", "team/app", "1.0.0")

    def test_it_follows_the_token_challenge_and_every_page(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.path == "/token":
                return httpx.Response(200, json={"token": "t-1"})
            if request.headers.get("Authorization") != "Bearer t-1":
                return httpx.Response(401, headers={
                    "WWW-Authenticate":
                        'Bearer realm="https://registry.example/token",service="stub"'})
            if "last=" not in str(request.url):
                return httpx.Response(200, json={"tags": ["1.0.0", "1.1.0"]}, headers={
                    "Link": '</v2/team/app/tags/list?last=1.1.0&n=1000>; rel="next"'})
            return httpx.Response(200, json={"tags": ["1.2.0"]})

        async def go():
            async with mock_client(handler) as client:
                return await list_tags(client, self.reference)

        tags, reason = run(go())
        self.assertEqual(reason, "")
        self.assertEqual(tags, ["1.0.0", "1.1.0", "1.2.0"])
        self.assertTrue(any("last=1.1.0" in url for url in seen))

    def test_a_private_repository_says_so(self):
        def handler(request):
            if request.url.path == "/token":
                return httpx.Response(403)
            return httpx.Response(401, headers={
                "WWW-Authenticate": 'Bearer realm="https://registry.example/token"'})

        async def go():
            async with mock_client(handler) as client:
                return await list_tags(client, self.reference)

        self.assertEqual(run(go()), ([], "needs credentials"))

    def test_an_endless_listing_is_cut_off_and_said_so(self):
        def handler(request):
            return httpx.Response(200, json={"tags": ["1.0.0"]}, headers={
                "Link": '</v2/team/app/tags/list?last=x>; rel="next"'})

        calls = []

        async def go():
            async def counting(request):
                calls.append(1)
                return handler(request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(counting)) as client:
                return await list_tags(client, self.reference)

        tags, reason = run(go())
        self.assertEqual(tags, [])
        self.assertIn(TOO_MANY_TAGS, reason)
        self.assertLessEqual(len(calls), MAX_TAG_PAGES + 1)

    def test_an_unreachable_registry_is_a_reason_not_an_exception(self):
        def handler(request):
            raise httpx.ConnectError("no route")

        async def go():
            async with mock_client(handler) as client:
                return await list_tags(client, self.reference)

        tags, reason = run(go())
        self.assertEqual(tags, [])
        self.assertIn("could not list tags", reason)


class WhereTheNotesAre(unittest.TestCase):
    def test_the_image_label_wins(self):
        labels = {"org.opencontainers.image.source": "https://github.com/linuxserver/docker-sonarr"}
        self.assertEqual(
            source_repository(labels, parse_reference("linuxserver/sonarr:4.0.20")),
            "linuxserver/docker-sonarr")

    def test_a_ghcr_image_is_its_own_repository(self):
        self.assertEqual(
            source_repository({}, parse_reference("ghcr.io/pocket-id/pocket-id:v2.16.0")),
            "pocket-id/pocket-id")

    def test_a_docker_hub_image_without_a_label_has_none(self):
        self.assertEqual(source_repository({}, parse_reference("neosmemo/memos:0.31.0")), "")

    def test_a_non_github_source_is_not_guessed_at(self):
        labels = {"org.opencontainers.image.source": "https://gitlab.com/team/app"}
        self.assertEqual(source_repository(labels, parse_reference("team/app:1.0")), "")


class ReadingReleaseNotes(unittest.TestCase):
    def test_a_warning_is_found(self):
        self.assertIn("database", breaking_line(
            "## Changes\n- faster\n### BREAKING CHANGE: the database moved to /data"))

    def test_no_breaking_changes_is_not_a_warning(self):
        for text in ("No breaking changes in this release.",
                     "There are no known breaking changes",
                     "non-breaking refactor"):
            with self.subTest(text):
                self.assertEqual(breaking_line(text), "")

    def releases(self, bodies):
        def handler(request):
            self.assertEqual(request.url.host, "api.github.com")
            return httpx.Response(200, json=[
                {"tag_name": tag, "html_url": f"https://github.com/o/r/releases/tag/{tag}",
                 "body": body, "draft": False, "prerelease": tag.endswith("rc1")}
                for tag, body in bodies
            ])

        async def go(newer):
            async with mock_client(handler) as client:
                return await release_notes(client, "o/r", newer)

        return go

    def test_only_releases_between_the_pin_and_the_newest_count(self):
        go = self.releases([
            ("4.2.0", "Breaking: not taken yet"),
            ("4.1.0.2300-ls301", "fine"),
            ("4.0.21", "Manual migration required for the queue"),
            ("4.0.20.2240-ls300", "Breaking: what is already running"),
            ("4.1.0-rc1", "Breaking: pre-release"),
        ])
        notes = run(go(Newer(current="4.0.20", newest="4.1.0", level="minor", behind=2)))
        self.assertEqual(notes["releases"], 2)  # 4.0.21 and 4.1.0.2300-ls301
        self.assertIn("migration", notes["breaking"])
        self.assertTrue(notes["breaking_url"].endswith("/4.0.21"))
        self.assertTrue(notes["url"].endswith("/4.1.0.2300-ls301"))

    def test_quiet_notes_flag_nothing(self):
        go = self.releases([("v2.1.0", "Bug fixes. No breaking changes.")])
        notes = run(go(Newer(current="v2.0.0", newest="v2.1.0", level="minor", behind=1)))
        self.assertEqual(notes["breaking"], "")
        self.assertTrue(notes["url"].endswith("/v2.1.0"))

    def test_github_refusing_still_leaves_a_link(self):
        def handler(request):
            return httpx.Response(403, text=json.dumps({"message": "rate limited"}))

        async def go():
            async with mock_client(handler) as client:
                return await release_notes(
                    client, "o/r", Newer(current="1.0", newest="1.1", level="minor", behind=1))

        notes = run(go())
        self.assertEqual(notes["url"], "https://github.com/o/r/releases")
        self.assertIn("403", notes["detail"])


class WaitingOutTheCooldown(unittest.TestCase):
    """A release counts once it is a few days old, as Dependabot waits."""

    NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    hub = Reference("registry-1.docker.io", "getmeili/meilisearch", "v1.54.1")
    TAGS = ["v1.54.0", "v1.54.1", "v1.54.2", "v1.54.3", "v1.54", "latest"]

    def pushed(self, ages_in_days, asked=None):
        """A Docker Hub that dates each tag `ages_in_days[tag]` days back."""
        def handler(request):
            self.assertEqual(request.url.host, "hub.docker.com")
            tag = request.url.path.rsplit("/", 1)[-1]
            if asked is not None:
                asked.append(tag)
            if tag not in ages_in_days:
                return httpx.Response(404, json={"message": "not found"})
            when = self.NOW - timedelta(days=ages_in_days[tag])
            return httpx.Response(200, json={
                "name": tag, "tag_last_pushed": when.isoformat().replace("+00:00", "Z")})
        return handler

    def settle(self, handler, reference=None, days=3, tags=None):
        async def go():
            async with mock_client(handler) as client:
                return await settled(client, reference or self.hub, tags or self.TAGS,
                                     days, now=self.NOW)
        return run(go())

    def test_a_settled_release_is_reported_as_it_is(self):
        newer, waiting = self.settle(self.pushed({"v1.54.3": 5, "v1.54.2": 6}))
        self.assertEqual((newer.newest, newer.behind), ("v1.54.3", 2))
        self.assertIsNone(waiting)

    def test_a_fresh_release_falls_back_to_the_newest_settled_one(self):
        newer, waiting = self.settle(self.pushed({"v1.54.3": 1.3, "v1.54.2": 3.9}))
        self.assertEqual((newer.newest, newer.level, newer.behind), ("v1.54.2", "patch", 1))
        self.assertEqual((waiting["tag"], waiting["level"]), ("v1.54.3", "patch"))
        # Pushed 1.3 days ago, so out of a 3-day cooldown 1.7 days from now.
        self.assertEqual(datetime.fromisoformat(waiting["ready_at"]),
                         self.NOW + timedelta(days=1.7))

    def test_when_every_newer_release_is_fresh_there_is_nothing_yet(self):
        newer, waiting = self.settle(self.pushed({"v1.54.3": 1, "v1.54.2": 2}))
        self.assertIsNone(newer)
        self.assertEqual(waiting["tag"], "v1.54.3")

    def test_the_cooldown_boundary_counts_as_settled(self):
        newer, _ = self.settle(self.pushed({"v1.54.3": 3, "v1.54.2": 9}))
        self.assertEqual(newer.newest, "v1.54.3")

    def test_an_unreadable_date_does_not_hide_an_update(self):
        newer, waiting = self.settle(self.pushed({}))
        self.assertEqual(newer.newest, "v1.54.3")
        self.assertIsNone(waiting)

    def test_registries_without_push_dates_are_not_asked(self):
        def handler(request):
            raise AssertionError(f"asked {request.url}")
        for registry in ("ghcr.io", "lscr.io"):
            with self.subTest(registry):
                newer, waiting = self.settle(
                    handler, reference=Reference(registry, "fosrl/newt", "v1.54.1"))
                self.assertEqual(newer.newest, "v1.54.3")
                self.assertIsNone(waiting)

    def test_a_cooldown_of_zero_asks_nobody(self):
        def handler(request):
            raise AssertionError(f"asked {request.url}")
        newer, _ = self.settle(handler, days=0)
        self.assertEqual(newer.newest, "v1.54.3")

    def test_lookups_stop_at_the_first_settled_release(self):
        asked: list[str] = []
        self.settle(self.pushed({"v1.54.3": 1, "v1.54.2": 10, "v1.54.0": 30}, asked))
        self.assertEqual(asked, ["v1.54.3", "v1.54.2"])

    def test_a_library_image_is_read_from_the_library_namespace(self):
        seen: list[str] = []

        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={"tag_last_pushed": "2026-09-23T13:51:22.315377Z"})

        async def go():
            async with mock_client(handler) as client:
                return await published_at(
                    client, parse_reference("nginx:1.31.0-alpine-slim"), "1.31.6-alpine-slim")

        self.assertEqual(run(go()), datetime(2026, 9, 23, 13, 51, 22, 315377, tzinfo=timezone.utc))
        self.assertEqual(seen, ["/v2/namespaces/library/repositories/nginx/tags/1.31.6-alpine-slim"])


class VersionOnlyDaemon(BaseHTTPRequestHandler):
    """A Supervisor needs a Docker API to start; these never ask it anything."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"Version": "27.0", "ApiVersion": "1.45"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThroughTheSupervisor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), VersionOnlyDaemon)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def supervisor(self, **env):
        from app.config import Settings
        from app.supervisor import Supervisor

        previous = dict(os.environ)
        os.environ.update(DOCTOR_DATA_DIR=tempfile.mkdtemp(), DOCTOR_MODE="observe",
                          ANTHROPIC_API_KEY="",
                          DOCKER_HOST=f"tcp://127.0.0.1:{self.server.server_address[1]}",
                          **env)
        try:
            supervisor = Supervisor(Settings())
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.addCleanup(supervisor.store.close)
        return supervisor

    def handler(self, asked):
        def handle(request):
            asked.append(request.url.host)
            if request.url.host == "hub.docker.com":
                # Long settled, unless a test says otherwise.
                tag = request.url.path.rsplit("/", 1)[-1]
                age = self.fresh.get(tag, 30)
                when = datetime.now(timezone.utc) - timedelta(days=age)
                return httpx.Response(200, json={"tag_last_pushed": when.isoformat()})
            if request.url.host == "api.github.com":
                return httpx.Response(200, json=[{
                    "tag_name": "4.1.0.2300-ls301", "body": "Breaking: config moved",
                    "html_url": "https://github.com/linuxserver/docker-sonarr/releases/tag/x",
                }])
            parsed = urllib.parse.urlparse(str(request.url))
            self.assertTrue(parsed.path.endswith("/tags/list"))
            return httpx.Response(200, json={"tags": SONARR_TAGS})
        return handle

    def newer(self, supervisor, image, labels, asked):
        async def go():
            async with mock_client(self.handler(asked)) as client:
                return await supervisor._newer_version(client, parse_reference(image), labels)
        newer, self.cooling = run(go())
        return newer

    LABELS = {"org.opencontainers.image.source": "https://github.com/linuxserver/docker-sonarr"}
    fresh: dict[str, float] = {}  # tag -> days since Docker Hub had it pushed

    def test_a_release_inside_its_cooldown_is_held_back(self):
        self.fresh = {"4.1.0": 1}
        newer = self.newer(self.supervisor(), "linuxserver/sonarr:4.0.20", self.LABELS, [])
        self.assertEqual((newer["tag"], newer["level"], newer["behind"]), ("4.0.21", "patch", 1))
        self.assertEqual((self.cooling["tag"], self.cooling["level"]), ("4.1.0", "minor"))

    def test_with_every_newer_release_fresh_only_the_cooling_one_is_left(self):
        self.fresh = {"4.1.0": 1, "4.0.21": 2}
        newer = self.newer(self.supervisor(), "linuxserver/sonarr:4.0.20", self.LABELS, [])
        self.assertIsNone(newer)
        self.assertEqual(self.cooling["tag"], "4.1.0")

    def test_too_many_tags_is_a_warning(self):
        import logging
        from unittest import mock

        # test_doctor silences warnings for the whole run.
        self.addCleanup(logging.disable, logging.root.manager.disable)
        logging.disable(logging.NOTSET)
        supervisor = self.supervisor()
        reason = f"more than {MAX_TAG_PAGES * 1000} tags - {TOO_MANY_TAGS}"
        with mock.patch("app.supervisor.list_tags", mock.AsyncMock(return_value=([], reason))), \
                self.assertLogs("app.supervisor", level="WARNING") as logs:
            newer = self.newer(supervisor, "ghcr.io/immich-app/immich-server:v3.3.3", {}, [])
        self.assertIsNone(newer)
        self.assertIn(TOO_MANY_TAGS, logs.output[0])

    def test_other_listing_failures_stay_info(self):
        import logging
        from unittest import mock

        self.addCleanup(logging.disable, logging.root.manager.disable)
        logging.disable(logging.NOTSET)
        supervisor = self.supervisor()
        with mock.patch("app.supervisor.list_tags",
                        mock.AsyncMock(return_value=([], "registry rate limit reached"))), \
                self.assertNoLogs("app.supervisor", level="WARNING"):
            self.newer(supervisor, "ghcr.io/immich-app/immich-server:v3.3.3", {}, [])

    def test_a_cooling_release_counts_towards_nothing(self):
        """Through _check_image_updates: listed apart, never as a newer version."""
        from unittest import mock

        self.fresh = {"4.1.0": 1, "4.0.21": 2}
        supervisor = self.supervisor()
        supervisor.containers = {
            "sonarr": {"name": "sonarr", "image": "linuxserver/sonarr:4.0.20"}}

        async def image(ref):
            return {"repo_digests": [], "labels": self.LABELS}

        supervisor.docker.image = image
        transport = httpx.MockTransport(self.handler([]))
        real = httpx.AsyncClient
        with mock.patch("app.supervisor.httpx.AsyncClient",
                        lambda **kwargs: real(transport=transport)):
            run(supervisor._check_image_updates())
        result = supervisor.image_updates
        self.assertEqual((result["newer_available"], result["cooling"]), (0, 1))
        entry = result["images"][0]
        self.assertIsNone(entry["newer"])
        self.assertEqual(entry["cooling"]["tag"], "4.1.0")

    def test_the_cooldown_can_be_turned_off(self):
        self.fresh = {"4.1.0": 1}
        asked: list[str] = []
        newer = self.newer(self.supervisor(DOCTOR_UPDATE_COOLDOWN_DAYS="0"),
                           "linuxserver/sonarr:4.0.20", self.LABELS, asked)
        self.assertEqual(newer["tag"], "4.1.0")
        self.assertIsNone(self.cooling)
        self.assertNotIn("hub.docker.com", asked)

    def test_a_pinned_version_reports_the_newer_one_with_its_notes(self):
        asked: list[str] = []
        newer = self.newer(self.supervisor(), "linuxserver/sonarr:4.0.20", self.LABELS, asked)
        self.assertEqual((newer["tag"], newer["level"], newer["behind"]), ("4.1.0", "minor", 2))
        self.assertIn("config moved", newer["breaking"])
        self.assertIn("api.github.com", asked)

    def test_a_version_pinned_to_a_digest_still_learns_of_newer_ones(self):
        asked: list[str] = []
        digest = "sha256:" + "2" * 64
        newer = self.newer(self.supervisor(), f"linuxserver/sonarr:4.0.20@{digest}",
                           self.LABELS, asked)
        self.assertEqual((newer["tag"], newer["level"], newer["behind"]), ("4.1.0", "minor", 2))

    def test_release_notes_can_be_turned_off(self):
        asked: list[str] = []
        newer = self.newer(self.supervisor(DOCTOR_RELEASE_NOTES="false"),
                           "linuxserver/sonarr:4.0.20", self.LABELS, asked)
        self.assertEqual(newer["tag"], "4.1.0")
        self.assertEqual(newer["breaking"], "")
        # Still a link - it costs nothing to build - but GitHub was not asked.
        self.assertEqual(newer["notes_url"], "https://github.com/linuxserver/docker-sonarr/releases")
        self.assertNotIn("api.github.com", asked)

    def test_the_whole_check_counts_newer_versions(self):
        """Through _check_image_updates, the way the dashboard receives it."""
        from unittest import mock

        supervisor = self.supervisor()
        supervisor.containers = {
            "sonarr": {"name": "sonarr", "image": "linuxserver/sonarr:4.0.20"},
            "memos": {"name": "memos", "image": "neosmemo/memos:stable"},
        }

        async def image(ref):
            return {"repo_digests": [], "labels": self.LABELS}

        supervisor.docker.image = image
        transport = httpx.MockTransport(self.handler([]))
        real = httpx.AsyncClient
        with mock.patch("app.supervisor.httpx.AsyncClient",
                        lambda **kwargs: real(transport=transport)):
            run(supervisor._check_image_updates())
        result = supervisor.image_updates
        self.assertEqual(result["newer_available"], 1)
        by_image = {entry["image"]: entry for entry in result["images"]}
        self.assertEqual(by_image["linuxserver/sonarr:4.0.20"]["newer"]["tag"], "4.1.0")
        self.assertIsNone(by_image["neosmemo/memos:stable"]["newer"])

    def test_the_list_fills_in_as_the_check_goes(self):
        """Each progress event carries the results so far, not just a count."""
        from unittest import mock

        supervisor = self.supervisor()
        supervisor.containers = {
            "memos": {"name": "memos", "image": "neosmemo/memos:stable"},
            "sonarr": {"name": "sonarr", "image": "linuxserver/sonarr:4.0.20"},
        }

        async def image(ref):
            return {"repo_digests": [], "labels": self.LABELS}

        supervisor.docker.image = image
        events: list[dict] = []
        supervisor.broadcast = lambda kind, payload: (
            events.append(payload) if kind == "images" else None)
        transport = httpx.MockTransport(self.handler([]))
        real = httpx.AsyncClient
        with mock.patch("app.supervisor.httpx.AsyncClient",
                        lambda **kwargs: real(transport=transport)):
            run(supervisor._check_image_updates())
        answered = [[entry["image"] for entry in event["partial"]] for event in events]
        # Images are checked in name order: sonarr, then memos.
        self.assertEqual(answered, [
            [],                                                       # asking about sonarr
            ["linuxserver/sonarr:4.0.20"],                            # asking about memos
            ["linuxserver/sonarr:4.0.20", "neosmemo/memos:stable"],   # wrapping up
        ])
        self.assertEqual(events[1]["partial"][0]["newer"]["tag"], "4.1.0")

    def test_a_moving_tag_asks_nobody(self):
        asked: list[str] = []
        self.assertIsNone(self.newer(self.supervisor(), "linuxserver/sonarr:latest", {}, asked))
        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
