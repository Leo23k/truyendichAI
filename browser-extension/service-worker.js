const ALARM = "gemini-companion-poll";
const GEMINI_URL = "https://gemini.google.com/app";
const GEMINI_PERMISSION = "https://gemini.google.com/*";
let busy = false;

function companionError(code, message, details = {}) {
  return Object.assign(new Error(message), { code, ...details });
}

function log(stage, data = {}) {
  console.info(`[GEMINI_COMPANION][${stage}]`, JSON.stringify({ code: "INFO", message: "", tabId: null, url: null, timestamp: new Date().toISOString(), ...data }));
}

function hostAccessError(error, host, tabId, url) {
  if (!/cannot access contents|permission to access the respective host|cannot access.*page/i.test(String(error?.message || error))) return error;
  return companionError("HOST_PERMISSION_MISSING", `Edge chưa cấp quyền truy cập ${host}. Hãy bật quyền site access cho host này trong chi tiết extension.`, { tabId, url, cause: error });
}

async function startPolling() {
  if (!await chrome.alarms.get(ALARM)) await chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
  const { appTabId } = await chrome.storage.local.get("appTabId");
  if (appTabId) void processOneJob();
}

chrome.runtime.onInstalled.addListener(() => void startPolling());
chrome.runtime.onStartup.addListener(() => void startPolling());
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) processOneJob();
});
void startPolling().catch(() => {});
chrome.tabs.onRemoved.addListener(async (tabId) => {
  const { appTabId } = await chrome.storage.local.get("appTabId");
  if (appTabId === tabId) await chrome.storage.local.remove(["appTabId", "appOrigin"]);
});

async function pageRequest(tabId, path, method = "GET", body) {
  const tab = await chrome.tabs.get(tabId);
  const host = `${new URL(tab.url).origin}/*`;
  if (!await chrome.permissions.contains({ origins: [host] })) throw companionError("HOST_PERMISSION_MISSING", `Edge chưa cấp quyền truy cập ${host}.`, { tabId, url: tab.url });
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId }, world: "MAIN",
      func: async ({ path, method, body }) => {
        const response = await fetch(path, {
          method, credentials: "same-origin",
          headers: body ? { "Content-Type": "application/json" } : {},
          body: body ? JSON.stringify(body) : undefined,
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
        return payload;
      },
      args: [{ path, method, body }],
    });
    return results[0]?.result;
  } catch (error) {
    throw hostAccessError(error, `${new URL(tab.url).origin}/*`, tabId, tab.url);
  }
}

async function activeAppTab() {
  const { appTabId } = await chrome.storage.local.get("appTabId");
  if (!appTabId) throw new Error("Hãy kết nối extension từ tab website Leo Truyện đang mở.");
  const tab = await chrome.tabs.get(appTabId).catch(() => null);
  if (!tab?.url?.startsWith("http")) throw new Error("Tab website đã đóng. Hãy kết nối lại từ website.");
  if (!["https://leotruyen.vercel.app", "http://127.0.0.1:8000", "http://localhost:8000"].includes(new URL(tab.url).origin)) throw companionError("UNSUPPORTED_APP_HOST", "Tab đã kết nối không phải Leo Truyện production hoặc localhost:8000.", { tabId: tab.id, url: tab.url });
  return tab;
}

async function setSiteConnection(tabId, message, connected) {
  await chrome.scripting.executeScript({
    target: { tabId }, world: "MAIN",
    func: ({ message, connected }) => {
      for (const node of document.querySelectorAll('[id$="-extension-status"]')) {
        node.textContent = message;
        node.className = `mt-2 text-xs ${connected ? "text-emerald-700" : "text-amber-700"}`;
      }
    }, args: [{ message, connected }],
  }).catch(() => {});
}

async function findGeminiTab() {
  const tabs = await chrome.tabs.query({ url: "https://gemini.google.com/*" });
  return tabs.find((tab) => tab.active) || tabs[0] || null;
}

async function waitForTabComplete(tabId) {
  return new Promise((resolve, reject) => {
    const finish = (error, tab) => {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      if (error) reject(error); else resolve(tab);
    };
    const timer = setTimeout(() => finish(companionError("TAB_LOAD_TIMEOUT", "Gemini Web chưa tải xong.")), 30000);
    const listener = (id, info, tab) => {
      if (id === tabId && info.status === "complete") finish(null, tab);
    };
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((tab) => {
      if (tab.status === "complete") finish(null, tab);
    }).catch((error) => finish(error));
  });
}

async function openGeminiTab() {
  const tab = await chrome.tabs.create({ url: GEMINI_URL, active: true });
  log("TAB", { code: "OPENED", tabId: tab.id, url: tab.url || GEMINI_URL });
  return waitForTabComplete(tab.id);
}

async function pingGemini(tabId) {
  try {
    const reply = await chrome.tabs.sendMessage(tabId, { type: "ping" });
    return reply?.ready === true;
  } catch {
    return false;
  }
}

async function ensureGeminiTabReady(tab) {
  tab = await waitForTabComplete(tab.id);
  if (!tab.url?.startsWith("https://gemini.google.com/")) {
    throw companionError("TAB_NOT_FOUND", "Tab không phải Gemini Web.", { tabId: tab.id, url: tab.url });
  }
  const hasPermission = await chrome.permissions.contains({ origins: [GEMINI_PERMISSION] });
  if (!hasPermission) throw companionError("HOST_PERMISSION_MISSING", `Edge chưa cấp quyền cho ${GEMINI_PERMISSION}`, { tabId: tab.id, url: tab.url });

  if (await pingGemini(tab.id)) {
    return { tab, injected: false };
  }
  log("INJECT", { code: "START", tabId: tab.id, url: tab.url });
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  } catch (error) {
    const mapped = hostAccessError(error, GEMINI_PERMISSION, tab.id, tab.url);
    throw mapped === error ? companionError("CONTENT_SCRIPT_UNAVAILABLE", String(error?.message || error), { tabId: tab.id, url: tab.url }) : mapped;
  }
  if (!await pingGemini(tab.id)) throw companionError("CONTENT_SCRIPT_UNAVAILABLE", "Content script đã inject nhưng không phản hồi ping.", { tabId: tab.id, url: tab.url });
  log("INJECT", { code: "READY", tabId: tab.id, url: tab.url });
  return { tab, injected: true };
}

async function ensureGeminiTab() {
  const tab = await findGeminiTab() || await openGeminiTab();
  return ensureGeminiTabReady(tab);
}

async function healthCheck() {
  const result = {
    code: "READY", ok: false,
    backend: { ok: false, code: "BACKEND_NOT_CONNECTED", message: "Chưa kết nối tab Leo Truyện." },
    browser: { ok: false, code: "HOST_PERMISSION_MISSING", host: GEMINI_PERMISSION },
    gemini: { ok: false, code: "TAB_NOT_FOUND", checks: {} },
  };
  try {
    const appTab = await activeAppTab();
    await pageRequest(appTab.id, "/api/gemini-extension/status");
    result.backend = { ok: true, code: "CONNECTED", url: new URL(appTab.url).origin };
  } catch (error) {
    result.backend = { ok: false, code: error.code || "BACKEND_UNAVAILABLE", message: error.message };
  }
  try {
    const { tab, injected } = await ensureGeminiTab();
    const dom = await chrome.tabs.sendMessage(tab.id, { type: "health-check" });
    result.browser = { ok: true, code: "CONNECTED", host: GEMINI_PERMISSION, injected };
    result.gemini = { ...dom, tabId: tab.id, url: tab.url };
  } catch (error) {
    const code = error.code || "CONTENT_SCRIPT_UNAVAILABLE";
    result.browser = { ok: code !== "HOST_PERMISSION_MISSING", code, host: GEMINI_PERMISSION };
    result.gemini = { ok: false, code, message: error.message, tabId: error.tabId, url: error.url };
  }
  result.ok = result.backend.ok && result.browser.ok && result.gemini.ok;
  if (!result.ok) result.code = [result.backend, result.browser, result.gemini].find((item) => !item.ok)?.code || "UI_CHANGED";
  log("HEALTH", { code: result.code, ok: result.ok, tabId: result.gemini.tabId, url: result.gemini.url, result });
  return result;
}

async function pauseJob(tabId, jobId, reason, message) {
  await pageRequest(tabId, `/api/gemini-extension/jobs/${encodeURIComponent(jobId)}/pause`, "POST", { reason, message });
}

async function processOneJob() {
  if (busy) return;
  busy = true;
  let appTab;
  let activeGeminiTab;
  let job;
  try {
    appTab = await activeAppTab();
    const response = await pageRequest(appTab.id, "/api/gemini-extension/jobs/next");
    if (!response?.job) return;
    job = response.job;
    await chrome.storage.local.set({ currentJob: `${job.chapter_number}` });
    const { tab } = await ensureGeminiTab();
    activeGeminiTab = tab;
    const result = await chrome.tabs.sendMessage(tab.id, { type: "translate-chapter", job });
    if (!result?.ok) {
      const message = result?.message || "Gemini Web cần thao tác thủ công.";
      await pauseJob(appTab.id, job.id, result?.reason || "manual", message);
      await chrome.storage.local.set({ lastError: message });
      return;
    }
    await pageRequest(appTab.id, `/api/gemini-extension/jobs/${encodeURIComponent(job.id)}/complete`, "POST", result.translation);
  } catch (error) {
    const message = String(error?.message || error);
    log("JOB", { code: error.code || "JOB_ERROR", message, tabId: activeGeminiTab?.id || appTab?.id, url: activeGeminiTab?.url || appTab?.url });
    if (job && appTab) {
      const reason = error.code === "QUOTA_PAUSED" ? "quota" : ["HOST_PERMISSION_MISSING", "MANUAL_ACTION_REQUIRED", "GEMINI_NOT_LOGGED_IN", "UI_CHANGED", "NEW_CHAT_SELECTOR_NOT_FOUND"].includes(error.code) ? "manual" : "transient";
      await pauseJob(appTab.id, job.id, reason, `Companion gặp lỗi sau khi nhận job: ${message}`).catch(() => {});
    }
    await chrome.storage.local.set({ lastError: message });
  } finally {
    await chrome.storage.local.remove("currentJob");
    busy = false;
    chrome.runtime.sendMessage({ type: "companion-state-changed" }).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "companion-log") {
    log(message.stage || "CONTENT", { ...message.data, tabId: sender.tab?.id ?? null, url: sender.tab?.url ?? null });
    return;
  }
  if (message.type === "connect-app") {
    (async () => {
      const tab = message.tab;
      if (!tab?.id || !tab.url?.startsWith("http")) throw new Error("Mở website Leo Truyện trước khi kết nối.");
      const appOrigin = new URL(tab.url).origin;
      if (!["https://leotruyen.vercel.app", "http://127.0.0.1:8000", "http://localhost:8000"].includes(appOrigin)) throw companionError("UNSUPPORTED_APP_HOST", "Chỉ hỗ trợ Leo Truyện production hoặc localhost:8000.", { tabId: tab.id, url: tab.url });
      await pageRequest(tab.id, "/api/gemini-extension/status");
      await chrome.storage.local.set({ appTabId: tab.id, appOrigin, lastError: "" });
      await setSiteConnection(tab.id, "Companion đã kết nối với website này.", true);
      sendResponse({ ok: true, origin: appOrigin });
      void processOneJob();
    })().catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message.type === "disconnect-app") {
    (async () => {
      const { appTabId } = await chrome.storage.local.get("appTabId");
      if (appTabId) await setSiteConnection(appTabId, "Extension chưa được kết nối với website này.", false);
      await chrome.storage.local.remove(["appTabId", "appOrigin"]);
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (message.type === "get-state") {
    chrome.storage.local.get(["appTabId", "appOrigin", "currentJob", "lastError"]).then(sendResponse);
    return true;
  }
  if (message.type === "resume-jobs") {
    (async () => {
      const tab = await activeAppTab();
      const result = await pageRequest(tab.id, "/api/gemini-extension/resume", "POST", {});
      await chrome.storage.local.set({ lastError: "" });
      sendResponse({ ok: true, resumed: result.resumed });
    })().catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message.type === "open-gemini") {
    chrome.tabs.create({ url: GEMINI_URL, active: true })
      .then((tab) => sendResponse({ ok: true, tabId: tab.id }))
      .catch((error) => sendResponse({ ok: false, code: "TAB_OPEN_FAILED", message: String(error?.message || error) }));
    return true;
  }
  if (message.type === "health-check") {
    healthCheck().then(sendResponse).catch((error) => sendResponse({ ok: false, code: error.code || "HEALTH_CHECK_FAILED", message: String(error?.message || error) }));
    return true;
  }
});
