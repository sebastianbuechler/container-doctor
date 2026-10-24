// Tour the demo dashboard: screenshots for the docs, and every API answer the
// tour reads, for the interactive demo to replay.
//
//   python -m demo.record            (starts the demo, runs this, stops it)
//
// Needs Playwright with Chromium. Run directly against a demo already running:
//   DEMO_PASSWORD=... NODE_PATH=$(npm root -g) node demo/record.cjs http://127.0.0.1:8090 OUT_DIR

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:8090";
const OUT = process.argv[3] || path.join(__dirname, "out");
const AUTH = "Basic " + Buffer.from(`doctor:${process.env.DEMO_PASSWORD}`).toString("base64");
const DESKTOP = { width: 1280, height: 820 };
const PHONE = { width: 390, height: 844 };

// name: [route, what to wait for or do first]
const SHOTS = [
  ["overview", "#/overview"],
  ["incidents", "#/incidents"],
  ["incident", (ids) => `#incident/${ids.approval}`],
  ["containers", "#/containers"],
  ["container", "#container/paperless-db"],
  ["storage", "#/storage"],
  ["drive", "#/storage"],
  ["host", "#/host"],
  ["network", "#/network"],
  ["updates", "#/updates"],
  ["activity", "#/activity"],
  ["llm", "#/llm"],
  ["settings", "#/settings"],
  ["classifier", "#/llm", "#panel-classifier"],
];

const recorded = {};

function record(page) {
  page.on("response", async (response) => {
    const request = response.request();
    const url = new URL(response.url());
    if (request.method() !== "GET" || !(url.pathname.startsWith("/api/") || url.pathname === "/healthz")) return;
    if (url.pathname === "/api/stream" || response.status() !== 200) return;
    try {
      const body = await response.text();
      recorded[url.pathname + url.search] = {
        type: response.headers()["content-type"] || "application/json",
        body,
      };
    } catch (_) { /* the page moved on before the body arrived */ }
  });
}

// Not "networkidle": the dashboard keeps its live stream open, so the network
// never goes quiet. Every view has loaded and drawn well within this.
async function settle(page, ms = 1500) {
  await page.waitForTimeout(ms);
}

async function go(page, route) {
  await page.evaluate((hash) => { location.hash = hash; }, route);
  await settle(page);
}

async function api(page, url) {
  return page.evaluate(async (u) => {
    const r = await fetch(u);
    return r.ok ? r.json().catch(() => null) : null;
  }, url);
}

async function tour(browser, scheme) {
  const context = await browser.newContext({
    viewport: DESKTOP, deviceScaleFactor: 2, colorScheme: scheme,
    extraHTTPHeaders: { Authorization: AUTH }, serviceWorkers: "block",
  });
  const page = await context.newPage();
  record(page);
  await page.goto(BASE + "/#/overview");
  await settle(page, 3000);

  // No status asks for all of them, the way the "All" filter does.
  const incidents = (await api(page, "/api/incidents?limit=100")).incidents;
  const approval = incidents.find((i) => i.status === "awaiting_approval");
  const ids = { approval: approval && approval.id };

  const dir = path.join(OUT, "screenshots");
  fs.mkdirSync(dir, { recursive: true });
  for (const [name, route, panel] of SHOTS) {
    await go(page, typeof route === "function" ? route(ids) : route);
    // A panel further down its page, brought to the top under the header.
    if (panel) {
      await page.evaluate((selector) => {
        const top = document.querySelector(selector).getBoundingClientRect().top;
        window.scrollBy(0, top - 96);
      }, panel);
      await page.waitForTimeout(400);
    }
    if (name === "container" || name === "incident") await page.waitForTimeout(1200);
    // A hard disk's own page, scrolled to its temperature and sleep.
    if (name === "drive") {
      await page.click(".bay[data-drive]");
      await page.waitForTimeout(1200);
      await page.evaluate(() => document.querySelector('#drive-body [data-chart="drive-temp"]')
        ?.closest(".detail-section").scrollIntoView({ block: "start" }));
      await page.waitForTimeout(400);
    }
    // The incident is scrolled to the top, under the sticky header.
    if (name === "incident") await page.evaluate(() => window.scrollBy(0, -96));
    await page.screenshot({ path: path.join(dir, `${name}-${scheme}.png`) });
    if (name === "drive") await page.keyboard.press("Escape");
  }

  await page.setViewportSize(PHONE);
  await go(page, "#/overview");
  await page.screenshot({ path: path.join(dir, `phone-${scheme}.png`) });
  await context.close();
}

// Everything a visitor of the replay could open: each incident and its
// history, each container and its logs, each model call, each filter.
async function collect(browser) {
  const context = await browser.newContext({
    viewport: DESKTOP, extraHTTPHeaders: { Authorization: AUTH }, serviceWorkers: "block",
  });
  const page = await context.newPage();
  record(page);
  await page.goto(BASE + "/#/overview");
  await settle(page, 3000);

  for (const filter of ["awaiting_approval", "resolved", "dismissed", "all", "active"]) {
    await go(page, "#/incidents");
    await page.click(`[data-filter="${filter}"]`).catch(() => {});
    await settle(page, 800);
  }
  for (const view of ["host", "llm"]) {
    await go(page, `#/${view}`);
  }
  for (const selector of ['[data-host-hours="168"]', '[data-host-hours="24"]']) {
    await go(page, "#/host");
    await page.click(selector).catch(() => {});
    await settle(page, 600);
  }
  for (const selector of ['[data-spend-days="14"]', '[data-spend-days="30"]']) {
    await go(page, "#/llm");
    await page.click(selector).catch(() => {});
    await settle(page, 600);
  }

  const incidents = (await api(page, "/api/incidents?limit=100"))?.incidents || [];
  const status = await api(page, "/api/state");
  const calls = (await api(page, "/api/llm/calls?limit=100"))?.calls || [];
  const urls = [];
  for (const incident of incidents) {
    const id = encodeURIComponent(incident.id);
    urls.push(`/api/incidents/${id}`, `/api/incidents/${id}/occurrences?limit=200`,
      `/api/incidents/${id}/handoff`, `/api/incidents/${incident.id}/context?minutes=5`);
  }
  for (const container of status?.containers || []) {
    const name = encodeURIComponent(container.name);
    urls.push(`/api/containers/${name}`, `/api/containers/${name}/logs?lines=300`);
  }
  for (const call of calls) urls.push(`/api/llm/calls/${encodeURIComponent(call.id)}`);
  for (const drive of status?.smart?.drives || []) {
    const key = encodeURIComponent(drive.serial || drive.device);
    for (const hours of [24, 168]) {
      urls.push(`/api/drive/temperature?drive=${key}&hours=${hours}`);
      if (drive.asleep || drive.rotation_rpm > 0) urls.push(`/api/drive/sleep?drive=${key}&hours=${hours}`);
    }
  }
  for (const image of status?.image_updates?.images || []) {
    if (image.newer) {
      urls.push(`/api/images/notes?image=${encodeURIComponent(image.image)}&tag=${encodeURIComponent(image.newer.tag)}`);
    }
  }
  for (const url of urls) await api(page, url);

  // Open a few the way a visitor would, so anything fetched on the side is in.
  for (const container of (status?.containers || []).slice(0, 4)) {
    await go(page, `#container/${encodeURIComponent(container.name)}`);
  }
  await settle(page, 1000);
  await context.close();
}

(async () => {
  const browser = await chromium.launch();
  try {
    await tour(browser, "light");
    await tour(browser, "dark");
    await collect(browser);
  } finally {
    await browser.close();
  }
  const out = { recorded_at: new Date().toISOString(), responses: recorded };
  fs.writeFileSync(path.join(OUT, "api.json"), JSON.stringify(out));
  console.log(`recorded ${Object.keys(recorded).length} answers and ${SHOTS.length * 2 + 2} screenshots in ${OUT}`);
})().catch((error) => { console.error(error); process.exit(1); });
