(() => {
if (globalThis.__leoGeminiCompanionLoaded) return;
globalThis.__leoGeminiCompanionLoaded = true;
const readBody = () => (document.body?.innerText || "").toLowerCase();
const buttonText = (button) => [
  button.getAttribute("aria-label"), button.innerText, button.title,
  button.getAttribute("data-tooltip"), button.getAttribute("mattooltip"),
  button.getAttribute("data-mat-icon-name"), button.querySelector("mat-icon")?.textContent,
  ...[...button.querySelectorAll("mat-icon, svg, [data-mat-icon-name]")].flatMap((icon) => [
    icon.getAttribute("data-mat-icon-name"), icon.getAttribute("aria-label"), icon.getAttribute("data-icon"), icon.textContent,
  ]),
  button.querySelector("svg")?.getAttribute("aria-label"),
].filter(Boolean).join(" ").toLowerCase();
const visible = (node) => node?.getClientRects().length > 0;
const MAX_CHAPTERS_PER_CONVERSATION = 10;
let activeJobSubmissions = 0;
const GeminiSelectors = {
  composer: () => [...document.querySelectorAll('[role="textbox"], textarea, [contenteditable="true"]')].filter(visible).at(-1),
  buttons: () => [...document.querySelectorAll('button, [role="button"]')].filter(visible),
  newChat: () => {
    const buttons = GeminiSelectors.buttons();
    return buttons.find((node) => /new (chat|conversation)|start (a )?(new )?chat|create (a )?(new )?(chat|conversation)|cuộc trò chuyện mới|tạo (cuộc trò chuyện|đoạn chat) mới/i.test(buttonText(node)))
      || buttons.find((node) => node.closest("nav, header") && /\b(edit_square|chat_add_on|add_2)\b/.test(buttonText(node)));
  },
  send: () => GeminiSelectors.buttons().find((node) => {
    const label = buttonText(node);
    return /\b(send(?:_up)?|gửi)\b/i.test(label) && !/feedback|stop|dừng|ngừng/i.test(label);
  }),
  responses: () => {
    for (const selector of ["message-content", "model-response", '[data-message-author-role="model"]']) {
      const nodes = [...document.querySelectorAll(selector)].filter((node) => node.innerText?.trim());
      if (nodes.length) return nodes;
    }
    return [];
  },
  stop: () => GeminiSelectors.buttons().some((node) => /(?:^|\s)(?:stop(?:_response|_generating)?|dừng(?: phản hồi| tạo)?|ngừng(?: phản hồi| tạo)?)(?:$|\s)/i.test(buttonText(node)) && !/sharing|chia sẻ|recording|ghi âm/i.test(buttonText(node))),
};

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

function stopVisible() {
  return GeminiSelectors.stop();
}

async function startNewConversation(tabId) {
  const key = `geminiConversationStats:${tabId}`;
  const stored = await chrome.storage.local.get(key);
  const stats = stored[key] || { chapters: 0 };
  if (!responseNodes().length) {
    await chrome.storage.local.set({ [key]: { chapters: 0 } });
    return;
  }
  if (stats.chapters < MAX_CHAPTERS_PER_CONVERSATION) return;
  const button = GeminiSelectors.newChat();
  if (!button) throw Object.assign(new Error("Không tìm thấy nút tạo cuộc trò chuyện mới trong Gemini."), { code: "NEW_CHAT_SELECTOR_NOT_FOUND", reason: "ui_changed" });
  log("NEW_CHAT", { code: "CLICK", url: location.href });
  button.click();
  await new Promise((resolve, reject) => {
    const deadline = setTimeout(() => finish(new Error("Gemini không mở được cuộc trò chuyện mới.")), 10000);
    const observer = new MutationObserver(() => {
      if (composer() && !responseNodes().length) finish();
    });
    const finish = (error) => {
      clearTimeout(deadline);
      observer.disconnect();
      if (error) reject(error); else resolve();
    };
    observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
    if (composer() && !responseNodes().length) finish();
  });
  await chrome.storage.local.set({ [key]: { chapters: 0 } });
}

async function recordConversationChapter(tabId) {
  const key = `geminiConversationStats:${tabId}`;
  const stored = await chrome.storage.local.get(key);
  const stats = stored[key] || { chapters: 0 };
  await chrome.storage.local.set({ [key]: { chapters: stats.chapters + 1 } });
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
    timer = setTimeout(() => finish(null, Object.assign(
      new Error("Gemini chưa hiển thị nút Gửi khả dụng sau khi nhập prompt."),
      { code: "GEMINI_SEND_BUTTON_NOT_FOUND", reason: "ui_changed" },
    )), timeoutMs);
    check();
  });
}

async function submitPrompt(prompt) {
  const blocked = classifyPage();
  if (blocked) throw Object.assign(new Error(blocked.message), blocked);
  const box = composer();
  if (!box) throw Object.assign(new Error("Không nhận diện được ô nhập Gemini; có thể UI đã thay đổi."), { code: "GEMINI_COMPOSER_NOT_FOUND", reason: "ui_changed" });
  if (stopVisible()) throw Object.assign(new Error("Gemini vẫn đang tạo phản hồi trước đó."), { code: "GENERATION_IN_PROGRESS", reason: "transient" });
  const before = responseNodes().length;
  const previousOutput = responseNodes().at(-1)?.innerText?.trim() || "";
  box.focus();
  if (box instanceof HTMLTextAreaElement || box instanceof HTMLInputElement) {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(box), "value")?.set;
    setter?.call(box, prompt);
    box.dispatchEvent(new Event("input", { bubbles: true }));
  } else {
    box.replaceChildren(document.createTextNode(prompt));
    box.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: prompt }));
  }
  const entered = (box.value ?? box.innerText ?? box.textContent ?? "").trim();
  if (entered !== prompt.trim()) throw Object.assign(new Error("Gemini không nhận đủ nội dung prompt; không gửi để tránh dịch thiếu."), { code: "PROMPT_INPUT_MISMATCH", reason: "transient" });
  const send = await waitForUsableSendButton();

  return new Promise((resolve, reject) => {
    let sawGenerating = false;
    let sawStopButton = false;
    let quietTimer;
    let readTimer;
    let startTimer;
    const deadline = setTimeout(() => finish(Object.assign(new Error("Gemini không hoàn tất phản hồi trong thời hạn chờ."), { code: "GEMINI_RESPONSE_TIMEOUT", reason: "manual" })), 240000);
    const finishStable = (output) => {
      if (output === previousOutput) return finish(Object.assign(new Error("Gemini trả lại đúng phản hồi cũ; job đã pause để tránh lưu trùng."), { code: "DUPLICATE_RESPONSE", reason: "manual" }));
      log("RESPONSE", { code: "EXTRACTED", chars: output.length, url: location.href });
      finish(null, output);
    };
    const observer = new MutationObserver(() => {
      const blockedNow = classifyPage();
      if (blockedNow) return finish(Object.assign(new Error(blockedNow.message), blockedNow));
      const nodes = responseNodes();
      const output = nodes.at(-1)?.innerText?.trim() || "";
      const generating = stopVisible();
      if (generating || nodes.length > before) {
        sawGenerating = true;
        clearTimeout(startTimer);
      }
      if (generating) sawStopButton = true;
      const composerNode = composer();
      const composerIdle = !!composerNode && !composerNode.disabled && composerNode.getAttribute("aria-disabled") !== "true";
      if (output && nodes.length > before && !stopVisible() && composerIdle) {
        clearTimeout(readTimer);
        clearTimeout(quietTimer);
        quietTimer = setTimeout(() => finishStable(output), 1200);
      } else if (sawGenerating && sawStopButton && !generating && composerIdle) {
        clearTimeout(readTimer);
        readTimer = setTimeout(() => finish(Object.assign(new Error("Gemini đã hoàn tất nhưng không đọc được nội dung phản hồi."), { code: "GEMINI_RESPONSE_READ_FAILED", reason: "manual" })), 1500);
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
      if (!sawGenerating) finish(Object.assign(new Error("Gemini chưa bắt đầu tạo phản hồi sau khi gửi prompt."), { code: "GENERATION_NOT_STARTED", reason: "transient" }));
    }, 15000);
    log("SUBMIT", { code: "SENT", chars: prompt.length, url: location.href });
    activeJobSubmissions += 1;
    send.click();
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

async function translateWholeChapter(job) {
  let whole = "";
  let issue = null;
  try {
    whole = cleanResult(await submitPrompt(job.prompt_snapshot.replace("{{text}}", job.content)));
    issue = translationIssue(job.content, whole, job.prompt_snapshot);
  } catch (error) {
    if (error.reason !== "too_long") throw error;
    issue = "truncated";
  }
  if (!issue) return whole;

  const { chunks, joiners } = fallbackChunks(job.content);
  if (chunks.length < 2) throw Object.assign(new Error(`Phản hồi Gemini không đạt kiểm tra (${issue}); không thể chia chương an toàn.`), { reason: "manual" });
  const translated = [];
  for (const chunk of chunks) {
    const result = cleanResult(await submitPrompt(job.prompt_snapshot.replace("{{text}}", chunk)));
    const chunkIssue = translationIssue(chunk, result, job.prompt_snapshot);
    if (chunkIssue) throw Object.assign(new Error(`Gemini trả chunk không hợp lệ (${chunkIssue}); job đã pause để kiểm tra.`), { reason: "manual" });
    translated.push(result);
  }
  const result = translated.map((part, index) => index ? `${joiners[index - 1] || " "}${part}` : part).join("");
  const finalIssue = translationIssue(job.content, result, job.prompt_snapshot);
  if (finalIssue) throw Object.assign(new Error(`Bản ghép chunk không đạt kiểm tra (${finalIssue}).`), { reason: "manual" });
  return result;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "ping") {
    sendResponse({ ready: true });
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
  (async () => {
    const { job } = message;
    activeJobSubmissions = 0;
    const tabId = sender.tab?.id;
    await startNewConversation(tabId);
    const translatedContent = await translateWholeChapter(job);
    const titlePrompt = job.prompt_snapshot.replace("{{text}}", job.title);
    const translatedTitle = cleanResult(await submitPrompt(titlePrompt));
    if (!translatedTitle || translatedTitle === job.title) {
      throw Object.assign(new Error("Tên chương chưa được dịch; job đã pause để kiểm tra."), { reason: "manual" });
    }
    const translation = { translated_title: translatedTitle, translated_content: translatedContent };
    if (job.metadata) {
      translation.translated_novel_title = cleanResult(await submitPrompt(job.prompt_snapshot.replace("{{text}}", job.metadata.title)));
      if (!translation.translated_novel_title || translation.translated_novel_title === job.metadata.title) throw Object.assign(new Error("Tên truyện chưa được dịch."), { reason: "manual" });
      if (job.metadata.author) {
        translation.translated_author = cleanResult(await submitPrompt(job.prompt_snapshot.replace("{{text}}", job.metadata.author)));
        if (!translation.translated_author || translation.translated_author === job.metadata.author) throw Object.assign(new Error("Tên tác giả chưa được dịch."), { reason: "manual" });
      }
      if (job.metadata.description) translation.translated_description = await translateWholeChapter({ content: job.metadata.description, prompt_snapshot: job.prompt_snapshot });
    }
    if (tabId != null) await recordConversationChapter(tabId);
    sendResponse({ ok: true, translation });
  })().catch((error) => {
    log("ERROR", { code: error.code || "TRANSLATION_ERROR", message: String(error.message || error), url: location.href });
    sendResponse({ ok: false, code: error.code || "TRANSLATION_ERROR", reason: error.reason || "transient", message: String(error.message || error), submittedPrompts: activeJobSubmissions });
  });
  return true;
});
})();
