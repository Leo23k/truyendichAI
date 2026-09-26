const CACHE = "novel-reader-static-v1";
const STATIC = ["/static/offline.js", "/manifest.webmanifest"];

self.addEventListener("install", event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(STATIC)).then(() => self.skipWaiting())));
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => {
      if (/^\/chapters\/[^/]+$/.test(url.pathname)) {
        return new Response('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Đọc offline</title><script src="/static/offline.js"></script><script>addEventListener("DOMContentLoaded",()=>NovelOffline.renderOfflineReader())</script>', { headers: { "Content-Type": "text/html; charset=utf-8" } });
      }
      return new Response("Bạn đang offline. Vui lòng thử lại khi có mạng.", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
    }));
    return;
  }
  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(caches.match(request).then(cached => cached || fetch(request).then(response => {
      if (response.ok) caches.open(CACHE).then(cache => cache.put(request, response.clone()));
      return response;
    })));
  }
});
