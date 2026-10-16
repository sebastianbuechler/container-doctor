// Container Doctor's service worker. It does one thing: when the dashboard is
// opened with no way to reach it - the phone is off the VPN - it shows a page
// of ours that says so and comes back by itself, instead of the browser's
// "cannot open the page". It never caches the dashboard or its data: every
// request goes to the network as before, and only a failed page load falls
// back to the offline page.
const OFFLINE = "/static/offline.html";
// Replaced when served, so a new offline page is a new worker.
const CACHE = "doctor-offline-__VERSION__";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll([OFFLINE, "/static/icon.svg"])),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE)));
});
