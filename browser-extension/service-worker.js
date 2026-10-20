const ALARM = "gemini-companion-poll";
let busy = false;
const readyGeminiTabs = new Set();

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
  readyGeminiTabs.delete(tabId);
  const { appTabId } = await chrome.storage.local.get("appTabId");
  if (appTabId === tabId) await chrome.storage.local.remove(["appTabId", "appOrigin"]);
});

async function pageRequest(tabId, path, method = "GET", body) {
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
}

async function activeAppTab() {
  const { appTabId } = await chrome.storage.local.get("appTabId");
  if (!appTabId) throw new Error("Hãy kết nối extension từ tab website Leo Truyện đang mở.");
  const tab = await chrome.tabs.get(appTabId).catch(() => null);
  if (!tab?.url?.startsWith("http")) throw new Error("Tab website đã đóng. Hãy kết nối lại từ website.");
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

async function geminiTab() {
  const tabs = await chrome.tabs.query({ url: "https://gemini.google.com/*" });
  if (tabs[0]?.id) {
    await waitForGeminiContent(tabs[0].id);
    return tabs[0];
  }
  const tab = await chrome.tabs.create({ url: "https://gemini.google.com/app", active: true });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Gemini Web mở quá lâu.")), 30000);
    const listener = (id, info) => {
      if (id === tab.id && info.status === "complete") {
        clearTimeout(timer); chrome.tabs.onUpdated.removeListener(listener); resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
  await waitForGeminiContent(tab.id);
  return tab;
}

function waitForGeminiContent(tabId) {
  if (readyGeminiTabs.has(tabId)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const ready = () => {
      readyGeminiTabs.add(tabId);
      clearTimeout(timer);
      chrome.runtime.onMessage.removeListener(listener);
      resolve();
    };
    const timer = setTimeout(() => {
      chrome.runtime.onMessage.removeListener(listener);
      reject(new Error("Gemini tab chưa sẵn sàng để nhận job."));
    }, 30000);
    const listener = (message, sender) => {
      if (message.type === "gemini-ready" && sender.tab?.id === tabId) ready();
    };
    chrome.runtime.onMessage.addListener(listener);
    chrome.tabs.sendMessage(tabId, { type: "ping" }).then((reply) => {
      if (reply?.ready) ready();
    }).catch(async () => {
      try {
        await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
        const reply = await chrome.tabs.sendMessage(tabId, { type: "ping" });
        if (reply?.ready) ready();
      } catch (error) {
        clearTimeout(timer);
        chrome.runtime.onMessage.removeListener(listener);
        reject(error);
      }
    });
  });
}

async function pauseJob(tabId, jobId, reason, message) {
  await pageRequest(tabId, `/api/gemini-extension/jobs/${encodeURIComponent(jobId)}/pause`, "POST", { reason, message });
}

async function processOneJob() {
  if (busy) return;
  busy = true;
  let appTab;
  let job;
  try {
    appTab = await activeAppTab();
    const response = await pageRequest(appTab.id, "/api/gemini-extension/jobs/next");
    if (!response?.job) return;
    job = response.job;
    await chrome.storage.local.set({ currentJob: `${job.chapter_number}` });
    const tab = await geminiTab();
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
    if (job && appTab) {
      await pauseJob(appTab.id, job.id, "transient", `Companion gặp lỗi sau khi nhận job: ${message}`).catch(() => {});
    }
    await chrome.storage.local.set({ lastError: message });
  } finally {
    await chrome.storage.local.remove("currentJob");
    busy = false;
    chrome.runtime.sendMessage({ type: "companion-state-changed" }).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "connect-app") {
    (async () => {
      const tab = message.tab;
      if (!tab?.id || !tab.url?.startsWith("http")) throw new Error("Mở website Leo Truyện trước khi kết nối.");
      await pageRequest(tab.id, "/api/gemini-extension/status");
      const appOrigin = new URL(tab.url).origin;
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
    chrome.tabs.create({ url: "https://gemini.google.com/app", active: true }).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message.type === "health-check") {
    (async () => {
      const tab = await geminiTab();
      const result = await chrome.tabs.sendMessage(tab.id, { type: "health-check" });
      if (!result?.ok) throw new Error(result?.message || "Gemini cần thao tác thủ công.");
      sendResponse({ ok: true });
    })().catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
});
