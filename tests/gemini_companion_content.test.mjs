import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const contentScript = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");
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

  assert.equal(manifest.version, "1.0.16");
  assert.match(serviceWorker, /HEALTH_CHECK_TIMEOUT_MS = 12000/);
  assert.match(serviceWorker, /HEALTH_CHECK_TIMEOUT/);
  assert.match(serviceWorker, /await Promise\.all\(\[/);
  assert.match(serviceWorker, /BACKEND_CHECK_TIMEOUT/);
  assert.match(serviceWorker, /GEMINI_CHECK_TIMEOUT/);
  assert.match(serviceWorker, /ensureGeminiTab\(9500\)/);
  assert.match(serviceWorker, /geminiCheck, 11000, "GEMINI_CHECK_TIMEOUT"/);
  assert.match(serviceWorker, /status", "GET", undefined, 4000/);
  assert.doesNotMatch(serviceWorker.slice(serviceWorker.indexOf('if (message.type === "connect-app")'), serviceWorker.indexOf('if (message.type === "disconnect-app")')), /\/api\/gemini-extension\/status/);
  assert.match(serviceWorker, /CONNECTION_STATUS_TIMEOUT/);
  assert.ok(serviceWorker.indexOf('sendResponse({ ok: true, origin: appOrigin })') < serviceWorker.indexOf('setSiteConnection(tab.id, "Companion đã kết nối với website này.", true)'));
  assert.match(popupScript, /MESSAGE_TIMEOUT_MS = 14000/);
  assert.match(popupScript, /send\(\{ type: "connect-app", tab \}, 8000\)/);
  assert.match(popupScript, /void refresh\(\)/);
  assert.match(popupScript, /includes\(geminiCode\)/);
  assert.match(popupScript, /result\?\.backend\?\.message/);
});

test("Gemini host access is optional and uses one explicit dynamic injection path", async () => {
  const manifest = JSON.parse(await readFile(new URL("../browser-extension/manifest.json", import.meta.url), "utf8"));

  assert.ok(manifest.permissions.includes("scripting"));
  assert.ok(manifest.optional_host_permissions.includes("https://gemini.google.com/*"));
  assert.ok(!manifest.host_permissions.includes("https://gemini.google.com/*"));
  assert.equal(manifest.content_scripts, undefined);
  assert.match(serviceWorker, /chrome\.scripting\.executeScript\(\{ target: \{ tabId: tab\.id \}, files: \["content\.js"\] \}\)/);
  assert.match(popupScript, /chrome\.permissions\.request\(\{ origins: \[GEMINI_PERMISSION\] \}\)/);
  assert.match(popupScript, /grantPermission\"\)\.addEventListener\(\"click\"/);
  assert.match(popupScript, /Chưa cấp quyền Gemini/);
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

test("an accessible page with no composer is not mislabeled as logged out", () => {
  let onMessage;
  const chrome = { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener: (listener) => { onMessage = listener; } } } };
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
  const chrome = { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener: (listener) => { onMessage = listener; } } } };
  const document = { body: { innerText: "Sign in to continue", getClientRects: () => [{}] }, querySelectorAll: () => [], querySelector: () => null };
  const context = { globalThis: {}, document, chrome, location: { href: "https://gemini.google.com/app" }, console: { info() {} }, MutationObserver: class {}, setTimeout, clearTimeout };
  vm.runInNewContext(contentScript, context);
  let result;
  onMessage({ type: "health-check" }, {}, (value) => { result = value; });

  assert.equal(result.ok, false);
  assert.equal(result.code, "GEMINI_LOGIN_REQUIRED");
  assert.equal(result.checks.loginRequired, true);
});

function loadServiceWorker({ runtimeGranted = true, injectError = null, backendTimeout = false, tabUrl = "https://gemini.google.com/app", tabStatus = "complete", manifestDeclared = true, permissionError = null } = {}) {
  let contentScriptReady = false;
  let injections = 0;
  const apiRequests = [];
  const storageState = { appTabId: 2 };
  const geminiTab = { id: 1, url: tabUrl, status: tabStatus };
  const appTab = { id: 2, url: "https://leotruyen.vercel.app/truyen/test" };
  const listeners = {};
  const chrome = {
    runtime: {
      id: "test-extension",
      getManifest: () => ({ version: "1.0.16", host_permissions: ["https://leotruyen.vercel.app/*"], optional_host_permissions: manifestDeclared ? ["https://gemini.google.com/*"] : [] }),
      onInstalled: { addListener: (fn) => { listeners.installed = fn; } },
      onStartup: { addListener: (fn) => { listeners.startup = fn; } },
      onMessage: { addListener: (fn) => { listeners.message = fn; } },
      sendMessage: async () => {},
    },
    alarms: { get: async () => ({ name: "gemini-companion-poll" }), create: async () => {}, onAlarm: { addListener: () => {} } },
    sidePanel: { setPanelBehavior: async () => {} },
    storage: { local: { get: async (key) => key === "appTabId" ? { appTabId: storageState.appTabId } : {}, set: async (value) => Object.assign(storageState, value), remove: async () => {} } },
    tabs: {
      get: async (id) => id === 1 ? geminiTab : appTab,
      query: async () => [geminiTab],
      create: async () => geminiTab,
      reload: async () => {},
      sendMessage: async (_id, message) => {
        if (message.type === "ping") return contentScriptReady ? { ready: true } : undefined;
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
        if (files?.includes("content.js")) {
          injections += 1;
          if (injectError) throw injectError;
          contentScriptReady = true;
          return [];
        }
        if (args?.[0]?.path) apiRequests.push(args[0].path);
        if (target.tabId === 2 && backendTimeout) return new Promise(() => {});
        return [{ result: { ok: true } }];
      },
    },
  };
  const context = { chrome, console: { info() {} }, URL, Date, Promise, Error, Object, String, Array, Math, JSON, setTimeout, clearTimeout };
  vm.runInNewContext(`${serviceWorker}\nglobalThis.__test = { ensureGeminiTabReady, healthCheck };`, context);
  return { api: context.__test, geminiTab, listeners, apiRequests, storageState, get injections() { return injections; } };
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

test("missing runtime permission stops before ping or injection", async () => {
  const worker = loadServiceWorker({ runtimeGranted: false });

  await assert.rejects(worker.api.ensureGeminiTabReady({ id: 1, url: "https://gemini.google.com/app" }, 1000), (error) => error.code === "HOST_PERMISSION_REQUIRED");
  assert.equal(worker.injections, 0);
});

test("missing manifest host declaration is distinct from a user-denied permission", async () => {
  const worker = loadServiceWorker({ manifestDeclared: false });

  await assert.rejects(worker.api.ensureGeminiTabReady({ id: 1, url: "https://gemini.google.com/app" }, 1000), (error) => error.code === "HOST_PERMISSION_MISSING");
  assert.equal(worker.injections, 0);
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

test("granted permission dynamically injects content.js once and waits for its ping", async () => {
  const worker = loadServiceWorker();

  const result = await worker.api.ensureGeminiTabReady({ id: 1, url: "https://gemini.google.com/app" }, 1000);

  assert.equal(result.injected, true);
  assert.equal(result.permissionDiagnostics.actualAccess, true);
  assert.equal(worker.injections, 1);
});

test("invalid tab URL is distinguished before injection", async () => {
  const worker = loadServiceWorker();

  await assert.rejects(worker.api.ensureGeminiTabReady({ id: 3, url: "https://example.com/" }, 1000), (error) => error.code === "TAB_URL_INVALID");
  assert.equal(worker.injections, 0);
});

test("a loading Gemini tab is allowed to finish before content script injection", async () => {
  const worker = loadServiceWorker({ tabStatus: "loading" });
  const tab = { id: 1, url: "https://gemini.google.com/app", status: "loading" };
  setTimeout(() => { worker.geminiTab.status = "complete"; }, 20);

  const result = await worker.api.ensureGeminiTabReady(tab, 1000);
  assert.equal(result.injected, true);
  assert.equal(worker.injections, 1);
});

test("a Gemini tab stuck loading returns a bounded load timeout", async () => {
  const worker = loadServiceWorker({ tabStatus: "loading" });
  const tab = { id: 1, url: "https://gemini.google.com/app", status: "loading" };

  await assert.rejects(worker.api.ensureGeminiTabReady(tab, 40), (error) => error.code === "TAB_LOAD_TIMEOUT" && error.tabStatus === "loading");
  assert.equal(worker.injections, 0);
});

test("Chrome injection denial is distinct from missing permission", async () => {
  const worker = loadServiceWorker({ injectError: new Error("Cannot access contents of the page. Extension manifest must request permission to access the respective host.") });

  await assert.rejects(worker.api.ensureGeminiTabReady({ id: 1, url: "https://gemini.google.com/app" }, 1000), (error) => error.code === "INJECT_FAILED" && error.diagnostics.runtimeGranted && error.diagnostics.actualAccess === false);
});

test("backend timeout does not hide a separate Gemini permission failure", async () => {
  const worker = loadServiceWorker({ runtimeGranted: false, backendTimeout: true });

  const result = await worker.api.healthCheck();

  assert.equal(result.backend.code, "BACKEND_CHECK_TIMEOUT");
  assert.equal(result.browser.code, "HOST_PERMISSION_REQUIRED");
  assert.equal(result.gemini.code, "HOST_PERMISSION_REQUIRED");
  assert.equal(result.code, "BACKEND_CHECK_TIMEOUT");
});

test("Gemini inject failure leaves a healthy backend healthy", async () => {
  const worker = loadServiceWorker({ injectError: new Error("Cannot access contents of the page. Extension manifest must request permission to access the respective host.") });

  const result = await worker.api.healthCheck();

  assert.equal(result.backend.ok, true);
  assert.equal(result.browser.code, "INJECT_FAILED");
  assert.equal(result.gemini.code, "INJECT_FAILED");
});
