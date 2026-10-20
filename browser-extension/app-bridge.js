(() => {
  if (globalThis.__leoCompanionAppBridge) return;
  globalThis.__leoCompanionAppBridge = true;

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "app-bridge-ping") {
      sendResponse({ ready: true });
      return;
    }
    if (message.type !== "app-bridge-request") return;
    (async () => {
      const { path, method = "GET", body, timeoutMs = 60000 } = message;
      if (typeof path !== "string" || !(path === "/health" || path.startsWith("/api/gemini-extension/"))) {
        throw new Error("Companion không hỗ trợ đường dẫn này.");
      }
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        let response;
        for (let attempt = 0; attempt < 3; attempt += 1) {
          try {
            response = await fetch(new URL(path, location.origin), {
              method, credentials: "same-origin", signal: controller.signal,
              headers: body ? { "Content-Type": "application/json" } : {},
              body: body ? JSON.stringify(body) : undefined,
            });
            break;
          } catch (error) {
            if (error.name !== "TypeError" || attempt === 2 || controller.signal.aborted) throw error;
            await new Promise((resolve) => setTimeout(resolve, 500 * (attempt + 1)));
          }
        }
        const payload = await response.json().catch(() => ({}));
        sendResponse(response.ok ? { ok: true, payload } : { ok: false, error: payload.detail || `HTTP ${response.status}` });
      } finally {
        clearTimeout(timer);
      }
    })().catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  });
})();
