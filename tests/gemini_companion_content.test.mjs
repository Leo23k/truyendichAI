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

  assert.equal(manifest.version, "1.0.12");
  assert.match(serviceWorker, /HEALTH_CHECK_TIMEOUT_MS = 12000/);
  assert.match(serviceWorker, /HEALTH_CHECK_TIMEOUT/);
  assert.match(serviceWorker, /await Promise\.all\(\[/);
  assert.match(serviceWorker, /BACKEND_CHECK_TIMEOUT/);
  assert.match(serviceWorker, /GEMINI_CHECK_TIMEOUT/);
  assert.match(serviceWorker, /status", "GET", undefined, 5000/);
  assert.match(serviceWorker, /CONNECTION_STATUS_TIMEOUT/);
  assert.match(popupScript, /MESSAGE_TIMEOUT_MS = 14000/);
  assert.match(popupScript, /send\(\{ type: "connect-app", tab \}, 8000\)/);
  assert.match(popupScript, /void refresh\(\)/);
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
