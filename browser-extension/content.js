(() => {
if (globalThis.__leoGeminiCompanionLoaded) return;
globalThis.__leoGeminiCompanionLoaded = true;
const CONTENT_SCRIPT_VERSION = "1.0.42";
const readBody = () => (document.body?.innerText || "").toLowerCase();
const buttonText = (button) => [
  button.getAttribute("aria-label"), button.innerText, button.title,
  button.value,
  button.getAttribute("data-tooltip"), button.getAttribute("mattooltip"),
  button.getAttribute("data-mat-icon-name"), button.querySelector("mat-icon")?.textContent,
  ...[...button.querySelectorAll("mat-icon, svg, [data-mat-icon-name]")].flatMap((icon) => [
    icon.getAttribute("data-mat-icon-name"), icon.getAttribute("aria-label"), icon.getAttribute("data-icon"), icon.textContent,
  ]),
  button.querySelector("svg")?.getAttribute("aria-label"),
].filter(Boolean).join(" ").toLowerCase();
const visible = (node) => node?.getClientRects().length > 0;
const MAX_CHAPTERS_PER_CONVERSATION = 10;
const PRIOR_GENERATION_TIMEOUT_MS = 10000;
let activeJobSubmissions = 0;
let conversationStatsKey = null;
let conversationInstructionsSent = false;
const GeminiSelectors = {
  composer: () => {
    const editors = [...document.querySelectorAll('[role="textbox"], textarea, [contenteditable="true"]')].filter(visible);
    const active = document.activeElement;
    return editors.filter((node) => node === active || node.contains?.(active)).at(-1) || editors.at(-1);
  },
  buttonElements: () => [...document.querySelectorAll('button, [role="button"], input[type="submit"]')],
  buttons: () => GeminiSelectors.buttonElements().filter(visible),
  newChat: () => {
    const buttons = GeminiSelectors.buttons();
    return buttons.find((node) => /new (chat|conversation)|start (a )?(new )?chat|create (a )?(new )?(chat|conversation)|cuộc trò chuyện mới|tạo (cuộc trò chuyện|đoạn chat) mới/i.test(buttonText(node)))
      || buttons.find((node) => node.closest("nav, header") && /\b(edit_square|chat_add_on|add_2)\b/.test(buttonText(node)));
  },
  send: () => inspectSendButton().button,
  responses: () => {
    for (const selector of ["message-content", "model-response", '[data-message-author-role="model"]']) {
      const nodes = [...document.querySelectorAll(selector)].filter((node) => node.innerText?.trim());
      if (nodes.length) return nodes;
    }
    return [];
  },
  userMessages: () => {
    for (const selector of ["user-query", '[data-message-author-role="user"]', '[data-testid="user-query"]']) {
      const nodes = [...document.querySelectorAll(selector)].filter((node) => node.innerText?.trim());
      if (nodes.length) return nodes;
    }
    return [];
  },
  stop: () => GeminiSelectors.buttons().some((node) => /(?:^|\s)(?:stop(?:_response|_generating)?|dừng(?: phản hồi| tạo)?|ngừng(?: phản hồi| tạo)?)(?:$|\s)/i.test(buttonText(node)) && !/sharing|chia sẻ|recording|ghi âm/i.test(buttonText(node))),
  sendCandidates: () => inspectSendButton().diagnostics.buttons,
};

function inspectSendButton() {
  const box = GeminiSelectors.composer();
  const boxRect = box?.getBoundingClientRect?.();
  const diagnostics = { composer: describeComposer(box), scope: [], buttonCount: 0, buttons: [] };
  if (!boxRect) return { button: null, reason: "not_found", diagnostics };
  const allButtons = GeminiSelectors.buttonElements();
  const isExcluded = (node) => /feedback|stop|dừng|ngừng|microphone|mic|attach|upload|menu|share|voice|model|flash|thinking|reasoning/i.test(buttonText(node));
  let root = box.parentElement;
  for (let depth = 0; root && depth < 6; depth += 1, root = root.parentElement) {
    const scoped = allButtons.filter((node) => root.contains?.(node));
    const inspected = scoped.map((node) => {
      const rect = node.getBoundingClientRect?.();
      const verticalGap = rect ? Math.max(0, rect.top - boxRect.bottom, boxRect.top - rect.bottom) : Infinity;
      const isVisible = visible(node);
      const nearby = isVisible && !!rect && verticalGap <= 48;
      const label = buttonText(node);
      const testId = [node.getAttribute("data-testid"), node.getAttribute("data-test-id"), node.getAttribute("data-tooltip")].filter(Boolean).join(" ").toLowerCase();
      const semanticSend = /\b(send(?:_up)?|submit|gửi)\b|arrow_upward/i.test(label) || /send|submit/i.test(testId);
      const disabled = !!node.disabled || node.hasAttribute?.("disabled") || node.getAttribute("aria-disabled") === "true";
      const excluded = isExcluded(node);
      const reason = !isVisible ? "not_visible" : disabled ? (node.getAttribute("aria-disabled") === "true" ? "aria_disabled" : "disabled")
        : excluded ? "unrelated_control" : !nearby ? "outside_composer_region" : semanticSend ? "eligible" : "no_send_semantics";
      return {
        node,
        summary: {
          tagName: node.tagName || "unknown", role: node.getAttribute("role"),
          ariaLabel: node.getAttribute("aria-label"), title: node.getAttribute("title"),
          testId: node.getAttribute("data-testid") || node.getAttribute("data-test-id"),
          disabled: !!node.disabled || !!node.hasAttribute?.("disabled"), ariaDisabled: node.getAttribute("aria-disabled"),
          visible: isVisible, nearby, semanticSend, reason,
        },
        nearby, semanticSend, disabled, excluded,
      };
    });
    const nearby = inspected.filter((candidate) => candidate.nearby);
    if (nearby.length) {
      diagnostics.scope = [];
      for (let node = box, count = 0; node && count < 5; node = node.parentElement, count += 1) {
        diagnostics.scope.push({ tagName: node.tagName || "unknown", role: node.getAttribute?.("role"), testId: node.getAttribute?.("data-testid") || node.getAttribute?.("data-test-id") });
        if (node === root) break;
      }
      diagnostics.buttonCount = nearby.length;
      diagnostics.buttons = nearby.slice(0, 12).map((candidate) => candidate.summary);
      const semantic = nearby.filter((candidate) => candidate.semanticSend && !candidate.excluded);
      const enabled = semantic.find((candidate) => !candidate.disabled);
      if (enabled) return { button: enabled.node, reason: null, diagnostics };
      return { button: null, reason: semantic.some((candidate) => candidate.disabled) ? "disabled" : "unknown", diagnostics };
    }
    if (!diagnostics.buttons.length) {
      diagnostics.buttonCount = inspected.length;
      diagnostics.buttons = inspected.slice(0, 12).map((candidate) => candidate.summary);
    }
  }
  return { button: null, reason: diagnostics.buttons.length ? "unknown" : "not_found", diagnostics };
}

function describeComposer(node) {
  if (!node) return null;
  return {
    tag: node.tagName || "unknown",
    role: node.getAttribute("role"),
    type: node.getAttribute("type"),
    contentEditable: node.getAttribute("contenteditable"),
    isContentEditable: !!node.isContentEditable,
    ariaLabel: node.getAttribute("aria-label"),
    visible: visible(node),
    connected: node.isConnected !== false,
  };
}

function describeSendButton(node) {
  return node ? {
    tag: node.tagName || "unknown",
    role: node.getAttribute("role"),
    ariaLabel: node.getAttribute("aria-label"),
    title: node.getAttribute("title"),
    testId: node.getAttribute("data-testid") || node.getAttribute("data-test-id"),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute("aria-disabled"),
    visible: visible(node),
    connected: node.isConnected !== false,
  } : null;
}

function log(stage, data = {}) {
  const entry = { code: "INFO", message: "", tabId: null, url: location.href, timestamp: new Date().toISOString(), ...data };
  console.info(`[GEMINI_COMPANION][${stage}]`, JSON.stringify(entry));
  chrome.runtime.sendMessage({ type: "companion-log", stage, data: entry }).catch(() => {});
}

chrome.runtime.sendMessage({ type: "gemini-ready" }).catch(() => {});

function classifyPage() {
  const body = readBody().slice(-2000);
  if (/captcha|verify it's you|xác minh bạn|verification/.test(body)) return { code: "MANUAL_ACTION_REQUIRED", reason: "manual", message: "Gemini yêu cầu xác minh/CAPTCHA. Hãy xử lý trong tab Gemini rồi resume." };
  if (/sign in|đăng nhập/.test(body) && !GeminiSelectors.composer()) return { code: "GEMINI_LOGIN_REQUIRED", reason: "manual", message: "Gemini chưa đăng nhập. Hãy đăng nhập trong tab Gemini rồi resume." };
  if (/usage limit|you've reached your limit|đã đạt giới hạn|vượt quá giới hạn|try again later/.test(body)) return { code: "QUOTA_PAUSED", reason: "quota", message: "Gemini báo giới hạn sử dụng. Job đã pause để bạn resume sau." };
  if (/prompt is too long|input is too long|message is too long|exceeds (the )?(context|token) limit/.test(body)) return { code: "PROMPT_TOO_LONG", reason: "too_long", message: "Chương vượt giới hạn một lần gửi; sẽ thử dịch theo đoạn." };
  return null;
}

function composer() {
  return GeminiSelectors.composer();
}

function responseNodes() {
  return GeminiSelectors.responses();
}

function inspectResponseDom() {
  const selectors = ["message-content", "model-response", '[data-message-author-role="model"]'];
  const describe = (node) => ({
    tag: node.tagName || "unknown",
    role: node.getAttribute?.("role") || null,
    testId: node.getAttribute?.("data-testid") || node.getAttribute?.("data-test-id") || null,
    authorRole: node.getAttribute?.("data-message-author-role") || null,
    ariaLabel: node.getAttribute?.("aria-label") || null,
    textChars: String(node.innerText || "").trim().length,
    visible: visible(node),
    childTags: [...(node.children || [])].slice(0, 8).map((child) => child.tagName || "unknown"),
  });
  const bySelector = selectors.map((selector) => {
    try {
      const nodes = [...document.querySelectorAll(selector)];
      return { selector, count: nodes.length, nonEmpty: nodes.filter((node) => node.innerText?.trim()).length, last: nodes.length ? describe(nodes.at(-1)) : null };
    } catch {
      return { selector, count: 0, nonEmpty: 0, last: null };
    }
  });
  let semanticCandidates = [];
  try {
    semanticCandidates = [...document.querySelectorAll("[data-testid], [data-test-id], [data-message-author-role], [role]")]
      .filter((node) => /model|response|message|turn|answer/i.test([
        node.getAttribute("data-testid"), node.getAttribute("data-test-id"),
        node.getAttribute("data-message-author-role"), node.getAttribute("role"),
      ].filter(Boolean).join(" ")))
      .slice(-12)
      .map(describe);
  } catch {}
  return { bySelector, semanticCandidateCount: semanticCandidates.length, semanticCandidates };
}

function stopVisible() {
  return GeminiSelectors.stop();
}

function waitForGenerationIdle(timeoutMs = PRIOR_GENERATION_TIMEOUT_MS, onProgress = () => {}) {
  if (!stopVisible()) return Promise.resolve();
  onProgress("Gemini còn đang tạo phản hồi trước. Đang chờ tối đa 10 giây; nếu chưa xong sẽ chuyển sang tab mới.");
  return new Promise((resolve, reject) => {
    let timer;
    const observer = new MutationObserver(check);
    const finish = (error) => {
      clearTimeout(timer);
      observer.disconnect();
      if (error) reject(error); else resolve();
    };
    function check() {
      if (!stopVisible()) finish();
    }
    observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
    timer = setTimeout(() => finish(Object.assign(
      new Error("Gemini vẫn đang tạo phản hồi trước đó. Hãy chờ phản hồi hoàn tất rồi tiếp tục job."),
      { code: "GENERATION_IN_PROGRESS", reason: "transient" },
    )), timeoutMs);
    check();
  });
}

async function startNewConversation(tabId) {
  log("CHAT_STATE", { code: "BEFORE_SEND", url: location.href, userMessages: GeminiSelectors.userMessages().length, responses: responseNodes().length });
  const key = `geminiConversationStats:${tabId}`;
  const stored = await chrome.storage.local.get(key);
  const stats = stored[key] || { chapters: 0 };
  conversationStatsKey = key;
  const hasConversationHistory = GeminiSelectors.userMessages().length > 0 || responseNodes().length > 0;
  if (!hasConversationHistory) {
    conversationInstructionsSent = false;
    await chrome.storage.local.set({ [key]: { chapters: 0, instructionSent: false } });
    return;
  }
  conversationInstructionsSent = stats.instructionSent ?? Number(stats.chapters || 0) > 0;
  if (stats.chapters < MAX_CHAPTERS_PER_CONVERSATION) return;
  const button = GeminiSelectors.newChat();
  if (!button) throw Object.assign(new Error("Không tìm thấy nút tạo cuộc trò chuyện mới trong Gemini."), { code: "CHAT_INITIALIZATION_FAILED", reason: "manual" });
  log("CHAT_INITIALIZATION", { code: "NEW_CHAT_BUTTON_FOUND", button: describeSendButton(button), url: location.href });
  button.click();
  await new Promise((resolve, reject) => {
    const deadline = setTimeout(() => finish(Object.assign(new Error("Gemini không xác nhận được trạng thái cuộc trò chuyện mới sau khi bấm Tạo chat."), { code: "CHAT_INITIALIZATION_FAILED", reason: "manual" })), 10000);
    const observer = new MutationObserver(() => {
      if (composer() && !GeminiSelectors.userMessages().length && !responseNodes().length) {
        log("CHAT_INITIALIZATION", { code: "EMPTY_CHAT_DOM_READY", url: location.href });
        finish();
      }
    });
    const finish = (error) => {
      clearTimeout(deadline);
      observer.disconnect();
      if (error) reject(error); else resolve();
    };
    observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
    if (composer() && !GeminiSelectors.userMessages().length && !responseNodes().length) finish();
  });
  conversationInstructionsSent = false;
  await chrome.storage.local.set({ [key]: { chapters: 0, instructionSent: false } });
}

async function recordConversationChapter(tabId) {
  const key = `geminiConversationStats:${tabId}`;
  const stored = await chrome.storage.local.get(key);
  const stats = stored[key] || { chapters: 0 };
  await chrome.storage.local.set({ [key]: { ...stats, chapters: stats.chapters + 1, instructionSent: conversationInstructionsSent } });
}

function translationPrompt(promptSnapshot, text) {
  return conversationInstructionsSent ? String(text) : promptSnapshot.replace("{{text}}", text);
}

async function markConversationInstructionsSent() {
  conversationInstructionsSent = true;
  if (!conversationStatsKey) return;
  const stored = await chrome.storage.local.get(conversationStatsKey);
  const stats = stored[conversationStatsKey] || { chapters: 0 };
  if (stats.instructionSent) return;
  await chrome.storage.local.set({ [conversationStatsKey]: { ...stats, instructionSent: true } });
}

async function submitTranslationText(promptSnapshot, text, onProgress = () => {}) {
  const prompt = translationPrompt(promptSnapshot, text);
  const result = await submitPrompt(prompt, onProgress);
  await markConversationInstructionsSent().catch((error) => log("CHAT_STATE", { code: "INSTRUCTION_STATE_SAVE_FAILED", message: String(error?.message || error), url: location.href }));
  return result;
}

function waitForUsableSendButton(timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    let timer;
    const observer = new MutationObserver(check);
    const finish = (button, error) => {
      clearTimeout(timer);
      observer.disconnect();
      if (error) reject(error); else resolve(button);
    };
    function check() {
      const button = GeminiSelectors.send();
      if (button && !button.disabled && button.getAttribute("aria-disabled") !== "true") finish(button);
    }
    observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
    timer = setTimeout(() => {
      const state = inspectSendButton();
      const code = state.reason === "disabled" ? "SEND_BUTTON_DISABLED" : state.reason === "not_found" ? "SEND_BUTTON_NOT_FOUND" : "SEND_STATE_UNKNOWN";
      const message = code === "SEND_BUTTON_DISABLED" ? "Nút gửi trong vùng composer đang bị Gemini khóa sau khi điền prompt."
        : code === "SEND_BUTTON_NOT_FOUND" ? "Không tìm thấy nút gửi trong vùng composer Gemini."
          : "Có nút gần composer nhưng không xác định được nút gửi khả dụng.";
      finish(null, Object.assign(new Error(message), { code, reason: "manual", diagnostics: state.diagnostics }));
    }, timeoutMs);
    check();
  });
}

function composerText(box) {
  return String(box.value ?? box.innerText ?? box.textContent ?? "");
}

function composerTextMatches(box, prompt) {
  const normalize = (value) => String(value)
    .replace(/[\u200b\u200c\u200d\ufeff]/g, "")
    .replace(/\u00a0/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return normalize(composerText(box)) === normalize(prompt);
}

function promptWasAccepted({ composerCleared, generating, responseAdded, userMessageMatches }) {
  // Clearing the editor is only a UI change. Confirm submission from a matching
  // user turn, a new model response, or Gemini's active-generation state.
  return !!(generating || responseAdded || userMessageMatches);
}

function waitForComposerText(box, prompt, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    let timer;
    const observer = new MutationObserver(check);
    const finish = (error) => {
      clearTimeout(timer);
      observer.disconnect();
      box.removeEventListener("input", check);
      if (error) reject(error); else resolve();
    };
    function check() {
      if (composerTextMatches(box, prompt)) finish();
    }
    observer.observe(box, { subtree: true, childList: true, characterData: true, attributes: true });
    box.addEventListener("input", check);
    timer = setTimeout(() => finish(Object.assign(
      new Error("Gemini không nhận đủ nội dung prompt; chưa gửi để tránh dịch thiếu."),
      { code: "PROMPT_INPUT_MISMATCH", reason: "transient" },
    )), timeoutMs);
    check();
  });
}

function writePromptToComposer(box, prompt) {
  box.focus();
  if (box instanceof HTMLTextAreaElement || box instanceof HTMLInputElement) {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(box), "value")?.set;
    setter?.call(box, prompt);
    box.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: prompt }));
    return;
  }
  if (!box.isContentEditable && box.getAttribute("contenteditable") !== "true") {
    throw Object.assign(new Error("Ô nhập Gemini được tìm thấy nhưng không phải editor có thể nhập."), { code: "PROMPT_INPUT_FAILED", reason: "ui_changed" });
  }
  const range = document.createRange();
  range.selectNodeContents(box);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  const inserted = document.execCommand("insertText", false, prompt);
  if (!inserted && !composerTextMatches(box, prompt)) throw Object.assign(new Error("Gemini từ chối chèn nội dung vào editor."), { code: "PROMPT_INPUT_FAILED", reason: "ui_changed" });
  // Some Gemini composer builds update their send-button state only after an input event.
  box.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: prompt }));
}

async function submitPrompt(prompt, onProgress = () => {}) {
  const blocked = classifyPage();
  if (blocked) throw Object.assign(new Error(blocked.message), blocked);
  const box = composer();
  if (!box) throw Object.assign(new Error("Không nhận diện được ô nhập Gemini; có thể UI đã thay đổi."), { code: "GEMINI_COMPOSER_NOT_FOUND", reason: "ui_changed" });
  log("COMPOSER_FOUND", { code: "FOUND", composer: describeComposer(box), url: location.href });
  await waitForGenerationIdle(undefined, onProgress);
  const before = responseNodes().length;
  const previousUserMessages = GeminiSelectors.userMessages();
  const previousOutput = responseNodes().at(-1)?.innerText?.trim() || "";
  try {
    writePromptToComposer(box, prompt);
    await waitForComposerText(box, prompt);
  } catch (error) {
    log("PROMPT_FILL_FAILED", { code: error.code || "PROMPT_INPUT_FAILED", composer: describeComposer(box), chars: prompt.length, url: location.href });
    error.code = "PROMPT_INPUT_FAILED";
    error.reason = error.reason || "ui_changed";
    throw error;
  }
  log("PROMPT_FILLED", { code: "DOM_MATCH", composer: describeComposer(box), chars: prompt.length, url: location.href });
  onProgress("Đã điền đủ nội dung vào ô nhập Gemini; đang tìm nút Gửi khả dụng.");
  let send;
  try { send = await waitForUsableSendButton(10000); }
  catch (error) {
    const details = error.diagnostics || inspectSendButton().diagnostics;
    log("SEND_BUTTON_NOT_READY", { code: error.code || "SEND_STATE_UNKNOWN", ...details, url: location.href });
    throw Object.assign(new Error(`${error.message} Chẩn đoán giao diện (không gồm nội dung prompt): ${JSON.stringify(details).slice(0, 1200)}`), { code: error.code || "SEND_STATE_UNKNOWN", reason: "manual" });
  }
  log("SEND_BUTTON_READY", { code: "READY", button: describeSendButton(send), url: location.href });
  const currentComposer = composer();
  if (!currentComposer || !composerTextMatches(currentComposer, prompt)) {
    throw Object.assign(new Error("Prompt không còn nguyên trong ô Gemini ngay trước khi gửi; đã dừng để tránh gửi thiếu."), { code: "PROMPT_INPUT_MISMATCH", reason: "manual" });
  }

  return new Promise((resolve, reject) => {
    let sawGenerating = false;
    let sawAccepted = false;
    let sawStopButton = false;
    let reportedAccepted = false;
    let reportedGenerating = false;
    let reportedComposerCleared = false;
    let reportedResponseDetected = false;
    let quietTimer;
    let readTimer;
    let startTimer;
    const deadline = setTimeout(() => finish(Object.assign(new Error("Gemini đã nhận prompt nhưng không hoàn tất phản hồi trong thời hạn chờ."), { code: "RESPONSE_TIMEOUT", reason: "manual" })), 240000);
    const finishStable = (output) => {
      if (output === previousOutput) return finish(Object.assign(new Error("Gemini trả lại đúng phản hồi cũ; job đã pause để tránh lưu trùng."), { code: "DUPLICATE_RESPONSE", reason: "manual" }));
      log("RESPONSE", { code: "EXTRACTED", chars: output.length, url: location.href });
      log("RESPONSE_COMPLETED", { code: "COMPLETE", chars: output.length, url: location.href });
      onProgress(`Gemini đã trả phản hồi (${output.length} ký tự); đang kiểm tra nội dung.`);
      finish(null, output);
    };
    const observer = new MutationObserver(() => {
      const blockedNow = classifyPage();
      if (blockedNow) return finish(Object.assign(new Error(blockedNow.message), blockedNow));
      const nodes = responseNodes();
      const output = nodes.at(-1)?.innerText?.trim() || "";
      const generating = stopVisible();
      const composerNode = composer();
      const composerCleared = !!composerNode && !composerText(composerNode).trim();
      const userMessages = GeminiSelectors.userMessages();
      const newUserMessage = userMessages.find((node) => !previousUserMessages.includes(node));
      const userMessageMatches = !!newUserMessage && composerTextMatches(newUserMessage, prompt);
      if (composerCleared && !reportedComposerCleared) {
        reportedComposerCleared = true;
        log("PROMPT_CLEARED", { code: "EDITOR_EMPTY", url: location.href });
      }
      const accepted = promptWasAccepted({ composerCleared, generating, responseAdded: nodes.length > before, userMessageMatches });
      if (accepted) {
        sawAccepted = true;
        markConversationInstructionsSent().catch((error) => log("CHAT_STATE", { code: "INSTRUCTION_STATE_SAVE_FAILED", message: String(error?.message || error), url: location.href }));
        clearTimeout(startTimer);
        if (!reportedAccepted) {
          reportedAccepted = true;
          log("PROMPT_SUBMITTED_CONFIRMED", { code: "CONFIRMED", evidence: userMessageMatches ? "matching_user_turn" : generating ? "generation_active" : "new_response", url: location.href });
          onProgress("Gemini đã nhận prompt; đang chờ bắt đầu dịch.");
        }
      }
      if (generating || nodes.length > before) {
        sawGenerating = true;
        if (!reportedGenerating) {
          reportedGenerating = true;
          log("RESPONSE_STARTED", { code: generating ? "GENERATION_ACTIVE" : "NEW_RESPONSE_NODE", url: location.href });
          onProgress("Gemini đã bắt đầu tạo phản hồi.");
        }
      }
      if (generating) sawStopButton = true;
      const composerIdle = !!composerNode && !composerNode.disabled && composerNode.getAttribute("aria-disabled") !== "true";
      if (output && nodes.length > before && !stopVisible() && composerIdle) {
        if (!reportedResponseDetected) {
          reportedResponseDetected = true;
          log("RESPONSE_DETECTED", { code: "NEW_RESPONSE_TEXT", chars: output.length, url: location.href });
        }
        clearTimeout(readTimer);
        clearTimeout(quietTimer);
        quietTimer = setTimeout(() => finishStable(output), 1200);
      } else if (sawGenerating && sawStopButton && !generating && composerIdle) {
        clearTimeout(readTimer);
        readTimer = setTimeout(() => {
          const finalNodes = responseNodes();
          const finalOutput = finalNodes.at(-1)?.innerText?.trim() || "";
          if (finalNodes.length > before && finalOutput) {
            if (!reportedResponseDetected) log("RESPONSE_DETECTED", { code: "FINAL_DOM_READ", chars: finalOutput.length, url: location.href });
            return finishStable(finalOutput);
          }
          const diagnostics = inspectResponseDom();
          log("RESPONSE_READ_FAILED", { code: "GEMINI_RESPONSE_READ_FAILED", priorResponseCount: before, responseCount: finalNodes.length, generating: stopVisible(), diagnostics, url: location.href });
          finish(Object.assign(new Error(`Gemini đã hoàn tất nhưng không đọc được nội dung phản hồi. Chẩn đoán DOM (không gồm nội dung phản hồi): ${JSON.stringify({ priorResponseCount: before, responseCount: finalNodes.length, diagnostics }).slice(0, 1800)}`), { code: "GEMINI_RESPONSE_READ_FAILED", reason: "manual" }));
        }, 5000);
      } else {
        clearTimeout(readTimer);
      }
    });
    const finish = (error, output) => {
      clearTimeout(deadline); clearTimeout(quietTimer); clearTimeout(readTimer); clearTimeout(startTimer); observer.disconnect();
      if (error) reject(error); else resolve(output);
    };
    observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
    startTimer = setTimeout(() => {
      if (!sawAccepted) {
        const currentComposer = composer();
        const retained = !!currentComposer && composerTextMatches(currentComposer, prompt);
        const details = {
          code: "PROMPT_NOT_ACCEPTED", method: "button.click", composer: describeComposer(currentComposer),
          composerChars: composerText(currentComposer || {}).length, retained,
          sendButton: describeSendButton(send), sendConnected: send.isConnected !== false,
          userMessageCount: GeminiSelectors.userMessages().length, responseCount: responseNodes().length,
          generating: stopVisible(), url: location.href,
        };
        log("SUBMIT_NOT_ACCEPTED", details);
        const reason = retained
          ? "Gemini giữ prompt trong ô nhập nhưng chưa thấy tin nhắn mới hoặc trạng thái tạo phản hồi sau khi bấm nút Gửi. Không tự gửi lại để tránh trùng prompt."
          : "Gemini chưa tạo tin nhắn/cuộc trò chuyện sau khi gửi; prompt không còn trong ô nhập. Hãy kiểm tra tab Gemini trước khi thử lại.";
        finish(Object.assign(new Error(`${reason} Chẩn đoán nút gửi: ${JSON.stringify({ method: details.method, composer: details.composer, composerChars: details.composerChars, retained: details.retained, sendButton: details.sendButton, sendConnected: details.sendConnected, userMessageCount: details.userMessageCount, responseCount: details.responseCount, generating: details.generating })}`), { code: "SEND_NOT_CONFIRMED", reason: "manual" }));
      }
    }, 15000);
    try { send.click(); }
    catch (error) {
      finish(Object.assign(new Error(`Không kích hoạt được nút Gửi Gemini: ${String(error?.message || error)}`), { code: "SEND_TRIGGER_FAILED", reason: "manual" }));
      return;
    }
    activeJobSubmissions += 1;
    log("SEND_TRIGGERED", { code: "CLICK_DISPATCHED", button: describeSendButton(send), chars: prompt.length, url: location.href });
    onProgress(`Đã bấm nút Gửi (${prompt.length} ký tự); đang chờ xác nhận tin nhắn trong Gemini.`);
  });
}

function cleanResult(value) {
  return value.replace(/^```(?:text|markdown)?\s*/i, "").replace(/\s*```$/, "").trim();
}

function translationIssue(source, output, prompt) {
  if (!output.trim()) return "empty";
  if (/\[(?:response )?truncated\]|output was cut off|response was truncated|输出已截断/i.test(output)) return "truncated";
  if (output === source || (source.length >= 100 && output.includes(source.slice(0, 100)))) return "source_echo";
  const prefix = prompt.split("{{text}}", 1)[0].trim();
  if (prefix.length >= 40 && output.startsWith(prefix.slice(0, 100))) return "prompt_echo";
  if (source.length >= 80 && output.length < Math.min(500, Math.max(30, Math.floor(source.length * 0.08)))) return "too_short";
  const paragraphs = output.split(/\n\s*\n/).map((part) => part.replace(/\s/g, "").toLowerCase()).filter(Boolean);
  if (paragraphs.length >= 4 && new Set(paragraphs).size <= Math.floor(paragraphs.length / 2)) return "duplicate_output";
  return null;
}

function fallbackChunks(text, maxSize = 1800) {
  const paragraphs = text.replace(/\r\n?/g, "\n").split(/\n\s*\n/).map((part) => part.trim()).filter(Boolean);
  const chunks = [];
  const joiners = [];
  let current = "";
  paragraphs.forEach((paragraph, paragraphIndex) => {
    const sentences = paragraph.match(/[^。！？!?；;]+[。！？!?；;]?/gu) || [paragraph];
    sentences.forEach((sentence, sentenceIndex) => {
      const separator = sentenceIndex === 0 && paragraphIndex > 0 ? "\n\n" : "";
      const candidate = current + separator + sentence;
      if (current && candidate.length > maxSize) {
        chunks.push(current);
        joiners.push(separator || " ");
        current = sentence;
      } else current = candidate;
    });
  });
  if (current) chunks.push(current);
  return { chunks, joiners };
}

async function translateWholeChapter(job, onProgress = () => {}) {
  let whole = "";
  let issue = null;
  try {
    onProgress(`Đang gửi toàn chương (${job.content.length} ký tự) lên Gemini.`, 0, 0);
    whole = cleanResult(await submitTranslationText(job.prompt_snapshot, job.content, onProgress));
    issue = translationIssue(job.content, whole, job.prompt_snapshot);
  } catch (error) {
    if (error.reason !== "too_long") throw error;
    issue = "truncated";
  }
  if (!issue) {
    log("TRANSLATION_EXTRACTED", { code: "VALIDATED", chars: whole.length, url: location.href });
    return whole;
  }

  const { chunks, joiners } = fallbackChunks(job.content);
  if (chunks.length < 2) throw Object.assign(new Error(`Phản hồi Gemini không đạt kiểm tra (${issue}); không thể chia chương an toàn.`), { reason: "manual" });
  onProgress(`Phản hồi toàn chương chưa đạt kiểm tra; chuyển sang ${chunks.length} đoạn.`, 0, chunks.length);
  const translated = [];
  for (const [index, chunk] of chunks.entries()) {
    onProgress(`Đang gửi đoạn ${index + 1}/${chunks.length} (${chunk.length} ký tự) lên Gemini.`, index, chunks.length);
    const result = cleanResult(await submitTranslationText(job.prompt_snapshot, chunk, onProgress));
    const chunkIssue = translationIssue(chunk, result, job.prompt_snapshot);
    if (chunkIssue) throw Object.assign(new Error(`Gemini trả chunk không hợp lệ (${chunkIssue}); job đã pause để kiểm tra.`), { reason: "manual" });
    translated.push(result);
    onProgress(`Đã nhận và kiểm tra đoạn ${index + 1}/${chunks.length}.`, index + 1, chunks.length);
  }
  const result = translated.map((part, index) => index ? `${joiners[index - 1] || " "}${part}` : part).join("");
  const finalIssue = translationIssue(job.content, result, job.prompt_snapshot);
  if (finalIssue) throw Object.assign(new Error(`Bản ghép chunk không đạt kiểm tra (${finalIssue}).`), { reason: "manual" });
  log("TRANSLATION_EXTRACTED", { code: "VALIDATED_CHUNKS", chars: result.length, chunks: chunks.length, url: location.href });
  return result;
}

async function runTranslationJob(job, tabId, onProgress = () => {}) {
  activeJobSubmissions = 0;
  onProgress("Đang chuẩn bị cuộc trò chuyện Gemini.", 0, 0);
  await startNewConversation(tabId);
  const translatedContent = await translateWholeChapter(job, onProgress);
  onProgress("Đang dịch tiêu đề chương.", 0, 0);
  const translatedTitle = cleanResult(await submitTranslationText(job.prompt_snapshot, job.title, onProgress));
  if (!translatedTitle || translatedTitle === job.title) {
    throw Object.assign(new Error("Tên chương chưa được dịch; job đã pause để kiểm tra."), { reason: "manual" });
  }
  const translation = { translated_title: translatedTitle, translated_content: translatedContent };
  if (job.metadata) {
    onProgress("Đang dịch tên truyện.", 0, 0);
    translation.translated_novel_title = cleanResult(await submitTranslationText(job.prompt_snapshot, job.metadata.title, onProgress));
    if (!translation.translated_novel_title || translation.translated_novel_title === job.metadata.title) throw Object.assign(new Error("Tên truyện chưa được dịch."), { reason: "manual" });
    if (job.metadata.author) {
      onProgress("Đang dịch tên tác giả.", 0, 0);
      translation.translated_author = cleanResult(await submitTranslationText(job.prompt_snapshot, job.metadata.author, onProgress));
      if (!translation.translated_author || translation.translated_author === job.metadata.author) throw Object.assign(new Error("Tên tác giả chưa được dịch."), { reason: "manual" });
    }
    if (job.metadata.description) translation.translated_description = await translateWholeChapter({ content: job.metadata.description, prompt_snapshot: job.prompt_snapshot }, onProgress);
  }
  if (tabId != null) await recordConversationChapter(tabId);
  onProgress("Đã nhận đủ bản dịch từ Gemini; đang gửi về website để lưu.");
  return translation;
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "gemini-translation") return;
  let started = false;
  const heartbeat = setInterval(() => {
    try { port.postMessage({ type: "heartbeat" }); } catch { clearInterval(heartbeat); }
  }, 15000);
  port.onDisconnect.addListener(() => clearInterval(heartbeat));
  port.onMessage.addListener((message) => {
    if (started || message?.type !== "translate-chapter") return;
    started = true;
    const tabId = message.tabId ?? port.sender?.tab?.id;
    // Port messages keep the MV3 worker alive while Gemini handles long prompts.
    runTranslationJob(message.job, tabId, (text, currentChunk = 0, totalChunks = 0) => {
      try { port.postMessage({ type: "progress", message: text, currentChunk, totalChunks }); } catch {}
    }).then((translation) => {
      try { port.postMessage({ type: "result", result: { ok: true, translation } }); } catch {}
    }).catch((error) => {
      log("ERROR", { code: error.code || "TRANSLATION_ERROR", message: String(error.message || error), url: location.href });
      try { port.postMessage({ type: "result", result: { ok: false, code: error.code || "TRANSLATION_ERROR", reason: error.reason || "transient", message: String(error.message || error), submittedPrompts: activeJobSubmissions } }); } catch {}
    });
  });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "ping") {
    sendResponse({ ready: true, version: CONTENT_SCRIPT_VERSION });
    return;
  }
  if (message.type === "health-check") {
    const blocked = classifyPage();
    const checks = {
      loggedIn: blocked?.code !== "GEMINI_LOGIN_REQUIRED" && !!composer(),
      loginRequired: blocked?.code === "GEMINI_LOGIN_REQUIRED",
      composer: !!composer(),
      sendButton: !!GeminiSelectors.send(),
      generating: stopVisible(),
      newChat: !!GeminiSelectors.newChat(),
      responseContainer: !!document.querySelector("main, message-content, model-response, [data-message-author-role='model']"),
      quota: blocked?.code === "QUOTA_PAUSED",
      manualAction: blocked?.code === "MANUAL_ACTION_REQUIRED",
    };
    const firstFailure = blocked || (!checks.composer ? { code: "GEMINI_COMPOSER_NOT_FOUND", message: "Không tìm thấy ô nhập Gemini; chưa đủ dấu hiệu để kết luận tài khoản đã đăng xuất." }
      : !checks.responseContainer ? { code: "UI_CHANGED", message: "Không tìm thấy vùng phản hồi Gemini." } : null);
    const result = { ok: !firstFailure, code: firstFailure?.code || "READY", message: firstFailure?.message || "Gemini đã sẵn sàng.", checks };
    log("HEALTH", { code: result.code, checks, url: location.href });
    sendResponse(result);
    return;
  }
  if (message.type !== "translate-chapter") return;
  runTranslationJob(message.job, sender.tab?.id).then((translation) => sendResponse({ ok: true, translation })).catch((error) => {
    log("ERROR", { code: error.code || "TRANSLATION_ERROR", message: String(error.message || error), url: location.href });
    sendResponse({ ok: false, code: error.code || "TRANSLATION_ERROR", reason: error.reason || "transient", message: String(error.message || error), submittedPrompts: activeJobSubmissions });
  });
  return true;
});
})();
