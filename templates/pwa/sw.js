/* CLEFRESH service worker ({{ version }}).
 *
 * - Static files (CSS, JS, fonts, images): cache-first, filled as they're used.
 * - Page navigations: always the network (pages show personal order and payment data, so they are
 *   never stored); when offline, show the cached /offline/ page instead.
 * - Never touched: non-GET requests, payments, sign-in, APIs, live updates, admin, private media.
 */
const VERSION = '{{ version }}';
const STATIC_CACHE = `${VERSION}-static`;
const PRECACHE = {{ precache_json|safe }};
const BYPASS = {{ bypass_json|safe }};

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => Promise.all(PRECACHE.map((url) => cache.add(url).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;               // fonts CDN, maps, PayMongo: browser default
  if (BYPASS.some((p) => url.pathname.startsWith(p))) return;
  if (req.headers.get('accept') === 'text/event-stream') return;  // notification stream

  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match('/offline/')));
    return;
  }

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(STATIC_CACHE).then((cache) => cache.put(req, copy));
        }
        return res;
      }))
    );
  }
});
