import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const contentScript = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
const appBridge = await readFile(new URL("../browser-extension/app-bridge.js", import.meta.url), "utf8");
const serviceWorker = await readFile(new URL("../browser-extension/service-worker.js", import.meta.url), "utf8");
const popupScript = await readFile(new URL("../browser-extension/popup.js", import.meta.url), "utf8");

test("extension opens its controls in the browser side panel", async () => {
  const manifest = JSON.parse(await readFile(new URL("../browser-extension/manifest.json", import.meta.url), "utf8"));

  assert.equal(manifest.side_panel.default_path, "popup.html");
  assert.ok(manifest.permissions.includes("sidePanel"));
  assert.equal(manifest.action.default_popup, undefined);
});

test("Gemini health check and popup messaging have bounded timeouts", async () => {
  const manifest = JSON.parse(await readFile(new URL("../browser-extension/manifest.json", import.meta.url), "utf8"));

  assert.equal(manifest.version, "1.0.28");
  assert.match(serviceWorker, /HEALTH_CHECK_TIMEOUT_MS = 35000/);
  assert.match(serviceWorker, /HEALTH_CHECK_TIMEOUT/);
  assert.match(serviceWorker, /await Promise\.all\(\[/);
  assert.match(serviceWorker, /BACKEND_CHECK_TIMEOUT/);
  assert.match(serviceWorker, /GEMINI_CHECK_TIMEOUT/);
  assert.match(serviceWorker, /ensureGeminiTab\(20000\)/);
  assert.match(serviceWorker, /geminiCheck, 22000, "GEMINI_CHECK_TIMEOUT"/);
  assert.match(serviceWorker, /"\/health", "GET", undefined, 30000/);
  assert.doesNotMatch(serviceWorker.slice(serviceWorker.indexOf("async function healthCheck()"), serviceWorker.indexOf("async function pauseJob(")), /\/api\/gemini-extension\/status/);
  assert.doesNotMatch(serviceWorker.slice(serviceWorker.indexOf('if (message.type === "connect-app")'), serviceWorker.indexOf('if (message.type === "disconnect-app")')), /\/api\/gemini-extension\/status/);
  assert.match(serviceWorker, /CONNECTION_STATUS_TIMEOUT/);
  assert.ok(serviceWorker.indexOf('sendResponse({ ok: true, origin: appOrigin })') < serviceWorker.indexOf('setSiteConnection(tab.id, "Companion đã kết nối với website này.", true)'));
  assert.match(popupScript, /MESSAGE_TIMEOUT_MS = 38000/);
  assert.match(popupScript, /send\(\{ type: "connect-app", tab \}, 8000\)/);
  assert.match(popupScript, /void refresh\(\)/);
  assert.match(popupScript, /includes\(geminiCode\)/);
  assert.match(popupScript, /result\?\.backend\?\.message/);
});

test("Gemini content script loads by default and the app bridge is injected after site access is granted", async () => {
  const manifest = JSON.parse(await readFile(new URL("../browser-extension/manifest.json", import.meta.url), "utf8"));

  assert.ok(manifest.permissions.includes("scripting"));
  assert.ok(manifest.host_permissions.includes("https://gemini.google.com/*"));
  assert.deepEqual(manifest.optional_host_permissions, ["https://leotruyen.vercel.app/*", "http://127.0.0.1/*", "http://localhost/*"]);
  const geminiScript = manifest.content_scripts.find((script) => script.js.includes("content.js"));
  assert.deepEqual(geminiScript.matches, ["https://gemini.google.com/*"]);
  assert.deepEqual(geminiScript.js, ["content.js"]);
  assert.equal(manifest.content_scripts.some((script) => script.js.includes("app-bridge.js")), false);
  assert.match(serviceWorker, /files: \["app-bridge\.js"\]/);
  assert.match(serviceWorker, /await ensureAppBridge\(tab\.id\)/);
  assert.doesNotMatch(serviceWorker, /files: \["content\.js"\]/);
  assert.match(popupScript, /geminiCode !== "CONTENT_SCRIPT_NOT_READY"/);
  assert.match(popupScript, /chrome\.permissions\.request\(\{ origins: \[`\$\{appUrl\.protocol\}\/\/\$\{appUrl\.hostname\}\/\*`\] \}\)/);
});

test("website app bridge keeps queue requests in the logged-in page origin", async () => {
  let onMessage;
  let requested;
  let fetchAttempts = 0;
  const chrome = { runtime: { onMessage: { addListener: (listener) => { onMessage = listener; } } } };
  const context = {
    globalThis: {}, chrome, location: { origin: "https://leotruyen.vercel.app" }, URL,
    AbortController, setTimeout, clearTimeout,
    fetch: async (url, options) => {
      fetchAttempts += 1;
      if (fetchAttempts === 1) throw new TypeError("Failed to fetch");
      requested = { url: String(url), options };
      return { ok: true, json: async () => ({ job: null }) };
    },
  };
  vm.runInNewContext(appBridge, context);
  const result = await new Promise((resolve) => onMessage({ type: "app-bridge-request", path: "/api/gemini-extension/jobs/next" }, {}, resolve));
  assert.equal(result.ok, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload)), { job: null });
  assert.equal(requested.url, "https://leotruyen.vercel.app/api/gemini-extension/jobs/next");
  assert.equal(requested.options.credentials, "same-origin");
  assert.equal(fetchAttempts, 2);
});

test("Gemini health check does not require send or new-chat buttons before a job", () => {
  const composer = {
    getClientRects: () => [{}],
    getAttribute: () => null,
  };
  let onMessage;
  const document = {
    body: { innerText: "", getClientRects: () => [{}] },
    querySelectorAll(selector) {
      if (selector === '[role="textbox"], textarea, [contenteditable="true"]') return [composer];
      return [];
    },
    querySelector: () => ({}),
  };
  const chrome = {
    runtime: {
      sendMessage: () => Promise.resolve(),
      onMessage: { addListener: (listener) => { onMessage = listener; } },
      onConnect: { addListener() {} },
    },
  };
  const context = {
    globalThis: {}, document, chrome, location: { href: "https://gemini.google.com/app" },
    console: { info() {} }, MutationObserver: class {}, setTimeout, clearTimeout,
  };

  vm.runInNewContext(contentScript, context);
  let result;
  onMessage({ type: "health-check" }, {}, (value) => { result = value; });

  assert.equal(result.ok, true);
  assert.equal(result.code, "READY");
  assert.equal(result.checks.sendButton, false);
  assert.equal(result.checks.newChat, false);
});

test("unrelated Stop controls do not block a Gemini job", () => {
  let onMessage;
  const composer = { getClientRects: () => [{}] };
  const stopSharing = {
    getClientRects: () => [{}],
    getAttribute: (name) => name === "aria-label" ? "Stop sharing screen" : null,
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
  };
  const document = {
    body: { innerText: "Gemini" },
    querySelectorAll: (selector) => selector.startsWith('[role="textbox"]') ? [composer] : selector.startsWith("button") ? [stopSharing] : [],
    querySelector: () => ({}),
  };
  const chrome = { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener: (listener) => { onMessage = listener; } }, onConnect: { addListener() {} } } };
  vm.runInNewContext(contentScript, { globalThis: {}, document, chrome, location: { href: "https://gemini.google.com/app" }, console: { info() {} }, MutationObserver: class {}, setTimeout, clearTimeout });
  let result;
  onMessage({ type: "health-check" }, {}, (value) => { result = value; });
  assert.equal(result.checks.generating, false);
});

test("a busy conversation retries in a fresh tab only before submitting this job", () => {
  assert.match(serviceWorker, /result\?\.code === "GENERATION_IN_PROGRESS" && result\.submittedPrompts === 0/);
  assert.match(serviceWorker, /const fresh = await openGeminiTab\(\)/);
  assert.match(contentScript, /send\.click\(\);\s*activeJobSubmissions \+= 1;/);
});

test("Gemini waits briefly for an old response, then retries in a fresh tab", async () => {
  let stopVisible = true;
  let onMutation;
  const stopButton = {
    getClientRects: () => [{}], getAttribute: (name) => name === "aria-label" ? "Stop response" : null,
    innerText: "", title: "", querySelector: () => null, querySelectorAll: () => [],
  };
  const document = {
    body: { innerText: "Gemini" }, querySelector: () => null,
    querySelectorAll: (selector) => selector.startsWith("button") ? (stopVisible ? [stopButton] : []) : [],
  };
  const chrome = { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } };
  const context = {
    globalThis: {}, document, chrome, location: { href: "https://gemini.google.com/app" },
    console: { info() {} }, setTimeout, clearTimeout,
    MutationObserver: class { constructor(callback) { onMutation = callback; } observe() {} disconnect() {} },
  };
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__waitIdle = waitForGenerationIdle;\n})();");
  vm.runInNewContext(instrumented, context);
  const waiting = context.globalThis.__waitIdle(1000);
  stopVisible = false;
  onMutation();
  await waiting;
  stopVisible = true;
  await assert.rejects(context.globalThis.__waitIdle(0), (error) => error.code === "GENERATION_IN_PROGRESS" && error.reason === "transient");
  assert.match(contentScript, /PRIOR_GENERATION_TIMEOUT_MS = 10000/);
  assert.match(serviceWorker, /result\?\.code === "GENERATION_IN_PROGRESS" && result\.submittedPrompts === 0/);
  assert.match(contentScript, /await waitForGenerationIdle\(undefined, onProgress\)/);
});

test("Gemini contenteditable input uses the native edit command and waits for the full prompt", () => {
  assert.match(contentScript, /document\.execCommand\("insertText", false, prompt\)/);
  assert.match(contentScript, /await waitForComposerText\(box, prompt\)/);
  assert.match(contentScript, /PROMPT_INPUT_MISMATCH/);
  assert.match(contentScript, /box\.replaceChildren\(document\.createTextNode\(prompt\)\)/);
});

test("Gemini contenteditable receives the complete prompt through native insertion", () => {
  const prompt = "Dịch tự nhiên\n\n第一章 …";
  const box = { innerText: "old content", focus() {} };
  const selection = { removeAllRanges() {}, addRange() {} };
  const document = {
    querySelectorAll: () => [], querySelector: () => null, body: { innerText: "Gemini" },
    createRange: () => ({ selectNodeContents() {} }), execCommand: (_command, _ui, value) => { box.innerText = value; return true; },
  };
  const chrome = { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } };
  const context = {
    globalThis: {}, document, chrome, window: { getSelection: () => selection }, location: { href: "https://gemini.google.com/app" },
    HTMLTextAreaElement: class {}, HTMLInputElement: class {}, InputEvent: class {}, MutationObserver: class {},
    console: { info() {} }, setTimeout, clearTimeout,
  };
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__writePrompt = writePromptToComposer;\n})();");
  vm.runInNewContext(instrumented, context);
  context.globalThis.__writePrompt(box, prompt);
  assert.equal(box.innerText, prompt);
});

test("an accessible page with no composer is not mislabeled as logged out", () => {
  let onMessage;
  const chrome = { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener: (listener) => { onMessage = listener; } }, onConnect: { addListener() {} } } };
  const document = { body: { innerText: "Gemini", getClientRects: () => [{}] }, querySelectorAll: () => [], querySelector: () => null };
  const context = { globalThis: {}, document, chrome, location: { href: "https://gemini.google.com/app" }, console: { info() {} }, MutationObserver: class {}, setTimeout, clearTimeout };
  vm.runInNewContext(contentScript, context);
  let result;
  onMessage({ type: "health-check" }, {}, (value) => { result = value; });

  assert.equal(result.ok, false);
  assert.equal(result.code, "GEMINI_COMPOSER_NOT_FOUND");
  assert.equal(result.checks.loginRequired, false);
  assert.equal(result.checks.loggedIn, false);
});

test("login is required only when the page shows a login prompt", () => {
  let onMessage;
  const chrome = { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener: (listener) => { onMessage = listener; } }, onConnect: { addListener() {} } } };
  const document = { body: { innerText: "Sign in to continue", getClientRects: () => [{}] }, querySelectorAll: () => [], querySelector: () => null };
  const context = { globalThis: {}, document, chrome, location: { href: "https://gemini.google.com/app" }, console: { info() {} }, MutationObserver: class {}, setTimeout, clearTimeout };
  vm.runInNewContext(contentScript, context);
  let result;
  onMessage({ type: "health-check" }, {}, (value) => { result = value; });

  assert.equal(result.ok, false);
  assert.equal(result.code, "GEMINI_LOGIN_REQUIRED");
  assert.equal(result.checks.loginRequired, true);
});

function loadServiceWorker({ runtimeGranted = true, contentScriptReady = false, newTabScriptReady = true, backendError = null, legacyStatusTimeout = false, queueRequestHangs = false, tabUrl = "https://gemini.google.com/app", tabStatus = "complete", manifestDeclared = true, permissionError = null } = {}) {
  let newTab;
  let createdTabs = 0;
  const apiRequests = [];
  const storageState = queueRequestHangs ? {} : { appTabId: 2 };
  const geminiTab = { id: 1, url: tabUrl, status: tabStatus };
  const appTab = { id: 2, url: "https://leotruyen.vercel.app/truyen/test" };
  const listeners = {};
  const chrome = {
    runtime: {
      id: "test-extension",
      getManifest: () => ({ version: "1.0.23", host_permissions: ["https://leotruyen.vercel.app/*", ...(manifestDeclared ? ["https://gemini.google.com/*"] : [])] }),
      onInstalled: { addListener: (fn) => { listeners.installed = fn; } },
      onStartup: { addListener: (fn) => { listeners.startup = fn; } },
      onMessage: { addListener: (fn) => { listeners.message = fn; } },
      sendMessage: async () => {},
    },
    alarms: { get: async () => ({ name: "gemini-companion-poll" }), create: async () => {}, onAlarm: { addListener: () => {} } },
    sidePanel: { setPanelBehavior: async () => {} },
    storage: { local: { get: async (keys) => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map((key) => [key, storageState[key]])), set: async (value) => Object.assign(storageState, value), remove: async (keys) => { for (const key of Array.isArray(keys) ? keys : [keys]) delete storageState[key]; } } },
    tabs: {
      get: async (id) => id === 1 ? geminiTab : id === 2 ? appTab : id === 3 && newTab ? newTab : Promise.reject(new Error("Tab missing")),
      query: async () => [geminiTab, ...(newTab ? [newTab] : [])],
      create: async () => { createdTabs += 1; newTab = { id: 3, url: "https://gemini.google.com/app", status: "complete" }; return newTab; },
      reload: async () => {},
      sendMessage: async (id, message) => {
        if (message.type === "app-bridge-ping") return id === 2 ? { ready: true } : undefined;
        if (message.type === "app-bridge-request") {
          apiRequests.push(message.path);
          if (message.path === "/api/gemini-extension/status" && legacyStatusTimeout) return new Promise(() => {});
          if (message.path === "/api/gemini-extension/jobs/next" && queueRequestHangs) return new Promise(() => {});
          if (message.path === "/health" && backendError) return { ok: false, error: backendError.message };
          return { ok: true, payload: {} };
        }
        if (message.type === "ping") return (id === 3 ? newTabScriptReady : contentScriptReady) ? { ready: true } : undefined;
        if (message.type === "health-check") return { ok: true, code: "READY", checks: { loggedIn: true, composer: true, sendButton: false, newChat: false, responseContainer: true } };
        return { ok: true };
      },
      onRemoved: { addListener: () => {} },
    },
    permissions: {
      contains: async () => {
        if (permissionError) throw permissionError;
        return runtimeGranted;
      },
      getAll: async () => ({ origins: runtimeGranted ? ["https://gemini.google.com/*"] : [] }),
    },
    scripting: {
      executeScript: async ({ target, files, args }) => {
        if (files?.includes("content.js")) throw new Error("Dynamic injection must not run");
        return [{ result: { ok: true } }];
      },
    },
  };
  const context = { chrome, console: { info() {} }, URL, Date, Promise, Error, Object, String, Array, Math, JSON, setTimeout, clearTimeout };
  vm.runInNewContext(`${serviceWorker}\nglobalThis.__test = { ensureGeminiTabReady, ensureGeminiTab, healthCheck, pageRequest };`, context);
  return { api: context.__test, geminiTab, listeners, apiRequests, storageState, get createdTabs() { return createdTabs; } };
}

test("connecting the website tab does not wait for the remote queue status API", async () => {
  const worker = loadServiceWorker();
  const response = await new Promise((resolve) => {
    worker.listeners.message({ type: "connect-app", tab: { id: 2, url: "https://leotruyen.vercel.app/truyen/test" } }, {}, resolve);
  });

  assert.equal(response.ok, true);
  assert.equal(worker.storageState.appTabId, 2);
  assert.ok(!worker.apiRequests.includes("/api/gemini-extension/status"));
});

test("a hung website queue request returns an error instead of waiting forever", async () => {
  const worker = loadServiceWorker({ queueRequestHangs: true });
  await assert.rejects(worker.api.pageRequest(2, "/api/gemini-extension/jobs/next", "GET", undefined, 1), (error) => error.code === "BACKEND_REQUEST_TIMEOUT");
  assert.match(serviceWorker, /if \(message.type === "get-state"\)[\s\S]*?void processOneJob\(\)/);
});

test("missing runtime permission stops before using a Gemini tab", async () => {
  const worker = loadServiceWorker({ runtimeGranted: false });

  await assert.rejects(worker.api.ensureGeminiTabReady({ id: 1, url: "https://gemini.google.com/app" }, 1000), (error) => error.code === "HOST_PERMISSION_REQUIRED");
  assert.equal(worker.createdTabs, 0);
});

test("missing manifest host declaration is distinct from a user-denied permission", async () => {
  const worker = loadServiceWorker({ manifestDeclared: false });

  await assert.rejects(worker.api.ensureGeminiTabReady({ id: 1, url: "https://gemini.google.com/app" }, 1000), (error) => error.code === "HOST_PERMISSION_MISSING");
  assert.equal(worker.createdTabs, 0);
});

test("permission API failure is not reported as an ordinary denial", async () => {
  const worker = loadServiceWorker({ permissionError: new Error("permissions unavailable") });

  await assert.rejects(worker.api.ensureGeminiTabReady({ id: 1, url: "https://gemini.google.com/app" }, 1000), (error) => error.code === "PERMISSION_CHECK_FAILED" && /permissions unavailable/.test(error.rawError));
});

test("permission API failure is not displayed as granted", async () => {
  const worker = loadServiceWorker({ permissionError: new Error("permissions unavailable") });

  const result = await worker.api.healthCheck();

  assert.equal(result.backend.ok, true);
  assert.equal(result.browser.ok, false);
  assert.equal(result.browser.code, "PERMISSION_CHECK_FAILED");
});

test("an existing Gemini tab with its content script is reused", async () => {
  const worker = loadServiceWorker({ contentScriptReady: true });

  const result = await worker.api.ensureGeminiTab(1000);

  assert.equal(result.injected, false);
  assert.equal(result.tab.id, 1);
  assert.equal(worker.createdTabs, 0);
});

test("a stale Gemini tab is left intact and a fresh Companion tab is used", async () => {
  const worker = loadServiceWorker();

  const result = await worker.api.ensureGeminiTab(6000);

  assert.equal(result.tab.id, 3);
  assert.equal(result.permissionDiagnostics.actualAccess, true);
  assert.equal(worker.storageState.geminiTabId, 3);
  assert.equal(worker.createdTabs, 1);
  await worker.api.ensureGeminiTab(1000);
  assert.equal(worker.createdTabs, 1);
});

test("a fresh tab without a content script fails without opening more tabs", async () => {
  const worker = loadServiceWorker({ newTabScriptReady: false });

  await assert.rejects(worker.api.ensureGeminiTab(6000), (error) => error.code === "CONTENT_SCRIPT_NOT_READY" && error.tabId === 3);
  await assert.rejects(worker.api.ensureGeminiTab(1000), (error) => error.code === "CONTENT_SCRIPT_NOT_READY");
  assert.equal(worker.createdTabs, 1);
});

test("a Companion tab in Google sign-in stays selected until login completes", async () => {
  const worker = loadServiceWorker({ tabUrl: "https://accounts.google.com/signin" });
  worker.storageState.geminiTabId = 1;

  await assert.rejects(worker.api.ensureGeminiTab(1000), (error) => error.code === "GEMINI_LOGIN_REQUIRED");
  assert.equal(worker.createdTabs, 0);
  assert.equal(worker.storageState.geminiTabId, 1);
});

test("invalid tab URL is distinguished before content script access", async () => {
  const worker = loadServiceWorker({ tabUrl: "https://example.com/" });

  await assert.rejects(worker.api.ensureGeminiTabReady({ id: 1, url: "https://example.com/" }, 1000), (error) => error.code === "TAB_URL_INVALID");
  assert.equal(worker.createdTabs, 0);
});

test("a loading Gemini tab is allowed to finish before content script access", async () => {
  const worker = loadServiceWorker({ tabStatus: "loading", contentScriptReady: true });
  const tab = { id: 1, url: "https://gemini.google.com/app", status: "loading" };
  setTimeout(() => { worker.geminiTab.status = "complete"; }, 20);

  const result = await worker.api.ensureGeminiTabReady(tab, 1000);
  assert.equal(result.injected, false);
  assert.equal(worker.createdTabs, 0);
});

test("a Gemini tab stuck loading returns a bounded load timeout", async () => {
  const worker = loadServiceWorker({ tabStatus: "loading" });
  const tab = { id: 1, url: "https://gemini.google.com/app", status: "loading" };

  await assert.rejects(worker.api.ensureGeminiTabReady(tab, 40), (error) => error.code === "TAB_LOAD_TIMEOUT" && error.tabStatus === "loading");
  assert.equal(worker.createdTabs, 0);
});

test("Gemini health check uses the lightweight health route even when queue status would hang", async () => {
  const worker = loadServiceWorker({ contentScriptReady: true, legacyStatusTimeout: true });

  const result = await worker.api.healthCheck();

  assert.equal(result.code, "READY");
  assert.equal(result.backend.ok, true);
  assert.ok(worker.apiRequests.includes("/health"));
  assert.ok(!worker.apiRequests.includes("/api/gemini-extension/status"));
});

test("a failed website health request does not hide a separate Gemini permission failure", async () => {
  const worker = loadServiceWorker({ runtimeGranted: false, backendError: new Error("Website unavailable") });

  const result = await worker.api.healthCheck();

  assert.equal(result.backend.ok, false);
  assert.equal(result.browser.code, "HOST_PERMISSION_REQUIRED");
  assert.equal(result.gemini.code, "HOST_PERMISSION_REQUIRED");
});

test("Gemini content script failure leaves a healthy backend healthy", async () => {
  const worker = loadServiceWorker({ newTabScriptReady: false });

  const result = await worker.api.healthCheck();

  assert.equal(result.backend.ok, true);
  assert.equal(result.browser.code, "CONTENT_SCRIPT_NOT_READY");
  assert.equal(result.gemini.code, "CONTENT_SCRIPT_NOT_READY");
});
