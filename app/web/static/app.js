/* Container Doctor dashboard. No build step and no framework: the whole UI is
   this file plus styles.css. */

const $ = (id) => document.getElementById(id);

// First page of every list. Small on purpose: on a phone thirty containers
// and fifty incidents is several screens before the action log is reached.
const PAGE = { containers: 8, incidents: 10, actions: 10, images: 5, changes: 10, calls: 15 };
// Compact rows are for reading many at once, so they come three pages at a time.
const PAGE_LIST = {
  containers: "containers", incidents: "incidents", actions: "activity", images: "updates",
  changes: "changes", calls: "calls",
};
function pageSize(kind) {
  return PAGE[kind] * (state.density[PAGE_LIST[kind]] === "compact" ? 3 : 1);
}
// The API's own ceiling for one request.
const MAX_ROWS = 200;

const state = {
  status: null,
  snapshot: { containers: [], disk: [], docker_connected: false },
  pollRequested: false,
  incidents: [],
  actions: [],
  filter: "active",
  me: null,
  settingsLoaded: false,
  incidentLimit: PAGE.incidents,
  incidentTotal: 0,
  actionLimit: PAGE.actions,
  actionTotal: 0,
  changes: [],
  changeLimit: PAGE.changes,
  changeTotal: 0,
  memory: null,
  memoryUnlimitedOpen: false,
  spendDays: 30,
  spend: null,
  calls: [],
  callsLoaded: false,
  callTotal: 0,
  callLimit: PAGE.calls,
  callFilter: "",
  classifier: null,
  shown: { containers: PAGE.containers, images: PAGE.images },
  containerFilter: "",
  onlyIssues: false,
  expanded: new Set(),
  classifierOpen: new Set(),
  // Incidents dismissed on screen whose POST has not come back yet.
  dismissing: new Set(),
  openReasons: new Set(),
  streamDown: false,
  // Set when the doctor cannot be reached at all: "network" (off the VPN, no
  // signal) or "restarting" (the proxy answers, the doctor does not yet).
  offline: null,
  lastContact: 0,
  source: null,
  // An incident opened from a link, kept on screen even when the current
  // filter would not list it.
  focusIncident: null,
  detailName: null,
  detailData: null,  // the container view's last answer, to re-render from
  grouped: false,
  // Per list: "full" (cards, charts, reasons) or "compact" (one line each).
  density: {},
  notes: new Map(),  // release notes opened on the Updates page
  stackOpen: new Map(),
  // Re-diagnose forms that are open, and what has been typed into them, so a
  // refresh arriving mid-sentence does not throw the sentence away.
  diagnoseOpen: new Set(),
  diagnoseDrafts: new Map(),
  diskHistory: null,
  network: null,
  networkKey: "",
  hostHours: 24,
  driveTempHours: 24,
  driveTemps: null,
  driveSleep: null,
  driveIo: null,
  driveCounters: null,
  hostHistory: null,
  hostHistoryAt: 0,
  view: "overview",
  briefing: null,
  // What was a skeleton on the last render, so text that replaces one fades in
  // once rather than on every re-render.
  briefSkeletons: new Set(),
};


const ACTION_LABELS = {
  none: "no action",
  notify_only: "notify only",
  restart_container: "restart container",
  stop_container: "stop container",
  prune_images: "prune dangling images",
  redeploy_stack: "redeploy stack",
  manual: "manual",
};

const SEVERITY_LEVEL = { info: "neutral", warning: "warning", critical: "critical" };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function bytes(value) {
  const n = Number(value) || 0;
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = n;
  for (const unit of units) {
    if (amount < 1024) return `${amount.toFixed(amount < 10 && unit !== "B" ? 1 : 0)} ${unit}`;
    amount /= 1024;
  }
  return `${amount.toFixed(1)} PiB`;
}

// Rendered timestamps used to freeze until the next poll re-rendered them, so
// "polled 2s ago" sat there reading 2s for half a minute. Emit a placeholder
// carrying the instant, and retime them once a second without touching the
// surrounding DOM - re-rendering would collapse an open incident mid-read.
// Small stable hash, so the notices box remembers a dismissal per set of
// notices rather than per position.
function hash(text) {
  let h = 5381;
  for (let i = 0; i < text.length; i += 1) h = ((h * 33) ^ text.charCodeAt(i)) >>> 0;
  return h.toString(36);
}

function agoTag(iso) {
  return `<span data-ago="${esc(iso || "")}">${ago(iso)}</span>`;
}

function retimeAll() {
  for (const el of document.querySelectorAll("[data-ago]")) {
    el.textContent = ago(el.dataset.ago);
  }
  renderPoll();
}

function ago(iso) {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

// Where a meter turns amber and red. Read from the running configuration, so
// a bar never calls something critical that the rules would not act on - the
// disk bar sat red at 92% while the threshold had been raised to 95.
function thresholds() {
  return state.status?.thresholds || {};
}

function level(pct, warn = 75, crit = 90) {
  if (pct >= crit) return "critical";
  if (pct >= warn) return "warning";
  return "ok";
}

function statusPill(label, levelName) {
  return `<span class="status" data-level="${levelName}">${esc(label)}</span>`;
}

// One bar under every list: how much is showing, a way to see more, and a
// way back to the first page, so no list quietly hides its tail.
function pager(kind, shown, total, page, cap = Infinity) {
  if (total <= page) return "";
  const left = Math.min(total, cap) - shown;
  const more = left > 0
    ? `<button type="button" class="btn btn-small" data-more="${kind}">Show ${Math.min(page, left)} more</button>`
    : "";
  const fewer = shown > page
    ? `<button type="button" class="btn btn-small btn-quiet" data-fewer="${kind}">Show fewer</button>`
    : "";
  return `<div class="more-bar">${more}${fewer}
    <span class="muted">${Math.min(shown, total)} of ${total}</span></div>`;
}

function toast(message, kind = "info") {
  // While the banner says the doctor is out of reach, every failed request
  // saying so again in a toast is noise on top of it.
  if (kind === "error" && state.offline) return;
  const el = $("toast");
  el.textContent = message;
  el.dataset.level = kind;
  el.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { el.hidden = true; }, 5000);
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (error) {
    // Nothing answered at all: no VPN, no signal, or the name does not resolve.
    connectionLost("network");
    throw error;
  }
  // Traefik's starting-up page, or a gateway with nothing behind it.
  if ([502, 503, 504].includes(response.status)) connectionLost("restarting");
  else noteContact();
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* keep statusText */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

// Reads go through here. The stream, the poll timer and the page itself all
// ask for the same lists, often at once:
// - one request per URL is in flight at a time. Asking while one runs queues
//   a single follow-up rather than joining it, since the running one may
//   predate whatever prompted the ask; any number of asks meanwhile share it.
// - each slot (a panel's list, say) only draws the answer to its latest ask,
//   so a slow reply for the old filter cannot overwrite the new one.
const inflight = new Map();
const tickets = new Map();
const SUPERSEDED = Symbol("superseded");

function fetchFresh(path) {
  const running = inflight.get(path);
  if (running) {
    running.next ||= running.promise.catch(() => {}).then(() => fetchFresh(path));
    return running.next;
  }
  const entry = {};
  entry.promise = api(path).finally(() => {
    if (inflight.get(path) === entry) inflight.delete(path);
  });
  inflight.set(path, entry);
  return entry.promise;
}

// Fetches `path` for `slot` and hands it to `draw`, unless a newer ask for the
// same slot was made meanwhile. Rejects on failure, for the caller to report.
function load(slot, path, draw) {
  const ticket = {};
  tickets.set(slot, ticket);
  return fetchFresh(path).then((data) => (tickets.get(slot) === ticket ? draw(data) : SUPERSEDED));
}

// The shape of what is on its way, until a list's first answer replaces it.
const SKELETON_WIDTHS = [34, 28, 40, 31, 36];
function skeletonRows(n, tag = "div") {
  return `<span class="visually-hidden">Loading\u2026</span>` + Array.from({ length: n }, (_, i) => `
    <${tag} class="skeleton-row list-skeleton-row" aria-hidden="true" style="--i:${i}">
      <span class="skeleton" style="width:14%"></span>
      <span class="skeleton" style="width:${SKELETON_WIDTHS[i % SKELETON_WIDTHS.length]}%"></span>
      <span class="skeleton" style="width:22%"></span></${tag}>`).join("");
}

function skeletonStats(n) {
  return Array.from({ length: n }, (_, i) => `
    <div class="spend-stat" aria-hidden="true"><span>&nbsp;</span>
      <span class="skeleton skeleton-title" style="width:60%;--i:${i}"></span>
      <span class="skeleton" style="width:45%;--i:${i + 1}"></span></div>`).join("");
}

function skeletonBars(n) {
  const heights = [38, 62, 24, 80, 46, 30, 70, 54, 20, 66, 42, 58];
  return `<div class="chart-skeleton" aria-hidden="true">${Array.from({ length: n }, (_, i) =>
    `<span class="skeleton chart-skeleton-bar" style="height:${heights[i % heights.length]}%;--i:${i % 6}"></span>`).join("")}</div>`;
}

// Every list that waits on a request or on the stream's first state, drawn
// as skeletons once at start; each renderer leaves them be until its data is in.
const SKELETONS = {
  incidents: () => skeletonRows(4), containers: () => skeletonRows(6), memory: () => skeletonRows(4, "li"),
  disks: () => skeletonRows(2), "drive-health": () => skeletonRows(2),
  "host-tiles": () => skeletonRows(3), "host-charts": () => skeletonBars(24), "host-detail": () => skeletonRows(2),
  connectivity: () => skeletonRows(2), "network-map": () => skeletonRows(4),
  "network-ports": () => skeletonRows(3), "image-updates": () => skeletonRows(3),
  actions: () => skeletonRows(5, "li"), changes: () => skeletonRows(5, "li"),
  settings: () => skeletonRows(6),
};

function paintSkeletons() {
  for (const [id, markup] of Object.entries(SKELETONS)) $(id).innerHTML = markup();
}


// Short enough to read standing up, and written against what the dashboard
// actually does rather than restating the label.
const INFO = {
  mode: `<ul>
      <li><strong>observe</strong> diagnoses and records, and never touches a container.</li>
      <li><strong>suggest</strong> proposes an action and waits for you to approve it.</li>
      <li><strong>auto</strong> applies the actions listed under "auto-applies" by itself, if
        the diagnosis clears the confidence bar; anything else still waits.</li>
    </ul>
    <p>Rate limits and the per-container cooldown apply in every mode.</p>`,
  incidents: `<p>The big number is what is still open - anything not resolved or dismissed,
    including whatever is awaiting your approval.</p>
    <p>Underneath is how many were opened in the last 24 hours, which counts the ones you
    have already dealt with.</p>`,
  approval: `<p>Actions the policy engine proposed but will not run on its own.</p>
    <p>They sit here until you approve or reject them; nothing times out into being
    applied.</p>`,
  usage: `<p>Spend today against the daily budget. Reaching it stops diagnosis, not
    detection - incidents are still recorded and notified.</p>
    <p>Identical failures reuse a cached diagnosis rather than paying twice.</p>
    <p>Free tiers meter requests rather than dollars, so the call cap is the one that binds
    there. The model and prices in use are under Settings.</p>`,
  containers: `<p>Every running container is watched unless it carries
    <code>doctor.enable=false</code>.</p>
    <p>Anything stopped, unhealthy or starting sorts to the top.</p>
    <p>CPU is shown against one core and against the whole host - the alert threshold uses
    the host figure.</p>`,
  filters: `<ul>
      <li><strong>Open</strong> is everything still wanting attention, including awaiting
        approval.</li>
      <li><strong>Dismissed</strong> is what you closed by hand, kept until the retention
        limit.</li>
      <li><strong>All</strong> is exactly that.</li>
    </ul>`,
  images: `<p>Two questions per image:</p>
    <ul>
      <li><strong>New build</strong>: the tag now points at a different image than the one
        running - a new build of <code>:latest</code>, or LinuxServer rebuilding
        <code>4.0.20</code>. Checked by digest.</li>
      <li><strong>Newer version</strong>: for a tag pinned to a version such as
        <code>4.0.20</code>, a newer release in the same line, labelled
        <em>major</em>, <em>minor</em> or <em>patch</em>. Read from the registry's tags.</li>
    </ul>
    <p>A new build is taken by redeploying its stack. With <code>DOCTOR_GITHUB_TOKEN</code>
    set, <strong>Redeploy</strong> runs the repository's Deploy stacks workflow for it, then
    checks the new build is what runs. A newer version means changing the pin in the
    stack's compose file.</p>
    <p>Where the image names its GitHub source, <strong>Release notes</strong> opens the
    notes of every release since the running one, and the check flags notes that mention
    breaking changes.</p>
    <p>A release counts once it has been on Docker Hub for the update cooldown (3 days unless
    changed), the wait Dependabot keeps too. Younger ones are listed under
    <strong>Cooling down</strong>, for information only.</p>
    <p><strong>Could not be checked</strong> lists what no registry would answer for -
    private, rate-limited or built locally. None of it counts as up to date.</p>
    <p>The doctor never pulls or edits a pin itself.</p>`,
  memory: `<p>Each container's highest hourly peak over the last 14 days, against its
    limit.</p>
    <p>A limit is a cap, not a reservation - setting one frees nothing, but it means a leak
    kills that one container instead of starving the host.</p>
    <p>The suggestion is the peak plus a third, rounded; set it as <code>mem_limit</code> on
    the service in its stack's compose file.</p>`,
  spend: `<p>What each day cost, split by what it paid for: diagnoses, the dashboard summary,
    and the classifier. Days older than the call log keep their total as
    <em>not itemized</em>.</p>
    <p>Prices come from the built-in table for Anthropic models, or from
    <code>DOCTOR_PRICE_*_PER_MTOK</code>. A model without a price counts as free, which is
    why the daily call cap exists.</p>`,
  calls: `<p>Every request sent to a model, as it was sent - the system prompt, the prompt
    with the evidence (already redacted), the schema - and what came back, or the error.
    Open one to read it.</p>
    <p>Kept for <code>DOCTOR_LLM_LOG_DAYS</code>; turn it off with 0.</p>`,
  classifier: `<p>A decision model's second opinion on the signals a rule decides by
    judgement - error bursts, fatal lines, failed health checks, CPU and memory - before
    they become incidents. Exits, OOM kills, disks and arrays are never asked about.</p>
    <ul>
      <li><strong>shadow</strong> records every verdict and changes nothing: read them
        against what happened before trusting it.</li>
      <li><strong>on</strong> hides a signal only when a calibrated model puts noise at or
        above the threshold, and may raise or lower a severity.</li>
    </ul>
    <p>Whenever it cannot answer - cap, timeout, error - the rule decides alone.</p>`,
  changes: `<p>Read from Docker between polls and timed by Docker itself:</p>
    <ul>
      <li><strong>recreated</strong> - a redeploy, on a new build of the same tag or a new
        version</li>
      <li><strong>renamed</strong> - the same service under a new container name</li>
      <li>restarted, started or stopped - restarts in a crash loop count up one row</li>
    </ul>
    <p>The diagnosis is shown the recent changes of the container it is diagnosing.</p>`,
  storage: `<p>Usage is sampled hourly; the dashed line carries the fitted rate forward from
    the latest reading to the day the disk would be full.</p>
    <p>Disk arrays come from the kernel's <code>/proc/mdstat</code>: a missing or failed
    disk, or a rebuild, opens an incident.</p>
    <p>On a Synology, DSM's system arrays md0 and md1 always show empty bays, so there they only
    count as degraded when they lose a disk they had. On any other host every array is judged alike.</p>`,
  network: `<p>Read on every poll:</p>
    <ul>
      <li>membership and addresses from each container</li>
      <li>driver, flags and subnets from Docker's own network list
        (<code>GET /networks</code>, read-only on the bundled proxy)</li>
    </ul>
    <p>Behind a proxy that refuses it, unused networks are missing and
    <strong>internal</strong> is inferred from containers getting no gateway.</p>
    <p>Point at a container or a network to see everywhere it appears. Containers on two or
    more networks are what joins them, and are drawn separately.</p>
    <p>The notes are wiring mistakes worth a look; none of them opens an incident.</p>`,
  host: `<p>Read from the kernel on every poll. These counters are system-wide, so the doctor
    sees the host's own numbers without any extra access.</p>
    <p>An incident opens when one of these lasts (ten polls by default):</p>
    <ul>
      <li>memory available falls below the threshold</li>
      <li>swap is being read back steadily - swap merely in use is normal</li>
      <li>the 5-minute load passes its limit per CPU</li>
      <li>I/O wait stays high</li>
      <li>a sensor runs hot, where the host exposes its sensors</li>
    </ul>
    <p>It closes by itself once the condition has been gone as long. A restart of the host is
    noticed from its boot time, notified, and listed under Activity.</p>
    <p>Network traffic is not here: a container only sees its own.</p>`,
  hostHistory: `<p>Averages per few minutes (<code>DOCTOR_HOST_SAMPLE_MINUTES</code>), with the
    live reading at the end, so short spikes are smoothed out.</p>
    <p>Linux load counts processes waiting on disks as well as on CPUs, so a scrub or a
    backup lifts it without the CPU being busy.</p>
    <p><strong>CPU by container</strong> names the containers behind the CPU line, from each
    one's hourly average.</p>`,
  hostDetail: `<ul>
      <li><strong>Disks</strong>: how busy each drive was since the last poll, and its
        throughput. Each drive's own page keeps the history.</li>
      <li><strong>Pressure</strong>: the kernel's account of time spent stalled, where the
        kernel provides it.</li>
      <li><strong>Temperatures</strong>: hardware sensors, where the host exposes them to
        containers.</li>
    </ul>`,
  connectivity: `<p>Checked from the doctor's own container, which reaches out the way every
    other container does. Each kind answers a different question:</p>
    <ul>
      <li><strong>reach</strong>: a connection to an address, no DNS involved. All failing
        means the internet is unreachable.</li>
      <li><strong>resolve</strong>: a name looked up the way containers look names up.
        Failing while reach works means DNS is broken, not the line.</li>
      <li><strong>DNS server</strong>: one server asked directly - a Pi-hole, AdGuard, the
        router - so a dead one stands out while the others answer.</li>
      <li><strong>HTTPS</strong>: a page fetched through DNS and TLS.</li>
    </ul>
    <p>Each failure is blamed once: no internet explains failing DNS too. An incident opens
    after a few failed rounds in a row and closes after as many good ones. The strip is the
    last hour.</p>`,
  smart: `<p>Each drive's own account of its health, read with smartctl by the
    <code>doctor-smart</code> sidecar - the doctor itself never touches a disk.</p>
    <ul>
      <li><strong>Hard disks</strong>: reallocated, pending and uncorrectable sectors. Any
        above zero is a warning; a drive that says it is failing is critical. CRC errors
        point at the cable or bay, so only new ones count.</li>
      <li><strong>NVMe</strong>: the controller's critical warning, media errors, wear and
        spare capacity.</li>
    </ul>
    <p>Read every five minutes. A drive asleep is not woken; it shows as asleep, and a hard
    disk's own page shows how much it slept and how often it woke. A drive's incident closes
    once nothing is wrong with it any more.</p>`,
  ports: `<p>Ports bound on the host itself.</p>
    <p><strong>Every interface</strong> means anything that can reach the host can reach the
    container directly - for a service behind Traefik that skips its TLS and sign-in.</p>
    <p>Bind to <code>127.0.0.1</code>, or drop the <code>ports:</code> entry, when the proxy
    is the only way in that should exist.</p>`,
};

// Hover and keyboard focus, handled in CSS: no open/close state to get out of
// step, and it still works on touch because tapping a button focuses it.
function infoTag(body) {
  if (!body) return "";
  return `<span class="info-wrap">
    <button type="button" class="info" aria-label="Explain">i</button>
    <span class="info-pop" role="note">${body}</span>
  </span>`;
}

// The pop is fixed rather than absolute, and placed here: centred under its
// button, then pulled back inside the viewport, and flipped above when there
// is no room below. Absolute positioning let it run off the edge of a phone,
// got it clipped by the panel's overflow, and widened the page even while
// hidden.
const POP_GAP = 6;
const POP_EDGE = 12;

function placePop(wrap) {
  const pop = wrap.querySelector(".info-pop");
  const button = wrap.querySelector(".info");
  if (!pop || !button) return;
  const viewport = document.documentElement.clientWidth;
  const height = window.innerHeight;
  pop.style.maxWidth = `${Math.min(300, viewport - POP_EDGE * 2)}px`;
  const anchor = button.getBoundingClientRect();
  const width = pop.offsetWidth;
  const tall = pop.offsetHeight;

  let left = anchor.left + anchor.width / 2 - width / 2;
  left = Math.max(POP_EDGE, Math.min(left, viewport - POP_EDGE - width));
  let top = anchor.bottom + POP_GAP;
  if (top + tall > height - POP_EDGE && anchor.top - POP_GAP - tall > POP_EDGE) {
    top = anchor.top - POP_GAP - tall;
  }
  pop.style.left = `${Math.round(left)}px`;
  pop.style.top = `${Math.round(top)}px`;
}

function placeOpenPops() {
  document.querySelectorAll(".info-wrap:hover, .info-wrap:focus-within").forEach(placePop);
}

// The headings carry a key; fill each one in once the page exists.
function mountHeadingInfo() {
  for (const button of [...document.querySelectorAll("button.info[data-info]")]) {
    const body = INFO[button.dataset.info];
    if (!body) continue;
    button.removeAttribute("data-info");
    const wrap = document.createElement("span");
    wrap.className = "info-wrap";
    button.replaceWith(wrap);          // wrap now sits where the button was
    wrap.appendChild(button);          // and the button moves inside it
    wrap.insertAdjacentHTML("beforeend", `<span class="info-pop" role="note">${body}</span>`);
  }
}

const THEMES = [["auto", "Auto"], ["light", "Light"], ["dark", "Dark"]];

function currentTheme() {
  return document.documentElement.dataset.theme || "auto";
}

// The browser's own bars (Android's status and navigation bars) take their
// colour from the theme-color tags, which follow the system scheme. A theme
// picked by hand pins both tags to it, so the bars match the page.
const THEME_COLORS = { light: "#f7f7f5", dark: "#111110" };

function syncThemeColor(name) {
  document.querySelectorAll('meta[name="theme-color"]').forEach((meta) => {
    const scheme = meta.media.includes("dark") ? "dark" : "light";
    meta.content = THEME_COLORS[THEME_COLORS[name] ? name : scheme];
  });
}

function applyTheme(name) {
  document.documentElement.dataset.theme = name;
  syncThemeColor(name);
  try { localStorage.setItem("doctor-theme", name); } catch (_) { /* private mode */ }
  document.querySelectorAll("[data-theme-choice]").forEach((button) =>
    button.setAttribute("aria-checked", String(button.dataset.themeChoice === name)));
}

// The Settings page grew past a screenful, so its sections sit under a few
// tabs, and a filter searches every tab at once.
const SETTINGS_TABS = [
  ["general", "General"],
  ["detection", "Detection"],
  ["response", "Response"],
  ["ai", "AI"],
];
const SETTINGS_TAB_KEY = "doctor-settings-tab";

function settingsTab() {
  let tab = "";
  try { tab = localStorage.getItem(SETTINGS_TAB_KEY) || ""; } catch (_) { /* private mode */ }
  return SETTINGS_TABS.some(([value]) => value === tab) ? tab : "general";
}

// A browser preference rather than server configuration, so it is drawn here
// and not fetched - and it still appears when /api/settings is refused.
function appearanceGroup() {
  const theme = currentTheme();
  return `
    <section class="settings-group" data-group="general">
      <h3>Appearance</h3>
      <dl>
        <div class="setting" data-search="appearance theme auto light dark colour color">
          <dt><span class="setting-label">Theme</span>
            <span class="setting-desc">Auto follows the operating system. Remembered in this browser only.</span></dt>
          <dd>
            <div class="segmented" role="radiogroup" aria-label="Colour theme">
              ${THEMES.map(([value, label]) => `<button type="button" role="radio"
                data-theme-choice="${value}" aria-checked="${value === theme}">${label}</button>`).join("")}
            </div>
          </dd>
        </div>
      </dl>
    </section>`;
}

// What was saved on this page, as a file: downloaded to keep, or brought in
// from another doctor where the page may save.
function backupGroup(editable) {
  return `
    <section class="settings-group" data-group="general">
      <h3>Backup</h3>
      <dl>
        <div class="setting" data-search="backup export download file json">
          <dt><span class="setting-label">Export</span>
            <span class="setting-desc">Download what was saved on this page, to keep or import elsewhere.</span></dt>
          <dd><div class="setting-control"><a class="btn btn-small" href="/api/settings/export"
            download="container-doctor-settings.json">Export</a></div></dd>
        </div>${editable ? `
        <div class="setting" data-search="backup import upload file json restore">
          <dt><span class="setting-label">Import</span>
            <span class="setting-desc">Save the settings in an exported file. You see what changes before anything is saved.</span></dt>
          <dd><div class="setting-control"><button type="button" class="btn btn-small"
            data-settings-import>Import…</button></div></dd>
        </div>` : ""}
      </dl>
    </section>`;
}

// What deploys the stacks, as DOCTOR_DOCKER_MANAGER names it - Portainer,
// Coolify... - or nothing, and then the labels speak of the stack environment.
function dockerManager() {
  return String(state.settingsManager ?? state.status?.docker_manager ?? "").trim();
}
function stackEnvironment() {
  const manager = dockerManager();
  return manager ? `${manager}'s stack environment` : "the stack environment";
}

// Which kind of value a row is: one the stack environment sets, one the
// compose file fixes, or a fact. Older servers send no kind: a fact.
function settingKinds() {
  return {
    setting: `Set in ${stackEnvironment()}`,
    fixed: "Fixed: the compose file does not pass this variable, so the stack environment cannot change it",
  };
}
// Where a value comes from, for the rows the stack environment or this page
// set. The environment wins: a variable set there overrides what is saved here.
function settingSources() {
  const where = stackEnvironment();
  return {
    env: [dockerManager() || "Stack env", "is-env", `Set in ${where}, which overrides anything saved here`],
    saved: ["saved here", "is-saved", `Saved on this page; a variable set in ${where} would override it`],
  };
}
const SETTINGS_VIEW_KEY = "doctor-settings-view";

// Remembered in this browser where it may; kept in memory regardless.
let settingsViewState = null;
function settingsView() {
  if (settingsViewState === null) {
    try { settingsViewState = JSON.parse(localStorage.getItem(SETTINGS_VIEW_KEY) || "{}") || {}; }
    catch (_) { settingsViewState = {}; }
  }
  return settingsViewState;
}

// A row with a closed set of values shows all of them, the ones in use
// highlighted. Read-only: the stack environment is where they change.
// A value in use that is not an option - a typo in the stack - is shown too,
// flagged, so it does not vanish from the page.
// Saving from the page: a closed set's choices become buttons when its one
// variable may be saved here.
function choiceStrip(item) {
  const c = item.choices;
  const target = editableVariables(item).length === 1 && (item.env || []).length === 1
    ? item.env[0] : null;
  const options = c.options.map((option) => {
    const on = c.selected.includes(option);
    if (target) {
      return `<li><button type="button" class="choice${on ? " is-current" : ""}"${target.restart
        ? ` title="Applies when the doctor restarts"` : ""}
        data-setting-choice="${esc(option)}" data-setting-key="${esc(target.key)}"
        data-multiple="${c.multiple ? "1" : ""}" aria-pressed="${on}">${esc(option)}</button></li>`;
    }
    return `<li class="choice${on ? " is-current" : ""}"${on ? ` aria-current="true"` : ""}>${esc(option)}</li>`;
  });
  const unknown = c.unknown.map((value) => `<li class="choice is-unknown" aria-current="true"
    title="Not one of the choices">${esc(value)}</li>`);
  const none = c.multiple && !c.selected.length && !c.unknown.length
    ? `<span class="choice-none">none chosen</span>` : "";
  return `<ul class="choices" aria-label="${esc(item.label)}: ${c.multiple ? "choices, the ones in use marked" : "choices, the one in use marked"}">${options.join("")}${unknown.join("")}</ul>${none}`;
}

function editableVariables(item) {
  return (item.env || []).filter((e) => e.editable);
}

// The values a row's variables hold, editable one by one; a saved value can be
// reset to the default from here.
function settingEditor(item) {
  const vars = editableVariables(item);
  if (!vars.length) return "";
  const resets = vars.filter((e) => e.source === "saved");
  // A choice row saves by clicking its options; only its reset lives here.
  if (item.choices && vars.length === 1 && item.env.length === 1) {
    return resets.length ? `<button type="button" class="link-btn setting-reset"
      data-setting-reset="${esc(vars[0].key)}">reset to default</button>` : "";
  }
  return `
    <button type="button" class="btn btn-small setting-edit-open" data-setting-edit aria-expanded="false">Edit</button>
    <form class="setting-editor" hidden>
      ${vars.map((e) => `
        <label class="setting-field">
          <span class="setting-field-name">${esc(e.name)}${e.restart
            ? ` <span class="setting-field-note">applies on restart</span>` : ""}</span>
          ${e.options && e.type === "str" ? `<select name="${esc(e.key)}" data-initial="${esc(e.current ?? "")}">
            ${e.options.map((o) => `<option${o === e.current ? " selected" : ""}>${esc(o)}</option>`).join("")}
          </select>` : `<input name="${esc(e.key)}" value="${esc(e.current ?? "")}" data-initial="${esc(e.current ?? "")}"
            inputmode="${e.type === "int" || e.type === "float" ? "decimal" : "text"}" autocomplete="off" spellcheck="false">`}
          ${e.source === "saved" ? `<button type="button" class="link-btn" data-setting-reset="${esc(e.key)}">reset</button>` : ""}
        </label>`).join("")}
      <div class="setting-editor-actions">
        <button type="submit" class="btn btn-small">Save</button>
        <button type="button" class="btn btn-small btn-quiet" data-setting-edit-cancel>Cancel</button>
      </div>
    </form>`;
}

function settingRow(section, item) {
  const env = item.env || [];
  const names = env.map((e) => e.name);
  const changed = item.kind === "setting" && item.changed;
  const kinds = settingKinds();
  const sources = settingSources();
  const from = item.kind === "setting" ? sources[item.source] : null;
  const marks = [
    changed ? `<span class="setting-mark is-changed" title="Differs from the default">Changed</span>` : "",
    from ? `<span class="setting-mark ${from[1]}" title="${esc(from[2])}">${from[0]}</span>` : "",
    item.kind === "fixed" ? `<span class="setting-mark is-fixed" title="${esc(kinds.fixed)}">Fixed</span>` : "",
  ].join("");
  const detail = item.kind === "setting" || item.kind === "fixed" ? `
    <span class="setting-detail">
      ${env.map((e) => `<code class="env-chip" data-source="${esc(e.source || "")}" title="${esc(item.kind === "setting"
        ? (sources[e.source] || [, , kinds.setting])[2] : kinds.fixed)}">${esc(e.name)}</code>`).join("")}
      ${item.default == null ? "" : changed
        ? `<span class="setting-default">default: ${esc(item.default)}</span>`
        : `<span class="setting-default">the default</span>`}
    </span>` : "";
  return `
    <div class="setting" data-kind="${esc(item.kind || "status")}"${changed ? " data-changed" : ""}
      data-search="${esc([section.title, item.label, item.value, item.note, ...names].join(" ").toLowerCase())}">
      <dt><span class="setting-label">${esc(item.label)}</span>${marks ? `<span class="setting-marks">${marks}</span>` : ""}${item.note
        ? `<span class="setting-desc">${esc(item.note)}</span>` : ""}${detail}</dt>
      <dd><div class="setting-control">${item.choices ? choiceStrip(item) : `<span class="setting-value">${esc(item.value)}</span>`}${settingEditor(item)}</div>${item.pending != null
        ? `<span class="setting-pending" title="Saved; takes effect when the doctor restarts">After restart: ${esc(item.pending)}</span>` : ""}</dd>
    </div>`;
}

// The last changes saved on this page, newest first: who, when, and from what
// to what. An empty value is the default - a reset, or a first change.
function renderSettingHistory(changes = []) {
  $("settings-history").hidden = !changes.length;
  $("settings-history-summary").textContent =
    `Changes made here (${changes.length}${changes.length >= 50 ? "+" : ""})`;
  const shown = (value) => value
    ? `<code>${esc(value)}</code>` : `<span class="settings-history-default">default</span>`;
  $("settings-history-list").innerHTML = changes.map((c) => `
    <li>
      <span class="settings-history-when" title="${esc(c.changed_at)}">${agoTag(c.changed_at)}</span>
      <span class="settings-history-who">${esc(c.changed_by || "someone")}</span>
      <code class="env-chip">${esc(c.env)}</code>
      ${shown(c.old_value)} <span aria-label="to">→</span> ${shown(c.new_value)}
    </li>`).join("");
}

function renderSettings(sections, composeRead = true, meta = {}) {
  if (typeof meta.manager === "string") state.settingsManager = meta.manager;
  const tab = settingsTab();
  $("settings-pending").hidden = !meta.pending;
  renderSettingHistory(meta.changes);
  const counts = {};
  const changed = sections.reduce((n, s) => n + s.items.filter((i) => i.kind === "setting" && i.changed).length, 0);
  // The key to the badges, behind an info button beside the description.
  $("settings-legend").innerHTML = !sections.length ? "" : infoTag(composeRead
    ? `<dl class="settings-key">
         <dt><span class="setting-mark is-changed">Changed</span></dt><dd>Differs from the default.</dd>
         <dt><span class="setting-mark is-env">${esc(dockerManager() || "Stack env")}</span></dt><dd>Set in ${esc(stackEnvironment())}, which wins over anything saved here.</dd>
         <dt><span class="setting-mark is-saved">Saved here</span></dt><dd>Saved on this page.</dd>
         <dt><span class="setting-mark is-fixed">Fixed</span></dt><dd>The compose file does not pass it; only an edit there changes it.</dd>
       </dl>
       <p>Unmarked rows are defaults or facts. Secrets show only as set or not set.</p>`
    : `<p>The deployment does not pass its settings through as <code>X=\${X:-}</code>, so which ones the stack
       environment can set is unknown. Secrets show only as set or not set.</p>`);
  const groups = sections.map((section) => {
    const group = SETTINGS_TABS.some(([value]) => value === section.group) ? section.group : "general";
    counts[group] = (counts[group] || 0)
      + section.items.filter((i) => i.kind === "setting" && i.changed).length;
    return `
    <section class="settings-group" data-group="${group}">
      <h3>${esc(section.title)}</h3>
      <dl>
        ${section.items.map((item) => settingRow(section, item)).join("")}
      </dl>
    </section>`;
  });
  $("settings-tabs").innerHTML = SETTINGS_TABS.map(([value, label]) => `
    <button type="button" role="tab" id="settings-tab-${value}" data-settings-tab="${value}"
      aria-controls="settings" aria-selected="${value === tab}" tabindex="${value === tab ? 0 : -1}">
      <span>${label}</span>${counts[value] ? `<span class="settings-tab-count"
        title="${counts[value]} changed from the default">${counts[value]}</span>` : ""}</button>`).join("");
  $("settings").innerHTML = appearanceGroup() + groups.join("") + (sections.length ? backupGroup(meta.editable) : "")
    + `<p class="empty settings-none" id="settings-none" hidden>No setting matches.</p>`;
  filterSettings();
}

// One tab's sections, or - while there is something in the filter, or only
// changed settings are asked for - every matching row on any tab.
function filterSettings() {
  const view = settingsView();
  const query = $("settings-search").value.trim().toLowerCase();
  const onlyChanged = Boolean(view.changed);
  const across = Boolean(query) || onlyChanged;
  const tab = settingsTab();
  $("settings").classList.toggle("show-details", Boolean(view.details));
  $("settings-changed").setAttribute("aria-pressed", String(onlyChanged));
  // The filter button says a filter is on even while its menu is closed.
  $("settings-filter-open").classList.toggle("is-active", onlyChanged || Boolean(view.details));
  $("settings-details").setAttribute("aria-pressed", String(Boolean(view.details)));
  $("settings-tabs").classList.toggle("is-searching", across);
  $("settings").setAttribute("aria-labelledby", across ? "settings-heading" : `settings-tab-${tab}`);
  let shown = 0;
  document.querySelectorAll("#settings .settings-group").forEach((group) => {
    let rows = 0;
    group.querySelectorAll(".setting").forEach((row) => {
      const match = (query ? row.dataset.search.includes(query) : true)
        && (!onlyChanged || row.hasAttribute("data-changed"));
      row.hidden = !match;
      if (match) rows += 1;
    });
    group.hidden = across ? rows === 0 : group.dataset.group !== tab;
    if (!group.hidden) shown += 1;
  });
  $("settings-none").hidden = shown > 0;
}

// -- saving settings ------------------------------------------------------

async function reloadSettings() {
  const answer = await api("/api/settings");
  renderSettings(answer.sections, answer.compose_read !== false, answer);
}

// Changes that let the doctor act on its own deserve a second look.
function confirmSetting(key, value, previous) {
  if (key === "DOCTOR_MODE" && value === "auto") {
    return confirm("Auto mode runs the auto-applied actions without asking. Save it?");
  }
  if (key === "DOCTOR_AUTO_ACTIONS") {
    const before = new Set(previous.split(",").map((v) => v.trim()).filter(Boolean));
    const added = value.split(",").map((v) => v.trim()).filter((v) => v && !before.has(v));
    if (added.length) return confirm(`In auto mode the doctor will run ${added.join(", ")} without asking. Save it?`);
  }
  return true;
}

// What a save or an import did: applied at once, or waiting for a restart.
function appliedMessage(result, what) {
  const later = result.restart || [];
  if (!later.length) return `${what}. In effect now.`;
  if (!(result.applied || []).length) return `${what}. Takes effect when the doctor restarts.`;
  return `${what}. In effect now, except ${later.join(", ")}, which waits for a restart.`;
}

async function saveSetting(key, value, previous = "") {
  if (!confirmSetting(key, value, previous)) return null;
  try {
    return await api(`/api/settings/${encodeURIComponent(key)}`, {
      method: "PUT", body: JSON.stringify({ value }),
    });
  } catch (error) {
    toast(error.message, "error");
    return null;
  }
}

async function importSettings(file) {
  let parsed;
  try {
    parsed = JSON.parse(await file.text());
  } catch (_) {
    toast("That file is not JSON.", "error");
    return;
  }
  if (!parsed || typeof parsed.settings !== "object" || Array.isArray(parsed.settings)) {
    toast("That file has no settings in it.", "error");
    return;
  }
  const body = { format: parsed.format, version: parsed.version, settings: parsed.settings };
  let preview;
  try {
    preview = await api("/api/settings/import", {
      method: "POST", body: JSON.stringify({ ...body, dry_run: true }),
    });
  } catch (error) {
    toast(`Not imported: ${error.message}`, "error");
    return;
  }
  const lines = preview.changes.map((c) =>
    `  ${c.env}: ${c.old_value || "default"} → ${c.new_value || "default"}`);
  const skipped = preview.skipped.map((s) => `  ${s.env}: ${s.reason}`);
  if (!lines.length) {
    toast(skipped.length ? `Nothing to change; ${skipped.length} skipped.` : "Nothing to change.");
    return;
  }
  const risky = preview.changes.some((c) => (c.env === "DOCTOR_MODE" && c.new_value.toLowerCase() === "auto")
    || c.env === "DOCTOR_AUTO_ACTIONS");
  const question = [`Save ${lines.length} change${lines.length === 1 ? "" : "s"}?`, ...lines,
    ...(skipped.length ? ["", "Skipped:", ...skipped] : []),
    ...(risky ? ["", "This changes what the doctor does on its own in auto mode."] : [])].join("\n");
  if (!confirm(question)) return;
  try {
    const result = await api("/api/settings/import", { method: "POST", body: JSON.stringify(body) });
    toast(appliedMessage(result, `Imported ${result.changes.length} setting${result.changes.length === 1 ? "" : "s"}`));
  } catch (error) {
    toast(`Not imported: ${error.message}`, "error");
  }
  await reloadSettings();
}

$("settings-import-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  event.target.value = "";  // the same file can be chosen again
  if (file) await importSettings(file);
});

$("settings").addEventListener("click", async (event) => {
  if (event.target.closest("[data-settings-import]")) {
    $("settings-import-file").click();
    return;
  }
  const choice = event.target.closest("[data-setting-choice]");
  if (choice) {
    const key = choice.dataset.settingKey;
    const option = choice.dataset.settingChoice;
    const strip = choice.closest(".choices");
    const current = [...strip.querySelectorAll("[data-setting-choice][aria-pressed='true']")]
      .map((b) => b.dataset.settingChoice);
    let value = option;
    if (choice.dataset.multiple) {
      value = (current.includes(option) ? current.filter((v) => v !== option) : [...current, option])
        .join(", ") || "none";
    } else if (current.includes(option)) {
      return;
    }
    const result = await saveSetting(key, value, current.join(", "));
    if (result) {
      toast(appliedMessage(result, `Saved ${key}`));
      await reloadSettings();
    }
    return;
  }
  const reset = event.target.closest("[data-setting-reset]");
  if (reset) {
    const result = await saveSetting(reset.dataset.settingReset, "");
    if (result) {
      toast(appliedMessage(result, `Reset ${reset.dataset.settingReset} to its default`));
      await reloadSettings();
    }
    return;
  }
  const open = event.target.closest("[data-setting-edit]");
  if (open) {
    const form = open.nextElementSibling;
    form.hidden = !form.hidden;
    open.setAttribute("aria-expanded", String(!form.hidden));
    if (!form.hidden) form.querySelector("input, select")?.focus();
    return;
  }
  const cancel = event.target.closest("[data-setting-edit-cancel]");
  if (cancel) {
    const form = cancel.closest("form");
    form.reset();
    form.hidden = true;
    form.previousElementSibling.setAttribute("aria-expanded", "false");
  }
});

$("settings").addEventListener("submit", async (event) => {
  const form = event.target.closest(".setting-editor");
  if (!form) return;
  event.preventDefault();
  const changed = [...form.querySelectorAll("input, select")].filter((i) => i.value.trim() !== i.dataset.initial);
  const results = [];
  for (const input of changed) {
    const result = await saveSetting(input.name, input.value.trim(), input.dataset.initial);
    if (!result) break;
    results.push(result);
  }
  if (results.length) {
    toast(appliedMessage({
      applied: results.flatMap((r) => r.applied || []),
      restart: [...new Set(results.flatMap((r) => r.restart || []))],
    }, "Saved"));
  }
  await reloadSettings();
});

$("settings-restart").addEventListener("click", async () => {
  if (!confirm("Restart the doctor now? It is back in a few seconds, with the saved settings.")) return;
  try {
    await api("/api/settings/restart", { method: "POST" });
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  // Read again once it is back: the page shows what runs.
  state.settingsLoaded = false;
  toast("Restarting. The page reconnects on its own.");
});

function chooseSettingsTab(tab, focus = false) {
  try { localStorage.setItem(SETTINGS_TAB_KEY, tab); } catch (_) { /* private mode */ }
  setSearchOpen(false);
  document.querySelectorAll("[data-settings-tab]").forEach((button) => {
    const current = button.dataset.settingsTab === tab;
    button.setAttribute("aria-selected", String(current));
    button.tabIndex = current ? 0 : -1;
    if (current && focus) button.focus();
  });
  filterSettings();
}

function renderStatus() {
  const s = state.status;
  if (!s) return;
  $("host-title").textContent = hostTitle();

  $("tile-mode").textContent = s.mode;
  $("tile-mode-note").textContent = {
    observe: "diagnoses only - nothing is ever executed",
    suggest: "actions wait for your approval below",
    auto: `auto-applies: ${(s.auto_actions || []).join(", ") || "nothing"}`,
  }[s.mode] || "";

  // The big number is what still wants attention; the note is what happened.
  // Counting everything from the last 24h made a cleaned-up run of false
  // alarms read as 240 live problems.
  $("tile-incidents").textContent = s.open_incidents ?? 0;
  $("tile-incidents-note").textContent = `${s.incidents_24h ?? 0} opened in the last 24h`;
  $("tile-pending").textContent = s.pending_approvals ?? 0;

  // On a free tier the quota is requests/day and the dollar meter reads zero
  // all day, so show whichever cap is actually the binding one.
  const budget = s.budget || {};
  const spent = Number(budget.spent_usd || 0);
  const usdCap = Number(budget.daily_usd || 0);
  const calls = Number(budget.calls || 0);
  const callCap = Number(budget.daily_call_limit || 0);
  const meterByCalls = callCap > 0 && spent === 0;

  let pct = 0;
  if (meterByCalls) {
    pct = Math.min(100, (calls / callCap) * 100);
    $("tile-spend").textContent = `${calls} / ${callCap}`;
    $("tile-spend-note").textContent = `calls today, free-tier cap ${callCap}/day`;
    $("budget-meter").setAttribute("aria-label", `${pct.toFixed(0)}% of the daily call quota used`);
  } else {
    pct = usdCap > 0 ? Math.min(100, (spent / usdCap) * 100) : 0;
    $("tile-spend").textContent = `$${spent.toFixed(3)}`;
    $("tile-spend-note").textContent = usdCap > 0
      ? `${calls} call(s), cap $${usdCap.toFixed(2)}/day`
      : `${calls} call(s), no cap set`;
    $("budget-meter").setAttribute(
      "aria-label",
      usdCap > 0 ? `${pct.toFixed(0)}% of the $${usdCap.toFixed(2)} daily budget used` : "no budget cap set",
    );
  }
  const fill = $("budget-fill");
  fill.style.width = `${pct}%`;
  fill.dataset.level = level(pct, 70, 95);

  // Collapsible, and it remembers being closed - but keyed to the notices
  // themselves, so a new one opens the box again rather than inheriting a
  // dismissal meant for something else.
  const warnings = s.warnings || [];
  const box = $("warnings");
  box.hidden = warnings.length === 0;
  if (!warnings.length) return;
  const key = "doctor-notices-" + hash(warnings.join("|"));
  let closed = false;
  try { closed = localStorage.getItem(key) === "closed"; } catch (_) { /* private mode */ }
  box.innerHTML = `<details ${closed ? "" : "open"}>
    <summary>${warnings.length} notice${warnings.length === 1 ? "" : "s"}</summary>
    ${warnings.map((w) => `<p><strong>Heads up:</strong> ${esc(w)}</p>`).join("")}
  </details>`;
  box.querySelector("details").addEventListener("toggle", (event) => {
    try {
      localStorage.setItem(key, event.target.open ? "open" : "closed");
    } catch (_) { /* private mode */ }
  });
}

// "Worth your attention": stopped, unhealthy, or still starting.
function troubled(c) {
  return c.state !== "running" || (c.health && c.health !== "none" && c.health !== "healthy") ? 1 : 0;
}

// The ring fills over one poll interval and empties when the next poll lands,
// so a stalled doctor shows as a ring that has stopped emptying - and turns
// amber once a poll is well overdue - without a line of text in the header.
const RING = 2 * Math.PI * 15;
let ringFraction = 0;

function renderPoll() {
  const { docker_connected, last_poll_at, last_poll_seconds } = state.snapshot;
  const interval = Number(state.snapshot.poll_interval_seconds)
                || Number(state.status?.poll_interval_seconds) || 30;
  // A poll that itself takes longer than the interval sets the pace instead.
  const cycle = Math.max(interval, Number(last_poll_seconds) || 0);
  renderPollNow();
  let fraction = 1;
  let name;
  let words;
  if (state.offline) {
    name = "down";
    words = state.offline === "restarting"
      ? "The doctor is restarting"
      : "Can't reach the doctor";
  } else if (state.streamDown) {
    name = "down";
    words = "Lost the connection to the doctor; reconnecting";
  } else if (!last_poll_at) {
    name = "connecting";
    words = "Waiting for the first poll";
  } else if (!docker_connected) {
    name = "down";
    words = `Docker unreachable \u00b7 last polled ${ago(last_poll_at)}`;
  } else {
    const age = Math.max(0, (Date.now() - new Date(last_poll_at).getTime()) / 1000);
    fraction = Math.min(1, age / cycle);
    name = age > cycle * 2.5 ? "late" : "live";
    const took = last_poll_seconds != null ? ` \u00b7 took ${Math.round(last_poll_seconds)}s` : "";
    const lead = state.snapshot.polling ? "Polling now" : name === "late" ? "Poll overdue" : "Connected";
    words = `${lead} \u00b7 polled ${ago(last_poll_at)}${took} \u00b7 every ${interval}s`;
  }

  const el = $("poll");
  const fill = $("poll-fill");
  el.dataset.state = name;
  el.setAttribute("aria-label", words);
  $("poll-tip").textContent = words;
  // Emptying must not animate backwards over a second: that reads as the
  // ring unwinding, not as a poll arriving.
  if (fraction < ringFraction) {
    fill.style.transition = "none";
    fill.style.strokeDashoffset = String(RING * (1 - fraction));
    void fill.getBoundingClientRect();
    fill.style.transition = "";
  } else {
    fill.style.strokeDashoffset = String(RING * (1 - fraction));
  }
  ringFraction = fraction;
}

function containerPill(c) {
  const health = c.health && c.health !== "none" ? c.health : null;
  if (c.state !== "running") return statusPill(c.state || "unknown", "critical");
  if (health === "unhealthy") return statusPill("unhealthy", "warning");
  if (health === "starting") return statusPill("starting", "serious");
  return statusPill(health === "healthy" ? "healthy" : "running", "good");
}

function containerCard(c, stale) {
  const running = c.state === "running";
  const health = c.health && c.health !== "none" ? c.health : null;
  let cls = "card";
  if (!running) cls += " is-down";
  else if (health === "unhealthy") cls += " is-unhealthy";
  if (!c.watched) cls += " is-unwatched";

  const stats = c.stats || {};
  // `cpu_pct` is `docker stats` semantics (100% = one core), so on a
  // multi-core host it can read 400%. The bar tracks the share of the whole
  // host instead, which is also what the alert threshold uses.
  const cpu = Number(stats.cpu_pct || 0);
  const cpuHost = Number(stats.cpu_host_pct ?? cpu);
  const cores = Number(stats.online_cpus || 1);
  const cpuLabel = cores > 1 ? `${cpu.toFixed(1)}% · ${cpuHost.toFixed(0)}% of host` : `${cpu.toFixed(1)}%`;
  const memPct = Number(stats.mem_pct || 0);
  const hasLimit = Number(stats.mem_limit_bytes || 0) > 0;

  const metrics = running && Object.keys(stats).length ? `
    <div class="card-metrics">
      <div>
        <div class="metric-row"><span>CPU</span><span>${cpuLabel}</span></div>
        <div class="meter"><div class="meter-fill" data-level="${level(cpuHost, thresholds().cpu ?? 75, 100)}" style="width:${Math.min(100, cpuHost)}%"></div></div>
      </div>
      <div>
        <div class="metric-row">
          <span>Memory</span>
          <span>${bytes(stats.mem_usage_bytes)}${hasLimit ? ` / ${bytes(stats.mem_limit_bytes)}` : ""}</span>
        </div>
        ${hasLimit ? `<div class="meter"><div class="meter-fill" data-level="${level(memPct, thresholds().memory ?? 75, 100)}" style="width:${Math.min(100, memPct)}%"></div></div>` : ""}
      </div>
    </div>` : "";

  const canAct = running && c.watched && !c.protected_reason;
  // The name is a link to the container's own view, stretched over the whole
  // card; the restart button sits above it so it stays its own target.
  return `
    <article class="${cls}">
      <div class="card-head">
        <div>
          <a class="card-name card-link" href="#container/${encodeURIComponent(c.name)}">${esc(c.name)}</a>
          <div class="card-image">${esc(c.image)}${
            stale.has(c.name) ? ` <span class="image-behind">${esc(stale.get(c.name))}</span>` : ""}</div>
        </div>
        ${containerPill(c)}
      </div>
      ${metrics}
      <div class="card-foot">
        <span>${c.restart_count ? `${c.restart_count} restart(s)` : "no restarts"}${c.watched ? "" : " · not watched"}</span>
        ${canAct ? `<button type="button" class="ghost-btn" data-restart="${esc(c.name)}">Restart</button>` : ""}
      </div>
    </article>`;
}

// The same container on one line: state, name, image, the two numbers and
// the restart button. For reading thirty of them at a glance; the card is one
// click away on the container's own view.
function containerRow(c, stale) {
  const running = c.state === "running";
  const health = c.health && c.health !== "none" ? c.health : null;
  let cls = "crow";
  if (!running) cls += " is-down";
  else if (health === "unhealthy") cls += " is-unhealthy";
  if (!c.watched) cls += " is-unwatched";
  const stats = running ? c.stats || {} : {};
  const cpuHost = stats.cpu_host_pct ?? stats.cpu_pct;
  const hasLimit = Number(stats.mem_limit_bytes || 0) > 0;
  const cpu = cpuHost === undefined ? "" : `${Number(cpuHost).toFixed(Number(cpuHost) < 10 ? 1 : 0)}%`;
  const mem = stats.mem_usage_bytes === undefined ? ""
    : hasLimit ? `${Number(stats.mem_pct || 0).toFixed(0)}%` : bytes(stats.mem_usage_bytes);
  const canAct = running && c.watched && !c.protected_reason;
  return `
    <div class="${cls}">
      <span class="crow-state">${containerPill(c)}</span>
      <span class="crow-name">
        <a class="card-link" href="#container/${encodeURIComponent(c.name)}">${esc(c.name)}</a>
        <span class="crow-image">${esc(c.image)}</span>
        ${stale.has(c.name) ? `<span class="image-behind">${esc(stale.get(c.name))}</span>` : ""}
      </span>
      <span class="crow-num" title="CPU, share of the host">${cpu ? `<small>CPU</small> ${cpu}` : ""}</span>
      <span class="crow-num" title="Memory${hasLimit ? ", share of its limit" : ""}">${mem ? `<small>MEM</small> ${mem}` : ""}</span>
      <span class="crow-num crow-restarts" title="Restarts">${c.restart_count ? `${c.restart_count}\u00d7 restarted` : ""}</span>
      <span class="crow-act">${canAct ? `<button type="button" class="ghost-btn" data-restart="${esc(c.name)}">Restart</button>` : ""}</span>
    </div>`;
}

function compact(list) {
  return state.density[list] === "compact";
}

// One group per Compose project, the ones with trouble first and open; the
// rest collapsed to a line each. Standalone containers go last.
function renderStacks(host, shown, stale, filtering) {
  const groups = new Map();
  for (const c of shown) {
    const key = c.stack || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }
  const ordered = [...groups.entries()].sort(([a, ca], [b, cb]) =>
    ((a === "") - (b === ""))
    || (cb.filter(troubled).length > 0) - (ca.filter(troubled).length > 0)
    || a.localeCompare(b));
  host.innerHTML = ordered.map(([name, members]) => {
    const issues = members.filter(troubled).length;
    const remembered = state.stackOpen.get(name);
    const open = filtering || (remembered ?? issues > 0);
    return `
      <details class="stack-group" data-stack="${esc(name)}" ${open ? "open" : ""}>
        <summary>
          <span class="chevron" aria-hidden="true">&#9656;</span>
          <strong>${name ? esc(name) : "Not in a stack"}</strong>
          <span class="muted">${members.length} container${members.length === 1 ? "" : "s"}</span>
          ${issues ? statusPill(`${issues} unwell`, "warning") : ""}
        </summary>
        ${compact("containers")
          ? `<div class="container-rows">${members.map((c) => containerRow(c, stale)).join("")}</div>`
          : `<div class="container-grid">${members.map((c) => containerCard(c, stale)).join("")}</div>`}
      </details>`;
  }).join("");
}

function renderContainers() {
  const { containers, last_poll_at } = state.snapshot;
  renderPoll();
  // Before the stream's first state, "no containers" would be a guess.
  if (!state.snapshotLoaded) return;

  const watched = containers.filter((c) => c.watched).length;
  $("containers-sub").innerHTML =
    `${watched} of ${containers.length} watched${last_poll_at ? ` · updated ${agoTag(last_poll_at)}` : ""}`;

  const host = $("containers");
  host.classList.toggle("container-grid", !state.grouped && !compact("containers"));
  host.classList.toggle("container-rows", !state.grouped && compact("containers"));
  host.classList.toggle("stack-list", state.grouped);
  $("containers-more").innerHTML = "";
  if (!containers.length) {
    host.innerHTML = `<p class="empty">No containers reported yet.</p>`;
    return;
  }

  const stale = staleImages();

  // Thirty containers is a lot of scrolling to reach the one that is broken,
  // so anything unhealthy sorts to the top and the rest can be filtered away.
  const needle = state.containerFilter.trim().toLowerCase();
  const shown = containers
    .filter((c) => !state.onlyIssues || troubled(c))
    .filter((c) => !needle ||
      (c.name || "").toLowerCase().includes(needle) ||
      (c.image || "").toLowerCase().includes(needle) ||
      (c.stack || "").toLowerCase().includes(needle))
    .sort((a, b) => (troubled(b) - troubled(a)) || (a.name || "").localeCompare(b.name || ""));

  if (!shown.length) {
    host.innerHTML = `<p class="empty">${
      state.onlyIssues ? "Nothing unhealthy - that is the good outcome." : "No container matches that filter."
    }</p>`;
    return;
  }

  // Grouped, the collapsed stacks are the paging: each is one line until
  // opened, so there is no second mechanism on top.
  if (state.grouped) {
    renderStacks(host, shown, stale, Boolean(needle) || state.onlyIssues);
    return;
  }

  // Anything unwell is already at the top, so the first page is the one that
  // matters; the healthy remainder waits behind "show more".
  const page = shown.slice(0, state.shown.containers);
  $("containers-more").innerHTML =
    pager("containers", page.length, shown.length, pageSize("containers"));
  host.innerHTML = page.map((c) => (compact("containers") ? containerRow : containerCard)(c, stale)).join("");
}

// --- charts -----------------------------------------------------------------
//
// One small SVG line chart, drawn at the width it is given: a 2px line with a
// 10% wash under it, hairline gridlines, and a crosshair that snaps to the
// nearest sample. A projection, where there is one, continues the line dashed
// - the one place a dash means something. One series per chart: CPU and
// memory are two charts, never one chart with two scales.

const CHART_HEIGHT = 150;
const CHART_PAD = { top: 12, right: 12, bottom: 22, left: 44 };

// Axis tops land on 1, 2, 2.5 or 5 times a power of ten, so the ticks read
// 0 / 10 / 20% rather than 0 / 5.8 / 12%.
function niceCeil(value) {
  if (!(value > 0)) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((m) => m * power >= value);
  return step * power;
}

// The same for bytes, in whichever binary unit the value is read in.
function niceBytes(value) {
  let unit = 1;
  while (value / unit >= 1024) unit *= 1024;
  return niceCeil(value / unit) * unit;
}

function shortTime(ms, spanMs) {
  const d = new Date(ms);
  if (spanMs <= 36 * 3600 * 1000) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function longTime(ms) {
  return new Date(ms).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

// spec: { points: [[ms, value, ...extra]], projection?: [[ms, value]], yMin, yMax,
//         format(value, long, point), label, refs?: [{ value, label, kind }] }
function drawChart(host, spec) {
  host._spec = spec;
  const width = Math.max(240, host.clientWidth || 0);
  const { top, right, bottom, left } = CHART_PAD;
  const plotW = width - left - right;
  const plotH = CHART_HEIGHT - top - bottom;
  const all = [...spec.points, ...(spec.projection || [])];
  if (spec.points.length < 2) {
    host.innerHTML = `<p class="chart-empty">${esc(spec.empty || "Not enough samples yet.")}</p>`;
    return;
  }
  const x0 = all[0][0];
  const x1 = all[all.length - 1][0];
  const span = Math.max(1, x1 - x0);
  const yMin = spec.yMin ?? 0;
  const yMax = spec.yMax > yMin ? spec.yMax : yMin + 1;
  const sx = (t) => left + ((t - x0) / span) * plotW;
  const sy = (v) => top + (1 - (Math.min(yMax, Math.max(yMin, v)) - yMin) / (yMax - yMin)) * plotH;
  const path = (pts) => pts.map(([t, v], i) => `${i ? "L" : "M"}${sx(t).toFixed(1)},${sy(v).toFixed(1)}`).join("");

  const ticks = [yMin, (yMin + yMax) / 2, yMax];
  const grid = ticks.map((v) => `
    <line class="chart-grid" x1="${left}" x2="${width - right}" y1="${sy(v)}" y2="${sy(v)}"></line>
    <text class="chart-axis" x="${left - 6}" y="${sy(v) + 4}" text-anchor="end">${esc(spec.format(v))}</text>`).join("");
  const refs = (spec.refs || []).filter((r) => r.value > yMin && r.value <= yMax).map((r) => `
    <line class="chart-ref" data-kind="${r.kind || "neutral"}" x1="${left}" x2="${width - right}"
      y1="${sy(r.value)}" y2="${sy(r.value)}"></line>
    <text class="chart-ref-label" x="${width - right}" y="${r.kind === "warning" ? sy(r.value) + 13 : sy(r.value) - 4}"
      text-anchor="end">${esc(r.label)}</text>`).join("");
  const line = path(spec.points);
  const last = spec.points[spec.points.length - 1];
  const area = `${line}L${sx(last[0]).toFixed(1)},${sy(yMin)}L${sx(spec.points[0][0]).toFixed(1)},${sy(yMin)}Z`;
  const projection = spec.projection?.length
    ? `<path class="chart-projection" d="${path([last, ...spec.projection])}"></path>` : "";
  const mid = x0 + span / 2;

  host.innerHTML = `
    <svg class="chart-svg" width="${width}" height="${CHART_HEIGHT}" role="img"
         aria-label="${esc(spec.label)}">
      ${grid}${refs}
      <path class="chart-area" d="${area}"></path>
      <path class="chart-line" d="${line}"></path>
      ${projection}
      <circle class="chart-end" cx="${sx(last[0])}" cy="${sy(last[1])}" r="4"></circle>
      <text class="chart-axis" x="${left}" y="${CHART_HEIGHT - 6}">${esc(shortTime(x0, span))}</text>
      <text class="chart-axis" x="${sx(mid)}" y="${CHART_HEIGHT - 6}" text-anchor="middle">${esc(shortTime(mid, span))}</text>
      <text class="chart-axis" x="${width - right}" y="${CHART_HEIGHT - 6}" text-anchor="end">${esc(shortTime(x1, span))}</text>
      <line class="chart-cross" x1="0" x2="0" y1="${top}" y2="${top + plotH}" hidden></line>
      <circle class="chart-dot" r="4" hidden></circle>
    </svg>
    <div class="chart-tip" hidden></div>`;
  host._geometry = { sx, sy, x0, span, left, plotW, all: spec.points.concat(spec.projection || []) };
}

// Crosshair: the pointer picks the time, the chart finds the sample nearest it.
function chartHover(event) {
  const host = event.target.closest(".chart");
  if (!host || !host._geometry) return;
  const { sx, sy, x0, span, left, plotW, all } = host._geometry;
  const box = host.getBoundingClientRect();
  const x = event.clientX - box.left;
  const t = x0 + ((x - left) / plotW) * span;
  let nearest = all[0];
  for (const point of all) {
    if (Math.abs(point[0] - t) < Math.abs(nearest[0] - t)) nearest = point;
  }
  const projected = host._spec.projection?.includes(nearest);
  const cross = host.querySelector(".chart-cross");
  const dot = host.querySelector(".chart-dot");
  const tip = host.querySelector(".chart-tip");
  const px = sx(nearest[0]);
  cross.setAttribute("x1", px);
  cross.setAttribute("x2", px);
  dot.setAttribute("cx", px);
  dot.setAttribute("cy", sy(nearest[1]));
  cross.hidden = false;
  dot.hidden = false;
  tip.hidden = false;
  tip.innerHTML = `<strong>${esc(host._spec.format(nearest[1], true, nearest))}</strong>
    <span>${esc(longTime(nearest[0]))}${projected ? " · projected" : ""}</span>`;
  const left0 = Math.max(0, Math.min(px - tip.offsetWidth / 2, host.clientWidth - tip.offsetWidth));
  tip.style.left = `${left0}px`;
}

function chartLeave(event) {
  const host = event.target.closest(".chart");
  if (!host) return;
  host.querySelectorAll(".chart-cross,.chart-dot,.chart-tip").forEach((el) => { el.hidden = true; });
}

function redrawCharts() {
  document.querySelectorAll(".chart").forEach((host) => {
    if (host.offsetParent === null) return;
    if (host._spec) drawChart(host, host._spec);
    else if (host._days) drawSpendChart(host, host._days);
  });
}

function pct(value) {
  return `${Number(value).toFixed(Number(value) < 10 && Number(value) > 0 ? 1 : 0)}%`;
}

// The percentage says how full; this says whether it is going anywhere. A
// disk can sit at 92% for a year or be gone by Friday and read the same.
function diskTrend(d) {
  const t = d.trend;
  if (!t) return "";
  if (t.days_until_full === null || t.days_until_full === undefined) {
    return ` \u00b7 <span class="muted">not filling</span>`;
  }
  const soon = t.days_until_full <= 21;
  return ` \u00b7 <span class="${soon ? "trend-soon" : "muted"}">${esc(t.rate)}, `
       + `full in ${esc(t.until_full)}</span>`;
}

const DAY_MS = 86400 * 1000;

// Where the fitted rate leads from the latest reading: as far ahead as the
// history reaches back, or to the day the disk is full if that comes first.
function diskProjection(disk, points) {
  const t = disk.trend;
  if (!t || t.days_until_full === null || t.days_until_full === undefined || points.length < 2) {
    return null;
  }
  const [lastT, lastV, lastCap] = points[points.length - 1];
  const horizon = Math.max(7 * DAY_MS, lastT - points[0][0]);
  const fullAt = lastT + t.days_until_full * DAY_MS;
  const end = Math.min(fullAt, lastT + horizon);
  const value = Math.min(disk.used_bytes + disk.free_bytes || disk.total_bytes,
                         lastV + (t.bytes_per_day * (end - lastT)) / DAY_MS);
  return [[end, value, lastCap]];
}

// Full is used + free, as the percentage and the forecast count it: the
// filesystem's own total also holds space reserved for root, which no
// container will ever write into.
function diskCapacity(disk) {
  return disk.used_bytes + disk.free_bytes || disk.total_bytes;
}

// A sample's share of the capacity the volume had then, not now: grown since,
// it would otherwise read as fuller back then than it was.
function diskShare(used, capacity) {
  return `${((used / capacity) * 100).toFixed(1)}%`;
}

function diskChartSpec(disk) {
  const total = diskCapacity(disk);
  const points = disk.samples.map(([at, used, capacity]) => [new Date(at).getTime(), used, capacity || total]);
  const lowest = Math.min(...points.map((p) => p[1]), disk.used_bytes);
  // Capacity is the top edge, so the dashed line visibly meets it; the floor
  // sits a little under the lowest reading rather than at zero, where a
  // year's growth on a 20 TB volume would be a flat line.
  const floor = Math.max(0, lowest - (total - lowest) * 0.35);
  const threshold = thresholds().disk;
  const until = disk.trend?.until_full;
  return {
    points,
    projection: diskProjection(disk, points),
    yMin: floor,
    yMax: total,
    format: (v, long, point) => {
      if (!long) return bytes(v);
      const capacity = point?.[2] || total;
      return `${bytes(v)} of ${bytes(capacity)} used (${diskShare(v, capacity)})`;
    },
    label: `${disk.path}: ${bytes(disk.used_bytes)} of ${bytes(total)} used`
         + (until ? `, full in ${until} at the current rate` : ", not filling"),
    refs: [
      { value: total, label: "capacity", kind: "neutral" },
      ...(threshold ? [{ value: (total * threshold) / 100, label: `alert at ${threshold}%`, kind: "warning" }] : []),
    ],
    empty: "The forecast needs a few hours of samples first.",
  };
}

// The chart's numbers as a table: one row per day, the last reading of each.
function diskTable(disk) {
  const byDay = new Map();
  const total = diskCapacity(disk);
  for (const [at, used, capacity] of disk.samples) byDay.set(at.slice(0, 10), [used, capacity || total]);
  const rows = [...byDay.entries()].slice(-14).reverse();
  if (!rows.length) return "";
  return `<details class="chart-table"><summary>Show as a table</summary>
    <table><thead><tr><th>Day</th><th>Used</th><th>Capacity</th><th>Share</th></tr></thead><tbody>
    ${rows.map(([day, [used, capacity]]) => `<tr><td>${esc(day)}</td><td>${bytes(used)}</td>
      <td>${bytes(capacity)}</td><td>${diskShare(used, capacity)}</td></tr>`).join("")}
    </tbody></table></details>`;
}

const ARRAY_ROLES = { md0: "DSM system", md1: "swap" };

function arrayState(a) {
  if (!a.healthy) {
    return a.operation === "recovery" || a.operation === "reshape"
      ? ["rebuilding", "warning"] : ["degraded", "critical"];
  }
  if (a.operation === "check" || a.operation === "repair") return ["scrubbing", "neutral"];
  if (a.operation === "resync") return ["resyncing", "neutral"];
  // Only a drive whose errors are recent enough to warn about: md's count
  // stays up until a reboot, long after the errors stopped.
  const warned = new Set(((state.snapshot.raid || {}).drives || [])
    .filter((d) => d.summary).map((d) => d.disk));
  if ((a.drives || []).some((d) => d.errors > 0 && warned.has(d.disk))) return ["read errors", "warning"];
  if ((a.warnings || []).length) return ["mismatches", "warning"];
  return ["healthy", "good"];
}

// "Drive 1", "Drive 2", "Drive 3", "Drive 5" -> "Drives 1–3, 5".
function driveList(drives) {
  const bays = drives.map((d) => /^Drive (\d+)$/.exec(d.label));
  if (!bays.length || bays.some((m) => !m)) return drives.map((d) => d.label).join(", ");
  const nums = bays.map((m) => Number(m[1])).sort((x, y) => x - y);
  const runs = [];
  for (const n of nums) {
    const last = runs[runs.length - 1];
    if (last && n === last[1] + 1) last[1] = n; else runs.push([n, n]);
  }
  const text = runs.map(([a, b]) => (a === b ? `${a}` : b === a + 1 ? `${a}, ${b}` : `${a}–${b}`)).join(", ");
  return `Drive${nums.length === 1 ? "" : "s"} ${text}`;
}

function arrayRow(a, { pool = null } = {}) {
  const [label, levelName] = arrayState(a);
  const cached = cacheFor(a);
  const role = ARRAY_ROLES[a.name] && a.system ? ` · ${ARRAY_ROLES[a.name]}`
    : cached ? ` · SSD cache${cached.length ? ` for ${cached.join(", ")}` : ""}` : "";
  const disks = a.system ? `${a.active} disk${a.active === 1 ? "" : "s"}` : `${a.active} of ${a.slots} disks`;
  const size = a.size_bytes && !a.system ? ` · ${bytes(a.size_bytes)}` : "";
  const layer = pool && pool.arrays.length > 1 && !cached
    ? ` · layer ${pool.arrays.indexOf(a.name) + 1} of ${pool.arrays.length}` : "";
  // Striped and moving like the layer on the drive tiles, in that layer's colour.
  const busy = BUSY_OPERATIONS.has(a.operation) ? " is-busy" : "";
  const tint = pool ? ` data-layer="${layerAttr(a)}"` : "";
  const progress = a.progress !== null && a.progress !== undefined ? `
    <div class="meter" role="img" aria-label="${esc(a.operation)} ${a.progress}% done">
      <div class="meter-fill${busy}"${tint} style="width:${Math.min(100, a.progress)}%"></div></div>
    <span class="muted">${esc(a.operation)} ${a.progress.toFixed(1)}%${
      a.finish_minutes ? ` · about ${duration(Math.ceil(a.finish_minutes) * 60)} left` : ""}</span>` : "";
  const details = [];
  if (!a.system && (a.drives || []).length) {
    const sizes = [...new Set(a.drives.map((d) => d.size_bytes).filter(Boolean))];
    details.push(`${driveList(a.drives)}${sizes.length === 1 ? ` · ${bytes(sizes[0])} each` : ""}`);
  }
  if (!a.system && a.last_sync === "check" && a.mismatches === 0 && !a.operation) {
    details.push("last scrub: no mismatches");
  }
  const notes = [
    ...(a.healthy ? [] : [a.summary]),
    ...(a.warnings || []).map((w) => w.summary),
  ];
  return `
    <div class="array-row">
      <div class="label">
        <code>${esc(a.name)}</code>
        <span>${esc(a.level || "md")} · ${disks}${size}${layer}${role}${a.failed.length ? ` · failed: ${esc(a.failed.join(", "))}` : ""}</span>
        ${statusPill(label, levelName)}
      </div>
      ${details.length ? `<p class="array-detail muted">${details.map(esc).join(" · ")}</p>` : ""}
      ${notes.map((n) => `<p class="array-note">${esc(n)}</p>`).join("")}
      ${progress}
      ${a.system || pool ? "" : driveBays([a])}
    </div>`;
}

const FILESYSTEMS = { btrfs: "Btrfs", ext4: "ext4", ext3: "ext3", xfs: "XFS", f2fs: "F2FS" };

// How the pool is built, said once above its drives: SHR's layers need it,
// a single RAID or an SSD cache is plain from the rows below.
function poolHow(pool) {
  const n = pool.arrays.length;
  if (pool.layout === "shr") {
    return `Built from ${n} arrays: SHR splits drives of different sizes into layers, makes each
      layer its own RAID, and joins them into one pool. Each layer survives a failed drive on its own.`;
  }
  if (pool.layout === "lvm") {
    return `Built from ${n} arrays, joined end to end by LVM. Each array keeps its own redundancy.`;
  }
  return "";
}

function volumeBits(v) {
  return [
    ...(v.paths || []).map((p) => `<code>${esc(p)}</code>`),
    ...(v.filesystem ? [esc(FILESYSTEMS[v.filesystem] || v.filesystem)] : []),
    ...(v.size_bytes ? [bytes(v.size_bytes)] : []),
    ...((v.cache || []).length ? [`SSD cache <code>${esc(v.cache.join(", "))}</code>`] : []),
  ];
}

function poolHead(pool) {
  const volumes = pool.volumes || [];
  const how = poolHow(pool);
  const head = (name, bits) => `<div class="pool-head"><strong>${esc(name)}</strong>${
    bits.length ? `<span>${bits.join(" · ")}</span>` : ""}</div>`;
  const tail = how ? `<p class="array-detail muted">${how}</p>` : "";
  // A pool without a name of its own (not a Synology) is named by its volume.
  if (!pool.pool && volumes.length <= 1) {
    const [v] = volumes;
    return head(v ? v.name : pool.arrays.join(" + "),
      v ? volumeBits(v) : pool.size_bytes ? [bytes(pool.size_bytes)] : []) + tail;
  }
  const allocated = volumes.reduce((sum, v) => sum + (v.size_bytes || 0), 0);
  // A pool can hold less in volumes than it has: a volume made smaller than its pool.
  const spare = pool.size_bytes && allocated ? pool.size_bytes - allocated : 0;
  const figures = [
    ...(pool.size_bytes ? [bytes(pool.size_bytes)] : []),
    ...(spare > pool.size_bytes * 0.02 ? [`${bytes(spare)} not in a volume`] : []),
  ];
  const rows = volumes.map((v) => {
    const bits = volumeBits(v);
    return `<p class="pool-volume"><strong>${esc(v.name)}</strong>${
      bits.length ? `<span>${bits.join(" · ")}</span>` : ""}</p>`;
  }).join("");
  return head(pool.pool || pool.arrays.join(" + "), figures) + rows + tail;
}

// --- drive bays ------------------------------------------------------------------
//
// The drives behind a pool, drawn as the bays of the host: one tile per drive,
// as tall as the drive is big next to its neighbours, filled by the layers
// that use it. On SHR that is what shows where mixed sizes go: a bigger drive
// carries a layer the smaller ones cannot. A tile opens the drive's own page.

// What one member gives its array: the array's size over its data disks.
function memberShare(a) {
  const n = a.slots || (a.drives || []).length;
  const data = { raid0: n, linear: n, raid1: 1, raid4: n - 1, raid5: n - 1, raid6: n - 2, raid10: n / 2 }[
    String(a.level || "").toLowerCase()];
  return a.size_bytes && data > 0 ? a.size_bytes / data : 0;
}

// The arrays a drive is in, keyed by the drive: one disk sits in several on SHR.
function driveEntries(arrays) {
  const out = new Map();
  for (const a of arrays) {
    const share = memberShare(a);
    for (const d of a.drives || []) {
      const key = d.disk || d.member;
      if (!out.has(key)) {
        out.set(key, { key, disk: d.disk, label: d.label, model: d.model, size_bytes: d.size_bytes, members: [] });
      }
      const entry = out.get(key);
      entry.size_bytes = entry.size_bytes || d.size_bytes;
      entry.model = entry.model || d.model;
      entry.members.push({ array: a, member: d.member, bytes: share, errors: d.errors, failed: d.failed, spare: d.spare });
    }
  }
  return [...out.values()].sort((x, y) => x.key.localeCompare(y.key, undefined, { numeric: true }));
}

function smartFor(entry) {
  const drives = state.snapshot.smart?.drives || [];
  return drives.find((s) => entry.disk && s.device === entry.disk) || drives.find((s) => s.label === entry.label);
}

function errorWarning(entry) {
  return ((state.snapshot.raid || {}).drives || []).find((d) => d.disk === entry.key && d.summary);
}

function driveState(entry) {
  const smart = smartFor(entry);
  const warned = errorWarning(entry);
  if (entry.members.some((m) => m.failed)) return ["failed", "critical"];
  if (smart?.severity === "critical") return [smart.passed === false ? "failing" : "critical", "critical"];
  if (warned) return ["read errors", warned.severity === "critical" ? "critical" : "warning"];
  if (smart?.severity === "warning") return ["warning", "warning"];
  if (entry.members.length && entry.members.every((m) => m.spare)) return ["spare", "neutral"];
  if (smart?.asleep) return ["asleep", "neutral"];
  return ["healthy", "good"];
}

function bayName(entry) {
  const bay = /^Drive (\d+)$/.exec(entry.label || "");
  return bay ? bay[1] : entry.label || entry.key;
}

// "5.5 TiB" -> "5.5T", for the densest grid.
function shortBytes(value) {
  return bytes(value).replace(/ (\w)i?B$/, "$1");
}

// Which colour a layer gets: its place in its pool, the same on every tile.
function layerIndex(a) {
  const pool = ((state.snapshot.raid || {}).pools || []).find((p) => p.arrays.includes(a.name));
  return pool ? Math.max(0, pool.arrays.indexOf(a.name)) : 0;
}

// The volumes an SSD cache array serves, or null when it is no cache.
function cacheFor(a) {
  const pools = (state.snapshot.raid || {}).pools || [];
  if (!pools.some((p) => (p.caches || []).includes(a.name))) return null;
  return pools.flatMap((p) => (p.volumes || []).filter((v) => (v.cache || []).includes(a.name)).map((v) => v.name));
}

// A cache has a colour of its own: it holds none of the pool's data.
function layerAttr(a) {
  return cacheFor(a) ? "cache" : String(layerIndex(a) % 4);
}

const BUSY_OPERATIONS = new Set(["recovery", "reshape", "resync", "check", "repair"]);

// The layers stacked on one drive, bottom up, and what is left unused.
function driveSegments(entry) {
  const size = entry.size_bytes || entry.members.reduce((sum, m) => sum + m.bytes, 0);
  let left = size;
  const parts = entry.members.filter((m) => !m.array.system).map((m) => {
    const used = Math.min(left, m.bytes || 0);
    left -= used;
    return { m, used, pct: size ? (used / size) * 100 : 0 };
  });
  return { size, parts, free: size ? Math.max(0, (left / size) * 100) : 0 };
}

function bayTile(entry, biggest, density) {
  const [label, levelName] = driveState(entry);
  const { size, parts, free } = driveSegments(entry);
  const smart = smartFor(entry);
  const temp = smart?.temperature !== null && smart?.temperature !== undefined ? `${Math.round(smart.temperature)}°` : "";
  const height = biggest ? Math.max(30, (size / biggest) * 100) : 100;
  const segments = parts.map(({ m, pct }) => `<span class="bay-seg${
    BUSY_OPERATIONS.has(m.array.operation) ? " is-busy" : ""}${m.failed ? " is-failed" : ""}" data-layer="${
    layerAttr(m.array)}" style="flex-basis:${pct.toFixed(2)}%"></span>`).join("");
  const sizeText = size ? (density === "dense" ? shortBytes(size) : bytes(size)) : "—";
  const aria = [entry.label, size ? bytes(size) : "", label, temp ? `${temp}C` : ""].filter(Boolean).join(", ");
  return `<button type="button" class="bay" data-level="${levelName}" data-drive="${esc(entry.key)}"
      aria-label="${esc(aria)}" title="${esc(`${entry.label}${entry.model ? ` · ${entry.model}` : ""} · ${label}`)}">
    <span class="bay-head" aria-hidden="true"><span class="bay-num">${esc(bayName(entry))}</span>${
      levelName === "good" ? "" : `<span class="bay-flag"></span>`}</span>
    <span class="bay-sled" aria-hidden="true"><span class="bay-disk" style="height:${height.toFixed(1)}%">${segments}${
      free > 2 ? `<span class="bay-seg bay-free" style="flex-basis:${free.toFixed(2)}%"></span>` : ""}</span></span>
    <span class="bay-size" aria-hidden="true">${esc(sizeText)}</span>
    ${temp && density !== "dense" ? `<span class="bay-temp" aria-hidden="true">${esc(temp)}</span>` : ""}
  </button>`;
}

function bayLegend(arrays, anyFree) {
  const data = arrays.filter((a) => !a.system);
  if (!data.length) return "";
  // "Layer 1 md2", bottom layer first, as the tiles stack them; a layer that
  // is busy says so, striped like its segments.
  const layers = data.filter((a) => !cacheFor(a)).length;
  const items = [...data].sort((x, y) => !!cacheFor(x) - !!cacheFor(y) || layerIndex(x) - layerIndex(y)).map((a) => {
    const busy = BUSY_OPERATIONS.has(a.operation);
    const what = cacheFor(a) ? "SSD cache" : layers > 1 ? `Layer ${layerIndex(a) + 1}` : "";
    return `<span><i class="bay-swatch${busy ? " is-busy" : ""}" data-layer="${layerAttr(a)}"></i>${
      what ? `<span class="bay-layer">${what}</span>` : ""}<code>${esc(a.name)}</code>${
      busy ? `<span class="bay-op">${esc(a.operation)}</span>` : ""}</span>`;
  });
  if (anyFree) items.push(`<span><i class="bay-swatch bay-free"></i>unused</span>`);
  return `<p class="bay-legend muted">${items.join("")}<span class="bay-hint">Select a drive for its details</span></p>`;
}

function driveBays(arrays) {
  const entries = driveEntries(arrays.filter((a) => !a.system));
  if (!entries.length) return "";
  const biggest = Math.max(...entries.map((e) => e.size_bytes || 0));
  const density = entries.length <= 4 ? "roomy" : entries.length > 12 ? "dense" : "normal";
  const anyFree = entries.some((e) => driveSegments(e).free > 2);
  return `<div class="bays-wrap"><div class="bays" data-density="${density}" role="list">${entries.map((e) =>
    `<div role="listitem">${bayTile(e, biggest, density)}</div>`).join("")}</div>${bayLegend(arrays, anyFree)}</div>`;
}

function renderArrays() {
  const report = state.snapshot.raid || {};
  const arrays = report.arrays || [];
  const host = $("arrays");
  if (!arrays.length) {
    // Said once and quietly: a Basic volume has no arrays, which is not news.
    host.innerHTML = report.reason && report.reason !== "off"
      ? `<p class="muted array-none">No disk arrays to watch: ${esc(report.reason)}.</p>` : "";
    return;
  }
  const byName = new Map(arrays.map((a) => [a.name, a]));
  const pools = (report.pools || []).filter((p) => p.arrays.some((n) => byName.has(n)));
  // A pool's own arrays, then the SSD caches in front of its volumes.
  const members = (pool) => [...pool.arrays, ...(pool.caches || [])].filter((n) => byName.has(n));
  const pooled = new Set(pools.flatMap(members));
  // Read errors are a drive's, said once however many layers it is in.
  const warned = (report.drives || []).filter((d) => d.summary);
  const inPool = (pool) => warned.filter((d) => members(pool).some((n) =>
    (byName.get(n)?.drives || []).some((m) => m.disk === d.disk)));
  const shown = new Set(pools.flatMap((p) => inPool(p).map((d) => d.disk)));
  const driveNotes = (list) => list.map((d) =>
    `<p class="array-note">${d.severity === "critical" ? "▲ " : ""}${esc(d.summary)}</p>`).join("");
  // Data arrays first; the system and swap arrays are DSM's, and rarely news.
  const rest = arrays.filter((a) => !pooled.has(a.name))
    .sort((a, b) => (a.system - b.system) || a.name.localeCompare(b.name));
  host.innerHTML = `<h3 class="sub-head">Disk arrays</h3>` +
    driveNotes(warned.filter((d) => !shown.has(d.disk))) +
    pools.map((pool) => `<div class="pool">${poolHead(pool)}${driveNotes(inPool(pool))}${
      driveBays(members(pool).map((n) => byName.get(n)))}${members(pool)
      .map((n) => arrayRow(byName.get(n), { pool })).join("")}</div>`).join("") +
    rest.map((a) => arrayRow(a)).join("");
  if ($("drive").open) renderDrive();
}

function renderDisk() {
  if (!state.snapshotLoaded) return;
  const disks = state.snapshot.disk || [];
  const report = state.snapshot.raid || {};
  const hasArrays = (report.arrays || []).length > 0 || (report.reason && report.reason !== "off");
  if (!disks.length && !hasArrays) {
    $("disks").dataset.key = "";
    $("disks").innerHTML = `<p class="empty">No storage is watched. Set <code>DOCTOR_DISK_PATHS</code> to see free space and the fill forecast here.</p>`;
    $("arrays").innerHTML = "";
    return;
  }
  const history = new Map((state.diskHistory || []).map((d) => [d.path, d]));
  // The rows are rebuilt only when the set of paths changes, so a chart being
  // read is not redrawn out from under the pointer on every poll.
  const host = $("disks");
  const key = disks.map((d) => d.path).join("|");
  if (host.dataset.key !== key) {
    host.dataset.key = key;
    host.innerHTML = disks.map((d, i) => `
      <div class="disk-row" data-disk="${i}">
        <div class="label"><code>${esc(d.path)}</code><span class="disk-figures"></span></div>
        <div class="meter"><div class="meter-fill"></div></div>
        <div class="chart" data-chart="disk-${i}"></div>
        <div class="disk-table"></div>
      </div>`).join("");
  }
  disks.forEach((d, i) => {
    const row = host.querySelector(`[data-disk="${i}"]`);
    row.querySelector(".disk-figures").innerHTML =
      `${d.used_pct.toFixed(0)}% used · ${bytes(d.free_bytes)} free${diskTrend(d)}`;
    const fill = row.querySelector(".meter-fill");
    fill.dataset.level = level(d.used_pct, thresholds().disk ?? 80, thresholds().disk_critical ?? 90);
    fill.style.width = `${Math.min(100, d.used_pct)}%`;
    const past = history.get(d.path);
    const chart = row.querySelector(".chart");
    if (past) {
      // The live reading is newer than the hourly bucket; fold it in.
      const merged = { ...past, ...d, samples: [...past.samples] };
      if (state.snapshot.last_poll_at) merged.samples.push([state.snapshot.last_poll_at, d.used_bytes, diskCapacity(d)]);
      drawChart(chart, diskChartSpec(merged));
      // Only when the history itself changed, or an open table snaps shut.
      if (row._past !== past) {
        row._past = past;
        row.querySelector(".disk-table").innerHTML = diskTable(past);
      }
    } else {
      chart.innerHTML = "";
    }
  });
  renderArrays();
}

// --- one drive -----------------------------------------------------------------

function openDrive(key) {
  state.driveKey = key;
  state.driveTemps = null;
  state.driveSleep = null;
  state.driveIo = null;
  state.driveCounters = null;
  renderDrive();
  refreshDriveTemps();
  refreshDriveSleep();
  refreshDriveIo();
  refreshDriveCounters();
  const dialog = $("drive");
  if (!dialog.open) dialog.showModal();
}

function renderDrive() {
  const body = $("drive-body");
  const key = state.driveKey;
  if (!key) return;
  const report = state.snapshot.raid || {};
  const entry = driveEntries(report.arrays || []).find((e) => e.key === key);
  if (!entry) {
    body.innerHTML = `<header class="detail-head"><div class="detail-title"><h2 id="drive-title">${esc(key)}</h2></div>
      <button type="button" class="detail-close" data-close-drive aria-label="Close">&times;</button></header>
      <p class="muted">This drive is no longer in any array the doctor reads.</p>`;
    return;
  }
  const [label, levelName] = driveState(entry);
  const smart = smartFor(entry);
  const warned = errorWarning(entry);
  const { size, parts, free } = driveSegments(entry);
  const value = (v) => (v === null || v === undefined || v === "" ? null : v);
  const count = (v) => (v === null || v === undefined ? null : Number(v).toLocaleString());
  const facts = [
    ["Size", size ? bytes(size) : null],
    ["Model", value(smart?.model) || value(entry.model)],
    ["Family", value(smart?.family)],
    ["Serial", value(smart?.serial)],
    ["Firmware", value(smart?.firmware)],
    ["Interface", value(smart?.protocol)],
    ["Device", value(entry.disk)],
    ["SMART self-assessment", smart && smart.passed !== null && smart.passed !== undefined ? (smart.passed ? "passed" : "failed") : null],
    ["Temperature", smart?.temperature !== null && smart?.temperature !== undefined ? `${Math.round(smart.temperature)} °C` : null],
    ["Powered on", smart?.power_on_hours !== null && smart?.power_on_hours !== undefined
      ? `${powerOn(smart.power_on_hours)} (${Number(smart.power_on_hours).toLocaleString()} h)` : null],
    ["Power cycles", count(smart?.power_cycles)],
    ...(smart ? driveLifetime(smart) : []),
  ].filter(([, v]) => v !== null && v !== undefined);
  const bar = parts.length ? `<div class="drive-bar" role="img" aria-label="${esc(parts.map(({ m, used }) =>
      `${m.array.name} uses about ${bytes(used)}`).join(", "))}${free > 2 ? `, ${free.toFixed(0)}% unused` : ""}">${
    parts.map(({ m, pct }) => `<span class="bay-seg${BUSY_OPERATIONS.has(m.array.operation) ? " is-busy" : ""}" data-layer="${
      layerAttr(m.array)}" style="flex-basis:${pct.toFixed(2)}%"></span>`).join("")}${
    free > 2 ? `<span class="bay-seg bay-free" style="flex-basis:${free.toFixed(2)}%"></span>` : ""}</div>` : "";
  const arrays = entry.members.map((m) => {
    const a = m.array;
    const [aLabel, aLevel] = arrayState(a);
    const cached = cacheFor(a);
    const role = a.system ? ARRAY_ROLES[a.name] || "system"
      : cached ? `SSD cache${cached.length ? ` for ${cached.join(", ")}` : ""}` : "";
    const bits = [
      a.level || "md",
      role,
      !a.system && m.bytes ? `about ${bytes(Math.min(m.bytes, size || m.bytes))} of this drive` : "",
      m.errors === null || m.errors === undefined ? "" : `${m.errors} read error${m.errors === 1 ? "" : "s"} corrected`,
      m.failed ? "failed out" : m.spare ? "spare" : "",
    ].filter(Boolean);
    return `<li>${a.system ? "" : `<i class="bay-swatch" data-layer="${layerAttr(a)}"></i>`}<code>${esc(a.name)}</code>
      <span class="muted">${esc(bits.join(" · "))}</span>${statusPill(aLabel, aLevel)}</li>`;
  }).join("");
  const anyErrors = entry.members.some((m) => m.errors !== null && m.errors !== undefined);
  body.innerHTML = `
    <header class="detail-head">
      <div class="detail-title">
        <h2 id="drive-title">${esc(entry.label)}</h2>
        <p class="muted">${esc([entry.disk, smart?.model || entry.model].filter(Boolean).join(" · "))}</p>
      </div>
      ${statusPill(label, levelName)}
      <button type="button" class="detail-close" data-close-drive aria-label="Close">&times;</button>
    </header>
    ${warned ? `<p class="array-note">${esc(warned.summary)}</p>` : ""}
    ${(smart?.problems || []).map((p) => `<p class="array-note">${esc(p)}</p>`).join("")}
    ${bar}
    <dl class="drive-facts">${facts.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
    ${smart ? "" : `<p class="muted drive-nosmart">No SMART data for this drive: temperature, hours and bad sectors
      come from the <code>doctor-smart</code> sidecar.</p>`}
    ${smart ? `<section class="detail-section"><div class="drive-temp-head"><h3>Temperature</h3>
      <div class="segmented" role="radiogroup" aria-label="Temperature range">${[[24, "24 hours"], [168, "7 days"]].map(([h, words]) =>
        `<button type="button" role="radio" data-drive-hours="${h}" aria-checked="${state.driveTempHours === h}">${words}</button>`).join("")}
      </div></div><div class="chart" data-chart="drive-temp"></div></section>` : ""}
    ${smart && canSleep(smart) ? `<section class="detail-section"><h3>Sleep</h3>
      <p class="muted" data-sleep-summary></p><div data-chart="drive-sleep"></div></section>` : ""}
    <section class="detail-section"><h3>Reads and writes</h3>
      <p class="muted" data-io-summary></p><div class="chart" data-chart="drive-io"></div></section>
    ${smart ? `<section class="detail-section"><h3>Counters over time</h3>
      <div data-drive-counters></div></section>` : ""}
    <section class="detail-section"><h3>Arrays on this drive</h3><ul class="detail-list">${arrays}</ul>
      ${anyErrors ? `<p class="muted">Read errors md has corrected on this drive, counted since each array was last
      assembled. That happens on every reboot, so a count back at 0 does not mean the drive got better: the
      warning above keeps the tally across restarts.</p>` : ""}</section>`;
  drawDriveTemps();
  drawDriveSleep();
  drawDriveIo();
  drawDriveCounters();
}

function driveHistoryKey(smart) {
  return smart ? smart.serial || smart.device : null;
}

async function refreshDriveTemps() {
  const entry = driveEntries((state.snapshot.raid || {}).arrays || []).find((e) => e.key === state.driveKey);
  const key = driveHistoryKey(entry && smartFor(entry));
  if (!key) return;
  const hours = state.driveTempHours;
  try {
    const history = await api(`/api/drive/temperature?drive=${encodeURIComponent(key)}&hours=${hours}`);
    if (driveHistoryKey(smartFor(entry)) !== key || state.driveTempHours !== hours) return;
    state.driveTemps = history;
    drawDriveTemps();
  } catch (_) {
    // The facts above still stand; the chart says it has nothing.
    if (state.driveTempHours === hours) state.driveTemps = { drive: key, samples: [] };
    drawDriveTemps();
  }
}

// SMART's own reading, every few minutes. Asleep, a drive is not asked, so a
// spun-down drive shows as a gap rather than a made-up line.
function drawDriveTemps() {
  const host = document.querySelector('#drive-body [data-chart="drive-temp"]');
  if (!host) return;
  const entry = driveEntries((state.snapshot.raid || {}).arrays || []).find((e) => e.key === state.driveKey);
  const smart = entry && smartFor(entry);
  const history = state.driveTemps;
  if (!history || history.drive !== driveHistoryKey(smart)) {
    host.innerHTML = `<p class="chart-empty">Loading…</p>`;
    return;
  }
  const points = history.samples.map(([at, c]) => [new Date(at).getTime(), Number(c)]);
  if (smart?.temperature !== null && smart?.temperature !== undefined && !smart.asleep && state.snapshot.smart?.checked_at) {
    points.push([new Date(state.snapshot.smart.checked_at).getTime(), Number(smart.temperature)]);
  }
  const t = thresholds();
  const alert = smart?.protocol === "NVMe" ? t.drive_nvme_temp ?? 70 : t.drive_temp ?? 55;
  drawChart(host, { points, ...tempRange(points.length ? points : [[0, alert - 20]], alert),
    format: celsiusFormat, label: `Temperature of ${entry.label}`,
    refs: [{ value: alert, label: "alert", kind: "warning" }],
    empty: "The chart fills in as the doctor reads SMART - a few minutes per point." });
}

// A hard disk, asleep or not: SSDs and NVMe drives never spin down.
function canSleep(smart) {
  return Boolean(smart.asleep || smart.rotation_rpm > 0);
}

async function refreshDriveSleep() {
  const entry = driveEntries((state.snapshot.raid || {}).arrays || []).find((e) => e.key === state.driveKey);
  const smart = entry && smartFor(entry);
  const key = driveHistoryKey(smart);
  if (!key || !canSleep(smart)) return;
  const hours = state.driveTempHours;
  try {
    const history = await api(`/api/drive/sleep?drive=${encodeURIComponent(key)}&hours=${hours}`);
    if (driveHistoryKey(smartFor(entry)) !== key || state.driveTempHours !== hours) return;
    state.driveSleep = { ...history, hours };
  } catch (_) {
    if (state.driveTempHours !== hours) return;
    state.driveSleep = { drive: key, hours, samples: [], asleep_pct: null, wakes: 0, step_minutes: 5 };
  }
  drawDriveSleep();
}

// One bar over the chosen range: asleep, awake, and blank where there was no
// reading. Buckets in a row with the same state are drawn as one stretch.
function drawDriveSleep() {
  const host = document.querySelector('#drive-body [data-chart="drive-sleep"]');
  const summary = document.querySelector("#drive-body [data-sleep-summary]");
  if (!host || !summary) return;
  const entry = driveEntries((state.snapshot.raid || {}).arrays || []).find((e) => e.key === state.driveKey);
  const smart = entry && smartFor(entry);
  const history = state.driveSleep;
  if (!history || history.drive !== driveHistoryKey(smart) || history.hours !== state.driveTempHours) {
    summary.textContent = "";
    host.innerHTML = `<p class="chart-empty">Loading…</p>`;
    return;
  }
  const range = history.hours === 168 ? "7 days" : `${history.hours} hours`;
  if (history.asleep_pct === null || history.asleep_pct === undefined) {
    summary.textContent = "";
    host.innerHTML = `<p class="chart-empty">No readings in the last ${esc(range)} yet. The bar fills in as
      the doctor reads SMART - a point every few minutes.</p>`;
    return;
  }
  const wakes = history.wakes === 1 ? "woke once" : `woke ${history.wakes} times`;
  summary.textContent = `Asleep ${history.asleep_pct}% of the last ${range} · ${wakes}`;
  const step = (history.step_minutes || 5) * 60000;
  const x1 = Date.now();
  const x0 = x1 - history.hours * 3600000;
  const stretches = [];
  for (const [at, share] of history.samples) {
    const start = new Date(at).getTime();
    const asleep = share > 0.5;
    const last = stretches[stretches.length - 1];
    if (last && last.asleep === asleep && start - last.end <= 1000) last.end = start + step;
    else stretches.push({ start, end: start + step, asleep });
  }
  const pct = (t) => (Math.min(x1, Math.max(x0, t)) - x0) / (x1 - x0) * 100;
  const when = (t) => new Date(t).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  const bars = stretches.filter((s) => s.end > x0).map((s) => `<i data-state="${s.asleep ? "asleep" : "awake"}"
      style="left:${pct(s.start).toFixed(2)}%;width:${Math.max(0.2, pct(s.end) - pct(s.start)).toFixed(2)}%"
      title="${esc(`${s.asleep ? "Asleep" : "Awake"} ${when(s.start)} - ${when(Math.min(s.end, x1))}`)}"></i>`).join("");
  const span = x1 - x0;
  host.innerHTML = `<div class="sleep-bar" role="img" aria-label="${esc(`${entry.label}: ${summary.textContent}`)}">${bars}</div>
    <div class="sleep-axis" aria-hidden="true"><span>${esc(shortTime(x0, span))}</span>
      <span>${esc(shortTime(x0 + span / 2, span))}</span><span>now</span></div>
    <div class="sleep-key" aria-hidden="true"><span><i data-state="asleep"></i>asleep</span>
      <span><i data-state="awake"></i>awake</span><span><i></i>no reading</span></div>`;
}

function currentDrive() {
  const entry = driveEntries((state.snapshot.raid || {}).arrays || []).find((e) => e.key === state.driveKey);
  return { entry, smart: entry && smartFor(entry) };
}

// The kernel's counters are there with or without the SMART sidecar; without
// it, a drive's I/O is filed under its device name.
function driveIoKey() {
  const { entry, smart } = currentDrive();
  return entry ? driveHistoryKey(smart) || entry.disk || entry.key : null;
}

async function refreshDriveIo() {
  const key = driveIoKey();
  if (!key) return;
  const hours = state.driveTempHours;
  try {
    const history = await api(`/api/drive/io?drive=${encodeURIComponent(key)}&hours=${hours}`);
    if (driveIoKey() !== key || state.driveTempHours !== hours) return;
    state.driveIo = { ...history, hours };
  } catch (_) {
    if (state.driveTempHours !== hours) return;
    state.driveIo = { drive: key, hours, samples: [], read_bytes: 0, written_bytes: 0 };
  }
  drawDriveIo();
}

// Reads and writes added up into one line: the question is when the drive
// was touched, and the summary says how much of it was which.
function drawDriveIo() {
  const host = document.querySelector('#drive-body [data-chart="drive-io"]');
  const summary = document.querySelector("#drive-body [data-io-summary]");
  if (!host || !summary) return;
  const history = state.driveIo;
  if (!history || history.drive !== driveIoKey() || history.hours !== state.driveTempHours) {
    summary.textContent = "";
    host.innerHTML = `<p class="chart-empty">Loading…</p>`;
    return;
  }
  const range = history.hours === 168 ? "7 days" : `${history.hours} hours`;
  const points = history.samples.map(([at, read, written]) => [new Date(at).getTime(), read + written]);
  if (points.length) {
    const [busiestAt] = history.samples.reduce((a, b) => (b[1] + b[2] > a[1] + a[2] ? b : a));
    summary.textContent = `Read ${bytes(history.read_bytes)}, wrote ${bytes(history.written_bytes)} in the last ${range}`
      + ` · busiest ${new Date(busiestAt).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}`;
  } else {
    summary.textContent = "";
  }
  drawChart(host, { points, yMin: 0, yMax: niceBytes(Math.max(1024, ...points.map((p) => p[1])) * 1.1),
    format: (v) => `${bytes(v)}/s`, label: `Reads and writes of ${currentDrive().entry?.label || "the drive"}`,
    empty: "The chart fills in as the doctor reads the host - a few minutes per point." });
}

async function refreshDriveCounters() {
  const { smart } = currentDrive();
  const key = driveHistoryKey(smart);
  if (!key) return;
  try {
    const history = await api(`/api/drive/counters?drive=${encodeURIComponent(key)}&days=30`);
    if (driveHistoryKey(currentDrive().smart) !== key) return;
    state.driveCounters = history;
  } catch (_) {
    state.driveCounters = { drive: key, days: 30, counters: [] };
  }
  drawDriveCounters();
}

// What the counters did over the range, not just where they stand: one that
// rose is the news, and spin-ups are worth a rate.
function drawDriveCounters() {
  const host = document.querySelector("#drive-body [data-drive-counters]");
  if (!host) return;
  const { smart } = currentDrive();
  const history = state.driveCounters;
  if (!history || history.drive !== driveHistoryKey(smart)) {
    host.innerHTML = `<p class="chart-empty">Loading…</p>`;
    return;
  }
  const counters = history.counters || [];
  if (!counters.length) {
    host.innerHTML = `<p class="muted">Nothing kept yet - the doctor keeps one reading an hour.</p>`;
    return;
  }
  const words = { reallocated: "Reallocated sectors", pending: "Pending sectors",
    uncorrectable: "Uncorrectable sectors", reported_uncorrect: "Reported uncorrectable",
    crc_errors: "CRC errors", media_errors: "Media errors", spin_ups: "Spin-ups" };
  const n = (v) => Number(v).toLocaleString();
  const since = new Date(counters[0].since);
  const days = Math.max(1 / 24, (Date.now() - since.getTime()) / 86400000);
  const hours = Math.max(1, Math.round(days * 24));
  const span = days >= 1.5 ? `${Math.round(days)} days` : hours === 1 ? "hour" : `${hours} hours`;
  const rows = counters.map((c) => {
    const moved = c.last !== c.first;
    const pill = c.key === "spin_ups" ? ""
      : c.last < c.first ? statusPill("went down", "neutral")
      : moved ? statusPill(`rose ${c.rises === 1 ? "once" : `${c.rises} times`}`, "warning")
      : statusPill("steady", "good");
    const detail = c.key === "spin_ups"
      ? `${n(c.last - c.first)} in ${span === "hour" ? "the last hour" : span}${days >= 1 ? `, about ${n(Math.round((c.last - c.first) / days))} a day` : ""}`
      : moved ? `${n(c.first)} → ${n(c.last)}${c.rose_at ? `, last rose ${new Date(c.rose_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}` : ""}`
      : n(c.last);
    return `<li><span>${esc(words[c.key] || c.key)}</span><span class="muted">${esc(detail)}</span>${pill}</li>`;
  }).join("");
  host.innerHTML = `<p class="muted">Over the last ${esc(span)}, one reading an hour.</p>
    <ul class="detail-list">${rows}</ul>`;
}

// --- drive health (SMART) ------------------------------------------------------------
//
// Read by the doctor-smart sidecar, which has the raw disk access the doctor
// does not. One row per drive; the counters that matter, and what is wrong.

const SMART_COUNTER_WORDS = {
  Reallocated_Sector_Ct: "reallocated", Current_Pending_Sector: "pending",
  Offline_Uncorrectable: "uncorrectable", Reported_Uncorrect: "reported uncorrectable",
};

function powerOn(hours) {
  if (hours === null || hours === undefined) return "-";
  const years = hours / 8766;
  return years >= 1 ? `${years.toFixed(1)} years` : `${Math.round(hours / 24)} days`;
}

function drivePill(d) {
  if (d.asleep) return statusPill("asleep", "neutral");
  if (d.severity === "critical") return statusPill(d.passed === false ? "failing" : "critical", "critical");
  if (d.severity === "warning") return statusPill("warning", "warning");
  if (d.passed === null || d.passed === undefined) return statusPill("no data", "neutral");
  return statusPill("healthy", "good");
}

// Everything else SMART says about a drive, for its own page: what it has
// written and read, its wear, and each counter on its own - a 0 is worth
// reading there too.
function driveLifetime(d) {
  const count = (v) => (v === null || v === undefined ? null : Number(v).toLocaleString());
  const nvme = d.nvme || {};
  const c = d.counters || {};
  const kind = d.protocol === "NVMe" ? null
    : d.rotation_rpm === 0 ? "SSD" : d.rotation_rpm ? `${Number(d.rotation_rpm).toLocaleString()} rpm` : null;
  return [
    ["Type", kind],
    ["Link", d.link_bps ? `${(d.link_bps / 1e9).toFixed(1)} Gb/s` : null],
    ["Written", d.bytes_written !== null && d.bytes_written !== undefined ? bytes(d.bytes_written) : null],
    ["Read", d.bytes_read !== null && d.bytes_read !== undefined ? bytes(d.bytes_read) : null],
    ["Life used", d.wear_pct !== null && d.wear_pct !== undefined ? `${Math.round(d.wear_pct)}%` : null],
    ["Spare left", nvme.available_spare !== undefined ? `${Math.round(nvme.available_spare)}%` : null],
    ["Media errors", count(nvme.media_errors)],
    ["Reallocated sectors", count(c.Reallocated_Sector_Ct)],
    ["Pending sectors", count(c.Current_Pending_Sector)],
    ["Uncorrectable sectors", count(c.Offline_Uncorrectable)],
    ["Reported uncorrectable", count(c.Reported_Uncorrect)],
    ["CRC errors", count(d.crc_errors)],
    ["Head parks", d.rotation_rpm ? count(d.load_cycles) : null],
    ["Spin-ups", d.rotation_rpm ? count(d.spin_ups) : null],
    ["Errors logged", count(d.error_log)],
    ["Failed self-tests", count(d.self_test_errors)],
  ];
}

function driveCounters(d) {
  if (d.protocol === "NVMe") {
    const n = d.nvme || {};
    return [
      n.percentage_used !== undefined ? `${Math.round(n.percentage_used)}% worn` : "",
      n.available_spare !== undefined ? `${Math.round(n.available_spare)}% spare` : "",
      n.media_errors ? `${n.media_errors} media errors` : "",
    ].filter(Boolean).join(" · ") || "-";
  }
  const c = d.counters || {};
  const bad = Object.entries(SMART_COUNTER_WORDS).filter(([k]) => c[k]).map(([k, w]) => `${c[k]} ${w}`);
  const crc = d.crc_errors ? `${d.crc_errors} CRC` : "";
  return [...bad, crc].filter(Boolean).join(" · ") || (Object.keys(c).length ? "no bad sectors" : "-");
}

function renderSmart() {
  if (!state.snapshotLoaded) return;
  const s = state.snapshot.smart;
  const host = $("drive-health");
  const sub = $("smart-sub");
  if (!s) {
    sub.textContent = "";
    host.innerHTML = `<p class="empty">Not read yet - drive health appears after the first poll,
      or is off (<code>DOCTOR_SMART_URL</code> empty).</p>`;
    return;
  }
  if (s.error) {
    sub.textContent = "";
    host.innerHTML = `<p class="empty">The <code>doctor-smart</code> sidecar is not answering
      (${esc(s.error)}). It reads SMART for the doctor; see <a href="https://sebastianbuechler.github.io/container-doctor/guides/storage/#drive-health" target="_blank" rel="noopener">Drive health</a> in the documentation.</p>`;
    return;
  }
  const drives = s.drives || [];
  sub.innerHTML = `${drives.length} drive${drives.length === 1 ? "" : "s"} · read ${agoTag(s.checked_at)}`;
  if (!drives.length) {
    host.innerHTML = `<p class="empty">The sidecar answers but reports no drives. It needs the
      drives' device files - set <code>DOCTOR_SMART_DEVICES</code> if they are not found.</p>`;
    return;
  }
  const t = thresholds();
  // A list rather than a table: one row per drive, its columns side by side on
  // a wide screen and folded under the drive's name on a phone, so nothing
  // scrolls sideways.
  const fact = (label, text) => `<span class="smart-fact"><span class="smart-k">${label}</span>${esc(text)}</span>`;
  host.innerHTML = `<div class="smart-head" aria-hidden="true"><span>Drive</span><span>Health</span>
      <span>Temp</span><span>Powered on</span><span>Counters</span></div>
    <ul class="smart-list">${drives.map((d) => `<li class="smart-row">
      <div class="smart-name"><strong>${esc(d.label)}</strong> <span class="muted">${esc(d.model || d.device)}</span>
        ${d.serial ? `<span class="muted smart-serial">· ${esc(d.serial)}</span>` : ""}</div>
      <div class="smart-pill">${drivePill(d)}</div>
      <div class="smart-facts">${fact("Temp", d.temperature !== null && d.temperature !== undefined ? `${Math.round(d.temperature)} °C` : "-")}
        ${fact("Powered on", powerOn(d.power_on_hours))}
        ${fact("Counters", driveCounters(d))}</div>
      ${(d.problems || []).length ? `<div class="smart-problem">${
        d.problems.map((p) => `<span>${esc(p)}</span>`).join("")}</div>` : ""}
    </li>`).join("")}</ul>`;
}

async function refreshDiskHistory() {
  if (!(state.snapshot.disk || []).length) return;
  try {
    await load("disk-history", "/api/disk/history?days=14", (answer) => {
      state.diskHistory = answer.disks;
      renderDisk();
    });
  } catch (_) { /* the meters still work without the chart */ }
}

// Thirty containers on `:latest` and nothing that says which of them are
// behind. Only the ones with something newer get a row: a list of twenty-seven
// "current" lines is the reason nobody reads it.
// --- host -----------------------------------------------------------------------
//
// The host itself, read from the kernel on every poll and pushed with the rest
// of the state. The charts are the stored history: averages per few minutes,
// with the live reading added at the end.

const HOST_ALERT_WORDS = {
  host_memory: "memory low", host_swap: "swapping", host_load: "load high",
  host_iowait: "waiting on disks", host_temperature: "running hot",
};

function duration(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function figure(value, digits = 0, unit = "%") {
  return value === null || value === undefined ? "-" : `${Number(value).toFixed(digits)}${unit}`;
}

function hostTile(label, value, note, fill, levelName) {
  const meter = fill === null || fill === undefined ? "" : `
    <div class="meter"><div class="meter-fill" data-level="${levelName}"
      style="width:${Math.min(100, Math.max(0, fill))}%"></div></div>`;
  return `<div class="host-tile">
    <span class="host-tile-label">${esc(label)}</span>
    <strong class="host-tile-value">${value}</strong>
    ${meter}
    <span class="host-tile-note">${note}</span>
  </div>`;
}

function hostTiles(h) {
  const t = thresholds();
  const memAlert = t.host_mem_available ?? 5;
  const loadAlert = (t.host_load_per_core ?? 2) * h.cores;
  const load5 = (h.load || [])[1];
  const tiles = [
    hostTile("CPU", figure(h.cpu_pct), `${h.cores} CPU${h.cores === 1 ? "" : "s"}`,
      h.cpu_pct, level(h.cpu_pct ?? 0, 75, t.cpu ?? 90)),
    hostTile("Waiting on disks", figure(h.iowait_pct), "share of CPU time in I/O wait",
      h.iowait_pct, level(h.iowait_pct ?? 0, (t.host_iowait ?? 40) / 2, t.host_iowait ?? 40)),
    hostTile("Load", load5 === undefined ? "-" : load5.toFixed(2),
      `5-min average · alert at ${loadAlert.toFixed(1)}`,
      load5 === undefined ? null : (load5 / loadAlert) * 100,
      level(load5 ?? 0, loadAlert / 2, loadAlert)),
    hostTile("Memory used", figure(h.mem_used_pct),
      `${bytes(h.mem_available)} available of ${bytes(h.mem_total)}`,
      h.mem_used_pct, level(h.mem_used_pct ?? 0, 100 - memAlert * 2, 100 - memAlert)),
    hostTile("Swap", h.swap_total ? figure(h.swap_used_pct) : "none",
      h.swap_total
        ? (h.swap_in_per_s ? `${h.swap_in_per_s.toFixed(0)} pages/s read back` : `${bytes(h.swap_used)} of ${bytes(h.swap_total)}, not being read back`)
        : "no swap configured",
      h.swap_total ? h.swap_used_pct : null,
      h.swap_in_per_s >= (t.host_swap_in ?? 256) ? "critical" : "neutral"),
  ];
  const sensors = h.temperatures || [];
  if (sensors.length) {
    const hot = sensors.reduce((a, b) => (b.celsius > a.celsius ? b : a));
    const limit = t.host_temp ?? 80;
    tiles.push(hostTile("Hottest sensor", `${hot.celsius.toFixed(0)} °C`,
      esc(`${hot.name} ${hot.label}`.trim()), (hot.celsius / (hot.critical || limit + 10)) * 100,
      level(hot.celsius, limit - 10, limit)));
  }
  return tiles.join("");
}

// What the host is called: DOCTOR_HOST_NAME, else the model the kernel names
// (Synology's), else just "the host".
function hostTitle() {
  const s = state.status || {};
  if (s.host_name) return s.host_name;
  if (s.host_model) return s.host_kind === "synology" ? `Synology ${s.host_model}` : s.host_model;
  return "The host";
}
function isSynology() {
  return state.status?.host_kind === "synology";
}

function renderHost() {
  if (!state.snapshotLoaded) return;
  $("host-title").textContent = hostTitle();
  const h = state.snapshot.host;
  if (!h) {
    $("host-sub").textContent = "";
    $("host-alerts").innerHTML = "";
    $("host-tiles").innerHTML = `<p class="empty">Nothing read from the host yet - it appears after
      the first poll. <code>DOCTOR_PROC_PATH</code> empty turns this page off.</p>`;
    $("host-detail").innerHTML = "";
    return;
  }
  const booted = h.boot_time ? new Date(h.boot_time) : null;
  $("host-sub").textContent = [
    `${h.cores} CPUs`,
    h.uptime_seconds !== undefined ? `up ${duration(h.uptime_seconds)}` : "",
    booted ? `since ${booted.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}` : "",
  ].filter(Boolean).join(" · ");
  const alerts = h.alerts || [];
  $("host-alerts").innerHTML = alerts.length
    ? `<p class="host-alert-line">${alerts.map((a) => statusPill(HOST_ALERT_WORDS[a] || a, "warning")).join(" ")}
       <span class="muted">over the threshold now; an incident opens if it lasts</span></p>` : "";
  $("host-tiles").innerHTML = hostTiles(h);
  renderHostDetail(h);
  drawHostCharts();
}

function renderHostDetail(h) {
  const disks = h.disks || [];
  const t = thresholds();
  const parts = [];
  parts.push(`<h3 class="sub-head">Disks</h3>` + (disks.length ? `
    <div class="net-matrix-wrap"><table class="host-table">
      <thead><tr><th scope="col">Disk</th><th scope="col">Busy</th><th scope="col">Read</th><th scope="col">Write</th></tr></thead>
      <tbody>${disks.map((d) => `<tr>
        <td><code>${esc(d.name)}</code></td>
        <td class="host-busy"><div class="meter"><div class="meter-fill" data-level="${level(d.busy_pct, 60, 90)}"
          style="width:${Math.min(100, d.busy_pct)}%"></div></div><span>${d.busy_pct.toFixed(0)}%</span></td>
        <td>${bytes(d.read_bps)}/s</td><td>${bytes(d.write_bps)}/s</td>
      </tr>`).join("")}</tbody></table></div>
    <p class="muted host-note">Busy is the share of time a disk had work in flight. One disk far busier
      than its siblings in the same array is worth a look - a failing drive often slows down before
      it reports errors.</p>`
    : `<p class="muted host-note">Disk activity needs two polls.</p>`));

  const pressure = h.pressure;
  parts.push(`<h3 class="sub-head">Pressure</h3>` + (pressure ? `
    <div class="net-matrix-wrap"><table class="host-table">
      <thead><tr><th scope="col">Resource</th><th scope="col">Something waited</th><th scope="col">Everything waited</th></tr></thead>
      <tbody>${["cpu", "memory", "io"].filter((r) => pressure[r]).map((r) => `<tr>
        <td>${{ cpu: "CPU", memory: "Memory", io: "Disk I/O" }[r]}</td>
        <td>${figure(pressure[r].some, 1)}</td><td>${r === "cpu" && !pressure[r].full ? "-" : figure(pressure[r].full, 1)}</td>
      </tr>`).join("")}</tbody></table></div>
    <p class="muted host-note">Share of the last minute spent stalled. Unlike load, it says how much
      work was actually held up.</p>`
    : `<p class="muted host-note">${pressureMissing(h.kernel)}</p>`));

  const sensors = h.temperatures || [];
  const limit = t.host_temp ?? 80;
  parts.push(`<h3 class="sub-head">Temperatures</h3>` + (sensors.length ? `
    <div class="net-matrix-wrap"><table class="host-table">
      <thead><tr><th scope="col">Sensor</th><th scope="col">Now</th><th scope="col">Critical</th></tr></thead>
      <tbody>${sensors.map((s) => `<tr>
        <td>${esc(s.name)} <span class="muted">${esc(s.label)}</span></td>
        <td>${statusPill(`${s.celsius.toFixed(0)} °C`, level(s.celsius, limit - 10, limit) === "critical" ? "critical"
          : level(s.celsius, limit - 10, limit) === "warning" ? "warning" : "good")}</td>
        <td>${s.critical ? `${s.critical.toFixed(0)} °C` : "-"}</td>
      </tr>`).join("")}</tbody></table></div>`
    : `<p class="muted host-note">This host does not expose temperature sensors to containers.${isSynology()
      ? " DSM's own Info Center still shows them." : ""}</p>`));
  $("host-detail").innerHTML = parts.join("");
}

// Pressure stall information arrived in Linux 4.20. DSM 7 runs 4.4 on most
// models, so on a Synology this is usually the kernel's age, not a setting.
function pressureMissing(kernel) {
  const [major, minor] = (/^(\d+)\.(\d+)/.exec(kernel || "") || []).slice(1).map(Number);
  if (major === undefined) return "This kernel does not report pressure stall information.";
  if (major < 4 || (major === 4 && minor < 20)) {
    return `No pressure stall information: it arrived in Linux 4.20, and this host runs ${esc(kernel)}.
      Only ${isSynology() ? "a DSM release on a newer kernel" : "a newer kernel"} brings it; until then load and I/O wait stand in for it.`;
  }
  return `No pressure stall information: this kernel (${esc(kernel)}) is new enough, but was built or
    booted without it (<code>CONFIG_PSI</code>, <code>psi=1</code>).`;
}

async function refreshHostHistory() {
  try {
    await load("host-history", `/api/host/history?hours=${state.hostHours}`, (history) => {
      state.hostHistory = history;
      state.hostHistoryAt = Date.now();
      drawHostCharts(true);
      renderReboots();
      renderCpuByContainer();
    });
  } catch (_) { /* the live tiles still work without the charts */ }
}

function hostSeries(key) {
  const rows = state.hostHistory?.samples || [];
  const points = rows.filter((r) => r[key] !== null && r[key] !== undefined)
    .map((r) => [new Date(r.at).getTime(), Number(r[key])]);
  const h = state.snapshot.host;
  const hottest = (h?.temperatures || []).reduce((a, b) => Math.max(a, b.celsius), -Infinity);
  const live = { cpu_pct: h?.cpu_pct, iowait_pct: h?.iowait_pct, mem_used_pct: h?.mem_used_pct,
                 load1: (h?.load || [])[0], temp_c: hottest > -Infinity ? hottest : null }[key];
  if (h && live !== null && live !== undefined) points.push([new Date(h.at).getTime(), Number(live)]);
  return points;
}

function hostChartSpecs() {
  const t = thresholds();
  const h = state.snapshot.host || {};
  const cores = h.cores || 1;
  const pctFormat = (v, long) => (long ? `${v.toFixed(1)}%` : `${v.toFixed(0)}%`);
  const loads = hostSeries("load1");
  const loadAlert = (t.host_load_per_core ?? 2) * cores;
  const iowait = hostSeries("iowait_pct");
  const iowaitTop = Math.min(100, Math.max(20, niceCeil(Math.max(...iowait.map((p) => p[1]), 0) * 1.2)));
  const memAlert = 100 - (t.host_mem_available ?? 5);
  const temps = hostSeries("temp_c");
  const tempAlert = t.host_temp ?? 80;
  const specs = [
    ["cpu", "CPU", { points: hostSeries("cpu_pct"), yMin: 0, yMax: 100, format: pctFormat,
      label: "CPU use of the whole host", refs: [] }],
    ["iowait", "Waiting on disks", { points: iowait, yMin: 0, yMax: iowaitTop, format: pctFormat,
      label: "Share of CPU time waiting on disks",
      refs: [{ value: t.host_iowait ?? 40, label: "alert", kind: "warning" }] }],
    ["memory", "Memory used", { points: hostSeries("mem_used_pct"), yMin: 0, yMax: 100, format: pctFormat,
      label: "Memory in use, not counting reclaimable cache",
      refs: [{ value: memAlert, label: "alert", kind: "warning" }] }],
    ["load", "Load", { points: loads, yMin: 0,
      yMax: niceCeil(Math.max(...loads.map((p) => p[1]), loadAlert * 1.1, 1)),
      format: (v, long) => (long ? `${v.toFixed(2)} (${(v / cores).toFixed(2)} per CPU)` : v.toFixed(1)),
      label: `1-minute load on ${cores} CPUs`,
      refs: [{ value: cores, label: "1 per CPU", kind: "neutral" },
             { value: loadAlert, label: "alert", kind: "warning" }] }],
  ];
  // Only where the host exposes a sensor at all; an empty chart would say nothing.
  if (temps.length) {
    specs.push(["temp", "Hottest sensor", { points: temps, ...tempRange(temps, tempAlert),
      format: celsiusFormat, label: "Temperature of the host's hottest sensor",
      refs: [{ value: tempAlert, label: "alert", kind: "warning" }] }]);
  }
  return specs;
}

const celsiusFormat = (v, long) => (long ? `${v.toFixed(1)} °C` : `${v.toFixed(0)}°`);

// Temperatures sit in a narrow band far from zero: a 0-100 axis flattens a
// 10° swing into a line. Fit the readings, keeping the alert in view.
function tempRange(points, alert) {
  const values = points.map((p) => p[1]);
  const low = Math.min(...values);
  const high = Math.max(...values, alert);
  return { yMin: Math.max(0, Math.floor((low - 5) / 10) * 10), yMax: Math.ceil((high + 5) / 10) * 10 };
}

function drawHostCharts(force = false) {
  const host = $("host-charts");
  if (!host || state.view !== "host" || !state.hostHistory) return;
  const specs = hostChartSpecs();
  if (force || host.dataset.built !== "1") {
    host.dataset.built = "1";
    host.innerHTML = specs.map(([key, title]) => `
      <div class="host-chart"><h3 class="sub-head">${esc(title)}</h3>
        <div class="chart" data-chart="host-${key}"></div></div>`).join("");
  }
  for (const [key, , spec] of specs) {
    drawChart(host.querySelector(`[data-chart="host-${key}"]`),
      { ...spec, empty: "The chart fills in as the doctor samples the host - a few minutes per point." });
  }
}

// Who the CPU line above was, from each container's hourly record.
function renderCpuByContainer() {
  const rows = state.hostHistory?.cpu_by_container || [];
  const when = (at) => new Date(at).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  $("host-cpu-top").innerHTML = rows.length
    ? `<h3 class="sub-head">CPU by container</h3><ul class="detail-list">${rows.map((r) => `<li>
        <a href="#container/${encodeURIComponent(r.name)}">${esc(r.name)}</a>
        <span class="muted">average ${pct(r.mean)} · busiest ${esc(when(r.busiest_hour))} at ${pct(r.busiest_mean)}
          · peak ${pct(r.peak)}</span></li>`).join("")}</ul>
      <p class="muted host-note">Share of the whole host, averaged per hour.</p>` : "";
}

function renderReboots() {
  const rows = state.hostHistory?.reboots || [];
  $("host-reboots").innerHTML = rows.length
    ? `<h3 class="sub-head">Restarts in this range</h3><ul class="host-reboot-list">${rows.map((r) =>
      `<li>${agoTag(r.at)} <span>${esc(r.summary)}</span></li>`).join("")}</ul>` : "";
}

// --- internet and DNS -------------------------------------------------------------
//
// Checked from the doctor's container on a timer of its own and pushed when a
// round finishes. Each row is one check; the strip is its recent rounds.

const NET_KIND_WORDS = { tcp: "reach", dns: "resolve", server: "DNS server", http: "HTTPS" };

function connectivityPill(ok) {
  if (ok === null || ok === undefined) return statusPill("not checked yet", "neutral");
  return ok ? statusPill("ok", "good") : statusPill("failing", "critical");
}

function strip(history) {
  const recent = history.slice(-60);
  return `<span class="net-strip" aria-hidden="true">${recent.map(([at, ok, ms]) =>
    `<i data-ok="${ok}" title="${esc(`${new Date(at).toLocaleTimeString()}: ${ok ? `${ms} ms` : "failed"}`)}"></i>`).join("")}</span>`;
}

function renderConnectivity() {
  if (!state.snapshotLoaded) return;
  const c = state.snapshot.connectivity;
  const host = $("connectivity");
  if (!c) {
    $("connectivity-sub").textContent = "";
    host.innerHTML = `<p class="empty">Internet and DNS checks are off (<code>DOCTOR_NET_CHECK_SECONDS=0</code>).</p>`;
    return;
  }
  $("connectivity-sub").innerHTML = c.last_round_at
    ? `Every ${c.interval_seconds}s from the doctor's container · last ${agoTag(c.last_round_at)}`
    : `First check within ${c.interval_seconds}s`;
  const word = (value, yes, no) => (value === null || value === undefined
    ? statusPill(`${yes.split(" ")[0]}: not checked`, "neutral")
    : value ? statusPill(yes, "good") : statusPill(no, "critical"));
  const summary = `<p class="conn-summary">${word(c.internet, "Internet reachable", "Internet unreachable")}
    ${word(c.dns, "DNS resolving", "DNS failing")}</p>`;
  const rows = c.checks.map((r) => `
    <li class="conn-row${r.ok === false ? " is-failing" : ""}">
      <div class="conn-what">
        <span class="net-tag">${esc(NET_KIND_WORDS[r.kind] || r.kind)}</span>
        <code title="${esc(r.target)}">${esc(r.target)}</code>
      </div>
      <div class="conn-state">${connectivityPill(r.ok)}
        <span class="conn-ms">${r.ms !== null && r.ms !== undefined ? `${Math.round(r.ms)} ms` : ""}</span></div>
      <div class="conn-detail muted" title="${esc(r.detail || "")}">${esc(r.detail || "")}</div>
      <div class="conn-history">${strip(r.history || [])}
        <span class="conn-uptime">${r.uptime_pct !== null && r.uptime_pct !== undefined ? `${r.uptime_pct}%` : ""}</span></div>
    </li>`).join("");
  host.innerHTML = summary + `<ul class="conn-list">${rows}</ul>`;
}

// --- network ------------------------------------------------------------------
//
// Read from each container's own inspect output on the last poll: the networks
// it is on, its address there, and what it publishes on the host. The map is
// redrawn only when that changes, so a chip being pointed at stays put.

async function refreshNetwork() {
  try {
    await load("network", "/api/networks", drawNetworkReport);
  } catch (_) { /* the next poll tries again */ }
}

function drawNetworkReport(report) {
  const { last_poll_at: _at, ...content } = report;
  const key = JSON.stringify(content);
  state.network = report;
  if (key !== state.networkKey) {
    state.networkKey = key;
    renderNetwork();
  }
  renderBadges();
}

function memberLevel(m) {
  if (m.state !== "running") return "critical";
  if (m.health === "unhealthy") return "warning";
  if (m.health === "starting") return "serious";
  return "good";
}

// How many networks each container is on - the ones on two or more are what
// joins the segments together.
function homes(report) {
  const count = new Map();
  for (const n of report.networks || []) {
    for (const m of n.members) count.set(m.name, (count.get(m.name) || 0) + 1);
  }
  return count;
}

function netTags(n) {
  const tags = [];
  if (n.config_only) tags.push(["template", "settings a macvlan network is created from; never has containers"]);
  else if (n.unused) tags.push(["unused", "nobody is attached to it"]);
  if (n.config_from) tags.push([`from ${n.config_from}`, "created from that template"]);
  if (n.driver && !["bridge", "null"].includes(n.driver)) {
    tags.push([n.driver, n.parent ? `on ${n.parent}` : n.driver]);
  }
  if (n.internal) {
    tags.push(["internal", n.source === "docker" ? "no way out to the internet"
      : "inferred: its containers get no gateway, so no way out to the internet"]);
  }
  if (n.icc === false) tags.push(["isolated", "containers on it cannot reach each other"]);
  if (n.ipv6) tags.push(["IPv6", (n.subnets || []).join(", ")]);
  if (n.kind === "default") tags.push(["default bridge", "no name resolution between containers"]);
  if (n.stacks.length > 1) tags.push([`${n.stacks.length} stacks`, n.stacks.join(", ")]);
  return tags.map(([label, title]) =>
    `<span class="net-tag" title="${esc(title)}">${esc(label)}</span>`).join("");
}

function netCard(n, count) {
  const members = [...n.members].sort((a, b) =>
    ((count.get(b.name) > 1) - (count.get(a.name) > 1))
    || (a.stack || "").localeCompare(b.stack || "") || a.name.localeCompare(b.name));
  const figures = [
    n.subnet ? `<code>${esc(n.subnet)}</code>` : "",
    n.members.length ? `${n.members.length} container${n.members.length === 1 ? "" : "s"}`
      : n.config_only ? (n.parent ? `parent <code>${esc(n.parent)}</code>` : "template")
      : (n.project ? `from stack ${esc(n.project)}` : "nobody on it"),
    n.running < n.members.length ? `${n.members.length - n.running} stopped` : "",
  ].filter(Boolean).join(" · ");
  return `
    <article class="net-card${n.members.length >= 8 ? " is-wide" : ""}${n.members.length ? "" : " is-unused"}" data-net="${esc(n.name)}" data-kind="${esc(n.kind)}${n.internal ? " internal" : ""}">
      <header class="net-card-head">
        <strong class="net-name">${esc(n.name)}</strong>${netTags(n)}
        <span class="net-figures">${figures}</span>
      </header>
      <ul class="net-members">${members.map((m) => {
        const on = count.get(m.name) || 1;
        const aliases = (m.aliases || []).length ? ` · also ${m.aliases.join(", ")}` : "";
        return `<li><a class="net-chip${on > 1 ? " is-bridge" : ""}" data-member="${esc(m.name)}"
            href="#container/${encodeURIComponent(m.name)}"
            title="${esc(`${m.name}${m.stack ? ` (${m.stack})` : ""}${m.ip ? ` at ${m.ip}` : ""}${aliases}`)}">
          <span class="net-dot" data-level="${memberLevel(m)}" aria-label="${esc(m.state || "unknown")}"></span>
          <span class="net-chip-name">${esc(m.name)}</span>
          ${m.ip ? `<code>${esc(m.ip)}</code>` : ""}
          ${on > 1 ? `<span class="net-multi" title="On ${on} networks">×${on}</span>` : ""}
        </a></li>`;
      }).join("")}</ul>
    </article>`;
}

function netMatrix(report, count) {
  const networks = report.networks || [];
  const rows = new Map();
  networks.forEach((n, i) => n.members.forEach((m) => {
    if (!rows.has(m.name)) rows.set(m.name, { ...m, cells: new Map() });
    rows.get(m.name).cells.set(i, m.ip || "attached");
  }));
  const sorted = [...rows.values()].sort((a, b) =>
    (a.stack || "~").localeCompare(b.stack || "~") || a.name.localeCompare(b.name));
  return `<div class="net-matrix-wrap"><table class="net-matrix">
    <thead><tr><th scope="col" class="net-matrix-corner">Container</th>${networks.map((n) =>
      `<th scope="col" data-net="${esc(n.name)}" title="${esc(n.subnet || n.name)}"><span>${esc(n.name)}</span></th>`).join("")}</tr></thead>
    <tbody>${sorted.map((r) => `<tr data-member="${esc(r.name)}"${count.get(r.name) > 1 ? ' class="is-bridge"' : ""}>
      <th scope="row"><span class="net-dot" data-level="${memberLevel(r)}"></span>
        <a href="#container/${encodeURIComponent(r.name)}">${esc(r.name)}</a>${
        r.stack ? ` <span class="muted">${esc(r.stack)}</span>` : ""}</th>
      ${networks.map((n, i) => r.cells.has(i)
        ? `<td data-net="${esc(n.name)}" title="${esc(`${r.name} on ${n.name}: ${r.cells.get(i)}`)}"><span class="net-cell" aria-label="attached"></span></td>`
        : `<td data-net="${esc(n.name)}"></td>`).join("")}
    </tr>`).join("")}</tbody></table></div>`;
}

// What connects the networks: every network with a container on more than
// one, and those containers, wired up. Thirty single-network containers are
// the cards' job; this is the part that is hard to see from compose files.
function drawBridges() {
  const host = $("network-bridges");
  const report = state.network;
  if (!host || !report) return;
  const count = homes(report);
  const bridges = [...count.entries()].filter(([, n]) => n > 1).map(([name]) => name).sort();
  // Measured off the panel: the strip itself is hidden while empty.
  const width = $("panel-network").clientWidth - 32;
  if (!bridges.length || width <= 0) {
    host.innerHTML = "";
    return;
  }
  const nets = (report.networks || []).filter((n) => n.members.some((m) => bridges.includes(m.name)));
  const ROW = 34;
  const BOX = 26;
  const height = Math.max(nets.length, bridges.length) * ROW + 8;
  const boxW = Math.min(240, Math.floor(width * 0.36));
  const left = 0;
  const right = width - boxW;
  const top = (n) => (height - n * ROW) / 2 + (ROW - BOX) / 2;
  const netY = new Map(nets.map((n, i) => [n.name, top(nets.length) + i * ROW]));
  const boxY = new Map(bridges.map((b, i) => [b, top(bridges.length) + i * ROW]));
  const chars = Math.max(6, Math.floor((boxW - 22) / 7));
  const clip = (text) => (text.length > chars ? `${text.slice(0, chars - 1)}…` : text);
  const levels = new Map(nets.flatMap((n) => n.members.map((m) => [m.name, memberLevel(m)])));

  const edges = [];
  for (const n of nets) {
    for (const m of n.members) {
      if (!boxY.has(m.name)) continue;
      const y1 = netY.get(n.name) + BOX / 2;
      const y2 = boxY.get(m.name) + BOX / 2;
      const x1 = left + boxW;
      const x2 = right;
      const mid = (x1 + x2) / 2;
      edges.push(`<path class="net-edge" data-net="${esc(n.name)}" data-member="${esc(m.name)}"
        d="M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}"><title>${esc(`${m.name} on ${n.name}${m.ip ? ` at ${m.ip}` : ""}`)}</title></path>`);
    }
  }
  const node = (kind, name, x, y, label, extra = "") => `
    <g class="net-node net-node-${kind}" data-${kind === "net" ? "net" : "member"}="${esc(name)}" tabindex="0">
      <title>${esc(name)}</title>
      <rect x="${x + 0.5}" y="${y + 0.5}" width="${boxW - 1}" height="${BOX}" rx="${kind === "net" ? 13 : 6}"/>
      ${extra}
      <text x="${x + (kind === "net" ? 12 : 22)}" y="${y + BOX / 2 + 4.5}">${esc(clip(label))}</text>
    </g>`;
  host.innerHTML = `
    <h3 class="sub-head">What connects them</h3>
    <svg class="net-graph" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"
         role="img" aria-label="${esc(`${bridges.length} containers join ${nets.length} networks`)}">
      ${edges.join("")}
      ${nets.map((n) => node("net", n.name, left, netY.get(n.name), n.name)).join("")}
      ${bridges.map((b) => node("member", b, right, boxY.get(b), b,
        `<circle class="net-node-dot" data-level="${levels.get(b) || "neutral"}" cx="${right + 11}" cy="${boxY.get(b) + BOX / 2}" r="4"/>`)).join("")}
    </svg>`;
}

function findingRow(f) {
  const level = f.severity === "warning" ? "warning" : "neutral";
  const who = f.container
    ? `<a href="#container/${encodeURIComponent(f.container)}"><strong>${esc(f.container)}</strong></a> `
    : "";
  return `<li class="net-finding">${statusPill(f.severity, level)}<span>${who}${esc(f.summary)}</span></li>`;
}

function renderNetwork() {
  const report = state.network;
  if (!report) return;
  const s = report.summary || {};
  const networks = report.networks || [];
  const count = homes(report);
  $("network-sub").textContent = networks.length
    ? `${s.networks} network${s.networks === 1 ? "" : "s"}, from the last poll${
      report.inventory ? "" : " - read from the containers alone, so unused ones are missing"}`
    : "";
  $("network-stats").innerHTML = networks.length || (report.host_mode || []).length ? [
    ["Networks", s.networks],
    ["On several", s.multi_homed],
    ["Published ports", s.published_ports],
    ["Host network", s.host_mode],
    ...(report.inventory ? [["Unused", s.unused]] : []),
  ].map(([label, value]) => `<div class="net-stat"><span>${label}</span><strong>${value ?? 0}</strong></div>`).join("") : "";

  const findings = report.findings || [];
  $("network-findings").innerHTML = findings.length
    ? `<ul class="net-finding-list">${findings.map(findingRow).join("")}</ul>` : "";

  const special = [];
  if ((report.host_mode || []).length) {
    special.push(`<p class="net-special"><strong>Host network</strong> - no isolation, listens on the
      host's own addresses: ${report.host_mode.map((n) => `<a href="#container/${encodeURIComponent(n)}">${esc(n)}</a>`).join(", ")}</p>`);
  }
  for (const entry of report.shared_stack || []) {
    special.push(`<p class="net-special"><a href="#container/${encodeURIComponent(entry.name)}">${esc(entry.name)}</a>
      uses the network of <strong>${esc(entry.via)}</strong>, and goes down with it.</p>`);
  }
  if ((report.isolated || []).length) {
    special.push(`<p class="net-special"><strong>No network</strong>: ${report.isolated.map(esc).join(", ")}</p>`);
  }

  const map = $("network-map");
  if (!networks.length) {
    map.innerHTML = special.join("") || `<p class="empty">No networks seen yet - they appear after the first poll.</p>`;
  } else {
    map.innerHTML = (compact("network") ? netMatrix(report, count)
      : `<div class="net-grid">${networks.map((n) => netCard(n, count)).join("")}</div>`) + special.join("");
  }
  drawBridges();
  renderPorts();
}

function renderPorts() {
  const exposed = (state.network || {}).exposed || [];
  const rows = exposed.flatMap((e) => e.ports.map((p) => ({ ...p, name: e.name, state: e.state, health: e.health })))
    .sort((a, b) => Number(a.host_port) - Number(b.host_port) || a.proto.localeCompare(b.proto));
  const wide = rows.filter((r) => ["0.0.0.0", "::"].includes(r.host_ip)).length;
  $("ports-sub").textContent = rows.length
    ? `${rows.length} on the host · ${wide} on every interface` : "";
  $("network-ports").innerHTML = rows.length ? `
    <div class="net-matrix-wrap"><table class="net-ports">
      <thead><tr><th scope="col">Host port</th><th scope="col">Container</th><th scope="col">Inside</th><th scope="col">Listens on</th></tr></thead>
      <tbody>${rows.map((r) => {
        const everywhere = ["0.0.0.0", "::"].includes(r.host_ip);
        return `<tr>
          <td><code>${esc(r.host_port)}</code><span class="muted">/${esc(r.proto)}</span></td>
          <td><span class="net-dot" data-level="${memberLevel(r)}"></span><a href="#container/${encodeURIComponent(r.name)}">${esc(r.name)}</a></td>
          <td><code>${esc(r.container_port)}</code></td>
          <td>${everywhere ? "every interface" : `<code>${esc(r.host_ip)}</code>`}</td>
        </tr>`;
      }).join("")}</tbody></table></div>`
    : `<p class="empty">Nothing is published on the host - everything is reached through a network.</p>`;
}

// Pointing at a container or a network lights it up everywhere it appears:
// every card it is on, its row and column in the matrix, its wires.
function netHighlight(event) {
  const panel = $("panel-network");
  if (!panel || !panel.contains(event.target)) return;
  const hit = event.target.closest("[data-member],[data-net]");
  panel.querySelectorAll(".is-hot").forEach((el) => el.classList.remove("is-hot"));
  panel.classList.toggle("is-pointing", Boolean(hit));
  if (!hit) return;
  const { member, net } = hit.dataset;
  // A chip names both: it is on the card for its network. The chip wins.
  const selector = member ? `[data-member="${CSS.escape(member)}"]` : `[data-net="${CSS.escape(net)}"]`;
  panel.querySelectorAll(selector).forEach((el) => el.classList.add("is-hot"));
  if (member) {
    // The networks it is on light up too.
    panel.querySelectorAll(`.net-edge${selector}`).forEach((edge) =>
      panel.querySelectorAll(`.net-node-net[data-net="${CSS.escape(edge.dataset.net)}"]`)
        .forEach((el) => el.classList.add("is-hot")));
  } else {
    panel.querySelectorAll(`.net-edge${selector}`).forEach((edge) =>
      panel.querySelectorAll(`.net-node-member[data-member="${CSS.escape(edge.dataset.member)}"]`)
        .forEach((el) => el.classList.add("is-hot")));
  }
}

function imageUpdates() {
  return state.snapshot.image_updates || {};
}

const LEVEL_WORDS = {
  major: "major", minor: "minor", patch: "patch", build: "new build", release: "newer release",
};

// Name -> the words for the card, for every container whose image has
// something newer: a new build of its tag, a newer version, or both.
function staleImages() {
  const stale = new Map();
  for (const entry of imageUpdates().images || []) {
    const words = entry.newer ? `${entry.newer.tag} available`
      : entry.state === "update" ? "update available" : "";
    if (words) (entry.containers || []).forEach((n) => stale.set(n, words));
  }
  return stale;
}

function behindImages(report) {
  return (report.images || []).filter((i) => i.state === "update" || i.newer);
}

// Links that arrive from GitHub's API. Escaping keeps them inside the
// attribute; this keeps a javascript: or data: URL out of it altogether.
function safeUrl(url) {
  return /^https:\/\//i.test(String(url || "")) ? String(url) : "";
}

// The newer version, how big a step it is, and what its notes warn about.
// With `entry`, the notes open in place on request; without it (the
// container view) they are a link.
function newerLine(raw, entry = null) {
  if (!raw) return "";
  const newer = { ...raw, notes_url: safeUrl(raw.notes_url), breaking_url: safeUrl(raw.breaking_url) };
  const levelName = newer.level === "major" ? "warning" : "neutral";
  const pill = `<span class="status" data-level="${levelName}">${esc(newer.tag)} · ${esc(LEVEL_WORDS[newer.level] || newer.level)}</span>`;
  const behind = newer.behind > 1 ? ` <span class="muted">${newer.behind} releases behind</span>` : "";
  const notes = entry?.source ? notesToggle(entry, newer.tag)
    : newer.notes_url
      ? ` <a class="notes-link" href="${esc(newer.notes_url)}" target="_blank" rel="noopener noreferrer">release notes</a>` : "";
  const safer = newer.same_major
    ? `<p class="image-note">Without the major step: <code>${esc(newer.same_major)}</code></p>` : "";
  const warning = newer.breaking
    ? `<p class="image-note image-warn"><strong>The notes mention:</strong> ${esc(newer.breaking)}${
        newer.breaking_url ? ` <a href="${esc(newer.breaking_url)}" target="_blank" rel="noopener noreferrer">read</a>` : ""}</p>` : "";
  return `<div class="image-newer">${pill}${behind}${notes}</div>${safer}${warning}`;
}

// The containers running an image, each opening its detail view, with the
// stack each sits in - the compose file a version bump lands in.
function containerLinks(names) {
  const byName = new Map((state.snapshot.containers || []).map((c) => [c.name, c]));
  return (names || []).map((n) => {
    const stack = byName.get(n)?.stack;
    return `<a class="image-user" href="#container/${encodeURIComponent(n)}">${esc(n)}</a>${
      stack ? ` <span class="image-stack" title="Stack ${esc(stack)}">in ${esc(stack)}</span>` : ""}`;
  }).join(", ");
}

// --- release notes, read on request -----------------------------------------
//
// The check only learns whether the notes warn about something; the notes
// themselves are fetched when someone opens them, and kept here so the
// re-render every poll brings does not fold them shut again.

function notesKey(image, tag) {
  return `${image}\u0000${tag}`;
}

function notesToggle(entry, tag) {
  const key = notesKey(entry.image, tag);
  const open = state.notes.has(key);
  return ` <button type="button" class="link-btn notes-toggle" data-notes-image="${esc(entry.image)}"
    data-notes-tag="${esc(tag)}" aria-expanded="${open}">${open ? "Hide release notes" : "Release notes"}</button>`;
}

async function toggleNotes(image, tag) {
  const key = notesKey(image, tag);
  if (state.notes.has(key)) {
    state.notes.delete(key);
    renderImages();
    rerenderDetail();
    return;
  }
  state.notes.set(key, { loading: true });
  renderImages();
  rerenderDetail();
  try {
    const notes = await api(`/api/images/notes?image=${encodeURIComponent(image)}&tag=${encodeURIComponent(tag)}`);
    if (state.notes.has(key)) state.notes.set(key, { notes });
  } catch (error) {
    if (state.notes.has(key)) state.notes.set(key, { error: error.message });
  }
  renderImages();
  rerenderDetail();
}

function notesPanel(entry, tag) {
  const open = tag ? state.notes.get(notesKey(entry.image, tag)) : null;
  if (!open) return "";
  if (open.loading) {
    return `<div class="image-notes"><p class="muted">Reading the release notes from GitHub…</p></div>`;
  }
  const source = safeUrl(open.notes?.url) || (entry.source ? `https://github.com/${entry.source}/releases` : "");
  const sourceLink = source ? `<a class="btn btn-small notes-source" href="${esc(source)}" target="_blank"
    rel="noopener noreferrer">Open on GitHub <span aria-hidden="true">↗</span></a>` : "";
  if (open.error) {
    return `<div class="image-notes"><div class="notes-head"><span class="muted">${esc(open.error)}</span>${sourceLink}</div></div>`;
  }
  const notes = open.notes;
  const releases = notes.releases || [];
  const head = `<div class="notes-head"><span class="muted">${releases.length
    ? `${releases.length} release${releases.length === 1 ? "" : "s"} after <code>${esc(notes.current)}</code>, newest first`
    : esc(notes.detail || "No release notes.")}</span>${sourceLink}</div>`;
  return `<div class="image-notes">${head}${releases.map((r) => {
    const url = safeUrl(r.url);
    const when = r.published_at ? new Date(r.published_at) : null;
    const date = when && !Number.isNaN(when.getTime())
      ? `<span class="muted">${esc(when.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" }))}</span>` : "";
    const title = r.name && r.name !== r.tag ? `${esc(r.tag)} <span class="muted">${esc(r.name)}</span>` : esc(r.tag);
    return `<article class="release">
      <header class="release-head">
        <strong>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${title}</a>` : title}</strong>
        ${date}${r.breaking ? statusPill("mentions breaking changes", "warning") : ""}
      </header>
      <div class="release-body">${notesHtml(r.body) || `<p class="muted">No notes for this release.</p>`}${
        r.truncated && url ? `<p><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Continued on GitHub</a></p>` : ""}</div>
    </article>`;
  }).join("")}</div>`;
}

// Release notes are Markdown written by strangers: everything is escaped,
// raw HTML is dropped, and only https links survive. Enough of Markdown to
// read a changelog - headings, lists, emphasis, code, links - and no more.
function notesHtml(markdown) {
  const text = String(markdown || "").replace(/\r\n?/g, "\n")
    .replace(/<!--[\s\S]*?-->/g, "");
  const out = [];
  let list = null;  // "ul" | "ol" while inside one
  let para = [];
  const flushPara = () => {
    if (para.length) out.push(`<p>${inlineMd(para.join(" "))}</p>`);
    para = [];
  };
  const closeList = () => {
    if (list) out.push(`</${list}>`);
    list = null;
  };
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const fence = line.match(/^\s*(```|~~~)/);
    if (fence) {
      flushPara(); closeList();
      const code = [];
      for (i += 1; i < lines.length && !lines[i].trim().startsWith(fence[1]); i += 1) code.push(lines[i]);
      out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = line.match(/^\s*#{1,6}\s+(.*)$/);
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (heading) {
      flushPara(); closeList();
      out.push(`<p class="release-h">${inlineMd(heading[1].replace(/\s+#+\s*$/, ""))}</p>`);
    } else if (bullet || numbered) {
      flushPara();
      const kind = bullet ? "ul" : "ol";
      if (list !== kind) { closeList(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inlineMd((bullet || numbered)[1])}</li>`);
    } else if (!line.trim()) {
      flushPara(); closeList();
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flushPara(); closeList();
    } else {
      if (list) closeList();
      para.push(line.trim().replace(/^>\s?/, ""));
    }
  }
  flushPara(); closeList();
  return out.join("");
}

function inlineMd(raw) {
  const kept = [];
  const keep = (html) => `\u0000${kept.push(html) - 1}\u0000`;
  let text = String(raw)
    .replace(/<\/?[a-zA-Z][^>]*>/g, "")  // raw HTML: dropped, its text kept
    .replace(/`([^`]+)`/g, (_, code) => keep(`<code>${esc(code)}</code>`))
    // An image is shown as its link: nothing loads from a changelog.
    .replace(/!?\[([^\]]*)\]\(([^)\s]+)[^)]*\)/g, (_, label, url) => {
      const href = safeUrl(url);
      return href ? keep(`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(label || href)}</a>`)
        : label;
    })
    .replace(/https:\/\/[^\s<>()]+[^\s<>().,;:!?'"]/g, (url) =>
      keep(`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url.replace(/^https:\/\/(www\.)?github\.com\//, ""))}</a>`));
  text = esc(text)
    .replace(/\*\*([^*]+)\*\*|__([^_]+)__/g, (_, a, b) => `<strong>${a || b}</strong>`)
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  return text.replace(/\u0000(\d+)\u0000/g, (_, n) => kept[Number(n)]);
}

// The button spins for as long as a check runs - whoever started it - and
// the state stream says when that is, so every open tab agrees. At rest it
// says how old the last check is.
function renderImageRefresh(report) {
  const button = $("images-refresh");
  const busy = Boolean(report.checking || state.imageRefreshing);
  button.disabled = busy;
  button.classList.toggle("is-busy", busy);
  button.setAttribute("aria-busy", String(busy));
  const progress = report.checking ? report.progress : null;
  const total = progress?.total || 0;
  const done = Math.min(progress?.done || 0, total);
  button.querySelector("span").textContent = !busy ? "Check now"
    : total ? `Checking ${done}/${total}…` : "Checking…";
  const age = button.querySelector(".refresh-age");
  const checked = report.checked_at;
  age.hidden = busy || !checked;
  age.dataset.ago = checked || "";
  age.textContent = checked ? ago(checked) : "";
  button.title = checked && !busy
    ? `Last checked ${new Date(checked).toLocaleString()} - check the registries again` : "";
  const current = progress?.current;
  renderProgress("image-progress", busy, done, total, !total
    ? "Starting the registry check…"
    : current ? `${done} of ${total} checked · asking about ${current}`
    : `${done} of ${total} checked · wrapping up`);
}

// A bar and what it is waiting on: thirty registries or thirty `docker
// stats` take a while, and a spinner alone does not say whether it is nearly
// done or stuck. No total yet means it has not counted what to do.
function renderProgress(id, busy, done, total, words) {
  const box = $(id);
  if (!box) return;
  box.hidden = !busy;
  if (!busy) return;
  const bar = box.querySelector(".progress-bar");
  const pct = total ? Math.round((done / total) * 100) : 0;
  bar.classList.toggle("is-indeterminate", !total);
  bar.querySelector("span").style.width = total ? `${pct}%` : "";
  if (total) bar.setAttribute("aria-valuenow", String(pct));
  else bar.removeAttribute("aria-valuenow");
  box.querySelector(".progress-text").textContent = words;
}

// Reads run several at once, so "what it is on" is a few names, not one.
function namesList(names) {
  const shown = names.slice(0, 3).join(", ");
  return names.length > 3 ? `${shown} +${names.length - 3}` : shown;
}

// Like the image check: the button spins while any poll runs, whoever
// started it, and the stream says when that is.
function renderPollNow() {
  const button = $("poll-now");
  if (!button) return;
  const busy = Boolean(state.snapshot.polling || state.pollRequested);
  button.disabled = busy;
  button.classList.toggle("is-busy", busy);
  button.setAttribute("aria-busy", String(busy));
  const progress = state.snapshot.polling ? state.snapshot.poll_progress : null;
  const total = progress?.total || 0;
  const done = Math.min(progress?.done || 0, total);
  const reading = progress?.reading || [];
  button.querySelector("span").textContent = !busy ? "Poll now"
    : total ? `Polling ${done}/${total}…` : "Polling…";
  renderProgress("poll-progress", busy, done, total, !total
    ? "Listing containers…"
    : reading.length ? `${done} of ${total} read · waiting on ${namesList(reading)}`
    : done >= total ? `${done} of ${total} read · checking for problems`
    : `${done} of ${total} read`);
}

async function pollNow() {
  state.pollRequested = true;
  renderPollNow();
  try {
    await api("/api/poll", { method: "POST" });
  } catch (error) {
    toast(error.message, "error");
  } finally {
    // From here the stream's `polling` flag carries the spinner.
    state.pollRequested = false;
    renderPollNow();
  }
}

async function refreshImages() {
  state.imageRefreshing = true;
  renderImageRefresh(imageUpdates());
  try {
    const result = await api("/api/images/refresh", { method: "POST" });
    if (!result.started && !result.checking) toast(result.reason || "Not started.");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    // From here the stream's `checking` flag carries the spinner.
    state.imageRefreshing = false;
    renderImageRefresh(imageUpdates());
  }
}

// The deploy workflow has the manager recreate every container in the stack,
// not only the one with the new build, so the question names them all.
async function redeployStack(button) {
  const { redeploy: container, redeployStack: stack, redeployMembers: members } = button.dataset;
  if (!confirm(`Redeploy stack ${stack}?\n\nThis runs the Deploy stacks workflow on GitHub, `
    + `and ${dockerManager() || "the stack's manager"} recreates ${members}. Afterwards the doctor checks that the new build `
    + "is running.")) return;
  button.disabled = true;
  try {
    await api("/api/redeploy", { method: "POST", body: JSON.stringify({ container }) });
    toast(`Redeploying ${stack} through GitHub - the result lands in the Activity log.`);
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
  }
}

// While a check runs, each image's fresh answer replaces the last check's as
// it arrives; the images not reached yet keep the last check's answer, marked
// stale, so the list fills in rather than emptying and refilling at the end.
function imagesSoFar(report) {
  const previous = report.images || [];
  if (!report.checking) return previous;
  const partial = report.partial || [];
  const fresh = new Set(partial.map((i) => i.image));
  return [...partial, ...previous.filter((i) => !fresh.has(i.image)).map((i) => ({ ...i, stale: true }))];
}

// Stands in for the images the running check has not answered yet.
function pendingImagesRow(report) {
  if (!report.checking) return "";
  const total = report.progress?.total || 0;
  const left = total ? Math.max(total - (report.partial || []).length, 0) : null;
  if (left === 0) return "";
  const words = left === null ? "Listing the images to check…"
    : `${left} image${left === 1 ? "" : "s"} still to check`;
  return `
    <div class="image-row image-pending">
      <div class="image-main">
        <span class="skeleton" style="width:52%;--i:0"></span>
        <span class="skeleton" style="width:24%;--i:1"></span>
        <span class="image-users">${esc(words)}</span>
      </div>
    </div>`;
}

// The stacks an image's containers belong to, and whether each can be
// redeployed from here.
function imageStacks(entry) {
  const report = imageUpdates();
  const byName = new Map((state.snapshot.containers || []).map((c) => [c.name, c]));
  const stacks = new Set((entry.containers || []).map((n) => byName.get(n)?.stack).filter(Boolean));
  // Unknown (null) until the doctor has read the repository: then all.
  const known = report.redeploy_stacks ? new Set(report.redeploy_stacks) : null;
  return [...stacks].sort().map((stack) => ({
    stack,
    allowed: Boolean(report.redeploy) && (!known || known.has(stack)),
    busy: Boolean((report.redeploying || {})[stack]),
    runUrl: safeUrl((report.redeploying || {})[stack]?.run_url),
    members: (state.snapshot.containers || []).filter((c) => c.stack === stack).map((c) => c.name),
    container: (entry.containers || []).find((n) => byName.get(n)?.stack === stack),
  }));
}

function redeployButtons(entry) {
  if (entry.state !== "update" || entry.stale) return "";
  return imageStacks(entry).filter((s) => s.allowed).map((s) => `${s.busy && s.runUrl
    ? `<a class="notes-link" href="${esc(s.runUrl)}" target="_blank" rel="noopener noreferrer">view run</a>` : ""}
    <button type="button" class="btn btn-small refresh-btn${s.busy ? " is-busy" : ""}"
      data-redeploy="${esc(s.container)}" data-redeploy-stack="${esc(s.stack)}"
      data-redeploy-members="${esc(s.members.join(", "))}" ${s.busy ? "disabled aria-busy=\"true\"" : ""}
      title="Redeploy stack ${esc(s.stack)} through the Deploy stacks workflow: ${esc(s.members.join(", "))}">
      <svg class="icon" aria-hidden="true"><use href="#i-refresh"/></svg><span>${
        s.busy ? "Redeploying…" : `Redeploy ${esc(s.stack)}`}</span></button>`).join("");
}

function renderImages() {
  if (!state.snapshotLoaded) return;
  const report = imageUpdates();
  renderImageRefresh(report);
  if (!report.checked_at && !report.checking) {
    $("images-more").innerHTML = "";
    $("image-updates").innerHTML = `<p class="empty">Not checked yet - the first registry check runs shortly after start.</p>`;
    renderCooling([]);
    renderUnchecked({ images: [] });
    return;
  }

  const images = imagesSoFar(report);
  const behind = behindImages({ images });
  const rows = behind.slice(0, state.shown.images);
  $("images-more").innerHTML = pager("images", rows.length, behind.length, pageSize("images"));
  // "Everything is up to date" would be a lie when nothing could be read.
  const compared = images.filter((i) => i.state === "current" || i.state === "update" || i.newer !== undefined).length;
  const empty = report.checking
    ? (images.length ? "Nothing newer found so far." : "")
    : compared
      ? "Everything that could be checked is up to date."
      : "Nothing could be compared against a registry.";
  const pending = pendingImagesRow(report);
  $("image-updates").innerHTML = (rows.length ? rows.map((entry) => `
    <div class="image-row${entry.stale ? " is-stale" : ""}"${
      entry.stale ? ` title="From the last check - not re-checked yet"` : ""}>
      <div class="image-main">
        <code>${esc(entry.image)}</code>
        <span class="image-users">${containerLinks(entry.containers)}</span>
        ${newerLine(entry.newer, entry)}
      </div>
      <div class="image-side">
        ${entry.state === "update" ? `<span class="status" data-level="warning"
          title="The registry publishes a different image for this tag">new build</span>` : ""}
        ${redeployButtons(entry)}
      </div>
    </div>${notesPanel(entry, entry.newer?.tag)}`).join("")
    : empty ? `<p class="image-none">${empty}</p>` : "") + pending;
  renderCooling(images);
  renderUnchecked({ ...report, images });
}

// Releases still inside the update cooldown: there to be seen, not taken.
// Dependabot holds them back too, so they count towards nothing - no badge,
// no container card, no briefing - until the check after they settle.
function renderCooling(images) {
  const cooling = images.filter((i) => i.cooling);
  const box = $("image-cooling");
  box.hidden = !cooling.length;
  box.innerHTML = cooling.length ? `
    <p class="image-section-head">Cooling down <span class="muted">· not proposed yet</span></p>
    ${cooling.map((entry) => {
      return `
      <div class="image-row${entry.stale ? " is-stale" : ""}">
        <div class="image-main">
          <code>${esc(entry.image)}</code>
          <span class="image-users">${containerLinks(entry.containers)}</span>
          ${coolingLine(entry)}
        </div>
      </div>${notesPanel(entry, entry.cooling.tag)}`;
    }).join("")}` : "";
}

// A release still inside the cooldown: its tag, how big a step, and the
// day it is proposed.
function coolingLine(entry) {
  const ready = new Date(entry.cooling.ready_at);
  const until = Number.isNaN(ready.getTime()) ? ""
    : ` <span class="muted">until ${esc(ready.toLocaleDateString([], { month: "short", day: "numeric" }))}</span>`;
  const level = LEVEL_WORDS[entry.cooling.level] || entry.cooling.level || "";
  return `<div class="image-newer"><span class="status" data-level="neutral">${esc(entry.cooling.tag)}${
    level ? ` · ${esc(level)}` : ""}</span>${until}${entry.source ? notesToggle(entry, entry.cooling.tag) : ""}</div>`;
}

// What the check could not answer, and why: a private registry, a rate
// limit, an image built on the host. Not counted as up to date anywhere.
function renderUnchecked(report) {
  const box = $("image-unchecked");
  const unchecked = (report.images || []).filter((i) => i.state === "unknown" || i.state === "denied");
  if (report.checking || !unchecked.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  const head = `<p class="image-section-head">Could not be checked <span class="muted">· ${unchecked.length}</span></p>`;
  if (report.needs_images_permission) {
    // Every image for the same reason: one sentence, not thirty rows of it.
    box.innerHTML = `${head}<p class="image-note">New builds are not checked: the socket proxy runs
      with <code>IMAGES=0</code>, so the doctor cannot read which build each image is.
      Newer versions are still found from the registries' tags.</p>`;
    return;
  }
  box.innerHTML = head + unchecked.map((entry) => `
    <div class="image-row">
      <div class="image-main">
        <code>${esc(entry.image)}</code>
        <span class="image-users">${containerLinks(entry.containers)}</span>
        <p class="image-note">${esc(entry.state === "denied"
          ? "A private registry - it needs credentials the doctor does not hold."
          : (entry.detail || "unknown").replace(/^./, (c) => c.toUpperCase()))}</p>
      </div>
      ${statusPill(entry.state === "denied" ? "needs credentials" : "unknown", "neutral")}
    </div>`).join("");
}

// The lines the rule fired on, shown outright: they are the reason the
// incident exists, and hiding them behind a disclosure meant reading a
// diagnosis without the evidence it was drawn from.
// One line of history for a condition that keeps firing: how often, and the
// span it has been going, rather than a fresh incident for every sighting.
function recurrence(incident) {
  const seen = Number(incident.occurrences) || 1;
  if (seen < 2) return agoTag(incident.opened_at);
  const last = incident.last_seen_at || incident.updated_at;
  return `seen ${seen}\u00d7 \u00b7 first ${agoTag(incident.opened_at)} \u00b7 last ${agoTag(last)}`;
}

function triggerLines(incident) {
  const matched = incident.evidence?.details?.matched_lines;
  if (!Array.isArray(matched) || !matched.length) return "";
  const total = incident.evidence?.details?.match_count || matched.length;
  const more = total > matched.length ? ` (showing ${matched.length} of ${total})` : "";
  return `<div class="trigger">
    <strong class="trigger-head">Lines that triggered this${esc(more)}</strong>
    <div class="evidence">${matched.map((line) => esc(String(line))).join("\n")}</div>
  </div>`;
}

// "all" is the absence of a filter; every other chip is a status the server
// understands, so the page count and the total describe the same set.
function incidentQuery() {
  return state.filter === "all" ? "" : `&status=${encodeURIComponent(state.filter)}`;
}

// A dependency that is itself unwell. Shown next to the diagnosis because it
// is the first place to look, whatever the model concluded.
function rootCauseHint(incident) {
  const root = incident.evidence?.likely_root_cause;
  if (!root) return "";
  return `<span class="root-hint">depends on <code>${esc(root.service)}</code>,`
       + ` which is ${esc(root.status)}</span>`;
}

// What the operator told the model, shown above what the model made of it:
// the diagnosis reads differently once you know it was given "I am migrating
// the database right now".
function operatorNote(d) {
  const note = d?.operator_context;
  if (!note?.text) return "";
  return `<figure class="operator-note">
    <figcaption>Context from ${esc(note.by || "the operator")} \u00b7 ${agoTag(note.at)}</figcaption>
    <blockquote>${esc(note.text)}</blockquote>
  </figure>`;
}

const DIAGNOSE_HINT = "Anything the logs cannot show: a change you just made, a planned "
  + "migration, an upstream outage, what you have already tried.";

function diagnoseForm(incident) {
  const id = incident.id;
  const previous = incident.diagnosis?.operator_context?.text || "";
  const draft = state.diagnoseDrafts.has(id) ? state.diagnoseDrafts.get(id) : previous;
  return `<form class="diagnose-form" data-diagnose-form="${esc(id)}">
    <label for="context-${esc(id)}">Additional context <span class="muted">(optional)</span></label>
    <textarea id="context-${esc(id)}" data-context-draft="${esc(id)}" rows="3" maxlength="2000"
      placeholder="${esc(DIAGNOSE_HINT)}">${esc(draft)}</textarea>
    <p class="diagnose-note muted">Redacted like the logs, then sent to the model with the evidence.</p>
    <div class="action-bar">
      <button type="submit" class="btn btn-primary">Run diagnosis</button>
      <button type="button" class="btn btn-quiet" data-diagnose-cancel="${esc(id)}">Cancel</button>
    </div>
  </form>`;
}

function renderIncidents() {
  const host = $("incidents");
  // Someone is typing context into a re-diagnose form: replacing the list now
  // would take the textarea, and the caret, out from under them. The next
  // refresh after they leave it catches up.
  if (document.activeElement?.matches?.("#incidents textarea")) return;
  if (!state.incidentsLoaded && !state.focusIncident) return;
  // A linked incident stays in view whatever the filter says: it is the one
  // the notification was about.
  const focus = state.focusIncident;
  const listed = focus && !state.incidents.some((i) => i.id === focus.id)
    ? [focus, ...state.incidents] : state.incidents;
  // A dismissal still on its way to the server: shown as done already, so a
  // refresh that set off before it landed cannot bring the row back.
  const keepDismissed = state.filter === "all" || state.filter === "dismissed";
  const rows = listed
    .map((i) => (state.dismissing.has(i.id) ? { ...i, status: "dismissed" } : i))
    .filter((i) => keepDismissed || !state.dismissing.has(i.id) || i.id === focus?.id);
  const total = Math.max(rows.length, state.incidentTotal - (listed.length - rows.length));
  $("incidents-more").innerHTML =
    pager("incidents", rows.length, total, pageSize("incidents"), MAX_ROWS);

  if (!rows.length) {
    host.innerHTML = `<p class="empty">${{
      active: "Nothing open. That is the good outcome.",
      awaiting_approval: "Nothing is waiting on you.",
      resolved: "Nothing resolved yet.",
      dismissed: "Nothing dismissed.",
      all: "No incidents recorded yet - that is the good outcome.",
    }[state.filter] || "Nothing here."}</p>`;
    return;
  }

  host.innerHTML = rows.map((incident) => {
    const d = incident.diagnosis;
    const open = state.expanded.has(incident.id);
    const pending = (incident.actions || []).find((a) => a.status === "pending");
    const sev = d?.severity || incident.severity;

    const body = open ? `
      <div class="incident-body">
        ${classificationNote(incident)}
        ${operatorNote(d)}
        ${d ? `
          <dl class="dl">
            <dt>Root cause</dt><dd>${esc(d.root_cause)}${rootCauseHint(incident)}</dd>
            <dt>Proposed</dt><dd>${esc(ACTION_LABELS[d.recommended_action] || d.recommended_action)} &mdash; ${esc(d.action_rationale)}</dd>
            <dt>Transient</dt><dd>${d.is_transient ? "likely clears on its own" : "needs attention"}</dd>
          </dl>
          <div class="confidence">
            <span>Confidence ${(Number(d.confidence) * 100).toFixed(0)}%</span>
            <div class="meter"><div class="meter-fill" style="width:${Math.min(100, Number(d.confidence) * 100)}%"></div></div>
            ${d.from_cache ? `<span>(reused from an identical earlier incident)</span>` : ""}
          </div>
          ${(d.human_fix_steps || []).length ? `<div><strong style="font-size:13px">What you should do</strong>
            <ol class="steps">${d.human_fix_steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol></div>` : ""}
          ${(d.evidence_used || []).length ? `<div class="evidence">${d.evidence_used.map(esc).join("\n")}</div>` : ""}
        ` : `<p class="panel-sub">No diagnosis recorded. ${esc((incident.actions || [])[0]?.reason || "")}</p>`}
        ${triggerLines(incident)}
        ${incident.evidence?.logs ? `<details><summary class="log-summary">Full log tail at detection</summary>
          <div class="evidence" style="margin-top:8px">${esc(incident.evidence.logs)}</div></details>` : ""}
        <div class="log-bar">
          ${incident.container_name.startsWith("host:") ? ""
            : `<button type="button" class="btn btn-small" data-tail="${esc(incident.container_name)}">Fetch live tail</button>`}
          <button type="button" class="btn btn-small" data-context="${esc(incident.id)}">&plusmn;5 min around detection</button>
          ${(Number(incident.occurrences) || 1) > 1
            ? `<button type="button" class="btn btn-small" data-history="${esc(incident.id)}">Sighting history (${Number(incident.occurrences)})</button>` : ""}
        </div>
        <div class="log-out" id="logout-${esc(incident.id)}"></div>
        <div class="action-bar">
          ${pending ? `
            <button type="button" class="btn btn-primary" data-approve="${esc(pending.id)}">
              Approve: ${esc(ACTION_LABELS[pending.action] || pending.action)}
            </button>
            <button type="button" class="btn" data-reject="${esc(pending.id)}">Reject</button>` : ""}
          ${incident.status !== "dismissed" && incident.status !== "resolved"
            ? `<button type="button" class="btn" data-dismiss="${esc(incident.id)}">Dismiss</button>` : ""}
          ${state.diagnoseOpen.has(incident.id) ? "" : `<button type="button" class="btn" data-diagnose-open="${esc(incident.id)}">Re-diagnose</button>`}
          <button type="button" class="btn btn-quiet" data-copy-link="${esc(incident.id)}">Copy link</button>
        </div>
        <div class="handoff-bar">
          <button type="button" class="btn btn-small" data-handoff="${esc(incident.id)}">Copy for an LLM</button>
          <a class="handoff-download" href="/api/incidents/${encodeURIComponent(incident.id)}/handoff?download=1"
             download="${esc(handoffName(incident.id))}">Download .md</a>
          <span class="muted">everything the doctor has on this, as Markdown for your own agent</span>
        </div>
        ${state.diagnoseOpen.has(incident.id) ? diagnoseForm(incident) : ""}
      </div>` : "";

    return `
      <div class="incident ${open ? "is-open" : ""}${focus?.id === incident.id ? " is-linked" : ""}"
           id="incident-${esc(incident.id)}">
        <button type="button" class="incident-head" data-toggle="${esc(incident.id)}" aria-expanded="${open}">
          <span class="chevron" aria-hidden="true">&#9656;</span>
          <span class="incident-title">
            <strong>${esc(d?.summary || incident.summary)}</strong>
            <span class="incident-meta">
              <code>${esc(incident.container_name)}</code> · ${esc(incident.kind.replace(/_/g, " "))}
              · ${recurrence(incident)}
              ${pending ? " · awaiting approval" : ""}
              ${d?.operator_context?.text ? " · with your context" : ""}
            </span>
          </span>
          ${statusPill(incident.status.replace(/_/g, " "), incident.status === "resolved" ? "good"
            : incident.status === "failed" ? "critical"
            : incident.status === "dismissed" ? "neutral"
            : SEVERITY_LEVEL[sev] || "warning")}
        </button>
        ${body}
      </div>`;
  }).join("");
}

// Reasons are clamped to two lines; the toggle appears only where the clamp
// actually cut something, which depends on the width, not on the length - a
// reason that wraps to five lines on a phone fits in two on a desktop.
function markClamps() {
  for (const row of document.querySelectorAll("#actions .audit-row")) {
    const reason = row.querySelector(".a-reason");
    const toggle = row.querySelector(".a-more");
    if (!reason || !toggle) continue;
    if (toggle.getAttribute("aria-expanded") === "true") {
      toggle.hidden = false;
      continue;
    }
    toggle.hidden = reason.scrollHeight <= reason.clientHeight + 1;
  }
}

function renderActions() {
  if (!state.actionsLoaded) return;
  const rows = state.actions;
  const body = $("actions");
  $("actions-more").innerHTML =
    pager("actions", rows.length, state.actionTotal, pageSize("actions"), MAX_ROWS);
  if (!rows.length) {
    body.innerHTML = `<li class="empty">No actions recorded yet.</li>`;
    return;
  }
  const outcomeLevel = {
    succeeded: "good", failed: "critical", rejected: "neutral",
    skipped: "neutral", pending: "warning", executing: "serious",
  };
  body.innerHTML = rows.map((a) => {
    // Why it was done, then how it went: a failed redeploy's result is the
    // only place that says what is still running the old build.
    const reason = [a.reason, a.result].filter((t) => t && t !== "started").join(" → ");
    const open = state.openReasons.has(a.id);
    return `
    <li class="audit-row"${reason ? ` title="${esc(reason)}"` : ""}>
      <span class="a-when" title="${esc(a.created_at)}">${agoTag(a.created_at)}</span>
      <span class="a-container">${esc(a.container_name)}</span>
      <span class="a-action">${esc(ACTION_LABELS[a.action] || a.action)}
        <span class="a-by">by ${esc(a.decided_by)}</span></span>
      <span class="a-outcome">${statusPill(a.status, outcomeLevel[a.status] || "neutral")}</span>
      ${reason ? `<p class="a-reason${open ? "" : " is-clamped"}">${esc(reason)}</p>
        <button type="button" class="a-more" data-reason="${esc(a.id)}" hidden
          aria-expanded="${open}">${open ? "less" : "more"}</button>` : ""}
    </li>`;
  }).join("");
  markClamps();
}

// --- what changed --------------------------------------------------------------

const CHANGE_LEVEL = {
  recreated: "warning", restarted: "neutral", started: "good", stopped: "critical",
  appeared: "neutral", removed: "neutral", renamed: "neutral", rebooted: "warning",
};

// Host-level rows are filed under host:<what>; they link to their page, not
// to a container view that has nothing to show.
function containerLink(name) {
  if (name === "host:system" || name === "host:nas") return `<a href="#/host">${esc(hostTitle() === "The host" ? "host" : hostTitle())}</a>`;
  if (name === "host:network") return `<a href="#/network">network</a>`;
  if (name.startsWith("host:")) return `<a href="#/storage">${esc(name)}</a>`;
  return `<a href="#container/${encodeURIComponent(name)}">${esc(name)}</a>`;
}

function changeKind(c) {
  const d = c.detail || {};
  if (c.kind === "recreated") {
    return { build: "new build", version: "new version", same: "recreated" }[d.change] || "recreated";
  }
  return c.kind;
}

function changeCount(c) {
  return Number(c.count) > 1 ? ` <span class="muted">(${Number(c.count)} times)</span>` : "";
}

function renderChanges() {
  if (!state.changesLoaded) return;
  const rows = state.changes;
  const body = $("changes");
  $("changes-more").innerHTML =
    pager("changes", rows.length, state.changeTotal, pageSize("changes"), MAX_ROWS);
  if (!rows.length) {
    body.innerHTML = `<li class="empty">No changes recorded yet. The first poll only takes note of what is running.</li>`;
    return;
  }
  body.innerHTML = rows.map((c) => `
    <li class="audit-row" title="${esc(c.at)}">
      <span class="a-when">${agoTag(c.at)}</span>
      <span class="a-container">${containerLink(c.container_name)}</span>
      <span class="a-action">${esc(c.summary)}${changeCount(c)}</span>
      <span class="a-outcome">${statusPill(changeKind(c), CHANGE_LEVEL[c.kind] || "neutral")}</span>
    </li>`).join("");
}

// The full list of run-config differences; the summary names the first few.
function configList(c) {
  const lines = (c.detail || {}).config || [];
  if (lines.length <= 3) return "";
  return `<ul class="muted">${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`;
}

function changesBlock(d) {
  const rows = d.changes || [];
  if (!rows.length) return `<p class="muted">Nothing has changed since the doctor started watching it.</p>`;
  return `<ul class="detail-list">${rows.map((c) => `
    <li><span>${esc(c.summary)}${changeCount(c)}${configList(c)}</span>
      <span class="muted">${agoTag(c.at)}</span>
      ${statusPill(changeKind(c), CHANGE_LEVEL[c.kind] || "neutral")}</li>`).join("")}</ul>`;
}

// --- memory limits -------------------------------------------------------------

const MEMORY_WORDS = {
  raise: "raise it", tight: "tight", unlimited: "no limit", loose: "could be lower",
  ok: "fine", collecting: "collecting",
};

function memoryBar(m) {
  if (!m.limit_bytes || !m.peak_bytes) return "";
  const share = Math.min(100, (m.peak_bytes / m.limit_bytes) * 100);
  const levelName = m.verdict === "raise" ? "critical" : m.verdict === "tight" ? "warning" : "ok";
  return `<div class="meter" role="img" aria-label="Peak ${share.toFixed(0)}% of its limit">
    <div class="meter-fill" data-level="${levelName}" style="width:${share}%"></div></div>`;
}

function memoryFigures(m) {
  if (m.verdict === "collecting") return esc(m.reason);
  const peak = `peak ${bytes(m.peak_bytes)}`;
  const limit = m.limit_bytes ? ` of ${bytes(m.limit_bytes)}` : ", no limit";
  return `${peak}${limit} · typical ${bytes(m.typical_bytes)}`;
}

function memorySuggestion(m) {
  if (!m.compose || m.verdict === "ok") return "";
  const where = m.stack ? ` on <code>${esc(m.service || m.name)}</code> in <code>${esc(m.stack)}/docker-compose.yml</code>` : "";
  return `<p class="mem-fix">Set <code>${esc(m.compose)}</code>${where}</p>`;
}

function renderMemory() {
  const host = $("memory");
  const report = state.memory;
  if (!report) return;
  const rows = report.containers || [];
  const worth = rows.filter((m) => !["ok", "collecting"].includes(m.verdict));
  const count = (verdict) => rows.filter((m) => m.verdict === verdict).length;
  const fine = count("ok");
  const collecting = count("collecting");
  const parts = [
    [count("raise") + count("tight"), "near the limit"],
    [count("loose"), "could be lower"],
    [count("unlimited"), "without a limit"],
    [fine, "fine"],
    [collecting, "still collecting"],
  ].filter(([n]) => n).map(([n, words]) => `${n} ${words}`);
  $("memory-sub").textContent = `${parts.join(" · ")} · last ${report.window_days} days`;
  if (!worth.length) {
    host.innerHTML = `<li class="empty">${rows.length && collecting === rows.length
      ? "Collecting - a suggestion needs a day of hourly peaks."
      : "Every limit fits what its container uses."}</li>`;
    return;
  }
  const row = (m) => `
    <li class="mem-row">
      <span class="mem-name"><a href="#container/${encodeURIComponent(m.name)}">${esc(m.name)}</a>
        ${m.stack ? `<span class="muted">${esc(m.stack)}</span>` : ""}</span>
      <span class="mem-usage">${memoryFigures(m)}${memoryBar(m)}</span>
      <span class="mem-verdict">${statusPill(MEMORY_WORDS[m.verdict] || m.verdict, m.level || "neutral")}</span>
      <p class="mem-reason">${esc(m.reason)}</p>
      ${memorySuggestion(m)}
    </li>`;
  // Most containers in a homelab run without a limit; one line for all of
  // them keeps the ones about to be OOM killed at the top of the page.
  const capped = worth.filter((m) => m.verdict !== "unlimited");
  const unlimited = worth.filter((m) => m.verdict === "unlimited");
  const open = state.memoryUnlimitedOpen ? "open" : "";
  host.innerHTML = capped.map(row).join("") + (unlimited.length ? `
    <li class="mem-group"><details ${open} data-mem-unlimited>
      <summary><span class="chevron" aria-hidden="true">&#9656;</span>
        <strong>${unlimited.length} container${unlimited.length === 1 ? "" : "s"} without a limit</strong>
        <span class="muted">largest first · a cap stops one leak starving the host</span></summary>
      <ol class="mem-list">${unlimited.map(row).join("")}</ol>
    </details></li>` : "");
}

async function refreshMemory() {
  try {
    await load("memory", "/api/memory", (memory) => {
      state.memory = memory;
      renderMemory();
    });
  } catch (_) { /* the rest of the page works without it */ }
}

function memoryBlock(d) {
  const m = d.memory;
  if (!m) return `<p class="muted">Not watched, so no history.</p>`;
  return `<div class="detail-memory">
    <p>${memoryFigures(m)} ${statusPill(MEMORY_WORDS[m.verdict] || m.verdict, m.level || "neutral")}</p>
    ${memoryBar(m)}
    ${m.verdict === "collecting" ? "" : `<p class="muted">${esc(m.reason)}</p>`}
    ${memorySuggestion(m)}</div>`;
}

// --- the LLM page -----------------------------------------------------------------
//
// Spend per day as stacked bars - one per day, split by what paid for it -
// and the call log underneath, each call openable to read exactly what was
// sent. The series colours are the validated categorical slots 1-3; anything
// else, and days older than the log, fold into one neutral "other".

const SPEND_SERIES = [
  ["diagnosis", "Diagnoses"],
  ["briefing", "Summary"],
  ["classifier", "Classifier"],
  ["other", "Other, or not itemized"],
];
const PURPOSE_WORDS = {
  container_diagnosis: "Diagnosis", briefing_section: "Summary, one section",
  briefing_overall: "Summary, overall", classifier_signal: "Classifier",
};

function usd(value, long = false) {
  const v = Number(value) || 0;
  if (v === 0) return "$0";
  if (v >= 1) return `$${v.toFixed(2)}`;
  if (v >= 0.01) return `$${v.toFixed(long ? 4 : 3)}`;
  return `$${v.toPrecision(2)}`;
}

function spendParts(day) {
  const by = day.by_purpose || {};
  const part = (key) => Number(by[key]?.cost_usd || 0);
  return {
    diagnosis: part("diagnosis"),
    briefing: part("briefing"),
    classifier: part("classifier"),
    other: part("other") + part("earlier"),
  };
}

function roundedTop(x, y, w, h, r) {
  const radius = Math.min(r, h, w / 2);
  return `M${x},${y + h}V${y + radius}Q${x},${y} ${x + radius},${y}H${x + w - radius}`
       + `Q${x + w},${y} ${x + w},${y + radius}V${y + h}Z`;
}

function drawSpendChart(host, days) {
  host._days = days;
  const width = Math.max(260, host.clientWidth || 0);
  const height = 190;
  const pad = { top: 14, right: 12, bottom: 24, left: 52 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const peak = Math.max(0, ...days.map((d) => d.cost_usd));
  if (!(peak > 0)) {
    host.innerHTML = `<p class="chart-empty">Nothing spent in this range.</p>`;
    return;
  }
  const yMax = niceCeil(peak * 1.1);
  const band = plotW / days.length;
  const barW = Math.max(2, Math.min(24, band * 0.62));
  const sy = (v) => pad.top + plotH - (v / yMax) * plotH;
  const ticks = [0, yMax / 2, yMax];
  const grid = ticks.map((v) => `
    <line class="chart-grid" x1="${pad.left}" x2="${width - pad.right}" y1="${sy(v)}" y2="${sy(v)}"></line>
    <text class="chart-axis" x="${pad.left - 6}" y="${sy(v) + 4}" text-anchor="end">${esc(usd(v))}</text>`).join("");
  const GAP = 2;
  const bars = days.map((day, i) => {
    const x = pad.left + band * i + (band - barW) / 2;
    const parts = spendParts(day);
    const keys = SPEND_SERIES.map(([k]) => k).filter((k) => parts[k] > 0);
    let base = pad.top + plotH;
    return keys.map((key, index) => {
      const h = (parts[key] / yMax) * plotH;
      const top = base - h;
      const last = index === keys.length - 1;
      // A 2px surface gap between stacked segments, taken off the lower one.
      const drawn = Math.max(0.5, h - (last ? 0 : GAP));
      base = top;
      return last
        ? `<path class="spend-bar" data-series="${key}" d="${roundedTop(x, top, barW, drawn, 4)}"></path>`
        : `<rect class="spend-bar" data-series="${key}" x="${x}" y="${top + (h - drawn)}" width="${barW}" height="${drawn}"></rect>`;
    }).join("");
  }).join("");
  const hits = days.map((day, i) => `<rect class="spend-hit" data-spend-day="${i}"
      x="${pad.left + band * i}" y="${pad.top}" width="${band}" height="${plotH}"></rect>`).join("");
  const label = (i, anchor) => `<text class="chart-axis" x="${pad.left + band * i + band / 2}" y="${height - 6}"
      text-anchor="${anchor}">${esc(new Date(`${days[i].day}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric" }))}</text>`;
  const budget = state.spend?.budget_usd || 0;
  const ref = budget > 0 && budget <= yMax ? `
    <line class="chart-ref" data-kind="warning" x1="${pad.left}" x2="${width - pad.right}" y1="${sy(budget)}" y2="${sy(budget)}"></line>
    <text class="chart-ref-label" x="${width - pad.right}" y="${sy(budget) - 4}" text-anchor="end">daily budget</text>` : "";
  host.innerHTML = `
    <svg class="chart-svg" width="${width}" height="${height}" role="img"
         aria-label="${esc(`Spend per day over ${days.length} days, peak ${usd(peak)}`)}">
      ${grid}${ref}${bars}
      ${label(0, "start")}${label(Math.floor((days.length - 1) / 2), "middle")}${label(days.length - 1, "end")}
      ${hits}
    </svg>
    <div class="chart-tip" hidden></div>`;
}

function spendHover(event) {
  const hit = event.target.closest("[data-spend-day]");
  const host = event.target.closest(".spend-chart");
  if (!host || !host._days) return;
  const tip = host.querySelector(".chart-tip");
  host.querySelectorAll(".spend-hit.is-hover").forEach((el) => el.classList.remove("is-hover"));
  if (!hit) { tip.hidden = true; return; }
  hit.classList.add("is-hover");
  const day = host._days[Number(hit.dataset.spendDay)];
  const parts = spendParts(day);
  const rows = SPEND_SERIES.filter(([k]) => parts[k] > 0).map(([k, words]) =>
    `<span class="tip-row"><i class="swatch" data-series="${k}"></i>${esc(words)} <b>${esc(usd(parts[k], true))}</b></span>`).join("");
  tip.hidden = false;
  tip.innerHTML = `<strong>${esc(usd(day.cost_usd, true))}</strong>
    <span>${esc(new Date(`${day.day}T12:00:00Z`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }))}
      · ${day.calls} call${day.calls === 1 ? "" : "s"}</span>${rows}`;
  const box = host.getBoundingClientRect();
  const x = Number(hit.getAttribute("x")) + Number(hit.getAttribute("width")) / 2;
  tip.style.left = `${Math.max(0, Math.min(x - tip.offsetWidth / 2, box.width - tip.offsetWidth))}px`;
}

function spendTable(days) {
  const rows = days.filter((d) => d.cost_usd > 0 || d.calls > 0).reverse();
  if (!rows.length) return "";
  return `<details class="chart-table"><summary>Show as a table</summary>
    <table><thead><tr><th>Day</th><th>Total</th>${SPEND_SERIES.map(([, w]) => `<th>${esc(w)}</th>`).join("")}<th>Calls</th><th>Tokens in / out</th></tr></thead><tbody>
    ${rows.map((d) => { const p = spendParts(d); return `<tr><td>${esc(d.day)}</td><td>${esc(usd(d.cost_usd, true))}</td>
      ${SPEND_SERIES.map(([k]) => `<td>${p[k] ? esc(usd(p[k], true)) : "-"}</td>`).join("")}
      <td>${d.calls}</td><td>${d.input_tokens.toLocaleString()} / ${d.output_tokens.toLocaleString()}</td></tr>`; }).join("")}
    </tbody></table></details>`;
}

function renderSpend() {
  const spend = state.spend;
  if (!spend) return;
  const days = spend.days || [];
  const total = days.reduce((sum, d) => sum + d.cost_usd, 0);
  const calls = days.reduce((sum, d) => sum + d.calls, 0);
  const today = days[days.length - 1] || { cost_usd: 0, calls: 0 };
  const active = days.filter((d) => d.cost_usd > 0).length;
  const budget = spend.budget_usd || 0;
  $("spend-sub").textContent = `last ${days.length} days`;
  $("spend-stats").innerHTML = `
    <div class="spend-stat"><span>In range</span><strong>${esc(usd(total, true))}</strong><small>${calls} calls</small></div>
    <div class="spend-stat"><span>Per active day</span><strong>${esc(usd(active ? total / active : 0, true))}</strong><small>${active} of ${days.length} days</small></div>
    <div class="spend-stat"><span>Today</span><strong>${esc(usd(today.cost_usd, true))}</strong><small>${budget ? `of ${esc(usd(budget))} budget` : "no budget set"}${spend.call_limit ? ` · ${today.calls} of ${spend.call_limit} calls` : ""}</small></div>`;
  $("spend-legend").innerHTML = SPEND_SERIES.map(([k, words]) =>
    `<li><i class="swatch" data-series="${k}"></i>${esc(words)}</li>`).join("");
  drawSpendChart($("spend-chart"), days);
  const table = $("spend-table");
  const html = spendTable(days);
  // Only when the numbers changed, or an open table snaps shut.
  if (table.dataset.html !== html) { table.dataset.html = html; table.innerHTML = html; }
  document.querySelectorAll("[data-spend-days]").forEach((b) =>
    b.setAttribute("aria-checked", String(Number(b.dataset.spendDays) === state.spendDays)));
}

function callOutcome(c) {
  if (c.status !== "ok") return statusPill("failed", "critical");
  return `<span class="call-cost">${esc(usd(c.cost_usd, true))}</span>`;
}

function renderCalls() {
  const rows = state.calls;
  const body = $("llm-calls");
  $("llm-calls-more").innerHTML = pager("calls", rows.length, state.callTotal, pageSize("calls"), MAX_ROWS);
  if (!rows.length) {
    body.innerHTML = `<li class="empty">${state.callFilter ? "No calls of this kind in the log." : "No calls logged yet."}</li>`;
    return;
  }
  body.innerHTML = rows.map((c) => `
    <li class="audit-row call-row">
      <span class="a-when" title="${esc(c.at)}">${agoTag(c.at)}</span>
      <span class="a-container"><button type="button" class="link-btn" data-llm-call="${esc(c.id)}">${esc(PURPOSE_WORDS[c.purpose] || c.purpose)}</button>
        ${c.ref ? `<span class="a-by">${esc(c.ref)}</span>` : ""}</span>
      <span class="a-action"><code>${esc(c.model)}</code>
        <span class="a-by">${Number(c.input_tokens).toLocaleString()} in · ${Number(c.output_tokens).toLocaleString()} out · ${(c.latency_ms / 1000).toFixed(1)}s</span></span>
      <span class="a-outcome">${callOutcome(c)}</span>
      ${c.error ? `<p class="a-reason is-clamped">${esc(c.error)}</p>` : ""}
    </li>`).join("");
}

// Until a part's first answer lands, it shows the shape of what is coming.
// Each part then draws as soon as its own request is back: the spend query is
// the slow one and used to hold the call log and classifier hostage.
function llmSkeletons() {
  if (!state.spend) {
    $("panel-spend").setAttribute("aria-busy", "true");
    $("spend-sub").textContent = "Loading\u2026";
    $("spend-stats").innerHTML = skeletonStats(3);
    $("spend-chart").innerHTML = skeletonBars(Math.min(state.spendDays, 30));
  }
  if (!state.callsLoaded) {
    $("panel-calls").setAttribute("aria-busy", "true");
    $("llm-calls").innerHTML = skeletonRows(5, "li");
  }
  if (!state.classifier) {
    $("panel-classifier").setAttribute("aria-busy", "true");
    $("classifier-sub").textContent = "Loading\u2026";
    $("classifier-stats").innerHTML = skeletonStats(3);
    $("classifications").innerHTML = skeletonRows(3, "li");
  }
}

async function refreshLlm() {
  llmSkeletons();
  const filter = state.callFilter ? `&purpose=${encodeURIComponent(state.callFilter)}` : "";
  const part = (slot, path, panel, draw) => load(slot, path, (data) => {
    draw(data);
    $(panel).setAttribute("aria-busy", "false");
  }).catch((error) => {
    $(panel).setAttribute("aria-busy", "false");
    throw error;
  });
  const results = await Promise.allSettled([
    part("llm-spend", `/api/llm/spend?days=${state.spendDays}`, "panel-spend", (spend) => {
      state.spend = spend;
      renderSpend();
    }),
    part("llm-calls", `/api/llm/calls?limit=${state.callLimit}${filter}`, "panel-calls", (calls) => {
      state.calls = calls.calls;
      state.callTotal = calls.total ?? calls.calls.length;
      state.callsLoaded = true;
      renderCalls();
    }),
    part("classifier", "/api/classifier?limit=15", "panel-classifier", (classifier) => {
      state.classifier = classifier;
      renderClassifier();
    }),
  ]);
  const failed = results.find((r) => r.status === "rejected");
  if (failed) toast(`Could not load the LLM log: ${failed.reason?.message || failed.reason}`, "error");
}

const VERDICT_WORDS = {
  real_fault: "a real fault", benign_noise: "noise", expected_transient: "passing",
  unclear: "unclear",
};
const ACTION_WORDS = {
  suppress: ["hide it", "hidden", "neutral"],
  downgrade: ["lower severity", "lowered", "neutral"],
  escalate: ["raise severity", "raised", "warning"],
  open: ["leave it", "left to the rule", "good"],
};

function verdictLine(v) {
  const p = v.probabilities?.[v.verdict];
  return `${esc(VERDICT_WORDS[v.verdict] || v.verdict || "no answer")}${
    p !== undefined ? ` <span class="muted">${Math.round(p * 100)}%</span>` : ""}`;
}

function actionPill(v) {
  const [would, did, levelName] = ACTION_WORDS[v.action] || [v.action, v.action, "neutral"];
  return statusPill(v.applied ? did : `would ${would}`, levelName);
}

// On the incident: what the classifier made of the signal before it opened.
// The working behind the one line stays folded until asked for.
function classificationNote(incident) {
  const v = incident.evidence?.classification;
  if (!v) return "";
  const shadow = v.mode === "shadow" ? " · shadow mode, nothing was changed" : "";
  const open = state.classifierOpen.has(String(incident.id));
  return `<div class="second-opinion-wrap"><p class="second-opinion">${icon("i-spark")}
    <span><strong>Classifier:</strong> ${verdictLine(v)}${v.severity ? `, ${esc(v.severity)}` : ""}.
      ${esc(v.reason || "")}<span class="muted">${esc(shadow)}${v.from_cache ? " · reused verdict" : ""}</span></span>
    ${actionPill(v)}</p>
    <details class="cls-details" data-classifier-for="${esc(incident.id)}"${open ? " open" : ""}>
      <summary>Details</summary>${open ? classifierDetails(v) : ""}</details></div>`;
}

const CAUSE_WORDS = {
  config: "configuration", dependency: "a dependency", resources: "resources",
  storage: "storage", network: "network", auth: "credentials", image: "the image",
  upgrade: "an upgrade", unknown: "unknown",
};
const NOISE_VERDICTS = new Set(["benign_noise", "expected_transient"]);

function share(x) {
  return `${Math.round(Number(x) * 100)}%`;
}

// One labelled horizontal bar per option, biggest first, the chosen one marked.
function probabilityBars(probs, words, chosen, tone) {
  const rows = Object.entries(probs || {}).filter(([, p]) => Number.isFinite(Number(p)))
    .sort((a, b) => b[1] - a[1]);
  if (!rows.length) return "";
  return `<ul class="cls-bars">${rows.map(([key, p]) => `
    <li${key === chosen ? ` class="is-chosen"` : ""}>
      <span class="cls-bar-label">${esc(words[key] || key.replace(/_/g, " "))}</span>
      <span class="cls-bar-track" role="img" aria-label="${esc(words[key] || key)}: ${share(p)}">
        <span class="cls-bar-fill" data-tone="${esc(tone(key))}" style="width:${Math.min(100, Number(p) * 100)}%"></span></span>
      <span class="cls-bar-value">${share(p)}</span>
    </li>`).join("")}</ul>`;
}

// A 0-100 track with the value filled in and the bar it would have to clear.
function thresholdGauge(value, bar, label, barLabel) {
  const v = Math.min(100, Math.max(0, Number(value) * 100));
  const t = bar == null ? null : Math.min(100, Math.max(0, Number(bar) * 100));
  const over = t != null && v >= t;
  return `<div class="cls-gauge" role="img" aria-label="${esc(label)} ${Math.round(v)}%${t != null ? `, ${esc(barLabel)} ${Math.round(t)}%` : ""}">
    <div class="cls-gauge-track">
      <span class="cls-gauge-fill${over ? " is-over" : ""}" style="width:${v}%"></span>
      ${t != null ? `<span class="cls-gauge-mark" style="left:${t}%" title="${esc(barLabel)} ${Math.round(t)}%"></span>` : ""}
    </div>
    ${t != null ? `<div class="cls-gauge-scale"><span class="cls-gauge-mark-label"
      style="left:${t}%" data-edge="${t > 75 ? "end" : t < 25 ? "start" : ""}">${esc(barLabel)} ${Math.round(t)}%</span></div>` : ""}
  </div>`;
}

// Three steps, info to critical: the rule's pick and the classifier's side by side.
function severityLadder(v) {
  const probs = v.severity_probabilities || {};
  const levels = ["info", "warning", "critical"];
  return `<div class="cls-ladder">${levels.map((level) => {
    const p = probs[level];
    const marks = [level === v.rule_severity ? "rule" : "", level === v.severity ? "classifier" : ""]
      .filter(Boolean).join(" + ");
    return `<div class="cls-step${level === v.severity ? " is-chosen" : ""}" data-level="${level}">
      <span class="cls-step-fill" style="height:${p != null ? Math.max(4, Number(p) * 100) : (level === v.severity ? 100 : 0)}%"></span>
      <strong>${esc(level)}</strong>
      <small>${p != null ? share(p) : level === v.severity ? "chosen" : "&nbsp;"}</small>
      <small class="cls-step-who">${esc(marks) || "&nbsp;"}</small>
    </div>`;
  }).join("")}</div>`;
}

function classifierDetails(v) {
  const sections = [];
  const verdictTone = (k) => (k === "real_fault" ? "real" : NOISE_VERDICTS.has(k) ? "noise" : "neutral");
  const bars = probabilityBars(v.probabilities, VERDICT_WORDS, v.verdict, verdictTone);
  if (bars) {
    sections.push(`<section><h4>Is it real?</h4>${bars}
      ${v.confidence != null ? `<p class="cls-foot">Stated confidence in the pick: ${share(v.confidence)}</p>` : ""}</section>`);
  }
  if (v.p_noise != null) {
    const bar = v.suppress_at;
    const verdict = bar == null ? ""
      : v.p_noise >= bar ? (v.applied ? "Cleared the bar: hidden." : "Cleared the bar: would hide in mode on.")
      : `${share(bar - v.p_noise)} short of the bar, so the incident stays.`;
    sections.push(`<section><h4>Chance it is noise</h4>
      ${thresholdGauge(v.p_noise, bar, "noise", "hide at")}
      <p class="cls-foot">${share(v.p_noise)} noise (harmless or passing combined). ${esc(verdict)}</p></section>`);
  } else if (!v.calibrated) {
    sections.push(`<section><h4>Chance it is noise</h4>
      <p class="cls-foot">Not measured: ${esc(v.model || "this model")} is uncalibrated, so its
      percentages are not read as probabilities and it can never hide an incident.</p></section>`);
  }
  if (v.severity || v.rule_severity) {
    const conf = v.severity_confidence != null ? ` with ${share(v.severity_confidence)} confidence` : "";
    sections.push(`<section><h4>Severity</h4>${severityLadder(v)}
      <p class="cls-foot">Rule said ${esc(v.rule_severity || "?")}; the classifier said ${esc(v.severity || "?")}${esc(conf)}.</p></section>`);
  }
  const causeBars = probabilityBars(
    Object.fromEntries(Object.entries(v.cause_probabilities || {}).sort((a, b) => b[1] - a[1]).slice(0, 5)),
    CAUSE_WORDS, v.cause, () => "cause");
  if (causeBars || v.cause) {
    sections.push(`<section><h4>Likely cause</h4>${causeBars
      || `<p class="cls-foot">${esc(CAUSE_WORDS[v.cause] || v.cause)}</p>`}</section>`);
  }
  if (v.self_heals != null) {
    sections.push(`<section><h4>Clears on its own within ~10 min</h4>
      ${thresholdGauge(v.self_heals, 0.5, "clears on its own", "coin flip")}
      <p class="cls-foot">${share(v.self_heals)} likely to clear without anyone acting.</p></section>`);
  }
  const meta = [
    ["Model", `${v.model || "?"}${v.calibrated ? "" : " (uncalibrated)"}`],
    ["Mode", `${v.mode}${v.applied ? ", acted on" : ", recorded only"}`],
    ...(v.input_tokens ? [["Tokens", `${Number(v.input_tokens).toLocaleString()} in · ${Number(v.output_tokens || 0).toLocaleString()} out`]] : []),
    ...(v.latency_ms ? [["Took", `${(v.latency_ms / 1000).toFixed(1)} s`]] : []),
    ["Cost", v.from_cache ? "nothing, reused an earlier verdict" : usd(v.cost_usd || 0, true)],
  ];
  return `<div class="cls-grid">${sections.join("")}</div>
    <dl class="dl cls-meta">${meta.map(([k, val]) => `<dt>${esc(k)}</dt><dd>${esc(val)}</dd>`).join("")}</dl>`;
}

function renderClassifier() {
  const c = state.classifier;
  if (!c) return;
  const stats = $("classifier-stats");
  const list = $("classifications");
  if (!c.enabled) {
    $("classifier-sub").textContent = c.mode === "off" ? "off" : `${c.mode}, but not configured`;
    stats.innerHTML = "";
    list.innerHTML = `<li class="empty">Off. Set <code>DOCTOR_CLASSIFIER=shadow</code> and
      <code>DOCTOR_CLASSIFIER_API_KEY</code> (an OpenRouter key) to start collecting verdicts
      without changing anything.${c.last_error ? ` ${esc(c.last_error)}` : ""}</li>`;
    return;
  }
  $("classifier-sub").textContent = `${c.mode} · ${c.model}${c.calibrated ? "" : " · uncalibrated, never hides"}`;
  const week = c.week || { by_action: {} };
  const by = week.by_action || {};
  const reviewed = Object.values(by).reduce((a, b) => a + b, 0);
  stats.innerHTML = `
    <div class="spend-stat"><span>Today</span><strong>${c.today.calls}</strong><small>of ${c.daily_calls} calls · ${esc(usd(c.today.cost_usd, true))} of ${esc(usd(c.daily_usd))}</small></div>
    <div class="spend-stat"><span>Last 7 days</span><strong>${reviewed}</strong><small>signals reviewed</small></div>
    <div class="spend-stat"><span>${c.mode === "on" ? "Hidden as noise" : "Would have hidden"}</span><strong>${by.suppress || 0}</strong>
      <small>${by.escalate || 0} raised · ${by.downgrade || 0} lowered${c.last_error ? ` · ${esc(c.last_error)}` : ""}</small></div>`;
  const rows = c.recent || [];
  list.innerHTML = rows.length ? rows.map((row) => `
    <li class="audit-row">
      <span class="a-when" title="${esc(row.at)}">${agoTag(row.at)}</span>
      <span class="a-container"><a href="#container/${encodeURIComponent(row.container_name)}">${esc(row.container_name)}</a>
        <span class="a-by">${esc(row.kind.replace(/_/g, " "))}</span></span>
      <span class="a-action">${verdictLine(row.verdict)}<span class="a-by">${esc(row.verdict.reason || "")}</span></span>
      <span class="a-outcome">${actionPill(row.verdict)}</span>
    </li>`).join("") : `<li class="empty">No signals reviewed yet.</li>`;
}

function fence(text) {
  return `<pre class="call-text">${esc(text || "")}</pre>`;
}

async function openCall(id) {
  const dialog = $("llm-call");
  const body = $("llm-call-body");
  body.innerHTML = `<p class="muted detail-loading">Loading…</p>`;
  if (!dialog.open) dialog.showModal();
  let c;
  try {
    c = await api(`/api/llm/calls/${encodeURIComponent(id)}`);
  } catch (error) {
    body.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
    return;
  }
  const r = c.request || {};
  const meta = [
    ["When", new Date(c.at).toLocaleString()],
    ["Sent to", c.endpoint],
    ["Model", c.model],
    ["Tokens", `${Number(c.input_tokens).toLocaleString()} in${c.cached_tokens ? ` (${Number(c.cached_tokens).toLocaleString()} cached)` : ""} · ${Number(c.output_tokens).toLocaleString()} out`],
    ["Cost", usd(c.cost_usd, true)],
    ["Took", `${(c.latency_ms / 1000).toFixed(1)} s`],
    ...(r.sent_as ? [["Sent as", r.sent_as]] : []),
    ...(r.effort ? [["Effort", r.effort]] : []),
  ];
  const incident = c.ref && c.purpose.includes("diagnosis")
    ? `<a class="btn btn-small" href="#incident/${encodeURIComponent(c.ref)}" data-close-call>Open the incident</a>` : "";
  body.innerHTML = `
    <header class="detail-head">
      <div class="detail-title">
        <h2 id="llm-call-title">${esc(PURPOSE_WORDS[c.purpose] || c.purpose)}</h2>
        <p class="muted">${agoTag(c.at)}${c.ref ? ` · ${esc(c.ref)}` : ""}</p>
      </div>
      ${c.status === "ok" ? statusPill("answered", "good") : statusPill("failed", "critical")}
      <button type="button" class="detail-close" data-close-call aria-label="Close">&times;</button>
    </header>
    <div class="detail-actions">${incident}
      <button type="button" class="btn btn-small" data-copy-call="prompt">Copy the prompt</button></div>
    <dl class="dl detail-dl">${meta.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    ${c.error ? `<section class="detail-section"><h3>Error</h3>${fence(c.error)}</section>` : ""}
    <section class="detail-section"><h3>${r.kind === "decision" ? "Questions" : "System prompt"}</h3>${fence(r.system)}</section>
    <section class="detail-section"><h3>${r.kind === "decision" ? "State" : "Prompt"}</h3>${fence(r.prompt)}</section>
    ${r.schema ? `<section class="detail-section"><details><summary class="log-summary">Answer schema <code>${esc(r.schema_name || "")}</code></summary>
      ${fence(JSON.stringify(r.schema, null, 2))}</details></section>` : ""}
    ${c.response_text ? `<section class="detail-section"><h3>Answer</h3>${fence(c.response_text)}</section>` : ""}`;
  body._call = c;
}

function renderLiveTail(host, result) {
  host.innerHTML = `
    <div class="log-head">
      <strong>${esc(result.container)}</strong>
      <span>last ${result.lines} lines, fetched just now \u00b7 secrets redacted</span>
    </div>
    <div class="evidence">${esc(result.logs) || "(no output)"}</div>`;
}

// Every sighting folded into one incident, newest first. Each is collapsed:
// the timestamps answer "how often", and the lines - which files this scan
// choked on - are there for the one sighting you want to look at.
function renderHistory(host, history) {
  const rows = history.occurrences || [];
  const missing = Math.max(0, Number(history.total || 0) - Number(history.kept || 0));
  const note = missing
    ? `<p class="muted history-note">${missing} earlier sighting(s) not shown: ${
        Number(history.kept) >= Number(history.limit)
          ? `only the last ${Number(history.limit)} are kept`
          : "they predate the history being recorded"}.</p>` : "";
  const items = rows.length ? rows.map((o) => {
    const lines = Array.isArray(o.lines) ? o.lines : [];
    const head = `<span class="history-when">${esc(new Date(o.at).toLocaleString())}</span>
      <span class="muted">${agoTag(o.at)}${lines.length ? ` \u00b7 ${lines.length} line(s)` : ""}</span>`;
    return lines.length || o.summary
      ? `<li><details><summary>${head}</summary>
           ${o.summary ? `<p class="history-summary">${esc(o.summary)}</p>` : ""}
           ${lines.length ? `<div class="evidence">${lines.map((l) => esc(String(l))).join("\n")}</div>` : ""}
         </details></li>`
      : `<li>${head}</li>`;
  }).join("") : `<li class="muted">No sightings recorded.</li>`;
  host.innerHTML = `
    <div class="log-head">
      <strong>Sighting history</strong>
      <span>${rows.length} shown of ${Number(history.total || rows.length)}</span>
    </div>
    ${note}
    <ul class="log-list history-list">${items}</ul>`;
}

function renderContext(host, ctx) {
  const neighbours = ctx.neighbours.length
    ? ctx.neighbours.map((n) => `
        <li>
          <strong>${esc(n.container)}</strong>
          <span class="muted">${n.matches} error line(s)</span>
          <div class="evidence" style="margin-top:6px">${n.sample.map(esc).join("\n")}</div>
        </li>`).join("")
    : `<li class="muted">Nothing else logged errors in this window.</li>`;

  const incidents = ctx.nearby_incidents.length
    ? `<div class="log-head" style="margin-top:14px"><strong>Other incidents in this window</strong></div>
       <ul class="log-list">${ctx.nearby_incidents.map((i) => `
         <li><strong>${esc(i.container_name)}</strong>
             <span class="muted">${esc(i.kind.replace(/_/g, " "))} \u00b7 ${agoTag(i.opened_at)}</span></li>`).join("")}</ul>`
    : "";

  host.innerHTML = `
    <div class="log-head">
      <strong>\u00b1${ctx.window_minutes} min around detection</strong>
      <span>${esc(ctx.since)} \u2192 ${esc(ctx.until)}</span>
    </div>
    <div class="evidence">${esc(ctx.logs) || "(no output in this window)"}</div>
    <div class="log-head" style="margin-top:14px"><strong>What else was failing</strong></div>
    <ul class="log-list">${neighbours}</ul>
    ${incidents}`;
}


// --- container detail --------------------------------------------------------
//
// One container on one screen: how it has been doing for the last day, what
// has happened to it, what its image is behind on, and what sits in its stack.
// A modal <dialog>, so focus, Escape and the backdrop behave without help, and
// it gets its own address - #container/<name> - so it can be linked to.

function historySpecs(history, stats) {
  const points = history.map((h) => [new Date(h.at).getTime(), h]);
  const hasLimit = Number(stats?.mem_limit_bytes || 0) > 0;
  const cpuPoints = points.map(([t, h]) => [t, h.cpu]);
  const memPoints = points.map(([t, h]) => [t, hasLimit ? h.mem_pct : h.mem_bytes]);
  const cpuMax = Math.min(100, niceCeil(Math.max(10, ...cpuPoints.map((p) => p[1])) * 1.1));
  const memMax = hasLimit ? 100 : niceBytes(Math.max(1, ...memPoints.map((p) => p[1])) * 1.1);
  const empty = "Collecting - a point a minute since the doctor last started.";
  const peak = (pts) => Math.max(...pts.map((p) => p[1]));
  return [
    {
      title: "CPU, share of the host",
      spec: {
        points: cpuPoints, yMin: 0, yMax: cpuMax, format: (v) => pct(v), empty,
        refs: thresholds().cpu ? [{ value: thresholds().cpu, label: `alert at ${thresholds().cpu}%`, kind: "warning" }] : [],
        label: cpuPoints.length ? `CPU over the last ${cpuPoints.length} minutes, peak ${pct(peak(cpuPoints))} of the host` : "CPU",
      },
    },
    {
      title: hasLimit ? "Memory, share of its limit" : "Memory (no limit set)",
      spec: {
        points: memPoints, yMin: 0, yMax: memMax, empty,
        format: hasLimit ? (v) => pct(v) : (v) => bytes(v),
        refs: hasLimit && thresholds().memory ? [{ value: thresholds().memory, label: `alert at ${thresholds().memory}%`, kind: "warning" }] : [],
        label: memPoints.length ? `Memory over the last ${memPoints.length} minutes` : "Memory",
      },
    },
  ];
}

function stackBlock(d) {
  const stack = d.stack || {};
  if (!stack.project) return `<p class="muted">Not started by Compose, so no stack to show.</p>`;
  const deps = (stack.depends_on || []).map((x) => x.service);
  const users = (stack.dependents || []).map((x) => x.service);
  const siblings = d.siblings.length ? `<ul class="detail-list">${d.siblings.map((o) => `
    <li><a href="#container/${encodeURIComponent(o.name)}">${esc(o.name)}</a>
      <span class="muted">${esc(o.service || "")}${deps.includes(o.service) ? " · this depends on it" : ""}${
        users.includes(o.service) ? " · depends on this" : ""}</span>
      ${containerPill(o)}</li>`).join("")}</ul>` : `<p class="muted">The only container in its stack.</p>`;
  return `<p class="muted">Stack <strong>${esc(stack.project)}</strong>, service <code>${esc(stack.service)}</code></p>${siblings}`;
}

// The same update as the Updates list shows it: the newer version with its
// notes readable in place, a release still cooling down, and where the bump
// goes - so the container view does not need the list beside it.
function imageBlock(d) {
  const entry = d.image;
  const image = d.container.image || "";
  if (!entry) return `<p><code>${esc(image)}</code></p><p class="muted">Not checked yet.</p>`;
  const words = {
    current: "The tag has not moved since this image was pulled.",
    update: "A newer build of this tag is published; a redeploy would pull it.",
    pinned: entry.newer || entry.cooling
      ? "Pinned to a digest: it stays on this build until the compose file names a newer version."
      : "Pinned to a digest, so it cannot drift.",
  }[entry.state] || `Could not check the tag: ${entry.detail || "unknown"}.`;
  // Only for a stack the repository holds, once the doctor has read it.
  const known = imageUpdates().redeploy_stacks;
  const stack = d.stack?.project && (!known || known.includes(d.stack.project)) ? d.stack.project : "";
  const where = entry.newer && stack
    ? `<p class="image-note">A version bump goes in <code>${esc(stack)}/docker-compose.yml</code>${
        d.stack.service ? `, service <code>${esc(d.stack.service)}</code>` : ""}.</p>` : "";
  const cooling = entry.cooling ? `<p class="image-section-head">Cooling down <span class="muted">· not proposed yet</span></p>
    ${coolingLine(entry)}${notesPanel(entry, entry.cooling.tag)}` : "";
  const shared = (entry.containers || []).filter((n) => n !== d.container.name);
  const others = shared.length ? `<p class="muted">Also running this image: ${containerLinks(shared)}</p>` : "";
  return `<p><code>${esc(image)}</code></p><p class="muted">${esc(words)}</p>
    ${entry.newer ? `${newerLine(entry.newer, entry)}${notesPanel(entry, entry.newer.tag)}${where}` : ""}
    ${redeployButtons(entry) ? `<div class="detail-actions">${redeployButtons(entry)}</div>` : ""}
    ${cooling}${others}`;
}

function incidentsBlock(d) {
  if (!d.incidents.length) return `<p class="muted">No incidents on record.</p>`;
  return `<ul class="detail-list">${d.incidents.map((i) => `
    <li><a href="#incident/${encodeURIComponent(i.id)}">${esc(i.diagnosis?.summary || i.summary)}</a>
      <span class="muted">${esc(i.kind.replace(/_/g, " "))} · ${agoTag(i.opened_at)}</span>
      ${statusPill(i.status.replace(/_/g, " "), i.status === "resolved" ? "good" : i.status === "dismissed" ? "neutral" : "warning")}</li>`).join("")}</ul>`;
}

function actionsBlock(d) {
  if (!d.actions.length) return `<p class="muted">No actions on record.</p>`;
  return `<ul class="detail-list">${d.actions.map((a) => `
    <li><span>${esc(ACTION_LABELS[a.action] || a.action)}</span>
      <span class="muted">by ${esc(a.decided_by)} · ${agoTag(a.created_at)}</span>
      ${statusPill(a.status, a.status === "succeeded" ? "good" : a.status === "failed" ? "critical" : "neutral")}</li>`).join("")}</ul>`;
}

function configBlock(d) {
  const c = d.container;
  const labels = Object.entries(d.labels || {});
  const rows = [
    ["Watched", c.watched ? "yes" : "no"],
    ["Mode", c.mode || "-"],
    ["Restart policy", c.restart_policy || "none"],
    ...(c.protected_reason ? [["Never acted on", c.protected_reason]] : []),
    ...labels.map(([k, v]) => [k, v]),
  ];
  return `<dl class="dl detail-dl">${rows.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
}

function renderDetail(d) {
  const c = d.container;
  const canAct = c.state === "running" && c.watched && !c.protected_reason;
  const charts = historySpecs(d.history, c.stats);
  const body = $("detail-body");
  body.innerHTML = `
    <header class="detail-head">
      <div class="detail-title">
        <h2 id="detail-title">${esc(c.name)}</h2>
        <p class="muted">${esc(c.image)}</p>
      </div>
      ${containerPill(c)}
      <button type="button" class="detail-close" data-close-detail aria-label="Close">&times;</button>
    </header>
    <div class="detail-actions">
      ${canAct ? `<button type="button" class="btn" data-restart="${esc(c.name)}">Restart</button>` : ""}
      ${c.watched ? `<button type="button" class="btn" data-detail-tail="${esc(c.name)}">Fetch live tail</button>` : ""}
    </div>
    <div class="log-out" id="detail-log"></div>
    <section class="detail-section">
      <h3>Last 24 hours</h3>
      <div class="detail-charts">
        ${charts.map((chart, i) => `<figure class="detail-chart">
          <figcaption>${esc(chart.title)}</figcaption>
          <div class="chart" data-detail-chart="${i}"></div></figure>`).join("")}
      </div>
    </section>
    <section class="detail-section"><h3>What changed</h3>${changesBlock(d)}</section>
    <section class="detail-section"><h3>Memory, last 14 days</h3>${memoryBlock(d)}</section>
    <section class="detail-section"><h3>CPU, last 14 days</h3>${cpuDaysBlock(d)}</section>
    <section class="detail-section"><h3>Image</h3>${imageBlock(d)}</section>
    <section class="detail-section"><h3>Stack</h3>${stackBlock(d)}</section>
    <section class="detail-section"><h3>Recent incidents</h3>${incidentsBlock(d)}</section>
    <section class="detail-section"><h3>Recent actions</h3>${actionsBlock(d)}</section>
    <section class="detail-section"><h3>Configuration</h3>${configBlock(d)}</section>`;
  charts.forEach((chart, i) => drawChart(body.querySelector(`[data-detail-chart="${i}"]`), chart.spec));
  drawCpuDays(d);
}

// Hourly, kept for weeks: what the 24-hour chart has already forgotten, such
// as which nights it kept the host busy.
function cpuDaysBlock(d) {
  const hours = d.cpu_hours || [];
  if (!d.container.watched) return `<p class="muted">Not watched, so no history.</p>`;
  if (!hours.length) return `<p class="muted">Collecting - one point an hour.</p>`;
  const mean = hours.reduce((sum, h) => sum + h[1], 0) / hours.length;
  const [busiestAt, busiest] = hours.reduce((a, b) => (b[1] > a[1] ? b : a));
  const peak = Math.max(...hours.map((h) => h[2]));
  return `<p class="muted">Average ${pct(mean)} of the host · busiest hour ${pct(busiest)},
      ${esc(new Date(busiestAt).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }))}
      · peak ${pct(peak)}</p>
    <div class="chart" data-chart="cpu-days"></div>`;
}

function drawCpuDays(d) {
  const host = document.querySelector('#detail-body [data-chart="cpu-days"]');
  if (!host) return;
  const points = (d.cpu_hours || []).map(([at, mean]) => [new Date(at).getTime(), mean]);
  drawChart(host, { points, yMin: 0, yMax: Math.min(100, niceCeil(Math.max(5, ...points.map((p) => p[1])) * 1.1)),
    format: (v) => pct(v), label: `${d.container.name}: CPU per hour over the last 14 days`,
    empty: "Collecting - one point an hour." });
}

async function loadDetail(name) {
  try {
    const detail = await api(`/api/containers/${encodeURIComponent(name)}`);
    if (state.detailName !== name) return;  // closed or moved on meanwhile
    state.detailData = detail;
    rerenderDetail();
  } catch (error) {
    if (state.detailName !== name) return;
    $("detail-body").innerHTML = `
      <header class="detail-head"><div class="detail-title"><h2 id="detail-title">${esc(name)}</h2></div>
        <button type="button" class="detail-close" data-close-detail aria-label="Close">&times;</button></header>
      <p class="muted">${esc(error.message)}</p>`;
  }
}

// Again from what was last fetched - opening release notes changes nothing
// on the server. An open log view is kept: it was fetched separately.
function rerenderDetail() {
  const detail = state.detailData;
  if (!detail || state.detailName !== detail.container?.name) return;
  const log = $("detail-log")?.innerHTML || "";
  renderDetail(detail);
  if (log) $("detail-log").innerHTML = log;
}

function openDetail(name) {
  const dialog = $("detail");
  const switching = state.detailName !== name;
  state.detailName = name;
  if (switching) {
    $("detail-body").innerHTML = `<p class="muted detail-loading">Loading ${esc(name)}…</p>`;
  }
  if (!dialog.open) dialog.showModal();
  loadDetail(name);
}

// Back to the page underneath: closing an incident or a container view leaves
// the address on the section it was opened over.
function clearRoute() {
  history.replaceState(null, "", `${location.pathname}${location.search}#/${state.view}`);
}

function closeDetail() {
  const dialog = $("detail");
  state.detailName = null;
  if (dialog.open) dialog.close();
}

// --- the briefing ------------------------------------------------------------
//
// The top of the Overview: a model's summary of each section, and the few
// things to work on. The server caches every part against the facts it was
// written from, so this is cheap to ask for as often as the page likes; the
// facts show on their own when there is no summary, or it is turned off.

const BRIEF_SECTIONS = {
  incidents: ["Incidents", "#/incidents", "i-incidents"],
  containers: ["Containers", "#/containers", "i-containers"],
  storage: ["Storage", "#/storage", "i-storage"],
  updates: ["Updates", "#/updates", "i-updates"],
};
const BRIEF_LEVEL = {
  ok: ["All clear", "good"],
  info: ["Worth knowing", "neutral"],
  attention: ["Needs attention", "warning"],
  critical: ["Act now", "critical"],
};

function icon(name) {
  return `<svg class="icon" aria-hidden="true"><use href="#${name}"/></svg>`;
}

function briefingMeta(b) {
  if (!b.enabled) {
    return `<span>Showing the facts - the summary is off, or no LLM is configured.</span>`;
  }
  const when = b.overall_generated_at ? `Summarized ${agoTag(b.overall_generated_at)}` : "Not summarized yet";
  const busy = b.generating ? `<span class="brief-busy">${icon("i-refresh")} Updating</span>` : "";
  return `<span>${icon("i-spark")} ${when}${b.model ? ` · ${esc(b.model)}` : ""}</span>${busy}`;
}

// Shimmering placeholder lines, in the shape of the text they stand in for.
function skeletonLines(widths, cls = "") {
  return widths.map((w, i) => `<span class="skeleton ${cls}" style="width:${w}%;--i:${i}"></span>`).join("");
}

// "Reveal" when this part was a skeleton last time and is text now.
function briefReveal(key, skeleton, seen) {
  if (skeleton) seen.add(key);
  return !skeleton && state.briefSkeletons.has(key) ? " brief-reveal" : "";
}

function briefingSection(name, section, seen) {
  const [title, href, glyph] = BRIEF_SECTIONS[name];
  const [word, levelName] = BRIEF_LEVEL[section.level] || BRIEF_LEVEL.ok;
  const reveal = briefReveal(name, section.updating, seen);
  // While a section is being rewritten its old words may be well out of date,
  // so it shows a skeleton until the new summary lands.
  if (section.updating) {
    return `
    <a class="brief-card brief-card-loading" href="${href}" data-level="${section.level}" aria-busy="true">
      <span class="brief-card-head">${icon(glyph)}<span>${title}</span>${statusPill(word, levelName)}</span>
      <span class="skeleton-stack" aria-hidden="true">
        ${skeletonLines([88, 56], "skeleton-title")}
        <span class="skeleton-gap"></span>
        ${skeletonLines([94, 78, 64])}
      </span>
      <span class="visually-hidden">Summarizing ${title.toLowerCase()}</span>
      <span class="brief-card-foot"><span class="brief-busy">${icon("i-refresh")} Updating</span><span class="brief-open">Open ${icon("i-arrow")}</span></span>
    </a>`;
  }
  const summary = section.summary;
  const headline = summary?.headline || section.headline || section.facts[0] || "";
  const points = summary ? summary.points
    : section.facts.filter((line) => line !== section.headline).slice(0, 3);
  const state = section.updating ? `<span class="brief-busy">${icon("i-refresh")} Updating</span>`
    : section.stale ? `<span class="brief-stale" title="Written before the latest change; rewritten shortly">Out of date</span>` : "";
  return `
    <a class="brief-card${reveal}" href="${href}" data-level="${section.level}">
      <span class="brief-card-head">${icon(glyph)}<span>${title}</span>${statusPill(word, levelName)}</span>
      <strong class="brief-card-headline">${esc(headline)}</strong>
      ${points.length ? `<ul>${points.map((point) => `<li>${esc(point)}</li>`).join("")}</ul>` : ""}
      <span class="brief-card-foot">${state}<span class="brief-open">Open ${icon("i-arrow")}</span></span>
    </a>`;
}

function renderBriefing() {
  const b = state.briefing;
  const host = $("briefing");
  if (!b) return;
  const [word, levelName] = BRIEF_LEVEL[b.level] || BRIEF_LEVEL.ok;
  // Same for the top line and priorities: a run rewrites them last, so while
  // one is going they may describe things that have since changed.
  const loading = Boolean(b.enabled && b.generating);
  const seen = new Set();
  const reveal = briefReveal("overall", loading, seen);
  const overall = b.overall;
  const headline = overall?.headline || b.fallback_headline;
  const priorities = overall?.priorities || [];
  const top = loading ? `
    <div class="skeleton-stack brief-top-loading" aria-hidden="true">
      ${skeletonLines([72, 44], "skeleton-headline")}
      <span class="skeleton-gap"></span>
      ${[0, 1, 2].map((i) => `<span class="skeleton-row" style="--i:${i}">
        <span class="skeleton skeleton-dot"></span><span class="skeleton" style="width:${[62, 48, 55][i]}%"></span></span>`).join("")}
    </div>
    <p class="visually-hidden" role="status">Writing a fresh summary&hellip;</p>` : `
    <div class="brief-top${reveal}">
    <h2 class="brief-headline">${esc(headline)}</h2>
    ${priorities.length ? `<ol class="brief-priorities">${priorities.map((item) => `
      <li>${item.href
        ? `<a href="${esc(item.href)}"><span>${esc(item.text)}</span>${icon("i-arrow")}</a>`
        : `<span>${esc(item.text)}</span>`}</li>`).join("")}</ol>` : ""}
    </div>`;
  host.dataset.level = b.level;
  host.setAttribute("aria-busy", String(loading));
  host.innerHTML = `
    <header class="brief-head">
      ${statusPill(word, levelName)}
      <div class="brief-meta">${briefingMeta(b)}</div>
      ${b.enabled ? `<button type="button" class="btn btn-small brief-refresh" data-brief-refresh
          ${b.generating ? "disabled" : ""} aria-label="Summarize again">${icon("i-refresh")}<span>Refresh</span></button>` : ""}
    </header>
    ${top}
    ${b.error ? `<p class="brief-error">${esc(b.error)}</p>` : ""}
    <div class="brief-grid">
      ${Object.keys(BRIEF_SECTIONS).map((name) => briefingSection(name, b.sections[name], seen)).join("")}
    </div>`;
  state.briefSkeletons = seen;
}

async function refreshBriefing() {
  try {
    await load("briefing", "/api/briefing", (briefing) => {
      state.briefing = briefing;
      renderBriefing();
    });
  } catch (error) {
    if (!state.briefing) {
      $("briefing").innerHTML = `<p class="brief-error">Could not load the summary: ${esc(error.message)}</p>`;
    }
  }
}

async function forceBriefing(button) {
  button.disabled = true;
  tickets.delete("briefing");  // a read already on its way would undo this
  try {
    state.briefing = await api("/api/briefing/refresh", { method: "POST" });
    renderBriefing();
    if (!state.briefing.started && !state.briefing.generating) toast("Nothing to summarize right now.");
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
  }
}

// --- profile ----------------------------------------------------------------
//
// Who you are, by the identity provider's word, and every place you are signed
// in. Only for OIDC sign-ins: the password has no sessions to list.

// The claims people recognise first; the rest follow in name order.
const CLAIM_ORDER = ["name", "given_name", "family_name", "preferred_username", "email",
  "email_verified", "groups", "sub"];

function claimValue(value) {
  if (Array.isArray(value)) return value.join(", ") || "none";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "");
}

async function refreshProfile() {
  let profile;
  try {
    profile = await api("/api/profile");
  } catch (error) {
    $("profile-identity").innerHTML = `<p class="empty">${esc(error.message)}</p>`;
    $("profile-sessions").innerHTML = "";
    $("profile-provider").innerHTML = "";
    return;
  }
  state.profile = profile;
  renderProfile(profile);
}

function renderProfile({ user, claims, provider, sessions }) {
  const avatar = user.picture
    ? `<img src="${esc(user.picture)}" alt="" data-initials="${esc(initials(user.name))}">`
    : esc(initials(user.name));
  const keys = Object.keys(claims).filter((key) => key !== "picture").sort((a, b) => {
    const rank = (key) => (CLAIM_ORDER.includes(key) ? CLAIM_ORDER.indexOf(key) : CLAIM_ORDER.length);
    return rank(a) - rank(b) || a.localeCompare(b);
  });
  $("profile-identity").innerHTML = `
    <div class="profile-who">
      <span class="avatar profile-avatar">${avatar}</span>
      <div>
        <strong>${esc(claims.name || user.name)}</strong>
        <span class="muted">${esc(claims.email || user.name)}</span>
      </div>
    </div>
    ${keys.length ? `<dl class="dl">${keys.map((key) =>
      `<dt>${esc(key)}</dt><dd>${esc(claimValue(claims[key]))}</dd>`).join("")}</dl>`
      : `<p class="muted">The provider has not described you beyond your subject, ${esc(user.sub)}.</p>`}`;

  $("sessions-end-others").hidden = sessions.filter((item) => !item.current).length === 0;
  $("profile-sessions").innerHTML = sessions.map((item) => {
    const what = [item.browser, item.platform].filter(Boolean).join(" on ");
    return `
    <li class="session"${item.current ? ' data-current="true"' : ""}>
      <div class="session-main">
        <div class="session-name">
          <strong>${esc(item.name)}</strong>
          ${item.current ? statusPill("This device", "ok") : ""}
        </div>
        <span class="muted">${esc([what, item.ip].filter(Boolean).join(" · ") || "No details")}</span>
        <span class="muted">Signed in ${agoTag(item.signed_in_at)} · last active ${agoTag(item.last_seen_at)}</span>
      </div>
      <div class="session-actions">
        <button type="button" class="btn btn-small btn-quiet" data-rename-session="${esc(item.id)}">Rename</button>
        <button type="button" class="btn btn-small btn-danger" data-end-session="${esc(item.id)}"
          data-current="${item.current}">Sign out</button>
      </div>
    </li>`;
  }).join("") || `<li class="empty">No sessions.</li>`;

  const mine = sessions.find((item) => item.current) || {};
  const rows = [
    ["Identity provider", provider.issuer],
    ["Client ID", provider.client_id],
    ["Scopes", provider.scopes.join(" ")],
    ["Groups claim", provider.groups_claim],
    ["Restricted to", provider.allowed_groups.join(", ") || "any account the provider authenticates"],
    ["Access token", mine.access_expires_at
      ? `renews ${new Date(mine.access_expires_at).toLocaleString()}` : "unknown"],
    ["Last renewed", mine.renewed_at ? new Date(mine.renewed_at).toLocaleString() : "not yet"],
    ["Refresh token", mine.renewable
      ? "held by the doctor, never sent to the browser"
      : "none - this session ends with its access token"],
    ["Idle limit", `${provider.idle_hours}h without a request signs a browser out`],
  ];
  $("profile-provider").innerHTML = `<dl class="dl">${rows.map(([key, value]) =>
    `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join("")}</dl>`;
}

async function endProfileSession(id, current) {
  const item = (state.profile?.sessions || []).find((s) => s.id === id);
  const question = current
    ? "Sign out of this device?"
    : `Sign out ${item ? item.name : "that session"}? It has to sign in again to come back.`;
  if (!confirm(question)) return;
  try {
    await api(`/api/profile/sessions/${encodeURIComponent(id)}/end`, { method: "POST" });
  } catch (error) {
    toast(`Could not sign it out: ${error.message}`, "error");
    return;
  }
  if (current) {
    location.reload();
    return;
  }
  toast("Signed out");
  refreshProfile();
}

async function endOtherSessions() {
  if (!confirm("Sign out every other session? Each has to sign in again to come back.")) return;
  try {
    const { ended } = await api("/api/profile/sessions/end-others", { method: "POST" });
    toast(`Signed out ${ended} session${ended === 1 ? "" : "s"}`);
  } catch (error) {
    toast(`Could not sign them out: ${error.message}`, "error");
  }
  refreshProfile();
}

async function renameProfileSession(id) {
  const item = (state.profile?.sessions || []).find((s) => s.id === id);
  const name = prompt("Name this session (empty goes back to the device's own name):",
    item?.label || item?.name || "");
  if (name === null) return;
  try {
    await api(`/api/profile/sessions/${encodeURIComponent(id)}/name`, {
      method: "PUT", body: JSON.stringify({ name: name.slice(0, 60) }),
    });
  } catch (error) {
    toast(`Could not rename it: ${error.message}`, "error");
    return;
  }
  refreshProfile();
}

// Browsers keep the name you gave a device to themselves; the model is the
// closest thing, and Chromium says it through Client Hints. Once a session.
async function reportDevice() {
  const hints = navigator.userAgentData;
  if (state.deviceReported || !hints?.getHighEntropyValues) return;
  state.deviceReported = true;
  try {
    const values = await hints.getHighEntropyValues(["model"]);
    await api("/api/profile/device", {
      method: "POST",
      body: JSON.stringify({ model: values.model || "", platform: values.platform || hints.platform || "" }),
    });
  } catch (_) { /* only a label; the User-Agent still names the kind of device */ }
}

// --- views ------------------------------------------------------------------
//
// One section of the app on screen at a time, chosen from the sidebar (or the
// tab bar on a phone), each with its own address: #/overview, #/incidents...

const VIEWS = {
  overview: ["Overview", "What is going on, and what to work on"],
  incidents: ["Incidents", "What the rules caught, and what was done about it"],
  containers: ["Containers", "Everything running on the host"],
  storage: ["Storage", "Free space, how fast it goes, and the disk arrays"],
  host: ["Host", "The host itself: CPU, memory, disks and temperatures"],
  network: ["Network", "Which containers share a network, and what the host publishes"],
  updates: ["Updates", "Images with a newer build or version"],
  activity: ["Activity", "Every decision, including the ones not taken"],
  llm: ["LLM", "What was sent to a model, and what it cost"],
  settings: ["Settings", "The running configuration"],
  profile: ["Profile", "Who you are signed in as, and where"],
};

function showView(name) {
  if (!VIEWS[name]) name = "overview";
  const changed = state.view !== name || !state.viewShown;
  state.view = name;
  state.viewShown = true;
  document.querySelectorAll(".view").forEach((view) => {
    view.hidden = view.dataset.view !== name;
  });
  document.querySelectorAll("[data-nav]").forEach((item) => {
    const current = item.dataset.nav === name
      || (item.dataset.nav === "more" && ["host", "network", "updates", "activity", "llm", "settings"].includes(name));
    if (current) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  // Configuration notices are about the doctor, not about this page: shown
  // where the doctor itself is the subject, not above every list.
  $("warnings").classList.toggle("is-offpage", !["overview", "settings"].includes(name));
  const [title, sub] = VIEWS[name];
  $("page-title").textContent = title;
  $("page-sub").textContent = sub;
  document.title = name === "overview" ? "Container Doctor" : `${title} - Container Doctor`;
  closeTabMenu();
  if (changed) {
    window.scrollTo({ top: 0 });
    // Charts drawn while their page was hidden had no width to draw at.
    requestAnimationFrame(() => { redrawCharts(); drawBridges(); });
    if (name === "overview") refreshBriefing();
    if (name === "llm") refreshLlm();
    if (name === "network") refreshNetwork();
    if (name === "host") { renderHost(); refreshHostHistory(); }
    if (name === "profile") refreshProfile();
  }
}

function closeTabMenu() {
  $("tab-menu").hidden = true;
  $("tab-more").setAttribute("aria-expanded", "false");
}

// The count that says a section wants a look, on its nav item.
function setBadge(name, count, level = "attention") {
  document.querySelectorAll(`[data-badge="${name}"]`).forEach((badge) => {
    badge.hidden = !count;
    badge.textContent = count > 99 ? "99+" : String(count || "");
    badge.dataset.level = level;
  });
}

function renderBadges() {
  const status = state.status || {};
  const containers = (state.snapshot.containers || []).filter((c) => c.watched && troubled(c)).length;
  const report = state.snapshot.raid || {};
  const arrays = (report.arrays || []).filter((a) => !a.healthy).length;
  const warned = (report.arrays || []).filter((a) => a.healthy && (a.warnings || []).length).length;
  // A drive with read errors is one problem, however many arrays it is in.
  const drives = (report.drives || []).filter((d) => d.summary);
  const drivesCritical = drives.some((d) => d.severity === "critical");
  const disks = (state.snapshot.disk || []).filter((d) =>
    d.used_pct >= (thresholds().disk ?? 90) || (d.trend?.days_until_full ?? Infinity) <= 21).length;
  const updates = imageUpdates();
  const behind = behindImages(updates).length;
  setBadge("incidents", Number(status.open_incidents || 0),
    Number(status.pending_approvals || 0) ? "critical" : "attention");
  setBadge("containers", containers);
  const smartBad = (state.snapshot.smart?.drives || []).filter((d) => d.severity);
  setBadge("storage", arrays + warned + drives.length + disks + smartBad.length,
    arrays || drivesCritical || smartBad.some((d) => d.severity === "critical") ? "critical" : "attention");
  const wiring = Number(state.network?.summary?.warnings || 0)
    + (state.snapshot.connectivity?.failing || []).length;
  const hostAlerts = (state.snapshot.host?.alerts || []).length;
  setBadge("updates", behind, "info");
  setBadge("network", wiring);
  setBadge("host", hostAlerts);
  setBadge("more", behind + wiring + hostAlerts, wiring || hostAlerts ? "attention" : "info");
}

// --- routes -------------------------------------------------------------------
//
// #incident/<id> and #container/<name>. Notifications link to the first, the
// container cards to the second; both survive a reload and a sign-in.

function currentRoute() {
  const view = location.hash.match(/^#\/([a-z]+)$/);
  if (view && VIEWS[view[1]]) return { kind: "view", id: view[1] };
  const match = location.hash.match(/^#(incident|container)\/(.+)$/);
  return match ? { kind: match[1], id: decodeURIComponent(match[2]) } : null;
}

async function focusIncident(id) {
  let incident;
  try {
    incident = await api(`/api/incidents/${encodeURIComponent(id)}`);
  } catch (_) {
    toast("That incident is no longer on record - it may have aged out.", "error");
    clearRoute();
    return;
  }
  state.focusIncident = incident;
  state.expanded.add(incident.id);
  renderIncidents();
  requestAnimationFrame(() => {
    const el = document.getElementById(`incident-${incident.id}`);
    if (!el) return;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
  });
}

async function followRoute() {
  const route = currentRoute();
  if (route?.kind === "container") {
    // Opened over the page it was linked from; straight from outside, over
    // the containers it belongs to.
    if (!state.viewShown) showView("containers");
    openDetail(route.id);
    return;
  }
  if (state.detailName) closeDetail();
  if (route?.kind === "incident") {
    showView("incidents");
    await focusIncident(route.id);
    return;
  }
  showView(route?.kind === "view" ? route.id : "overview");
}

// The provider's round trip drops the fragment, so a notification tapped
// while signed out would land on the top of the dashboard. Kept for the one
// tab, and put back once the sign-in completes.
const RETURN_KEY = "doctor-return-to";

function rememberRoute() {
  if (!currentRoute()) return;
  try { sessionStorage.setItem(RETURN_KEY, location.hash); } catch (_) { /* private mode */ }
}

function restoreRoute() {
  let saved = "";
  try {
    saved = sessionStorage.getItem(RETURN_KEY) || "";
    sessionStorage.removeItem(RETURN_KEY);
  } catch (_) { /* private mode */ }
  if (saved && !currentRoute() && /^#(incident|container)\//.test(saved)) {
    history.replaceState(null, "", location.pathname + location.search + saved);
  }
}

// Asked before anything else: every other endpoint 401s when OIDC is on and
// the browser has no session, and a wall of failed requests is a worse answer
// than a sign-in button.
async function refreshAccount() {
  let me;
  try {
    me = await api("/api/me");
  } catch (_) {
    return true;  // /api/me should not fail; carry on rather than lock the page
  }
  state.me = me;
  const blocked = !me.authenticated;
  if (blocked) rememberRoute();
  // The identity provider when OIDC is configured; otherwise the browser's
  // own password prompt, which only a challenge from the server brings up.
  const button = document.querySelector(".signin-btn");
  if (button && !me.oidc) {
    button.href = "/auth/password";
    button.textContent = "Sign in";
  }
  // No sign-in configured at all: nothing to press, only what to set.
  const unconfigured = me.configured === false;
  if (button) button.hidden = unconfigured;
  $("signin-setup").hidden = !unconfigured;
  $("signin").hidden = !blocked;
  watchProvider(blocked && me.oidc);
  $("dashboard").hidden = blocked;
  // The top bar is about a dashboard you cannot see yet, and its live dot sits
  // there reading "reconnecting" because the stream needs the session it is
  // asking you to create.
  $("shell").hidden = blocked;
  $("account").hidden = !me.user;
  if (me.user) renderAccount(me);
  if (me.profile && !me.device_reported) reportDevice();
  return !blocked;
}

// The dot under the sign-in button: is the identity provider answering? Asked while the
// card is up, and again when the tab comes back, since that is when someone
// is about to press the button.
let providerTimer = null;
function watchProvider(on) {
  const pill = $("provider-status");
  if (!pill) return;
  pill.hidden = !on;
  clearInterval(providerTimer);
  providerTimer = null;
  if (!on) return;
  checkProvider();
  providerTimer = setInterval(() => { if (!document.hidden) checkProvider(); }, 15000);
}

async function checkProvider() {
  const pill = $("provider-status");
  const text = $("provider-status-text");
  const button = document.querySelector(".signin-btn");
  let answer;
  try {
    answer = await api("/api/auth/provider");
  } catch (_) {
    return;  // the doctor itself is unreachable; the offline banner says so
  }
  pill.dataset.state = answer.online ? "online" : "offline";
  text.textContent = answer.online
    ? "The identity provider is online"
    : `The identity provider is unreachable${answer.detail ? ` (${answer.detail})` : ""}`;
  // Still pressable: the probe can be wrong, and the button explains a
  // failure itself. Dimmed so it does not promise more than it can do.
  if (button) button.dataset.provider = answer.online ? "online" : "offline";
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && providerTimer) checkProvider();
});

// An avatar that fails to load becomes its initials. Listened for here, not
// with an onerror attribute: the Content-Security-Policy allows no inline
// script. Error events do not bubble, so this listens in the capture phase.
document.addEventListener("error", (event) => {
  const img = event.target;
  if (img instanceof HTMLImageElement && img.dataset.initials !== undefined && img.parentElement) {
    img.parentElement.textContent = img.dataset.initials;
  }
}, true);

function initials(name) {
  return name.split(/[\s._@-]+/).filter(Boolean).slice(0, 2)
    .map((part) => part[0].toUpperCase()).join("") || "?";
}

function renderAccount(me) {
  const avatar = $("account-avatar");
  $("account-name").textContent = me.user;
  if (me.picture) {
    // Loaded straight from the provider; initials if that fails, so a blocked
    // or missing image never leaves an empty circle.
    // Setting the parent's textContent replaces the <img>; removing it first
    // would null out parentElement and leave a broken-image glyph.
    avatar.innerHTML = `<img src="${esc(me.picture)}" alt="" data-initials="${esc(initials(me.user))}">`;
  } else {
    avatar.textContent = initials(me.user);
  }
  $("account-menu").innerHTML = `
    <div class="account-head">
      <strong>${esc(me.user)}</strong>
      ${(me.groups || []).length ? `<span>${me.groups.map(esc).join(", ")}</span>` : ""}
    </div>
    ${me.profile ? `<button type="button" role="menuitem" data-account="profile">Profile</button>` : ""}
    <button type="button" role="menuitem" data-account="settings">Settings</button>
    <a role="menuitem" href="/auth/logout">Sign out</a>`;
}

function closeAccountMenu() {
  $("account-menu").hidden = true;
  $("account-btn").setAttribute("aria-expanded", "false");
}

async function refreshAll({ account = true } = {}) {
  if (account && !(await refreshAccount())) return;
  // Each part draws as soon as its own answer is in: the slowest of them no
  // longer holds back the rest of the page.
  const parts = [
    load("status", "/api/status", (status) => {
      state.status = status;
      renderStatus();
      renderBadges();
    }),
    load("incidents", `/api/incidents?limit=${state.incidentLimit}${incidentQuery()}`, async (incidents) => {
      state.incidents = incidents.incidents;
      state.incidentTotal = incidents.total ?? incidents.incidents.length;
      state.incidentsLoaded = true;
      // Outside the filter, the linked incident is not in the page just
      // fetched; ask for it again so an approval on it shows up.
      const focus = state.focusIncident;
      if (focus && !state.incidents.some((i) => i.id === focus.id)) {
        try { state.focusIncident = await api(`/api/incidents/${encodeURIComponent(focus.id)}`); } catch (_) { /* keep the last copy */ }
      }
      renderIncidents();
    }),
    load("actions", `/api/actions?limit=${state.actionLimit}`, (actions) => {
      state.actions = actions.actions;
      state.actionTotal = actions.total ?? actions.actions.length;
      state.actionsLoaded = true;
      renderActions();
    }),
    load("changes", `/api/changes?limit=${state.changeLimit}`, (changes) => {
      state.changes = changes.changes;
      state.changeTotal = changes.total ?? changes.changes.length;
      state.changesLoaded = true;
      renderChanges();
    }),
  ];
  // Configuration changes only on restart, so fetch it once - alongside the
  // rest, not ahead of it: it can wait on GitHub for the deploy stacks.
  if (!state.settingsLoaded && !state.settingsLoading) {
    state.settingsLoading = true;
    load("settings", "/api/settings", (answer) => {
      renderSettings(answer.sections, answer.compose_read !== false, answer);
      state.settingsLoaded = true;
    }).catch(() => {
      // A reader without access: the theme is still theirs to choose.
      renderSettings([]);
    }).finally(() => { state.settingsLoading = false; });
  }
  refreshMemory();
  if (state.view === "llm") refreshLlm();
  const failed = (await Promise.allSettled(parts)).find((r) => r.status === "rejected");
  if (failed) toast(`Could not refresh: ${failed.reason?.message || failed.reason}`, "error");
}

// -- reachability ------------------------------------------------------------
// An installed PWA stays open while the phone drops off the VPN. Rather than
// a toast per failed request, say once what is going on, keep what was last
// seen on screen (dimmed), and keep trying until the doctor answers again.

const RETRY_SECONDS = [3, 5, 10, 20, 30];
let retryTimer = null;
let retryAt = 0;
let retryStep = 0;
let checking = false;

function noteContact() {
  state.lastContact = Date.now();
}

function connectionLost(kind) {
  if (state.offline === kind) return;
  const first = !state.offline;
  state.offline = kind;
  if (first) {
    retryStep = 0;
    $("toast").hidden = true;
    scheduleCheck();
  }
  renderOffline();
  renderPoll();
}

function connectionBack() {
  if (!state.offline) return;
  state.offline = null;
  clearTimeout(retryTimer);
  retryTimer = null;
  noteContact();
  renderOffline();
  // The stream retries by itself, but a phone that slept may have closed it
  // for good; and everything else has been standing still since.
  if (!state.source || state.source.readyState === EventSource.CLOSED) connectStream();
  refreshAll();
  if (state.view === "overview") refreshBriefing();
}

function scheduleCheck(seconds) {
  clearTimeout(retryTimer);
  const wait = seconds ?? RETRY_SECONDS[Math.min(retryStep, RETRY_SECONDS.length - 1)];
  retryStep += 1;
  retryAt = Date.now() + wait * 1000;
  retryTimer = setTimeout(checkConnection, wait * 1000);
  renderOffline();
}

// /healthz needs no session and does no work, so it is the cheapest honest
// answer to "is the doctor there?".
async function checkConnection() {
  if (checking) return;
  checking = true;
  clearTimeout(retryTimer);
  retryAt = 0;
  renderOffline();
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 6000);
  let kind = null;
  try {
    const response = await fetch("/healthz", { cache: "no-store", signal: abort.signal });
    if (!response.ok) kind = "restarting";
  } catch (_) {
    kind = "network";
  } finally {
    clearTimeout(timeout);
    checking = false;
  }
  if (kind) {
    connectionLost(kind);
    if (!document.hidden) scheduleCheck();
    else renderOffline();  // tried again when the app comes back to the front
  } else {
    connectionBack();
  }
}

function renderOffline() {
  const box = $("offline");
  document.body.toggleAttribute("data-offline", Boolean(state.offline));
  box.hidden = !state.offline;
  if (!state.offline) return;
  const restarting = state.offline === "restarting";
  $("offline-title").textContent = restarting
    ? "Container Doctor is restarting"
    : "Can't reach Container Doctor";
  const seen = state.lastContact
    ? ` What you see is from ${new Date(state.lastContact).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
      + ` (${ago(new Date(state.lastContact).toISOString())}) and is not updating.`
    : "";
  $("offline-text").textContent = (restarting
    ? "It is coming back up - usually within a minute."
    : "Your device may be off the VPN or the home network.") + seen;
  const button = $("offline-retry");
  button.disabled = checking;
  button.classList.toggle("is-busy", checking);
  const left = Math.max(0, Math.ceil((retryAt - Date.now()) / 1000));
  $("offline-when").textContent = checking ? "Checking…"
    : retryAt ? `Trying again in ${left}s` : "Paused while in the background";
}

window.addEventListener("offline", () => connectionLost("network"));
window.addEventListener("online", () => { if (state.offline) checkConnection(); });
document.addEventListener("visibilitychange", () => {
  // Back from the background, possibly with the VPN now up: look straight
  // away instead of waiting out the backoff.
  if (!document.hidden && state.offline) checkConnection();
});

function connectStream() {
  if (state.source) state.source.close();
  const source = new EventSource("/api/stream");
  state.source = source;
  source.onopen = () => { state.streamDown = false; noteContact(); connectionBack(); renderPoll(); };
  source.onmessage = (event) => {
    state.streamDown = false;
    noteContact();
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.type === "state") {
      state.snapshot = message.payload;
      state.snapshotLoaded = true;
      renderContainers();
      renderDisk();
      renderSmart();
      renderImages();
      renderBadges();
      refreshNetwork();
      renderConnectivity();
      if (state.view === "host") {
        renderHost();
        // The stored history moves on a few minutes at a time.
        if (Date.now() - state.hostHistoryAt > 120000) refreshHostHistory();
      }
      if (state.detailName) loadDetail(state.detailName);
      if (state.diskHistory === null && (state.snapshot.disk || []).length) {
        state.diskHistory = [];  // once; the interval keeps it fresh after that
        refreshDiskHistory();
      }
    } else if (message.type === "settings") {
      // Applied from this tab or another: show what runs now.
      if (state.settingsLoaded) reloadSettings().catch(() => {});
    } else if (message.type === "connectivity") {
      state.snapshot = { ...state.snapshot, connectivity: message.payload };
      renderConnectivity();
      renderBadges();
    } else if (message.type === "images") {
      state.snapshot = {
        ...state.snapshot,
        image_updates: { ...imageUpdates(), ...message.payload },
      };
      renderImages();  // the results so far, as well as the progress
    } else if (message.type === "poll") {
      state.snapshot = {
        ...state.snapshot,
        polling: Boolean(message.payload?.polling),
        poll_progress: message.payload?.progress ?? null,
      };
      renderPoll();
    } else if (message.type === "briefing") {
      if (state.view === "overview") refreshBriefing();
    } else {
      // Any incident/diagnosis/action event changes the lists and the tiles.
      refreshAll();
    }
  };
  source.onerror = () => {
    state.streamDown = true;
    renderPoll();
    // The stream retries on its own; find out whether the doctor is gone
    // altogether, which is worth a banner rather than a red ring.
    if (!state.offline) checkConnection();
  };
}


function setFilter(name) {
  state.filter = name;
  state.focusIncident = null;
  state.incidentLimit = pageSize("incidents");  // a new filter starts at the first page
  document.querySelectorAll(".chip").forEach((chip) =>
    chip.classList.toggle("is-active", chip.dataset.filter === name));
}

function jumpTo(selector) {
  const target = document.querySelector(selector);
  if (!target) return;
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
}

// Paging is local for what the page already holds, and a bigger request for
// what the server pages. "Show fewer" also brings the list's head back into
// view: collapsing a long list otherwise strands you below it.
const PAGED_PANELS = {
  containers: "#panel-containers", images: "#image-panel",
  incidents: "#panel-incidents", actions: "#panel-actions", changes: "#panel-changes",
  calls: "#panel-calls",
};

function page(kind, grow) {
  if (kind === "incidents") {
    state.incidentLimit = grow ? Math.min(state.incidentLimit + pageSize("incidents"), MAX_ROWS) : pageSize("incidents");
    refreshAll();
  } else if (kind === "actions") {
    state.actionLimit = grow ? Math.min(state.actionLimit + pageSize("actions"), MAX_ROWS) : pageSize("actions");
    refreshAll();
  } else if (kind === "changes") {
    state.changeLimit = grow ? Math.min(state.changeLimit + pageSize("changes"), MAX_ROWS) : pageSize("changes");
    refreshAll();
  } else if (kind === "calls") {
    state.callLimit = grow ? Math.min(state.callLimit + pageSize("calls"), MAX_ROWS) : pageSize("calls");
    refreshLlm();
  } else {
    state.shown[kind] = grow ? state.shown[kind] + pageSize(kind) : pageSize(kind);
    kind === "containers" ? renderContainers() : renderImages();
  }
  if (!grow) jumpTo(PAGED_PANELS[kind]);
}

document.addEventListener("click", (event) => {
  const tile = event.target.closest(".tile-link");
  if (tile && tile.dataset.jumpFilter) {
    // The link itself changes the page; this only picks the filter first.
    setFilter(tile.dataset.jumpFilter);
    refreshAll();
    return;
  }
  const more = event.target.closest("[data-more],[data-fewer]");
  if (more) {
    page(more.dataset.more || more.dataset.fewer, Boolean(more.dataset.more));
    return;
  }
  const reason = event.target.closest("[data-reason]");
  if (reason) {
    const id = reason.dataset.reason;
    state.openReasons.has(id) ? state.openReasons.delete(id) : state.openReasons.add(id);
    renderActions();
    return;
  }
  const theme = event.target.closest("[data-theme-choice]");
  if (theme) {
    applyTheme(theme.dataset.themeChoice);
    return;
  }
  const settingsTabButton = event.target.closest("[data-settings-tab]");
  if (settingsTabButton) {
    chooseSettingsTab(settingsTabButton.dataset.settingsTab);
    return;
  }
  if (event.target.closest("#offline-retry")) {
    checkConnection();
    return;
  }
  if (event.target.closest("#poll-now")) {
    pollNow();
    return;
  }
  if (event.target.closest("#images-refresh")) {
    refreshImages();
    return;
  }
  const notesButton = event.target.closest("[data-notes-image]");
  if (notesButton) {
    toggleNotes(notesButton.dataset.notesImage, notesButton.dataset.notesTag);
    return;
  }
  const redeployButton = event.target.closest("[data-redeploy]");
  if (redeployButton) {
    redeployStack(redeployButton);
    return;
  }
  const summarize = event.target.closest("[data-brief-refresh]");
  if (summarize) {
    forceBriefing(summarize);
    return;
  }
  const density = event.target.closest("[data-density][data-mode]");
  if (density) {
    setDensity(density.dataset.density, density.dataset.mode);
    return;
  }
  const grouping = event.target.closest("[data-group]");
  if (grouping) {
    setGrouped(grouping.dataset.group === "stacks");
    return;
  }
  // Remembered only when a person toggles it: the open attribute is also set
  // by rendering, and that is not a preference.
  const call = event.target.closest("[data-llm-call]");
  if (call) {
    openCall(call.dataset.llmCall);
    return;
  }
  const bay = event.target.closest("[data-drive]");
  if (bay) {
    openDrive(bay.dataset.drive);
    return;
  }
  if (event.target.closest("[data-close-drive]")) {
    $("drive").close();
    return;
  }
  if (event.target.closest("[data-close-call]")) {
    $("llm-call").close();
    return;
  }
  const copyCall = event.target.closest("[data-copy-call]");
  if (copyCall) {
    const c = $("llm-call-body")._call;
    const r = c?.request || {};
    const text = `${r.system || ""}\n\n---\n\n${r.prompt || ""}`;
    navigator.clipboard?.writeText(text).then(() => toast("Prompt copied."),
      () => toast("The clipboard is not available here.", "error"));
    return;
  }
  const driveHours = event.target.closest("[data-drive-hours]");
  if (driveHours) {
    state.driveTempHours = Number(driveHours.dataset.driveHours);
    document.querySelectorAll("[data-drive-hours]").forEach((b) =>
      b.setAttribute("aria-checked", String(b === driveHours)));
    refreshDriveTemps();
    refreshDriveSleep();
    refreshDriveIo();
    return;
  }
  const hostHours = event.target.closest("[data-host-hours]");
  if (hostHours) {
    state.hostHours = Number(hostHours.dataset.hostHours);
    document.querySelectorAll("[data-host-hours]").forEach((b) =>
      b.setAttribute("aria-checked", String(b === hostHours)));
    refreshHostHistory();
    return;
  }
  const days = event.target.closest("[data-spend-days]");
  if (days) {
    state.spendDays = Number(days.dataset.spendDays);
    document.querySelectorAll("[data-spend-days]").forEach((b) =>
      b.setAttribute("aria-checked", String(b === days)));
    $("panel-spend").setAttribute("aria-busy", "true");
    refreshLlm();
    return;
  }
  const callFilter = event.target.closest("[data-call-filter]");
  if (callFilter) {
    state.callFilter = callFilter.dataset.callFilter;
    state.callLimit = pageSize("calls");
    document.querySelectorAll("[data-call-filter]").forEach((chip) =>
      chip.classList.toggle("is-active", chip === callFilter));
    $("panel-calls").setAttribute("aria-busy", "true");
    refreshLlm();
    return;
  }
  const unlimited = event.target.closest("[data-mem-unlimited] > summary");
  if (unlimited) {
    state.memoryUnlimitedOpen = !unlimited.parentElement.open;
    return;
  }
  const stack = event.target.closest(".stack-group > summary");
  if (stack) {
    const details = stack.parentElement;
    state.stackOpen.set(details.dataset.stack, !details.open);
    return;
  }
  if (event.target.closest("[data-close-detail]")) {
    clearRoute();
    closeDetail();
    return;
  }
  const brief = event.target.closest("[data-handoff]");
  if (brief) {
    copyHandoff(brief.dataset.handoff, brief);
    return;
  }
  if (event.target.closest("[data-close-handoff]")) {
    $("handoff").close();
    return;
  }
  const copy = event.target.closest("[data-copy-link]");
  if (copy) {
    copyLink(`${location.origin}${location.pathname}#incident/${copy.dataset.copyLink}`);
  }
});

// The whole case as Markdown, for an LLM agent the operator runs themselves.
// Safari only lets a page write to the clipboard inside the click itself, and
// the text has to be fetched first; a ClipboardItem built from the pending
// fetch satisfies both. Anything that cannot do that gets the text in a box.
async function copyHandoff(id, button) {
  const url = `/api/incidents/${encodeURIComponent(id)}/handoff`;
  const fetched = fetch(url).then(async (response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.text();
  });
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Copying\u2026";
  let text = "";
  try {
    if (window.ClipboardItem && navigator.clipboard?.write) {
      await navigator.clipboard.write([new ClipboardItem({
        "text/plain": fetched.then((body) => new Blob([body], { type: "text/plain" })),
      })]);
      text = await fetched;
    } else {
      text = await fetched;
      await navigator.clipboard.writeText(text);
    }
    toast(`Copied ${Math.max(1, Math.round(text.length / 1024))} KB of Markdown - paste it into your agent.`);
  } catch (_) {
    try {
      text = text || await fetched;
      showHandoff(text, id);
    } catch (error) {
      toast(`Could not build the brief: ${error.message}`, "error");
    }
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

// Named on the link as well as by the server: given an empty download
// attribute, Chromium made up its own name rather than use the header's.
function handoffName(id) {
  return `incident-${String(id).replace(/[^A-Za-z0-9]/g, "").slice(0, 32) || "incident"}.md`;
}

// The fallback: the text, selected, with a download next to it.
function showHandoff(text, id) {
  const dialog = $("handoff");
  $("handoff-text").value = text;
  $("handoff-download").href = `/api/incidents/${encodeURIComponent(id)}/handoff?download=1`;
  $("handoff-download").setAttribute("download", handoffName(id));
  if (!dialog.open) dialog.showModal();
  $("handoff-text").focus();
  $("handoff-text").select();
}

async function copyLink(url) {
  try {
    await navigator.clipboard.writeText(url);
    toast("Link copied.");
  } catch (_) {
    // No clipboard outside a secure context: show it to copy by hand.
    window.prompt("Copy this link", url);
  }
}

// The panel carries the choice, so the stylesheet does most of the work;
// the containers are the one list drawn differently rather than restyled.
const DENSITY_PANELS = {
  incidents: "panel-incidents", containers: "panel-containers", storage: "disk-panel",
  updates: "image-panel", activity: "panel-actions", memory: "panel-memory",
  changes: "panel-changes", calls: "panel-calls", network: "panel-network",
};

function applyDensity() {
  for (const [list, panel] of Object.entries(DENSITY_PANELS)) {
    const mode = compact(list) ? "compact" : "full";
    $(panel).dataset.density = mode;
    document.querySelectorAll(`[data-density="${list}"]`).forEach((button) =>
      button.setAttribute("aria-checked", String(button.dataset.mode === mode)));
  }
}

function setDensity(list, mode) {
  state.density[list] = mode;
  try { localStorage.setItem("doctor-density", JSON.stringify(state.density)); } catch (_) { /* private mode */ }
  applyDensity();
  // Back to the first page, at the new page size.
  if (list === "containers") { state.shown.containers = pageSize("containers"); renderContainers(); }
  if (list === "updates") { state.shown.images = pageSize("images"); renderImages(); }
  if (list === "incidents") { state.incidentLimit = pageSize("incidents"); refreshAll(); }
  if (list === "activity") { state.actionLimit = pageSize("actions"); refreshAll(); }
  if (list === "changes") { state.changeLimit = pageSize("changes"); refreshAll(); }
  if (list === "calls") { state.callLimit = pageSize("calls"); refreshLlm(); }
  // Charts drawn while hidden have no width to go by.
  if (list === "storage") redrawCharts();
  if (list === "network") renderNetwork();
}

function setGrouped(grouped) {
  state.grouped = grouped;
  try { localStorage.setItem("doctor-grouped", grouped ? "stacks" : "flat"); } catch (_) { /* private mode */ }
  document.querySelectorAll("[data-group]").forEach((button) =>
    button.setAttribute("aria-checked", String((button.dataset.group === "stacks") === grouped)));
  renderContainers();
}

document.addEventListener("click", async (event) => {
  const tail = event.target.closest("[data-detail-tail]");
  if (tail) {
    const host = $("detail-log");
    tail.disabled = true;
    try {
      renderLiveTail(host, await api(`/api/containers/${encodeURIComponent(tail.dataset.detailTail)}/logs?lines=300`));
    } catch (error) {
      host.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
    } finally {
      tail.disabled = false;
    }
  }
});

// Dismissing answers on screen straight away; the server catches up behind
// it. Only a failure puts the incident back.
async function dismissIncident(id) {
  if (state.dismissing.has(id)) return;
  const wasOpen = [...state.incidents, state.focusIncident].some((i) =>
    i?.id === id && i.status !== "resolved" && i.status !== "dismissed");
  const adjustOpen = (delta) => {
    if (!wasOpen || !state.status) return;
    state.status.open_incidents = Math.max(0, Number(state.status.open_incidents || 0) + delta);
    $("tile-incidents").textContent = state.status.open_incidents;
    renderBadges();
  };
  state.dismissing.add(id);
  adjustOpen(-1);
  renderIncidents();
  toast("Incident dismissed.");
  try {
    await api(`/api/incidents/${encodeURIComponent(id)}/dismiss`, { method: "POST" });
  } catch (error) {
    state.dismissing.delete(id);
    adjustOpen(1);
    renderIncidents();
    toast(`Could not dismiss: ${error.message}`, "error");
    return;
  }
  // Fetched after the dismissal landed, so the lists now say so themselves.
  await refreshAll();
  state.dismissing.delete(id);
  renderIncidents();
}

document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-toggle],[data-approve],[data-reject],[data-dismiss],[data-restart],[data-filter],[data-tail],[data-context],[data-history]");
  if (!target) return;

  if (target.dataset.toggle) {
    const id = target.dataset.toggle;
    state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
    // Closing the incident a link opened: the address no longer describes
    // what is on screen.
    if (!state.expanded.has(id) && currentRoute()?.id === id) clearRoute();
    renderIncidents();
    return;
  }

  if (target.dataset.filter) {
    setFilter(target.dataset.filter);
    refreshAll();
    return;
  }

  const run = async (label, fn) => {
    target.disabled = true;
    try {
      await fn();
      toast(label);
      await refreshAll();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      target.disabled = false;
    }
  };

  if (target.dataset.tail || target.dataset.context || target.dataset.history) {
    // These re-render into the incident's own output pane rather than
    // refreshing the page, so an open log view survives the next poll.
    const incidentId = target.closest(".incident").querySelector("[data-toggle]").dataset.toggle;
    const host = document.getElementById(`logout-${incidentId}`);
    const original = target.textContent;
    target.disabled = true;
    target.textContent = "Loading\u2026";
    try {
      if (target.dataset.tail) {
        renderLiveTail(host, await api(`/api/containers/${encodeURIComponent(target.dataset.tail)}/logs?lines=300`));
      } else if (target.dataset.history) {
        renderHistory(host, await api(
          `/api/incidents/${encodeURIComponent(target.dataset.history)}/occurrences?limit=200`));
      } else {
        renderContext(host, await api(`/api/incidents/${target.dataset.context}/context?minutes=5`));
      }
    } catch (error) {
      host.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
    } finally {
      target.disabled = false;
      target.textContent = original;
    }
    return;
  }

  if (target.dataset.approve) {
    await run("Action approved and running.", () =>
      api(`/api/actions/${target.dataset.approve}/approve`, { method: "POST" }));
  } else if (target.dataset.reject) {
    await run("Action rejected.", () =>
      api(`/api/actions/${target.dataset.reject}/reject`, { method: "POST" }));
  } else if (target.dataset.dismiss) {
    await dismissIncident(target.dataset.dismiss);
  } else if (target.dataset.restart) {
    const name = target.dataset.restart;
    if (!confirm(`Restart ${name}?`)) return;
    await run(`Restarting ${name}.`, () =>
      api("/api/manual-action", {
        method: "POST",
        body: JSON.stringify({ container: name, action: "restart_container" }),
      }));
  }
});

document.addEventListener("click", (event) => {
  if (event.target.closest("#account-btn")) {
    const menu = $("account-menu");
    menu.hidden = !menu.hidden;
    $("account-btn").setAttribute("aria-expanded", String(!menu.hidden));
    return;
  }
  if (event.target.closest('[data-account="settings"]')) {
    closeAccountMenu();
    location.hash = "#/settings";
    return;
  }
  if (event.target.closest('[data-account="profile"]')) {
    closeAccountMenu();
    location.hash = "#/profile";
    return;
  }
  const endSession = event.target.closest("[data-end-session]");
  if (endSession) {
    endProfileSession(endSession.dataset.endSession, endSession.dataset.current === "true");
    return;
  }
  const renameSession = event.target.closest("[data-rename-session]");
  if (renameSession) {
    renameProfileSession(renameSession.dataset.renameSession);
    return;
  }
  if (event.target.closest("#sessions-end-others")) {
    endOtherSessions();
    return;
  }
  if (event.target.closest("#tab-more")) {
    const menu = $("tab-menu");
    menu.hidden = !menu.hidden;
    $("tab-more").setAttribute("aria-expanded", String(!menu.hidden));
    return;
  }
  if (!event.target.closest("#tab-menu")) closeTabMenu();
  if (!event.target.closest("#account-menu")) closeAccountMenu();
});

// Placed as they open, and kept in place while the page moves under them.
document.addEventListener("mouseover", (event) => {
  const wrap = event.target.closest(".info-wrap");
  if (wrap) placePop(wrap);
  netHighlight(event);
});
document.addEventListener("focusin", (event) => {
  const wrap = event.target.closest(".info-wrap");
  if (wrap) placePop(wrap);
  netHighlight(event);
});
$("panel-network").addEventListener("mouseleave", () => {
  $("panel-network").classList.remove("is-pointing");
  $("panel-network").querySelectorAll(".is-hot").forEach((el) => el.classList.remove("is-hot"));
});
window.addEventListener("scroll", placeOpenPops, { passive: true, capture: true });
window.addEventListener("resize", () => {
  placeOpenPops();
  markClamps();
  redrawCharts();
  drawBridges();
});

document.addEventListener("pointermove", chartHover);
document.addEventListener("pointermove", spendHover);
// pointerleave does not bubble; caught on the way down instead.
document.addEventListener("pointerleave", (event) => {
  if (event.target.classList?.contains("chart")) chartLeave(event);
}, true);

// --- re-diagnose with context --------------------------------------------------

document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-diagnose-open]");
  if (opener) {
    const id = opener.dataset.diagnoseOpen;
    state.diagnoseOpen.add(id);
    renderIncidents();
    document.getElementById(`context-${id}`)?.focus();
    return;
  }
  const cancel = event.target.closest("[data-diagnose-cancel]");
  if (cancel) {
    const id = cancel.dataset.diagnoseCancel;
    document.activeElement?.blur();
    state.diagnoseOpen.delete(id);
    state.diagnoseDrafts.delete(id);
    renderIncidents();
  }
});

document.addEventListener("input", (event) => {
  const box = event.target.closest("[data-context-draft]");
  if (box) state.diagnoseDrafts.set(box.dataset.contextDraft, box.value);
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-diagnose-form]");
  if (!form) return;
  event.preventDefault();
  const id = form.dataset.diagnoseForm;
  const context = form.querySelector("textarea").value.trim();
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  button.textContent = "Diagnosing\u2026";
  try {
    await api(`/api/incidents/${encodeURIComponent(id)}/diagnose`, {
      method: "POST",
      body: JSON.stringify({ context }),
    });
    state.diagnoseOpen.delete(id);
    state.diagnoseDrafts.delete(id);
    document.activeElement?.blur();
    toast(context ? "Re-diagnosed with your context." : "Re-diagnosed.");
    await refreshAll();
  } catch (error) {
    // The form stays open with what was typed, so a budget or provider
    // error costs a retry rather than the note.
    toast(error.message, "error");
    button.disabled = false;
    button.textContent = "Run diagnosis";
  }
});

window.addEventListener("hashchange", followRoute);

// The drive's dialog: likewise, and forgotten once closed.
$("drive").addEventListener("click", (event) => {
  if (event.target === $("drive")) $("drive").close();
});
$("drive").addEventListener("close", () => { state.driveKey = null; });

// The call log's dialog: a click on the backdrop lands on the dialog itself.
// The classifier's details are drawn when opened and stay open across re-renders.
document.addEventListener("toggle", (event) => {
  const box = event.target;
  if (!(box instanceof HTMLDetailsElement) || !box.dataset.classifierFor) return;
  const id = box.dataset.classifierFor;
  if (!box.open) {
    state.classifierOpen.delete(id);
    return;
  }
  state.classifierOpen.add(id);
  if (box.querySelector(".cls-grid")) return;
  const incident = [...(state.incidents || []), state.focusIncident].find((i) => i && String(i.id) === id);
  const v = incident?.evidence?.classification;
  if (v) box.insertAdjacentHTML("beforeend", classifierDetails(v));
}, true);

$("llm-call").addEventListener("click", (event) => {
  if (event.target === $("llm-call")) $("llm-call").close();
});

const detailDialog = $("detail");
// Escape closes a <dialog> by itself; keep the address in step with it.
detailDialog.addEventListener("close", () => {
  state.detailName = null;
  if (currentRoute()?.kind === "container") clearRoute();
});
// A click on the backdrop lands on the dialog element itself.
detailDialog.addEventListener("click", (event) => {
  if (event.target === detailDialog) {
    clearRoute();
    closeDetail();
  }
});

$("container-search").addEventListener("input", (event) => {
  state.containerFilter = event.target.value;
  state.shown.containers = pageSize("containers");
  renderContainers();
});

$("settings-search").addEventListener("input", filterSettings);

// Search and the view filters sit behind icons until asked for.
function setSearchOpen(open) {
  const wrap = $("settings-search-wrap");
  const input = $("settings-search");
  wrap.classList.toggle("is-open", open);
  $("settings-search-open").setAttribute("aria-expanded", String(open));
  input.tabIndex = open ? 0 : -1;
  if (open) {
    input.focus();
  } else if (input.value) {
    input.value = "";
    filterSettings();
  }
}
$("settings-search-open").addEventListener("click", () => {
  setSearchOpen(!$("settings-search-wrap").classList.contains("is-open"));
});
$("settings-search").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    setSearchOpen(false);
    $("settings-search-open").focus();
  }
});
$("settings-search").addEventListener("blur", () => {
  if (!$("settings-search").value) setSearchOpen(false);
});

function setFilterMenuOpen(open) {
  $("settings-filter-menu").hidden = !open;
  $("settings-filter-open").setAttribute("aria-expanded", String(open));
}
$("settings-filter-open").addEventListener("click", () => {
  setFilterMenuOpen($("settings-filter-menu").hidden);
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".settings-filter")) setFilterMenuOpen(false);
});
$("settings-filter-menu").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    setFilterMenuOpen(false);
    $("settings-filter-open").focus();
  }
});
for (const [id, key] of [["settings-changed", "changed"], ["settings-details", "details"]]) {
  $(id).addEventListener("click", () => {
    const view = settingsView();
    view[key] = !view[key];
    try { localStorage.setItem(SETTINGS_VIEW_KEY, JSON.stringify(view)); } catch (_) { /* private mode */ }
    filterSettings();
  });
}

// Arrow keys move between the Settings tabs, as a tablist should.
$("settings-tabs").addEventListener("keydown", (event) => {
  const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
  if (!step && !["Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const tabs = SETTINGS_TABS.map(([value]) => value);
  const at = tabs.indexOf(settingsTab());
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
    : (at + step + tabs.length) % tabs.length;
  chooseSettingsTab(tabs[next], true);
});

$("only-issues").addEventListener("change", (event) => {
  state.onlyIssues = event.target.checked;
  state.shown.containers = pageSize("containers");
  renderContainers();
});

$("dismiss-all").addEventListener("click", async () => {
  // The server dismisses every open incident, not the page on screen, so the
  // count has to be the server's too.
  const open = Number(state.status?.open_incidents ?? 0);
  if (!open) return toast("Nothing open to dismiss.");
  if (!confirm(`Dismiss ${open} open incident(s)? They stay in the record and age out at the retention limit.`)) return;
  const res = await fetch("/api/incidents/bulk-dismiss", { method: "POST" });
  if (!res.ok) return toast("Could not dismiss - are you signed in?");
  const body = await res.json();
  toast(`Dismissed ${body.dismissed}.`);
  refreshAll();
});


try {
  const saved = localStorage.getItem("doctor-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  syncThemeColor(currentTheme());
  state.grouped = localStorage.getItem("doctor-grouped") === "stacks";
  const density = JSON.parse(localStorage.getItem("doctor-density") || "{}");
  if (density && typeof density === "object") state.density = density;
} catch (_) { /* private mode, or a value this version cannot read */ }
applyDensity();
state.incidentLimit = pageSize("incidents");
state.actionLimit = pageSize("actions");
state.changeLimit = pageSize("changes");
state.callLimit = pageSize("calls");
state.shown = { containers: pageSize("containers"), images: pageSize("images") };
document.querySelectorAll("[data-group]").forEach((button) =>
  button.setAttribute("aria-checked", String((button.dataset.group === "stacks") === state.grouped)));

mountHeadingInfo();
paintSkeletons();
// Only the account check goes first - without a session everything else
// 401s. Then the stream, the page and the lists all start together.
refreshAccount().then((signedIn) => {
  if (!signedIn) return;
  connectStream();
  restoreRoute();
  followRoute();
  refreshAll({ account: false });
});
setInterval(refreshAll, 60000);
// Asking is free - the server only rewrites what went stale - so the
// Overview looks again every few minutes, and whenever the tab comes back.
setInterval(() => { if (state.view === "overview" && !document.hidden) refreshBriefing(); }, 5 * 60 * 1000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.view === "overview") refreshBriefing();
});
// Samples are hourly; the chart gains nothing from asking more often.
setInterval(refreshDiskHistory, 10 * 60 * 1000);
setInterval(retimeAll, 1000);
setInterval(() => { if (state.offline) renderOffline(); }, 1000);
// Only so a cold start without the VPN shows a page of ours rather than the
// browser's error; see sw.js.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* http, or blocked */ });
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeAccountMenu();
});
