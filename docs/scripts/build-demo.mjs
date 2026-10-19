// Put the real dashboard in the docs as a demo, at /container-doctor/demo/.
//
// Copies app/web/static into public/demo/ (generated, not committed) and adds
// demo-shim.js before the dashboard's own script. The shim answers the
// dashboard's API calls from docs/demo/api.json, which `python -m demo.record`
// recorded from the real doctor running against a made-up host.
//
// Runs before `astro dev` and `astro build`; see package.json.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const docs = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const statics = path.join(docs, "..", "app", "web", "static");
const out = path.join(docs, "public", "demo");
const recording = path.join(docs, "demo", "api.json");

fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(path.join(out, "static"), { recursive: true });
for (const name of fs.readdirSync(statics)) {
  if (["index.html", "sw.js", "offline.html", "manifest.webmanifest"].includes(name)) continue;
  fs.copyFileSync(path.join(statics, name), path.join(out, "static", name));
}
fs.copyFileSync(path.join(docs, "demo", "demo-shim.js"), path.join(out, "static", "demo-shim.js"));
fs.copyFileSync(recording, path.join(out, "api.json"));

let html = fs.readFileSync(path.join(statics, "index.html"), "utf8");
html = html
  .replace(/<link rel="manifest"[^>]*>\n?/, "")
  .replaceAll('"/static/', '"static/')
  .replace('<script src="static/app.js"></script>',
    '<script src="static/demo-shim.js"></script>\n<script src="static/app.js"></script>')
  .replace("<head>", '<head>\n<meta name="robots" content="noindex">');
// The same visitor counting as the rest of the site; see astro.config.mjs.
const beacon = process.env.CF_BEACON_TOKEN?.trim();
if (beacon) {
  html = html.replace("</head>", `<script defer src="https://static.cloudflareinsights.com/beacon.min.js" data-cf-beacon='${JSON.stringify({ token: beacon })}'></script>\n</head>`);
}
if (!html.includes("demo-shim.js")) throw new Error("index.html no longer loads static/app.js the way this expects");
fs.writeFileSync(path.join(out, "index.html"), html);
console.log("demo: wrote public/demo/");
