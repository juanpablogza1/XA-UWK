// v5: la app y los datos siempre se piden a la red primero (revalidando),
// el cache solo se usa sin conexion.
const C = "xauwk-v5";
const LEAFLET = ["https://unpkg.com/leaflet@1.9.4/dist/leaflet.css", "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"];
const SHELL = ["./", "index.html", "manifest.webmanifest", "icons/icon-192.png"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(C)
    .then(c => c.addAll(SHELL.map(u => new Request(u, { cache: "reload" })))
      .then(() => c.addAll(LEAFLET).catch(() => {})))  // si unpkg falla, no bloquear la actualizacion
    .then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(k => Promise.all(k.filter(x => x !== C).map(x => caches.delete(x)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  const u = new URL(e.request.url);
  if (LEAFLET.includes(u.href)) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
    return;
  }
  if (u.origin !== self.location.origin) return; // mosaicos del mapa: directo a la red
  const key = u.pathname;
  e.respondWith(fetch(e.request.url, { cache: "no-cache" }).then(r => {
    if (r.ok) { const cp = r.clone(); caches.open(C).then(c => c.put(key, cp)); }
    return r;
  }).catch(() => caches.match(key).then(r => r || caches.match(new URL("index.html", self.registration.scope).pathname))));
});
