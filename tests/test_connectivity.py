"""Internet and DNS checks: the DNS wire format, the verdicts, and recovery."""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app import connectivity  # noqa: E402
from app.connectivity import Check, Monitor, Result, verdict  # noqa: E402
from app.config import Settings  # noqa: E402
from app.remediator import Policy  # noqa: E402
from app.store import Store  # noqa: E402


def answer_for(query: bytes, addresses: list[str], rcode: int = 0) -> bytes:
    """A response to `query`, its answers pointing back at the question's name."""
    query_id = struct.unpack(">H", query[:2])[0]
    question = query[12:]
    header = struct.pack(">HHHHHH", query_id, 0x8180 | rcode, 1, len(addresses), 0, 0)
    answers = b"".join(
        struct.pack(">HHHIH", 0xC00C, 1, 1, 60, 4) + socket.inet_aton(a) for a in addresses)
    return header + question + answers


class WireFormat(unittest.TestCase):
    def test_query_is_an_a_question_with_recursion(self):
        packet = connectivity.build_query(0x1234, "github.com")
        self.assertEqual(packet[:4], b"\x12\x34\x01\x00")
        self.assertEqual(packet[12:], b"\x06github\x03com\x00\x00\x01\x00\x01")

    def test_answers_with_compressed_names(self):
        query = connectivity.build_query(7, "cloudflare.com")
        rcode, addresses = connectivity.parse_response(
            answer_for(query, ["104.16.132.229", "104.16.133.229"]), 7)
        self.assertEqual((rcode, addresses), (0, ["104.16.132.229", "104.16.133.229"]))

    def test_an_answer_to_another_question_is_refused(self):
        query = connectivity.build_query(7, "cloudflare.com")
        with self.assertRaises(ValueError):
            connectivity.parse_response(answer_for(query, ["1.2.3.4"]), 8)

    def test_nxdomain(self):
        query = connectivity.build_query(9, "nope.invalid")
        self.assertEqual(connectivity.parse_response(answer_for(query, [], rcode=3), 9), (3, []))


class FakeDNS(asyncio.DatagramProtocol):
    def __init__(self, addresses, rcode=0):
        self.addresses, self.rcode = addresses, rcode

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.transport.sendto(answer_for(data, self.addresses, self.rcode), addr)


class ServerCheck(unittest.TestCase):
    def ask(self, addresses, rcode=0):
        async def run():
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: FakeDNS(addresses, rcode), local_addr=("127.0.0.1", 0))
            port = transport.get_extra_info("sockname")[1]
            try:
                return await connectivity.check_server(
                    Check("server", f"127.0.0.1:{port}", name="example.com"), 2.0)
            finally:
                transport.close()
        return asyncio.run(run())

    def test_a_server_that_answers(self):
        result = self.ask(["93.184.216.34"])
        self.assertTrue(result.ok)
        self.assertEqual(result.detail, "93.184.216.34")

    def test_a_server_that_refuses(self):
        result = self.ask([], rcode=5)
        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "refused")

    def test_a_server_that_is_not_there(self):
        async def run():
            # A port nothing listens on: refused, or silence until the timeout.
            return await connectivity.check_server(
                Check("server", "127.0.0.1:9", name="example.com"), 0.5)
        self.assertFalse(asyncio.run(run()).ok)


def result(kind, target, ok, detail=""):
    return Result(Check(kind, target, name="cloudflare.com"), ok, 10.0 if ok else None, detail)


class Verdicts(unittest.TestCase):
    def test_no_internet_is_blamed_once(self):
        v = verdict([result("tcp", "1.1.1.1:443", False), result("tcp", "9.9.9.9:443", False),
                     result("dns", "github.com", False), result("http", "https://x", False)])
        self.assertEqual(list(v["failing"]), ["net_internet|"])
        self.assertEqual(v["failing"]["net_internet|"][0], "critical")

    def test_dns_down_while_the_line_works(self):
        v = verdict([result("tcp", "1.1.1.1:443", True), result("dns", "github.com", False, "timed out"),
                     result("http", "https://x", False)])
        self.assertEqual(list(v["failing"]), ["net_dns|"])

    def test_one_dns_server_down_while_others_answer(self):
        v = verdict([result("tcp", "1.1.1.1:443", True), result("dns", "github.com", True),
                     result("server", "192.168.1.2", False, "timed out"),
                     result("server", "1.1.1.1", True)])
        self.assertEqual(list(v["failing"]), ["net_dns_server|192.168.1.2"])
        self.assertEqual(v["failing"]["net_dns_server|192.168.1.2"][0], "warning")

    def test_one_site_down(self):
        v = verdict([result("tcp", "1.1.1.1:443", True), result("dns", "github.com", True),
                     result("http", "https://x", False, "HTTP 503")])
        self.assertEqual(list(v["failing"]), ["net_http|https://x"])

    def test_all_well(self):
        v = verdict([result("tcp", "1.1.1.1:443", True), result("dns", "github.com", True)])
        self.assertEqual((v["internet"], v["dns"], v["failing"]), (True, True, {}))


class Rounds(unittest.TestCase):
    def test_reports_after_n_rounds_then_quietly(self):
        monitor = Monitor(Settings(net_fail_rounds=3))
        down = [result("tcp", "1.1.1.1:443", False)]
        reports = []
        for _ in range(5):
            monitor.record(down)
            reports.append(monitor.evaluate())
        self.assertEqual([len(r) for r in reports], [0, 0, 1, 1, 1])
        self.assertEqual([r[0][5] for r in reports[2:]], [False, True, True])

    def test_a_good_round_starts_the_count_again(self):
        monitor = Monitor(Settings(net_fail_rounds=2))
        monitor.record([result("tcp", "1.1.1.1:443", False)])
        monitor.evaluate()
        monitor.record([result("tcp", "1.1.1.1:443", True)])
        monitor.evaluate()
        monitor.record([result("tcp", "1.1.1.1:443", False)])
        self.assertEqual(monitor.evaluate(), [])

    def test_view_keeps_history_and_uptime(self):
        monitor = Monitor(Settings(net_tcp_targets=["1.1.1.1:443"], net_dns_names=[],
                                   net_http_urls=[], net_dns_servers=[]))
        for ok in (True, True, False, True):
            monitor.record([result("tcp", "1.1.1.1:443", ok)])
        [row] = monitor.view()["checks"]
        self.assertEqual(row["uptime_pct"], 75.0)
        self.assertEqual(len(row["history"]), 4)

    def test_none_empties_a_list_setting(self):
        os.environ["DOCTOR_NET_HTTP_URLS"] = "none"
        try:
            self.assertEqual(Settings().net_http_urls, [])
        finally:
            del os.environ["DOCTOR_NET_HTTP_URLS"]


class Recovery(unittest.TestCase):
    """Closing is judged against the incidents actually open, so one left open
    across a restart still closes."""

    def test_an_incident_from_before_a_restart_closes_after_n_good_rounds(self):
        from app.detector import Signal
        from app.supervisor import Supervisor
        store = Store(":memory:")
        self.addCleanup(store.close)
        signal = Signal(kind="net_internet", severity="critical", summary="no internet",
                        container=dict(connectivity.NET_CONTAINER))
        incident_id = store.open_incident(
            container_id="", container_name="host:network", image="", kind="net_internet",
            fingerprint=signal.fingerprint, severity="critical", summary="no internet",
            evidence={}, mode="observe", rule_severity="critical", lines=[])
        sup = Supervisor.__new__(Supervisor)
        sup.settings = Settings(net_fail_rounds=2, public_url="")
        sup.store = store
        sup.net = Monitor(sup.settings)
        sup._net_ok_rounds = {}
        sup.broadcast = lambda *args: None
        sup.notifier = SimpleNamespace(notify_recovered=AsyncMock())
        sup._process_signal = AsyncMock()

        sup.net.record([result("tcp", "1.1.1.1:443", True)])
        asyncio.run(sup._net_round_done())
        self.assertNotEqual(store.get_incident(incident_id)["status"], "resolved")
        sup.net.record([result("tcp", "1.1.1.1:443", True)])
        asyncio.run(sup._net_round_done())
        self.assertEqual(store.get_incident(incident_id)["status"], "resolved")
        sup.notifier.notify_recovered.assert_awaited_once()

    def test_network_incidents_never_lead_to_an_action(self):
        decision = Policy(Settings(mode="auto"), store=None).decide(
            incident={"container_name": "host:network", "kind": "net_dns", "fingerprint": "f"},
            diagnosis={"recommended_action": "restart_container", "confidence": 0.99},
            container_policy=SimpleNamespace(enabled=True, mode=None, actions=None,
                                             max_actions_per_hour=None),
        )
        self.assertEqual((decision.action, decision.status), ("notify_only", "skipped"))


if __name__ == "__main__":
    unittest.main()
