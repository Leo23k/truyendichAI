import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const contentScript = await readFile(new URL("../browser-extension/content.js", import.meta.url), "utf8");

test("extension opens its controls in the browser side panel", async () => {
  const manifest = JSON.parse(await readFile(new URL("../browser-extension/manifest.json", import.meta.url), "utf8"));

  assert.equal(manifest.side_panel.default_path, "popup.html");
  assert.ok(manifest.permissions.includes("sidePanel"));
  assert.equal(manifest.action.default_popup, undefined);
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
