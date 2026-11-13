const CACHE = "angebots-radar-v1.0";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // Daten & App: zuerst Netz (immer aktuell), offline aus dem Cache
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok) { const copy = r.clone(); const key = url.pathname.endsWith("offers.json") ? new Request(url.pathname) : e.request; caches.open(CACHE).then(c => c.put(key, copy)); }
      return r;
    }).catch(() => caches.match(url.pathname.endsWith("offers.json") ? new Request(url.pathname) : e.request))
  );
});
