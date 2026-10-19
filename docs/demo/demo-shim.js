// The demo's stand-in for the doctor's server, loaded before the dashboard.
//
// Answers every GET the dashboard makes from api.json - recorded from the real
// doctor running against a made-up host (python -m demo.record) - with its
// times moved up to now, so "3 minutes ago" stays 3 minutes ago. Anything that
// would change something is refused with a note that this is a demo. There is
// no live stream and no service worker.
(() => {
  "use strict";

  const DEMO_NOTE = "This is a demo: it replays a recording, so nothing here changes anything.";
  const realFetch = window.fetch.bind(window);
  const recording = realFetch(new URL("api.json", document.baseURI)).then((r) => r.json());

  // ISO times and plain dates, shifted by how long ago the recording was made.
  const STAMP = /\b(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?/g;
  const DAY = /"(\d{4}-\d{2}-\d{2})"/g;
  let shift = 0;

  function moved(text) {
    if (!shift) return text;
    return text
      .replace(STAMP, (whole, date, time, fraction = "", zone = "") => {
        const at = Date.parse(`${date}T${time}${fraction}${zone || "Z"}`);
        if (Number.isNaN(at)) return whole;
        const iso = new Date(at + shift).toISOString(); // 2026-10-17T22:07:21.000Z
        const head = iso.slice(0, 19);
        const frac = fraction ? (fraction.length > 4 ? fraction : iso.slice(19, 19 + fraction.length)) : "";
        return head + frac + (zone === "Z" ? "Z" : zone ? "+00:00" : "");
      })
      .replace(DAY, (whole, date) => {
        const at = Date.parse(`${date}T12:00:00Z`);
        return Number.isNaN(at) ? whole : `"${new Date(at + shift).toISOString().slice(0, 10)}"`;
      });
  }

  function answer(status, body, type = "application/json") {
    return new Response(body, { status, headers: { "Content-Type": type } });
  }

  async function replay(url, method) {
    const data = await recording;
    if (!shift) shift = Math.max(0, Date.now() - Date.parse(data.recorded_at));
    if (method !== "GET") return answer(403, JSON.stringify({ detail: DEMO_NOTE }));
    if (url.pathname === "/healthz") return answer(200, '{"ok": true}');
    const key = url.pathname + url.search;
    let hit = data.responses[key];
    if (!hit) {
      // The same endpoint asked another way (a longer page, another filter).
      const near = Object.keys(data.responses).find((k) => k.split("?")[0] === url.pathname);
      hit = near && data.responses[near];
      if (hit) console.debug("demo: answered", key, "with", near);
    }
    if (!hit) {
      console.debug("demo: not recorded", key);
      return answer(404, JSON.stringify({ detail: "Not part of the demo recording." }));
    }
    return answer(200, moved(hit.body), hit.type);
  }

  window.fetch = (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.origin);
    const method = (init.method || (typeof input === "object" && input.method) || "GET").toUpperCase();
    if (url.origin === location.origin && (url.pathname.startsWith("/api/") || url.pathname === "/healthz")) {
      return replay(url, method);
    }
    return realFetch(input, init);
  };

  // No server to stream from: a stream that opens, says what the real one
  // says first - the whole snapshot, which everything drawn from the poll
  // waits for - and then repeats it on the poll interval, stamped as just
  // polled, so the dashboard reads as live rather than overdue.
  class QuietEventSource extends EventTarget {
    constructor(url) {
      super();
      this.url = url;
      this.readyState = 0;
      this.onmessage = null;
      this.onerror = null;
      this.onopen = null;
      this.timer = null;
      this.opening = this.open();
    }
    async open() {
      const snapshot = await replay(new URL("/api/state", location.origin), "GET");
      const payload = snapshot.ok ? await snapshot.json() : null;
      if (this.readyState !== 0) return;
      this.readyState = 1;
      this.emit(new Event("open"), "onopen");
      if (!payload) return;
      const poll = () => {
        const at = new Date().toISOString();
        this.emit(new MessageEvent("message", {
          data: JSON.stringify({ type: "state", at, payload: { ...payload, last_poll_at: at } }),
        }), "onmessage");
      };
      poll();
      this.timer = setInterval(poll, 1000 * (Number(payload.poll_interval_seconds) || 30));
    }
    emit(event, handler) {
      this.dispatchEvent(event);
      if (this[handler]) this[handler](event);
    }
    close() {
      this.readyState = 2;
      clearInterval(this.timer);
    }
  }
  QuietEventSource.CONNECTING = 0;
  QuietEventSource.OPEN = 1;
  QuietEventSource.CLOSED = 2;
  window.EventSource = QuietEventSource;

  if (navigator.serviceWorker) {
    try { navigator.serviceWorker.register = () => Promise.resolve(); } catch (_) { /* read-only */ }
  }

  // Sign-in and sign-out lead to the server, which is not there.
  document.addEventListener("click", (event) => {
    const link = event.target.closest && event.target.closest('a[href^="/auth/"]');
    if (!link) return;
    event.preventDefault();
    const toast = document.getElementById("toast");
    if (toast) {
      toast.textContent = DEMO_NOTE;
      toast.dataset.level = "info";
      toast.hidden = false;
      setTimeout(() => { toast.hidden = true; }, 5000);
    }
  }, true);

  // Say what this is, once, in a corner.
  window.addEventListener("DOMContentLoaded", () => {
    const badge = document.createElement("a");
    badge.href = "../getting-started/live-demo/";
    badge.target = "_top";
    badge.textContent = "Demo · recorded";
    badge.title = DEMO_NOTE;
    badge.className = "demo-badge";
    const style = document.createElement("style");
    style.textContent = ".demo-badge{position:fixed;right:12px;bottom:12px;z-index:9999;"
      + "padding:4px 10px;border-radius:999px;font:600 12px/1.6 system-ui,sans-serif;"
      + "text-decoration:none;background:#4f46e5;color:#fff;opacity:.9;"
      + "box-shadow:0 2px 8px rgb(0 0 0 / .25)}"
      // Above the tab bar on a phone.
      + "@media (max-width:860px){.demo-badge{bottom:calc(76px + env(safe-area-inset-bottom))}}";
    document.head.appendChild(style);
    document.body.appendChild(badge);
  });
})();
