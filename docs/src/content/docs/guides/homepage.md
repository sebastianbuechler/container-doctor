---
title: "Homepage"
description: "A Container Doctor tile on Homepage or another dashboard, read with a token that opens nothing else."
---

[Homepage](https://gethomepage.dev) and similar start pages show a few numbers
per service. Container Doctor answers them at `/api/widget`: open incidents,
actions waiting for approval, container counts, the LLM spend today, and how
long ago it last polled.

The rest of the API needs a signed-in person, which Homepage is not - with OIDC
it cannot sign in at all. So the widget has a token of its own,
`DOCTOR_WIDGET_TOKEN`, sent as `Authorization: Bearer <token>`. It opens
`/api/widget` and nothing else: not the incidents, not the logs, not the
settings, and no write. Someone who reads it from Homepage's config learns how
many incidents are open, not what they are.

## Set it up

1. Make a token and give it to the doctor:

   ```bash
   openssl rand -hex 24
   ```

   ```yaml
   environment:
     - DOCTOR_WIDGET_TOKEN=${DOCTOR_WIDGET_TOKEN:-}
   ```

   It is environment-only, like every secret, and shorter than 12 characters
   gets a warning on the dashboard.

2. Give the same value to Homepage as `HOMEPAGE_VAR_DOCTOR_WIDGET_TOKEN`, so it
   does not have to be written into a label.

3. Point Homepage's `customapi` widget at it. As labels on the doctor's
   container:

   ```yaml
   labels:
     - homepage.group=Monitoring
     - homepage.name=Container Doctor
     - homepage.href=https://doctor.example.com
     - homepage.widget.type=customapi
     - homepage.widget.url=http://container-doctor:8080/api/widget
     - homepage.widget.refreshInterval=60000
     - homepage.widget.headers.Authorization=Bearer {{HOMEPAGE_VAR_DOCTOR_WIDGET_TOKEN}}
     - homepage.widget.mappings[0].field=open_incidents
     - homepage.widget.mappings[0].label=Open
     - homepage.widget.mappings[0].format=number
     - homepage.widget.mappings[1].field=pending_approvals
     - homepage.widget.mappings[1].label=To approve
     - homepage.widget.mappings[1].format=number
     - homepage.widget.mappings[2].field=containers_unhealthy
     - homepage.widget.mappings[2].label=Unhealthy
     - homepage.widget.mappings[2].format=number
   ```

   Or the same in `services.yaml`:

   ```yaml
   - Container Doctor:
       href: https://doctor.example.com
       widget:
         type: customapi
         url: http://container-doctor:8080/api/widget
         refreshInterval: 60000
         headers:
           Authorization: Bearer {{HOMEPAGE_VAR_DOCTOR_WIDGET_TOKEN}}
         mappings:
           - field: open_incidents
             label: Open
             format: number
           - field: pending_approvals
             label: To approve
             format: number
           - field: containers_unhealthy
             label: Unhealthy
             format: number
   ```

   Homepage asks from its own container, so the URL is the doctor's name on a
   network they share, not the public address.

Homepage's `siteMonitor` can keep pointing at `/healthz`, which needs no token.

## The fields

```json
{
  "status": "ok",
  "open_incidents": 0,
  "pending_approvals": 0,
  "incidents_24h": 3,
  "containers_watched": 42,
  "containers_running": 41,
  "containers_unhealthy": 0,
  "containers_stopped": 1,
  "image_updates": 2,
  "llm_spent_today_usd": 0.14,
  "llm_budget_usd": 1.0,
  "docker_connected": true,
  "last_poll_seconds_ago": 12,
  "version": "0.5.0"
}
```

| Field | What it counts |
|---|---|
| `status` | `ok`, `attention` (an incident is open), `awaiting approval` (an action waits for you) or `docker unreachable` |
| `open_incidents` | incidents not yet resolved or dismissed |
| `pending_approvals` | proposed actions waiting in the dashboard |
| `incidents_24h` | incidents opened in the last 24 hours |
| `containers_watched` | containers the doctor watches; excluded ones are left out of every count |
| `containers_running` | of those, running |
| `containers_unhealthy` | running, but their healthcheck fails |
| `containers_stopped` | not running |
| `image_updates` | images with a newer build in their registry |
| `llm_spent_today_usd` | LLM spend today (UTC), in dollars |
| `llm_budget_usd` | `DOCTOR_DAILY_USD_BUDGET` |
| `docker_connected` | whether the doctor reaches Docker |
| `last_poll_seconds_ago` | seconds since the last poll; `null` before the first |
| `version` | the doctor's version |

These names are kept: a field may be added, but none is renamed or removed
without a note in the release, since they are written into other apps'
configs. Any three or four make a tile; for a duration, use
`format: duration` on `last_poll_seconds_ago`.

A signed-in browser can read `/api/widget` too, so you can check what Homepage
will see. With the token, from a shell:

```bash
curl -H "Authorization: Bearer $DOCTOR_WIDGET_TOKEN" http://localhost:8080/api/widget
```

A wrong token is answered `401`; after 10 wrong ones within 5 minutes, that
client gets `429` until the window passes; wrong passwords count toward the
same 10.
