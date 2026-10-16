// Keeps the player page openable with no signal. Network first, so a fresh
// page always wins; the saved copy is only for when the network is gone.
// Live data (/api/) is never cached: the page keeps its own last update.
const CACHE = 'ggwp-v6';
const SHELL = ['/', 'https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css', 'https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js'];

self.addEventListener('install', e => {
    e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
    self.skipWaiting();
});

self.addEventListener('activate', e => {
    e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
    self.clients.claim();
});

self.addEventListener('fetch', e => {
    const url = new URL(e.request.url);
    if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
    // Only the player page is kept. Other pages (/gm, a 404) go straight
    // to the network, so they never overwrite the saved copy.
    const page = e.request.mode === 'navigate';
    if (page && url.pathname !== '/') return;
    if (!page && !SHELL.includes(url.href) && url.origin === location.origin) return;
    if (!page && url.origin !== location.origin && !SHELL.includes(url.href)) return;
    e.respondWith(
        fetch(e.request).then(r => {
            if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(page ? '/' : e.request, copy)); }
            return r;
        }).catch(() => caches.match(page ? '/' : e.request))
    );
});
