const CACHE = "novel-reader-static-v17";
const MEDIA_CACHE = "novel-reader-media-v1";
const OFFLINE_SCRIPT = "/static/offline.js?v=17";
const READER_STYLES = "/static/reader.css?v=3";
const STATIC = [OFFLINE_SCRIPT, READER_STYLES, "/static/reader_resume.js", "/static/pwa-icon.svg", "/manifest.webmanifest"];

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
  if (request.method !== "GET") return;
  if (request.destination === "image" && url.origin !== self.location.origin) {
    event.respondWith(caches.open(MEDIA_CACHE).then(async cache => {
      const cached = await cache.match(request);
      try { const response = await fetch(request); if (response.ok || response.type === "opaque") await cache.put(request, response.clone()); return response.ok || response.type === "opaque" ? response : cached || response; }
      catch { return cached || Response.error(); }
    }));
    return;
  }
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    const offlinePage = html => new Response(
      html.replace('<script src="/static/offline.js"></script>', `<link rel="stylesheet" href="${READER_STYLES}"><script src="/static/reader_resume.js"></script><script src="${OFFLINE_SCRIPT}"></script>`),
      { headers: { "Content-Type": "text/html; charset=utf-8" } }
    );
    if (url.pathname === "/ebooks/offline") {
      event.respondWith(Promise.resolve(offlinePage('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ebook offline</title><script src="/static/offline.js"></script><script>addEventListener("DOMContentLoaded",()=>NovelOffline.renderOfflineEbookLibrary())</script>')));
      return;
    }
    event.respondWith(fetch(request).catch(() => {
      if (url.pathname === "/" || url.pathname === "/ebooks") {
        return offlinePage('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Thư viện offline</title><script src="/static/offline.js"></script><script>addEventListener("DOMContentLoaded",()=>NovelOffline.renderOfflineHome())</script>');
      }
      if (url.pathname === "/ebooks/offline") {
        return offlinePage('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ebook offline</title><script src="/static/offline.js"></script><script>addEventListener("DOMContentLoaded",()=>NovelOffline.renderOfflineEbookLibrary())</script>');
      }
      if (/^\/ebooks\/[^/]+\/read\/\d+$/.test(url.pathname)) {
        return offlinePage('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ebook offline</title><script src="/static/offline.js"></script><script>addEventListener("DOMContentLoaded",()=>NovelOffline.renderOfflineEbookReader())</script>');
      }
      if (/^\/ebooks\/[^/]+$/.test(url.pathname)) {
        return offlinePage('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ebook offline</title><script src="/static/offline.js"></script><script>addEventListener("DOMContentLoaded",()=>NovelOffline.renderOfflineEbookDetail())</script>');
      }
      if (/^\/chapters\/[^/]+$/.test(url.pathname) || /^\/truyen\/[^/]+\/\d+$/.test(url.pathname)) {
        return offlinePage('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Đọc offline</title><script src="/static/offline.js"></script><script>addEventListener("DOMContentLoaded",()=>NovelOffline.renderOfflineReader())</script>');
      }
      if (/^\/truyen\/[^/]+$/.test(url.pathname)) {
        return offlinePage('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Truyện offline</title><script src="/static/offline.js"></script><script>addEventListener("DOMContentLoaded",()=>NovelOffline.renderOfflineBook())</script>');
      }
      return offlinePage('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Thư viện offline</title><script src="/static/offline.js"></script><script>addEventListener("DOMContentLoaded",()=>NovelOffline.renderOfflineHome())</script>');
    }));
    return;
  }
  if (request.destination === "image") {
    event.respondWith(caches.open(MEDIA_CACHE).then(async cache => {
      const cached = await cache.match(request);
      try {
        const response = await fetch(request);
        if (response.ok || response.type === "opaque") await cache.put(request, response.clone());
        return response.ok || response.type === "opaque" ? response : cached || response;
      } catch { return cached || Response.error(); }
    }));
    return;
  }
  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    if (url.pathname === "/static/offline.js") {
      event.respondWith(caches.open(CACHE).then(async cache => {
        try {
          const response = await fetch(request);
          if (response.ok) {
            await cache.put(request, response.clone());
            return response;
          }
          return await cache.match(request) || await cache.match(OFFLINE_SCRIPT) || response;
        } catch {
          return await cache.match(request) || await cache.match(OFFLINE_SCRIPT) || Response.error();
        }
      }));
      return;
    }
    event.respondWith(caches.match(request).then(cached => cached || fetch(request).then(response => {
      if (response.ok) caches.open(CACHE).then(cache => cache.put(request, response.clone()));
      return response;
    })));
  }
});
