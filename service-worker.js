const CACHE = "novel-reader-static-v2";
const STATIC = ["/static/offline.js", "/static/pwa-icon.svg", "/manifest.webmanifest"];

self.addEventListener("install", event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(STATIC)).then(() => self.skipWaiting())));
self.addEventListener("activate", event => event.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith("novel-reader-static-") && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim())
));
self.addEventListener("push", event => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch { payload = { message: event.data?.text() || "Có chương mới." }; }
  const url = typeof payload.url === "string" && payload.url.startsWith("/") && !payload.url.startsWith("//") ? payload.url : "/notifications";
  event.waitUntil(self.registration.showNotification(payload.title || "Leo Truyện", {
    body: payload.message || "Có cập nhật mới.",
    icon: "/static/pwa-icon.svg",
    badge: "/static/pwa-icon.svg",
    data: { url },
    tag: payload.id || undefined,
  }));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const path = event.notification.data?.url || "/notifications";
  const target = new URL(path, self.location.origin);
  if (target.origin !== self.location.origin) return;
  event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(async windows => {
    for (const client of windows) {
      if (new URL(client.url).origin !== self.location.origin) continue;
      await client.focus();
      return client.navigate(target.href);
    }
    return clients.openWindow(target.href);
  }));
});
self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => {
      if (/^\/chapters\/[^/]+$/.test(url.pathname) || /^\/truyen\/[^/]+\/\d+$/.test(url.pathname)) {
        return new Response('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Đọc offline</title><script src="/static/offline.js"></script><script>addEventListener("DOMContentLoaded",()=>NovelOffline.renderOfflineReader())</script>', { headers: { "Content-Type": "text/html; charset=utf-8" } });
      }
      if (/^\/truyen\/[^/]+$/.test(url.pathname)) {
        return new Response('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Truyện offline</title><script src="/static/offline.js"></script><script>addEventListener("DOMContentLoaded",()=>NovelOffline.renderOfflineBook())</script>', { headers: { "Content-Type": "text/html; charset=utf-8" } });
      }
      return new Response('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>My Novel Reader</title><script src="/static/offline.js"></script><script>addEventListener("DOMContentLoaded",()=>NovelOffline.renderOfflineHome())</script>', { headers: { "Content-Type": "text/html; charset=utf-8" } });
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
