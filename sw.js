const C = "xauwk-v2";
const LEAFLET = ["https://unpkg.com/leaflet@1.9.4/dist/leaflet.css", "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(C).then(c => c.addAll(["./", "index.html", "manifest.webmanifest", "icons/icon-192.png", ...LEAFLET])).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(k => Promise.all(k.filter(x => x !== C).map(x => caches.delete(x)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  const u = new URL(e.request.url);
  const same = u.origin === self.location.origin;
  if (!same && !LEAFLET.includes(u.href)) return; // mosaicos del mapa: directo a la red
  if (LEAFLET.includes(u.href)) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
    return;
  }
  // App y datos: red primero, cache como respaldo sin conexion
  const key = u.pathname.includes("/data/") ? u.pathname : e.request;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok) { const cp = r.clone(); caches.open(C).then(c => c.put(key, cp)); }
    return r;
  }).catch(() => caches.match(key).then(r => r || caches.match("index.html"))));
});
