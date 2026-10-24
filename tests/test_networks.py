"""The network map: read from container inspect, never from /networks."""

from __future__ import annotations

import unittest

from app.networks import container_network, report


def attrs(name, networks=None, mode="", ports=None, bindings=None):
    return {
        "Name": f"/{name}",
        "HostConfig": {"NetworkMode": mode or next(iter(networks or {}), "bridge"),
                       "PortBindings": bindings or {}},
        "NetworkSettings": {"Networks": networks or {}, "Ports": ports or {}},
    }


def endpoint(ip, gateway="172.20.0.1", prefix=16, aliases=(), net_id="a" * 64):
    return {"NetworkID": net_id, "IPAddress": ip, "IPPrefixLen": prefix,
            "Gateway": gateway, "Aliases": list(aliases)}


def container(name, raw, state="running", labels=None, project=""):
    labels = dict(labels or {})
    if project:
        labels["com.docker.compose.project"] = project
        labels["com.docker.compose.service"] = name
    return {"name": name, "state": state, "health": "none", "labels": labels,
            "network": container_network(raw)}


class ContainerNetwork(unittest.TestCase):
    def test_reads_addresses_and_drops_id_aliases(self):
        net = container_network(attrs("app", {
            "proxy": endpoint("172.20.0.5", aliases=["app", "web", "0123456789ab"]),
        }))
        self.assertEqual(net["mode"], "proxy")
        [proxy] = net["networks"]
        self.assertEqual((proxy["ip"], proxy["prefix"]), ("172.20.0.5", 16))
        self.assertEqual(proxy["aliases"], ["web"])

    def test_dual_stack_bind_is_one_port(self):
        net = container_network(attrs("app", {"proxy": endpoint("172.20.0.5")}, ports={
            "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"},
                         {"HostIp": "::", "HostPort": "8080"}],
            "9000/tcp": None,  # exposed, not published
        }))
        self.assertEqual(net["ports"], [{"host_ip": "0.0.0.0", "host_port": "8080",
                                         "container_port": "8080", "proto": "tcp"}])

    def test_a_stopped_container_reports_its_configured_ports(self):
        net = container_network(attrs("app", {}, mode="proxy", ports={}, bindings={
            "53/udp": [{"HostIp": "", "HostPort": "53"}]}))
        self.assertEqual(net["ports"][0]["host_port"], "53")
        self.assertEqual(net["ports"][0]["proto"], "udp")


class Report(unittest.TestCase):
    def test_groups_members_and_infers_subnet_and_internal(self):
        result = report([
            container("traefik", attrs("traefik", {"proxy": endpoint("172.20.0.2")}),
                      project="networking"),
            container("app", attrs("app", {
                "proxy": endpoint("172.20.0.3"),
                "app_internal": endpoint("172.30.0.2", gateway="", prefix=24),
            }), project="app"),
            container("db", attrs("db", {"app_internal": endpoint("172.30.0.3", gateway="",
                                                                  prefix=24)}), project="app"),
        ])
        by_name = {n["name"]: n for n in result["networks"]}
        self.assertEqual(by_name["proxy"]["subnet"], "172.20.0.0/16")
        self.assertFalse(by_name["proxy"]["internal"])
        self.assertTrue(by_name["app_internal"]["internal"])
        self.assertEqual(by_name["proxy"]["stacks"], ["app", "networking"])
        # The shared network sorts first.
        self.assertEqual(result["networks"][0]["name"], "proxy")
        self.assertEqual(result["summary"]["multi_homed"], 1)

    def test_host_mode_and_shared_stacks_are_listed_apart(self):
        result = report([
            container("plex", attrs("plex", {"host": endpoint("")}, mode="host")),
            container("qbit", attrs("qbit", {}, mode="container:gluetun")),
        ])
        self.assertEqual(result["networks"], [])
        self.assertEqual(result["host_mode"], ["plex"])
        self.assertEqual(result["shared_stack"], [{"name": "qbit", "via": "gluetun"}])

    def test_traefik_pointed_at_a_network_the_container_is_not_on(self):
        result = report([container("app", attrs("app", {"app_default": endpoint("172.21.0.2")}),
                                   labels={"traefik.enable": "true",
                                           "traefik.docker.network": "proxy"})])
        [finding] = result["findings"]
        self.assertEqual(finding["kind"], "traefik_network_missing")
        self.assertEqual(finding["severity"], "warning")
        self.assertEqual(result["summary"]["warnings"], 1)

    def test_a_compose_prefixed_network_satisfies_the_label(self):
        result = report([container("app", attrs("app", {"stack_proxy": endpoint("172.21.0.2")}),
                                   labels={"traefik.enable": "true",
                                           "traefik.docker.network": "proxy"})])
        self.assertEqual(result["findings"], [])

    def test_two_networks_and_no_label_is_ambiguous(self):
        result = report([container("app", attrs("app", {
            "a": endpoint("172.21.0.2"), "b": endpoint("172.22.0.2")}),
            labels={"traefik.enable": "true"})])
        self.assertEqual([f["kind"] for f in result["findings"]], ["traefik_network_ambiguous"])

    def test_traefiks_own_default_network_settles_it(self):
        traefik = attrs("traefik", {"proxy": endpoint("172.20.0.2")})
        traefik["Config"] = {"Cmd": ["--providers.docker=true",
                                     "--providers.docker.network=proxy"]}
        app = attrs("app", {"proxy": endpoint("172.20.0.3"), "db": endpoint("172.21.0.3")})
        result = report([container("traefik", traefik),
                         container("app", app, labels={"traefik.enable": "true"})])
        self.assertEqual(result["findings"], [])

    def test_traefiks_default_from_the_environment_can_be_missing(self):
        traefik = attrs("traefik", {"proxy": endpoint("172.20.0.2")})
        traefik["Config"] = {"Env": ["TRAEFIK_PROVIDERS_DOCKER_NETWORK=proxy"]}
        app = attrs("app", {"a": endpoint("172.21.0.3"), "b": endpoint("172.22.0.3")})
        result = report([container("traefik", traefik),
                         container("app", app, labels={"traefik.enable": "true"})])
        [finding] = result["findings"]
        self.assertEqual(finding["kind"], "traefik_network_missing")
        self.assertIn("providers.docker.network", finding["summary"])

    def test_proxied_and_published_on_every_interface(self):
        raw = attrs("app", {"proxy": endpoint("172.20.0.3")}, ports={
            "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8081"}],
            "81/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8082"}]})
        result = report([container("app", raw, labels={"traefik.enable": "true"})])
        [finding] = result["findings"]
        self.assertEqual(finding["kind"], "published_and_proxied")
        self.assertIn("8081", finding["summary"])
        self.assertNotIn("8082", finding["summary"])

    def test_default_bridge_is_pointed_out(self):
        result = report([container("old", attrs("old", {"bridge": endpoint("172.17.0.2")}))])
        self.assertEqual(result["networks"][0]["kind"], "default")
        self.assertEqual([f["kind"] for f in result["findings"]], ["default_bridge"])

    def test_overlapping_subnets(self):
        result = report([
            container("a", attrs("a", {"one": endpoint("10.0.0.2", prefix=16)})),
            container("b", attrs("b", {"two": endpoint("10.0.5.2", prefix=24)})),
        ])
        self.assertEqual([f["kind"] for f in result["findings"]], ["subnet_overlap"])

    def test_containers_without_network_facts_are_skipped(self):
        self.assertEqual(report([{"name": "x", "labels": {}}])["networks"], [])



def listed(name, driver="bridge", internal=False, subnet="172.20.0.0/16", **extra):
    return {"Name": name, "Id": "c" * 64, "Driver": driver, "Internal": internal,
            "IPAM": {"Config": [{"Subnet": subnet}] if subnet else []}, **extra}


class Inventory(unittest.TestCase):
    """What GET /networks adds, when the proxy permits it."""

    def test_docker_has_the_last_word_on_internal_and_driver(self):
        # A gateway would make the guess say "not internal"; Docker knows better.
        result = report([container("app", attrs("app", {"backend": endpoint("172.20.0.2")}))],
                        [listed("backend", internal=True)])
        [n] = result["networks"]
        self.assertTrue(n["internal"])
        self.assertEqual((n["driver"], n["source"]), ("bridge", "docker"))
        self.assertTrue(result["inventory"])

    def test_without_it_the_map_still_works(self):
        result = report([container("app", attrs("app", {"backend": endpoint("172.20.0.2")}))])
        self.assertFalse(result["inventory"])
        self.assertEqual(result["networks"][0]["source"], "containers")

    def test_a_network_nobody_is_on_shows_and_is_noted(self):
        result = report([], [listed("old_default", Labels={"com.docker.compose.project": "old"}),
                             listed("bridge", subnet="172.17.0.0/16"),
                             listed("host", driver="host", subnet=""),
                             listed("none", driver="null", subnet="")])
        self.assertEqual([n["name"] for n in result["networks"]], ["old_default"])
        [finding] = result["findings"]
        self.assertEqual(finding["kind"], "unused_network")
        self.assertIn("stack old", finding["summary"])
        self.assertEqual(result["summary"]["unused"], 1)

    def test_macvlan_is_explained(self):
        result = report([], [listed("lan", driver="macvlan", subnet="192.168.1.0/24",
                                    Options={"parent": "eth0"})])
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("lan_network", kinds)
        self.assertIn("eth0", next(f["summary"] for f in result["findings"]
                                   if f["kind"] == "lan_network"))

    def test_icc_off_is_read_from_the_options(self):
        result = report([], [listed("quiet", Options={
            "com.docker.network.bridge.enable_icc": "false"})])
        self.assertFalse(result["networks"][0]["icc"])

    def test_a_network_with_two_subnets_does_not_overlap_itself(self):
        net = listed("dual")
        net["IPAM"]["Config"] = [{"Subnet": "10.1.0.0/16"}, {"Subnet": "10.1.0.0/24"}]
        result = report([], [net])
        self.assertNotIn("subnet_overlap", [f["kind"] for f in result["findings"]])


    def test_a_macvlan_template_in_use_is_not_unused_and_does_not_overlap(self):
        template = listed("mvl-200-template", driver="null", subnet="192.168.200.0/24",
                          ConfigOnly=True, Options={"parent": "eth4.200"})
        child = listed("mvl-200", driver="macvlan", subnet="192.168.200.0/24",
                       ConfigFrom={"Network": "mvl-200-template"},
                       Options={"parent": "eth4.200"})
        result = report([container("pihole", attrs("pihole", {
            "mvl-200": endpoint("192.168.200.5", prefix=24)}))], [template, child])
        kinds = [f["kind"] for f in result["findings"]]
        self.assertNotIn("subnet_overlap", kinds)
        self.assertNotIn("unused_network", kinds)
        self.assertEqual(result["summary"]["unused"], 0)
        by_name = {n["name"]: n for n in result["networks"]}
        self.assertTrue(by_name["mvl-200-template"]["config_only"])
        self.assertFalse(by_name["mvl-200-template"]["unused"])
        self.assertEqual(by_name["mvl-200"]["config_from"], "mvl-200-template")

    def test_a_template_nothing_is_built_from_is_unused(self):
        result = report([], [listed("old-template", driver="null", ConfigOnly=True)])
        self.assertEqual([f["kind"] for f in result["findings"]], ["unused_network"])


if __name__ == "__main__":
    unittest.main()
