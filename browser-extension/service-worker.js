const ALARM = "gemini-companion-poll";
const GEMINI_URL = "https://gemini.google.com/app";
const GEMINI_PERMISSION = "https://gemini.google.com/*";
const HEALTH_CHECK_TIMEOUT_MS = 24000;
let busy = false;
let geminiTabOpening;

function withTimeout(promise, timeoutMs, code, message) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(companionError(code, message)), timeoutMs);
    }),
  ]).finally(() => clearTimeout(timer));
}

function companionError(code, message, details = {}) {
  return Object.assign(new Error(message), { code, ...details });
}

function log(stage, data = {}) {
  console.info(`[GEMINI_COMPANION][${stage}]`, JSON.stringify({ code: "INFO", message: "", tabId: null, url: null, timestamp: new Date().toISOString(), ...data }));
}

function hostAccessError(error, host, tabId, url) {
  if (!/cannot access contents|permission to access the respective host|cannot access.*page/i.test(String(error?.message || error))) return error;
  return companionError("HOST_PERMISSION_MISSING", `Trình duyệt chưa cấp quyền truy cập ${host}. Hãy cấp Site access cho host này trong chi tiết extension.`, { tabId, url, cause: error });
}

async function geminiPermissionState(tabId, url) {
  const manifest = chrome.runtime.getManifest();
  const manifestHosts = [...(manifest.host_permissions || []), ...(manifest.optional_host_permissions || [])];
  const manifestDeclared = manifestHosts.includes(GEMINI_PERMISSION);
  let runtimeGranted;
  let granted;
  try {
    [runtimeGranted, granted] = await Promise.all([
      chrome.permissions.contains({ origins: [GEMINI_PERMISSION] }),
      chrome.permissions.getAll(),
    ]);
  } catch (error) {
    throw companionError("PERMISSION_CHECK_FAILED", "Không kiểm tra được quyền truy cập Gemini của trình duyệt.", { tabId, url, rawError: String(error?.message || error), cause: error });
  }
  const state = {
    manifestDeclared,
    manifestHosts,
    runtimeGranted,
    runtimeOrigins: granted.origins || [],
    tabUrl: url || null,
    tabId: tabId ?? null,
    extensionVersion: manifest.version,
    extensionId: chrome.runtime.id,
  };
  log("PERMISSION", { code: runtimeGranted ? "RUNTIME_GRANTED" : "RUNTIME_NOT_GRANTED", ...state });
  return state;
}

async function startPolling() {
  if (!await chrome.alarms.get(ALARM)) await chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
  const { appTabId } = await chrome.storage.local.get("appTabId");
  if (appTabId) void processOneJob();
}

chrome.runtime.onInstalled.addListener(() => void startPolling());
chrome.runtime.onStartup.addListener(() => void startPolling());
if (chrome.sidePanel?.setPanelBehavior) {
  void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch((error) => {
    log("SIDEPANEL", { code: "SETUP_FAILED", message: String(error?.message || error) });
  });
}
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) processOneJob();
});
void startPolling().catch(() => {});
chrome.tabs.onRemoved.addListener(async (tabId) => {
  const { appTabId, geminiTabId } = await chrome.storage.local.get(["appTabId", "geminiTabId"]);
  if (appTabId === tabId) await chrome.storage.local.remove(["appTabId", "appOrigin"]);
  if (geminiTabId === tabId) await chrome.storage.local.remove("geminiTabId");
});

async function pageRequest(tabId, path, method = "GET", body, timeoutMs = 60000) {
  const tab = await chrome.tabs.get(tabId);
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId }, world: "MAIN",
      func: async ({ path, method, body, timeoutMs }) => {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        try {
          const response = await fetch(path, {
            method, credentials: "same-origin", signal: controller.signal,
            headers: body ? { "Content-Type": "application/json" } : {},
            body: body ? JSON.stringify(body) : undefined,
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
          return payload;
        } finally {
          clearTimeout(timer);
        }
      },
      args: [{ path, method, body, timeoutMs }],
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
  const { geminiTabId } = await chrome.storage.local.get("geminiTabId");
  if (geminiTabId) {
    const saved = await chrome.tabs.get(geminiTabId).catch(() => null);
    if (saved?.url?.startsWith("https://gemini.google.com/") || saved?.url?.startsWith("https://accounts.google.com/") || saved?.pendingUrl?.startsWith("https://gemini.google.com/")) return saved;
    await chrome.storage.local.remove("geminiTabId");
  }
  const tabs = await chrome.tabs.query({ url: "https://gemini.google.com/*" });
  return tabs.find((tab) => tab.active) || tabs[0] || null;
}

async function openGeminiTab() {
  if (geminiTabOpening) return geminiTabOpening;
  geminiTabOpening = (async () => {
    const tab = await chrome.tabs.create({ url: GEMINI_URL, active: true });
    await chrome.storage.local.set({ geminiTabId: tab.id });
    log("TAB", { code: "OPENED", tabId: tab.id, url: tab.url || GEMINI_URL });
    return tab;
  })();
  try { return await geminiTabOpening; }
  finally { geminiTabOpening = null; }
}

async function pingGemini(tabId) {
  try {
    const reply = await withTimeout(
      chrome.tabs.sendMessage(tabId, { type: "ping" }),
      1000, "GEMINI_PING_TIMEOUT", "Tab Gemini không phản hồi ping.",
    );
    return reply?.ready === true;
  } catch {
    return false;
  }
}

async function waitForGeminiContentScript(tabId) {
  const deadline = Date.now() + 30000;
  let tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab) throw companionError("TAB_NOT_FOUND", "Không tìm thấy tab Gemini sau khi nạp lại.", { tabId });
  if (!tab.url?.startsWith("https://gemini.google.com/")) throw companionError("TAB_URL_INVALID", "Tab sau khi nạp lại không còn ở Gemini Web.", { tabId, url: tab.url });
  const diagnostics = await geminiPermissionState(tabId, tab.url);
  if (!diagnostics.manifestDeclared) throw companionError("HOST_PERMISSION_MISSING", `Manifest chưa khai báo ${GEMINI_PERMISSION}.`, { tabId, url: tab.url, diagnostics });
  if (!diagnostics.runtimeGranted) throw companionError("HOST_PERMISSION_REQUIRED", "Edge chưa cho Companion truy cập Gemini. Hãy bật Site access cho Gemini trong chi tiết extension.", { tabId, url: tab.url, diagnostics });
  while (Date.now() < deadline) {
    tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw companionError("TAB_NOT_FOUND", "Không tìm thấy tab Gemini sau khi nạp lại.", { tabId });
    if (!tab.url?.startsWith("https://gemini.google.com/")) throw companionError("TAB_URL_INVALID", "Tab sau khi nạp lại không còn ở Gemini Web.", { tabId, url: tab.url });
    if (await pingGemini(tabId)) return tab;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw companionError("CONTENT_SCRIPT_NOT_READY", "Tab Gemini đã nạp lại nhưng content.js chưa phản hồi. Hãy tải lại tab Gemini rồi kiểm tra extension.", { tabId, url: tab?.url });
}

async function ensureGeminiTabReady(tab, timeoutMs = 30000, scriptWaitMs = 2500) {
  if (!tab?.id) throw companionError("TAB_NOT_FOUND", "Không tìm thấy tab Gemini.");
  const tabId = tab.id;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw companionError("TAB_NOT_FOUND", "Tab Gemini đã đóng.", { tabId });
    if (tab.url?.startsWith("https://gemini.google.com/") && tab.status !== "loading") break;
    if (tab.url?.startsWith("https://accounts.google.com/")) {
      throw companionError("GEMINI_LOGIN_REQUIRED", "Hãy đăng nhập Google trong tab Gemini rồi kiểm tra lại.", { tabId: tab.id, url: tab.url });
    }
    if (tab.url && tab.url !== "about:blank" && !tab.url.startsWith("https://gemini.google.com/")) {
      throw companionError("TAB_URL_INVALID", "Tab đang mở không thuộc Gemini Web.", { tabId: tab.id, url: tab.url });
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  if (!tab.url?.startsWith("https://gemini.google.com/") || tab.status === "loading") {
    throw companionError("TAB_LOAD_TIMEOUT", `Tab Gemini vẫn đang tải sau ${Math.ceil(timeoutMs / 1000)} giây.`, { tabId: tab.id, url: tab.url, tabStatus: tab.status });
  }
  const permissionDiagnostics = await geminiPermissionState(tab.id, tab.url);
  permissionDiagnostics.runtimeCheck = permissionDiagnostics.manifestDeclared && permissionDiagnostics.runtimeGranted;
  if (!permissionDiagnostics.manifestDeclared) {
    throw companionError("HOST_PERMISSION_MISSING", `Manifest extension đang chạy chưa khai báo ${GEMINI_PERMISSION}. Hãy nạp lại đúng thư mục browser-extension.`, { tabId: tab.id, url: tab.url, diagnostics: permissionDiagnostics });
  }
  if (!permissionDiagnostics.runtimeGranted) {
    throw companionError("HOST_PERMISSION_REQUIRED", "Edge chưa cho Companion truy cập Gemini. Hãy bật Site access cho Gemini trong chi tiết extension.", { tabId: tab.id, url: tab.url, diagnostics: permissionDiagnostics });
  }
  if (await pingGemini(tab.id)) {
    permissionDiagnostics.actualAccess = true;
    return { tab, injected: false, permissionDiagnostics };
  }
  const readyDeadline = Math.min(deadline, Date.now() + scriptWaitMs);
  while (Date.now() < readyDeadline) {
    if (await pingGemini(tab.id)) {
      permissionDiagnostics.actualAccess = true;
      return { tab, injected: false, permissionDiagnostics };
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw companionError("CONTENT_SCRIPT_NOT_READY", "Trang Gemini không có mã Companion dù quyền truy cập đã được cấp.", { tabId: tab.id, url: tab.url, diagnostics: permissionDiagnostics });
}

async function ensureGeminiTab(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  const tab = await findGeminiTab();
  if (tab) {
    try {
      return await ensureGeminiTabReady(tab, timeoutMs);
    } catch (error) {
      const { geminiTabId } = await chrome.storage.local.get("geminiTabId");
      if (error.code !== "CONTENT_SCRIPT_NOT_READY" || tab.id === geminiTabId) throw error;
    }
  }
  const fresh = await openGeminiTab();
  const remaining = Math.max(1000, deadline - Date.now());
  return ensureGeminiTabReady(fresh, remaining, remaining);
}

async function healthCheck() {
  const result = {
    code: "READY", ok: false,
    backend: { ok: false, code: "BACKEND_NOT_CONNECTED", message: "Chưa kết nối tab Leo Truyện." },
    browser: { ok: false, code: "HOST_PERMISSION_MISSING", host: GEMINI_PERMISSION },
    gemini: { ok: false, code: "TAB_NOT_FOUND", checks: {} },
  };
  const backendCheck = (async () => {
    try {
      const appTab = await activeAppTab();
      await pageRequest(appTab.id, "/api/gemini-extension/status", "GET", undefined, 12000);
      result.backend = { ok: true, code: "CONNECTED", url: new URL(appTab.url).origin };
    } catch (error) {
      result.backend = { ok: false, code: error.code || "BACKEND_UNAVAILABLE", message: error.message };
    }
  })();
  const geminiCheck = (async () => {
    try {
      const { tab, injected, permissionDiagnostics } = await ensureGeminiTab(20000);
      const dom = await withTimeout(
        chrome.tabs.sendMessage(tab.id, { type: "health-check" }),
        1500, "GEMINI_CHECK_TIMEOUT", "Tab Gemini không phản hồi kiểm tra trong 1,5 giây.",
      );
      result.browser = { ok: true, code: "CONNECTED", host: GEMINI_PERMISSION, injected, diagnostics: permissionDiagnostics };
      result.gemini = { ...dom, tabId: tab.id, url: tab.url };
    } catch (error) {
      const code = error.code || "CONTENT_SCRIPT_UNAVAILABLE";
      result.browser = { ok: !["HOST_PERMISSION_MISSING", "HOST_PERMISSION_REQUIRED", "PERMISSION_CHECK_FAILED"].includes(code), code, host: GEMINI_PERMISSION, diagnostics: error.diagnostics };
      result.browser = { ...result.browser, message: error.message };
      result.gemini = { ok: false, code, message: error.message, rawError: error.rawError, diagnostics: error.diagnostics, tabId: error.tabId, url: error.url, checks: {} };
    }
  })();
  await Promise.all([
    withTimeout(backendCheck, 13000, "BACKEND_CHECK_TIMEOUT", "Backend không phản hồi trong 13 giây.").catch((error) => {
      result.backend = { ok: false, code: error.code, message: error.message };
    }),
    withTimeout(geminiCheck, 22000, "GEMINI_CHECK_TIMEOUT", "Kiểm tra tab Gemini quá 22 giây. Tải lại tab Gemini rồi thử lại.").catch((error) => {
      result.browser = { ok: false, code: error.code, host: GEMINI_PERMISSION, message: error.message };
      result.gemini = { ok: false, code: error.code, message: error.message, checks: {} };
    }),
  ]);
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
      const reason = error.code === "QUOTA_PAUSED" ? "quota" : ["HOST_PERMISSION_MISSING", "HOST_PERMISSION_REQUIRED", "PERMISSION_CHECK_FAILED", "TAB_NOT_FOUND", "TAB_URL_INVALID", "TAB_LOAD_TIMEOUT", "CONTENT_SCRIPT_NOT_READY", "MANUAL_ACTION_REQUIRED", "GEMINI_LOGIN_REQUIRED", "GEMINI_COMPOSER_NOT_FOUND", "GEMINI_SEND_BUTTON_NOT_FOUND", "GEMINI_RESPONSE_TIMEOUT", "GEMINI_RESPONSE_READ_FAILED", "UI_CHANGED", "NEW_CHAT_SELECTOR_NOT_FOUND"].includes(error.code) ? "manual" : "transient";
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
      await chrome.storage.local.set({ appTabId: tab.id, appOrigin, lastError: "" });
      sendResponse({ ok: true, origin: appOrigin });
      void withTimeout(
        setSiteConnection(tab.id, "Companion đã kết nối với website này.", true),
        1500, "CONNECTION_STATUS_TIMEOUT", "",
      ).catch((error) => log("CONNECT_STATUS", { code: error.code, message: "Kết nối đã lưu; chưa cập nhật được nhãn trên trang." }));
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
    openGeminiTab()
      .then((tab) => sendResponse({ ok: true, tabId: tab.id }))
      .catch((error) => sendResponse({ ok: false, code: "TAB_OPEN_FAILED", message: String(error?.message || error) }));
    return true;
  }
  if (message.type === "reload-gemini") {
    (async () => {
      const tab = await findGeminiTab();
      if (!tab) throw companionError("TAB_NOT_FOUND", "Không tìm thấy tab Gemini đang mở.");
      await chrome.tabs.reload(tab.id);
      const reloaded = await waitForGeminiContentScript(tab.id);
      sendResponse({ ok: true, tabId: reloaded.id, url: reloaded.url });
    })().catch((error) => sendResponse({ ok: false, code: error.code || "TAB_RELOAD_FAILED", message: String(error?.message || error) }));
    return true;
  }
  if (message.type === "health-check") {
    withTimeout(
      healthCheck(), HEALTH_CHECK_TIMEOUT_MS, "HEALTH_CHECK_TIMEOUT", "Kiểm tra quá 24 giây. Hãy tải lại tab Gemini rồi thử lại.",
    ).then(sendResponse).catch((error) => sendResponse({
      ok: false,
      code: error.code || "HEALTH_CHECK_FAILED",
      message: String(error?.message || error),
      backend: { ok: false, code: "NOT_CHECKED" },
      browser: { ok: false, code: "NOT_CHECKED", host: GEMINI_PERMISSION },
      gemini: { ok: false, code: error.code || "HEALTH_CHECK_FAILED", message: String(error?.message || error), checks: {} },
    }));
    return true;
  }
});
