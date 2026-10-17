---
title: "Network"
description: "The network map, its wiring checks, and internet and DNS from the host."
---

**Network** shows every Docker network, who is on it and at what address, and
what the host publishes. Membership, addresses and ports come from the
container inspect the poll already does. Driver, the *internal* flag, subnets
and networks nobody is on come from `GET /networks`, which the bundled proxy
permits read-only (`NETWORKS=1`; `POST=0` still denies creating, connecting or
removing one). Pointed at a proxy that refuses it, the page still works: unused
networks are missing and *internal* is inferred from containers getting no
gateway.

- **What connects them** draws only the containers on two or more networks,
  wired to those networks. Those are the ones joining segments together — the
  doctor itself, anything behind Traefik that also talks to its own database.
- **Cards** (or the **matrix**, from the layout switch) list every network's
  members. Pointing at a container lights it up on every network it is on.
- **Published ports** lists what is bound on the host itself, and whether on
  every interface or one address.

It also notes wiring mistakes. None opens an incident:

| Note | When |
|---|---|
| `traefik.docker.network` names a network the container is not on | Traefik will route to an address it cannot reach — 502/504 |
| Routed by Traefik, on several networks, no `traefik.docker.network` | Which address Traefik picks is left to chance |
| Behind Traefik and published on every interface | The port skips the proxy's TLS and sign-in |
| On Docker's default `bridge` | No name resolution between containers there |
| Two networks' subnets overlap | Traffic goes to whichever Docker set up last |
| Nobody is on a network | Left over from a removed stack, or one that is down |
| A network is `macvlan` / `ipvlan` | Its containers are on the LAN, and the host itself cannot reach them without a shim |

## Internet and DNS

At the top of the page, checked every minute from the doctor's own container,
which reaches out the way every other container does. Four kinds of check,
each answering a different question:

| Check | Default | Failing means |
|---|---|---|
| **reach** | TCP to `1.1.1.1:443`, `9.9.9.9:443` | All failing: the internet is unreachable. No DNS is involved |
| **resolve** | `cloudflare.com`, `github.com` through the container resolver | DNS is broken, while the line works |
| **DNS server** | none; set `DOCTOR_NET_DNS_SERVERS` | That one server is down while the others answer |
| **HTTPS** | `https://www.cloudflare.com/cdn-cgi/trace` | That site cannot be fetched, through DNS and TLS |

A DNS server is asked with a hand-built UDP query, so no resolver library and
no extra access is needed. List your Pi-hole, AdGuard or router there: a dead
local DNS server is the classic "everything is broken" that the line itself
does not explain.

Each failure is blamed once: no internet explains failing DNS and HTTPS too,
and failing DNS explains failing HTTPS. After `DOCTOR_NET_FAIL_ROUNDS` (3)
failed rounds in a row an incident opens under `host:network`, `critical` for
no internet or no DNS, `warning` for one server or one site. It closes after
as many good rounds, also across a restart of the doctor. None leads to an
action. Every diagnosis is told whether the host could reach the internet and
resolve names at the time.
