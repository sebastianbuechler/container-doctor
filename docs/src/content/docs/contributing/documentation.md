---
title: "Documentation"
description: "How the docs are built, and the rule that every change checks them."
---

These docs are an [Astro Starlight](https://starlight.astro.build) site in
`docs/`, published to GitHub Pages from `main`.

## Every change checks the docs

A change is not done until the docs say what the code does:

1. **Read the pages your change touches.** Search the site, or
   `grep -ri "<the feature>" docs/src/content/docs`. A new setting, label,
   incident kind, action, page or behaviour belongs somewhere; a changed one
   probably already is somewhere, and is now wrong.
2. **Regenerate the reference.** The environment variable reference is
   generated from `app/config.py` and `.env.example`:

   ```bash
   python scripts/gen_docs.py
   ```

   A setting is described by its comment in `.env.example`, or the comment
   above its declaration in `app/config.py` - write one there rather than
   editing the generated page. The tests fail when the page is out of date,
   and when the code reads a `doctor.*` label the
   [labels page](/container-doctor/reference/labels/) does not list.
3. **Build it** when you changed pages:

   ```bash
   cd docs && npm ci && npm run build
   ```

   The build fails on broken internal links.
4. **Retake the screenshots** when you changed what the dashboard shows or
   what its API answers - see below.

## Screenshots and the live demo

The screenshots in these pages and the [live demo](/container-doctor/getting-started/live-demo/)
come from the real doctor, run against a made-up host:

```bash
python -m demo.record
```

It starts `demo/serve.py` - the doctor with Docker, `/proc` and `/sys`, the
SMART exporter, the registries, the network checks and the model replaced by
stand-ins in `demo/`, and two weeks of seeded history - waits until it has
polled, diagnosed and written its briefing, then tours the dashboard with
Playwright. It writes:

- `docs/src/assets/screenshots/<view>-light.webp` and `-dark.webp`, shown with
  `<Screenshot name="<view>" alt="..." />` in an `.mdx` page; the reader sees
  the one matching their theme.
  The start page shows four of them, and the phone one, in a tour
  (`docs/src/components/Tour.astro`).
- `docs/demo/api.json`, every API answer the tour read.

The demo is the dashboard's own `app/web/static`, copied into the site at build
time by `docs/scripts/build-demo.mjs`, with `docs/demo/demo-shim.js` answering
its API calls from `api.json`. It also stands in for the live stream
(`/api/stream`): like the real one, it sends the recorded snapshot (`/api/state`)
first - every panel drawn from the poll waits for it - and again on each poll
interval, stamped as just polled. So a change to the dashboard reaches the demo
by itself, but a change to what an endpoint or the stream sends needs a new
recording, or the demo shows the new page with the old data. In the browser's
console, the shim logs every request the recording cannot answer
(`demo: not recorded ...`).

To change the scenario - containers, incidents, what the model says - edit
`demo/world.py`, `demo/seed.py` and `demo/model.py`. To look at it live:
`python -m demo.serve`, then open `http://127.0.0.1:8090` and sign in as `doctor` with the password set as `PASSWORD` in `demo/serve.py`.

Recording needs Playwright with Chromium (`npm i -g playwright`, then
`npx playwright install chromium`) and the docs' dependencies (`cd docs && npm ci`).

## Writing

- One page per task or topic; put it in the sidebar in `docs/astro.config.mjs`.
- Lead with what the reader can do, then why. Short paragraphs, real examples.
- Link instead of repeating: settings to the
  [environment variable reference](/container-doctor/reference/environment-variables/),
  labels to [Container labels](/container-doctor/reference/labels/).
- Asides (`:::note`, `:::caution`) for what a reader must not miss - sparingly.

## Running it locally

```bash
cd docs
npm ci
npm run dev   # http://localhost:4321/container-doctor/
```

## Visitor numbers

The published site counts visits with
[Cloudflare Web Analytics](https://www.cloudflare.com/web-analytics/), which
sets no cookies and does not fingerprint visitors, so the site needs no cookie
banner. Each page, the live demo included, loads Cloudflare's beacon when the
build has a token.

The token is the repository variable `CF_BEACON_TOKEN` (**Settings → Secrets
and variables → Actions → Variables**), which the Docs workflow passes to the
build of `main` only. Local builds, pull requests and forks carry no analytics.
Without the variable, the site carries none at all. The numbers are in the
Cloudflare dashboard, under Web Analytics.

