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

  assert.equal(manifest.version, "1.0.46");
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

test("Gemini tabs with stale Companion code are rejected by the version handshake", () => {
  assert.match(contentScript, /CONTENT_SCRIPT_VERSION = "1\.0\.46"/);
  assert.match(contentScript, /sendResponse\(\{ ready: true, version: CONTENT_SCRIPT_VERSION \}\)/);
  assert.match(serviceWorker, /reply\.version === chrome\.runtime\.getManifest\(\)\.version/);
  assert.match(serviceWorker, /await activateGeminiTab\(tab\.id\)/);
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

test("website app bridge preserves structured save errors instead of returning [object Object]", async () => {
  let onMessage;
  const chrome = { runtime: { onMessage: { addListener: (listener) => { onMessage = listener; } } } };
  const context = {
    globalThis: {}, chrome, location: { origin: "https://leotruyen.vercel.app" }, URL,
    AbortController, setTimeout, clearTimeout,
    fetch: async () => ({ ok: false, status: 422, json: async () => ({ detail: [{ msg: "chapter title is required" }] }) }),
  };
  vm.runInNewContext(appBridge, context);
  const result = await new Promise((resolve) => onMessage({ type: "app-bridge-request", path: "/api/gemini-extension/jobs/1/complete", method: "POST", body: {} }, {}, resolve));
  assert.equal(result.ok, false);
  assert.match(result.error, /chapter title is required/);
  assert.doesNotMatch(result.error, /\[object Object\]/);
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

test("Gemini chooses the active composer when multiple text editors are visible", () => {
  const inactive = { getClientRects: () => [{}], contains: () => false };
  const active = { getClientRects: () => [{}], contains: () => false };
  const document = { activeElement: active, querySelectorAll: () => [inactive, active] };
  const context = { globalThis: {}, document, chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } }, console: { info() {} } };
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__composer = GeminiSelectors.composer;\n})();");
  vm.runInNewContext(instrumented, context);

  assert.equal(context.globalThis.__composer(), active);
});

test("unrelated Stop controls do not block a Gemini job", () => {
  let onMessage;
  const composer = { tagName: "DIV", getClientRects: () => [{}], getAttribute: () => null };
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
  assert.match(contentScript, /send\.click\(\)/);
  assert.doesNotMatch(contentScript, /new KeyboardEvent\("keydown", \{ key: "Enter"/);
  assert.match(contentScript, /SEND_BUTTON_NOT_READY/);
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
  assert.match(contentScript, /PROMPT_INPUT_FAILED/);
  assert.doesNotMatch(contentScript, /box\.replaceChildren\(document\.createTextNode\(prompt\)\)/);
});

test("Gemini prompt verification tolerates editor whitespace but rejects missing content", () => {
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__matchesPrompt = composerTextMatches;\n})();");
  const context = { globalThis: {}, chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } }, console: { info() {} } };
  vm.runInNewContext(instrumented, context);

  assert.equal(context.globalThis.__matchesPrompt({ innerText: "Dịch tự nhiên\n第一章　黎明" }, "Dịch tự nhiên\n\n第一章　黎明"), true);
  assert.equal(context.globalThis.__matchesPrompt({ innerText: "Dịch tự nhiên\n第一章" }, "Dịch tự nhiên\n\n第一章　黎明"), false);
});

test("Gemini send detection supports send-button test IDs and synchronizes composer input", () => {
  assert.match(contentScript, /querySelectorAll\('button, \[role="button"\], input\[type="submit"\]'\)/);
  assert.match(contentScript, /node\.getAttribute\("data-testid"\)/);
  assert.match(contentScript, /send\|submit/i);
  assert.match(contentScript, /box\.dispatchEvent\(new InputEvent\("input"/);
  assert.match(contentScript, /SEND_BUTTON_READY/);
  assert.match(contentScript, /SEND_TRIGGERED/);
  assert.doesNotMatch(contentScript, /form\.requestSubmit\(\)/);
  assert.match(contentScript, /send\.click\(\)/);
});

test("Gemini send detection selects the composer-associated send label, not the microphone", () => {
  const root = { parentElement: null, contains: (node) => node.parentElement === root, getBoundingClientRect: () => ({ left: 0, right: 500, width: 500, top: 0, bottom: 140 }) };
  const makeControl = (label, right) => ({
    parentElement: root, innerText: "", title: "", value: "", disabled: false,
    getClientRects: () => [{}], getAttribute: (name) => name === "aria-label" ? label : null,
    querySelector: () => null, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: right - 30, right, width: 30, top: 90, bottom: 120 }),
  });
  const microphone = makeControl("Use microphone", 480);
  const sendControl = makeControl("Send message", 450);
  const unrelatedSend = makeControl("Send message", 490);
  unrelatedSend.parentElement = {};
  const box = {
    parentElement: root, getClientRects: () => [{}], getBoundingClientRect: () => ({ left: 0, right: 500, width: 500, top: 0, bottom: 80 }),
    getAttribute: () => null, querySelector: () => null, querySelectorAll: () => [],
  };
  const document = {
    querySelectorAll: (selector) => selector.includes('role="textbox"') ? [box] : selector.startsWith("button") ? [microphone, sendControl, unrelatedSend] : [],
  };
  const context = { globalThis: {}, document, chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } }, console: { info() {} } };
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__send = GeminiSelectors.send;\n})();");
  vm.runInNewContext(instrumented, context);

  assert.equal(context.globalThis.__send(), sendControl);
  assert.match(contentScript, /verticalGap <= 48/);
  assert.match(contentScript, /candidate\.semanticSend/);
  assert.doesNotMatch(contentScript, /sort\(\(left, right\) => right\.node\.getBoundingClientRect\(\)\.right/);
  assert.match(contentScript, /model\|flash\|thinking\|reasoning/);
});

test("Gemini never clicks a send-button-container DIV instead of its interactive child", () => {
  const root = { parentElement: null, contains: (node) => node.parentElement === root, getAttribute: () => null, getBoundingClientRect: () => ({ left: 0, right: 500, width: 500, top: 0, bottom: 140 }) };
  const container = {
    tagName: "DIV", parentElement: root, getClientRects: () => [{}],
    getAttribute: (name) => name === "data-testid" ? "send-button-container" : null,
    getBoundingClientRect: () => ({ left: 450, right: 500, width: 50, top: 90, bottom: 120 }),
  };
  const button = {
    tagName: "BUTTON", parentElement: root, disabled: false,
    getClientRects: () => [{}], getAttribute: (name) => name === "aria-label" ? "Send message" : null,
    hasAttribute: () => false, querySelector: () => null, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 460, right: 490, width: 30, top: 90, bottom: 120 }),
  };
  const box = {
    tagName: "DIV", parentElement: root, isContentEditable: true,
    getClientRects: () => [{}], getAttribute: (name) => name === "role" ? "textbox" : null,
    getBoundingClientRect: () => ({ left: 0, right: 400, width: 400, top: 0, bottom: 80 }),
  };
  const document = {
    activeElement: box,
    querySelectorAll: (selector) => selector.includes('role="textbox"') ? [box]
      : selector.includes("data-testid") ? [container]
      : selector.startsWith("button") ? [button] : [],
  };
  const context = { globalThis: {}, document, chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } }, console: { info() {} } };
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__send = GeminiSelectors.send;\n})();");
  vm.runInNewContext(instrumented, context);

  assert.equal(context.globalThis.__send(), button);
});

test("Gemini response diagnostics describe DOM metadata without exposing response text", () => {
  const response = {
    tagName: "MODEL-RESPONSE", innerText: "PRIVATE TRANSLATION OUTPUT", children: [{ tagName: "DIV" }, { tagName: "P" }],
    getClientRects: () => [{}],
    getAttribute: (name) => ({ "data-testid": "model-response", role: "article", "data-message-author-role": "model" })[name] ?? null,
  };
  const document = {
    querySelectorAll: (selector) => selector === "model-response" || selector === "[data-testid], [data-test-id], [data-message-author-role], [role]" ? [response] : [],
  };
  const context = { globalThis: {}, document, chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } }, console: { info() {} } };
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__inspectResponses = inspectResponseDom;\n})();");
  vm.runInNewContext(instrumented, context);
  const details = context.globalThis.__inspectResponses();

  assert.equal(details.bySelector.find((item) => item.selector === "model-response").nonEmpty, 1);
  assert.equal(details.semanticCandidates[0].textChars, "PRIVATE TRANSLATION OUTPUT".length);
  assert.equal(JSON.stringify(details).includes("PRIVATE TRANSLATION OUTPUT"), false);
});

test("Gemini reads the model-response wrapper when an older message-content node is the only nonempty child selector", () => {
  const oldContent = { tagName: "MESSAGE-CONTENT", innerText: "older content", children: [], getAttribute: () => null };
  const emptyContent = { tagName: "MESSAGE-CONTENT", innerText: "", children: [], getAttribute: () => null };
  const modelResponse = { tagName: "MODEL-RESPONSE", innerText: "new translated text", children: [emptyContent], getAttribute: () => null };
  const document = {
    querySelectorAll: (selector) => selector === "model-response" ? [modelResponse]
      : selector === "message-content" ? [oldContent, emptyContent]
      : [],
  };
  const context = { globalThis: {}, document, chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } }, console: { info() {} } };
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__responses = GeminiSelectors.responses;\n})();");
  vm.runInNewContext(instrumented, context);

  assert.deepEqual(Array.from(context.globalThis.__responses()), [modelResponse]);
});

test("Gemini recognizes response text updates when the model-response node count stays the same", () => {
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__responseChanged = responseChangedSinceSubmit;\n})();");
  const context = { globalThis: {}, chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } }, console: { info() {} } };
  vm.runInNewContext(instrumented, context);

  assert.equal(context.globalThis.__responseChanged({ priorCount: 4, currentCount: 4, previousOutput: "old", output: "new translation", generationSeen: true }), true);
  assert.equal(context.globalThis.__responseChanged({ priorCount: 4, currentCount: 4, previousOutput: "same", output: "same", generationSeen: true }), false);
  assert.equal(context.globalThis.__responseChanged({ priorCount: 4, currentCount: 4, previousOutput: "old", output: "new", generationSeen: false }), false);
  assert.equal(context.globalThis.__responseChanged({ priorCount: 4, currentCount: 5, previousOutput: "old", output: "", generationSeen: false }), true);
});

test("Gemini translation submits and returns the translated chapter title", async () => {
  const context = {
    globalThis: {}, prompts: [], location: { href: "https://gemini.google.com/app" },
    chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } },
    console: { info() {} },
  };
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, `
    startNewConversation = async () => { conversationInstructionsSent = false; conversationStatsKey = null; };
    translateWholeChapter = async (job) => {
      await submitTranslationText(job.prompt_snapshot, job.content);
      return "Translated chapter text";
    };
    submitPrompt = async (prompt) => { prompts.push(prompt); return prompts.length === 1 ? "Translated chapter text" : "Translated chapter title"; };
    globalThis.__runTranslationJob = runTranslationJob;
  })();`);
  vm.runInNewContext(instrumented, context);

  const result = await context.globalThis.__runTranslationJob({
    title: "第一章 夜无疆", content: "原文", prompt_snapshot: "Translate: {{text}}",
  }, null);

  assert.deepEqual(context.prompts, ["Translate: 原文", "第一章 夜无疆"]);
  assert.equal(result.translated_title, "Translated chapter title");
  assert.equal(result.translated_content, "Translated chapter text");
});

test("Gemini does not choose an unlabeled rightmost control as Send", () => {
  const root = { parentElement: null, contains: (node) => node.parentElement === root, getAttribute: () => null, getBoundingClientRect: () => ({ left: 0, right: 500, width: 500, top: 0, bottom: 140 }) };
  const control = {
    tagName: "BUTTON", parentElement: root, innerText: "", title: "", disabled: false,
    getClientRects: () => [{}], getAttribute: () => null, hasAttribute: () => false,
    querySelector: () => null, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 470, right: 500, width: 30, top: 90, bottom: 120 }),
  };
  const box = {
    tagName: "DIV", parentElement: root, isContentEditable: true,
    getClientRects: () => [{}], getAttribute: () => "true",
    getBoundingClientRect: () => ({ left: 0, right: 400, width: 400, top: 0, bottom: 80 }),
  };
  const document = { querySelectorAll: (selector) => selector.startsWith('[role="textbox"]') ? [box] : [control] };
  const context = { globalThis: {}, document, chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } }, console: { info() {} } };
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__inspectSend = inspectSendButton;\n})();");
  vm.runInNewContext(instrumented, context);
  const result = context.globalThis.__inspectSend();

  assert.equal(result.button, null);
  assert.equal(result.reason, "unknown");
  assert.equal(result.diagnostics.buttons[0].reason, "no_send_semantics");
  assert.deepEqual(Array.from(result.diagnostics.buttons[0].childTags), []);
  assert.deepEqual(Array.from(result.diagnostics.buttons[0].icons), []);
});

test("Gemini reports a distinct error when no button exists near the composer", async () => {
  let mutation;
  const root = { tagName: "DIV", contains: () => false, getAttribute: () => null };
  const box = {
    tagName: "DIV", parentElement: root, isContentEditable: true,
    getClientRects: () => [{}], getAttribute: () => "true",
    getBoundingClientRect: () => ({ left: 0, right: 400, width: 400, top: 0, bottom: 80 }),
  };
  const document = { body: {}, querySelectorAll: (selector) => selector.startsWith('[role="textbox"]') ? [box] : [] };
  const context = {
    globalThis: {}, document, chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } },
    console: { info() {} }, MutationObserver: class { constructor(callback) { mutation = callback; } observe() {} disconnect() {} }, setTimeout, clearTimeout,
  };
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__waitSend = waitForUsableSendButton;\n})();");
  vm.runInNewContext(instrumented, context);
  const waiting = context.globalThis.__waitSend(5);
  mutation();
  await assert.rejects(waiting, (error) => error.code === "SEND_BUTTON_NOT_FOUND" && error.diagnostics.buttonCount === 0);
});

test("Gemini exposes safe diagnostics when a clicked Send button is not accepted", async () => {
  const prompt = "SECRET TRANSLATION PROMPT";
  const root = { parentElement: null, contains: (node) => node.parentElement === root, getAttribute: () => null };
  const box = {
    tagName: "DIV", parentElement: root, isContentEditable: true, innerText: prompt,
    getClientRects: () => [{}], getAttribute: (name) => name === "contenteditable" ? "true" : null,
    getBoundingClientRect: () => ({ left: 0, right: 400, width: 400, top: 0, bottom: 80 }),
  };
  let clicks = 0;
  const send = {
    tagName: "BUTTON", parentElement: root, disabled: false, innerText: "", title: "Send message",
    isConnected: true, click: () => { clicks += 1; },
    getClientRects: () => [{}], hasAttribute: () => false,
    getAttribute: (name) => ({ "aria-label": "Send message", title: "Send message", role: "button", "data-testid": "send-button" })[name] ?? null,
    querySelector: () => null, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 370, right: 400, width: 30, top: 90, bottom: 120 }),
  };
  const document = {
    body: { innerText: "Gemini" }, activeElement: box,
    querySelectorAll: (selector) => selector.includes('role="textbox"') ? [box] : selector.startsWith("button") || selector.startsWith("[") ? [send] : [],
  };
  const context = {
    globalThis: {}, document, location: { href: "https://gemini.google.com/app" },
    chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } },
    console: { info() {} }, MutationObserver: class { observe() {} disconnect() {} },
    setTimeout: (callback, delay) => delay === 15000 ? setTimeout(callback, 0) : setTimeout(callback, delay), clearTimeout,
  };
  context.send = send;
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, `
    waitForGenerationIdle = async () => {};
    writePromptToComposer = () => {};
    waitForComposerText = async () => {};
    waitForUsableSendButton = async () => send;
    globalThis.__submit = submitPrompt;
  })();`);
  vm.runInNewContext(instrumented, context);

  await assert.rejects(context.globalThis.__submit(prompt), (error) => {
    assert.equal(error.code, "SEND_NOT_CONFIRMED");
    assert.match(error.message, /\"method\":\"button\.click\"/);
    assert.match(error.message, /\"retained\":true/);
    assert.match(error.message, /\"ariaLabel\":\"Send message\"/);
    assert.match(error.message, /\"sendConnected\":true/);
    assert.doesNotMatch(error.message, new RegExp(prompt));
    return true;
  });
  assert.equal(clicks, 1);
});

test("Gemini logs DOM evidence for each submission step without logging prompt text", () => {
  for (const stage of ["COMPOSER_FOUND", "PROMPT_FILLED", "SEND_BUTTON_READY", "SEND_TRIGGERED", "PROMPT_CLEARED", "PROMPT_SUBMITTED_CONFIRMED", "RESPONSE_STARTED", "RESPONSE_DETECTED", "RESPONSE_COMPLETED", "TRANSLATION_EXTRACTED"]) {
    assert.match(contentScript, new RegExp(`log\\("${stage}"`));
  }
  assert.match(contentScript, /log\("SEND_BUTTON_NOT_READY", \{ code: error\.code \|\| "SEND_STATE_UNKNOWN", \.\.\.details/);
  assert.match(contentScript, /Nút gần ô nhập: \$\{summary\}/);
  assert.match(serviceWorker, /log\("GEMINI_TAB_FOUND"/);
  assert.match(serviceWorker, /saved\?\.status !== "completed"/);
  assert.match(serviceWorker, /log\("BACKEND_SAVE_CONFIRMED"/);
  assert.match(serviceWorker, /log\("BACKEND_SAVE_FAILED"/);
  assert.match(contentScript, /composerChars: composerText\(currentComposer \|\| \{\}\)\.length/);
  assert.doesNotMatch(contentScript, /log\([^\n]*prompt\s*[,}]/i);
  assert.doesNotMatch(contentScript, /new KeyboardEvent\("keydown", \{ key: "Enter"/);
  assert.match(contentScript, /sendButton: describeSendButton\(send\)/);
  assert.match(contentScript, /method: "button\.click"/);
});

test("Gemini acceptance detection recognizes cleared composer, user turn, response, or generation", () => {
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__accepted = promptWasAccepted;\n})();");
  const context = { globalThis: {}, chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } }, console: { info() {} } };
  vm.runInNewContext(instrumented, context);

  assert.equal(context.globalThis.__accepted({ composerCleared: true }), false);
  assert.equal(context.globalThis.__accepted({ userMessageMatches: true }), true);
  assert.equal(context.globalThis.__accepted({ responseAdded: true }), true);
  assert.equal(context.globalThis.__accepted({ generating: true }), true);
  assert.equal(context.globalThis.__accepted({}), false);
});

test("Gemini contenteditable receives the complete prompt through native insertion", () => {
  const prompt = "Dịch tự nhiên\n\n第一章 …";
  const box = { innerText: "old content", isContentEditable: true, focus() {}, dispatchEvent() {}, getAttribute: () => "true" };
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

test("Gemini does not treat a disabled send control as ready", async () => {
  let mutation;
  let ariaDisabled = null;
  const disabledSend = {
    tagName: "BUTTON", innerText: "", title: "",
    disabled: true,
    getClientRects: () => [{}],
    getAttribute: (name) => name === "aria-disabled" ? ariaDisabled : name === "aria-label" ? "Send message" : null,
    hasAttribute: () => false, querySelector: () => null, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 470, right: 500, width: 30, top: 90, bottom: 120 }),
  };
  const root = {
    tagName: "DIV",
    contains: (node) => node === disabledSend || node === box,
    getAttribute: () => null,
    getBoundingClientRect: () => ({ left: 0, right: 500, width: 500, top: 0, bottom: 140 }),
  };
  const box = {
    tagName: "DIV", isContentEditable: true,
    parentElement: root,
    getClientRects: () => [{}],
    getAttribute: () => "true",
    getBoundingClientRect: () => ({ left: 0, right: 400, width: 400, top: 0, bottom: 80 }),
  };
  const document = { body: {}, querySelectorAll: (selector) => selector.startsWith('[role="textbox"]') ? [box] : [disabledSend] };
  const context = {
    globalThis: {}, document,
    chrome: { runtime: { sendMessage: () => Promise.resolve(), onMessage: { addListener() {} }, onConnect: { addListener() {} } } },
    console: { info() {} }, MutationObserver: class { constructor(callback) { mutation = callback; } observe() {} disconnect() {} },
    setTimeout, clearTimeout,
  };
  const instrumented = contentScript.replace(/\}\)\(\);\s*$/, "globalThis.__waitSend = waitForUsableSendButton;\n})();");
  vm.runInNewContext(instrumented, context);

  const waiting = context.globalThis.__waitSend(5);
  mutation();
  await assert.rejects(waiting, (error) => error.code === "SEND_BUTTON_DISABLED" && error.diagnostics.buttonCount === 1 && error.diagnostics.buttons[0].reason === "disabled");

  disabledSend.disabled = false;
  ariaDisabled = "true";
  const ariaWaiting = context.globalThis.__waitSend(5);
  mutation();
  await assert.rejects(ariaWaiting, (error) => error.code === "SEND_BUTTON_DISABLED" && error.diagnostics.buttons[0].reason === "aria_disabled");

  ariaDisabled = null;
  const becomesReady = context.globalThis.__waitSend(100);
  mutation();
  assert.equal(await becomesReady, disabledSend);
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

function loadServiceWorker({ runtimeGranted = true, contentScriptReady = false, contentScriptVersion = "1.0.45", newTabScriptReady = true, backendError = null, legacyStatusTimeout = false, queueRequestHangs = false, tabUrl = "https://gemini.google.com/app", tabStatus = "complete", manifestDeclared = true, permissionError = null, activeGeminiTabId = null } = {}) {
  let newTab;
  let createdTabs = 0;
  const apiRequests = [];
  const storageState = queueRequestHangs ? {} : { appTabId: 2 };
  const geminiTab = { id: 1, url: tabUrl, status: tabStatus };
  geminiTab.active = activeGeminiTabId === geminiTab.id;
  const otherActiveGeminiTab = activeGeminiTabId && activeGeminiTabId !== geminiTab.id
    ? { id: activeGeminiTabId, url: "https://gemini.google.com/app", status: "complete", active: true }
    : null;
  const activatedTabIds = [];
  const appTab = { id: 2, url: "https://leotruyen.vercel.app/truyen/test" };
  const listeners = {};
  const chrome = {
    runtime: {
      id: "test-extension",
      getManifest: () => ({ version: "1.0.45", host_permissions: ["https://leotruyen.vercel.app/*", ...(manifestDeclared ? ["https://gemini.google.com/*"] : [])] }),
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
      query: async () => [geminiTab, ...(otherActiveGeminiTab ? [otherActiveGeminiTab] : []), ...(newTab ? [newTab] : [])],
      update: async (id, options) => { if (options?.active) activatedTabIds.push(id); return id === 1 ? geminiTab : otherActiveGeminiTab; },
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
        if (message.type === "ping") return (id === 3 ? newTabScriptReady : contentScriptReady) ? { ready: true, version: id === 3 ? "1.0.45" : contentScriptVersion } : undefined;
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
  vm.runInNewContext(`${serviceWorker}\nglobalThis.__test = { ensureGeminiTabReady, ensureGeminiTab, findGeminiTab, activateGeminiTab, healthCheck, pageRequest, jobStatusMessage };`, context);
  return { api: context.__test, geminiTab, listeners, apiRequests, storageState, activatedTabIds, get createdTabs() { return createdTabs; } };
}

test("Gemini jobs prefer the visible Gemini tab and focus the exact tab used for the job", async () => {
  const worker = loadServiceWorker({ contentScriptReady: true, activeGeminiTabId: 4 });
  worker.storageState.geminiTabId = 1;

  const selected = await worker.api.findGeminiTab();
  assert.equal(selected.id, 4);
  await worker.api.activateGeminiTab(selected.id);
  assert.deepEqual(worker.activatedTabIds, [4]);
  assert.equal(worker.storageState.geminiTabId, 4);
});

test("connecting the website tab does not wait for the remote queue status API", async () => {
  const worker = loadServiceWorker();
  const response = await new Promise((resolve) => {
    worker.listeners.message({ type: "connect-app", tab: { id: 2, url: "https://leotruyen.vercel.app/truyen/test" } }, {}, resolve);
  });

  assert.equal(response.ok, true);
  assert.equal(worker.storageState.appTabId, 2);
  assert.ok(!worker.apiRequests.includes("/api/gemini-extension/status"));
});

test("Companion caps progress and pause messages below the backend's 1000-character limit", () => {
  const worker = loadServiceWorker();
  const message = worker.api.jobStatusMessage(`${"x".repeat(1200)}${"🙂".repeat(200)}`);

  assert.equal(message.length, 900);
  assert.ok(Array.from(message).length <= 900);
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

test("a Gemini tab with stale Companion code is replaced with a current tab", async () => {
  const worker = loadServiceWorker({ contentScriptReady: true, contentScriptVersion: "1.0.31" });

  const result = await worker.api.ensureGeminiTab(6000);

  assert.equal(result.tab.id, 3);
  assert.equal(worker.createdTabs, 1);
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
