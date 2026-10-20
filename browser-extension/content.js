const readBody = () => (document.body?.innerText || "").toLowerCase();
const buttonText = (button) => `${button.getAttribute("aria-label") || ""} ${button.innerText || ""} ${button.title || ""}`.toLowerCase();

chrome.runtime.sendMessage({ type: "gemini-ready" }).catch(() => {});

function classifyPage() {
  const body = readBody();
  if (/captcha|verify it's you|xác minh bạn|verification/.test(body)) return { reason: "manual", message: "Gemini yêu cầu xác minh/CAPTCHA. Hãy xử lý trong tab Gemini rồi resume." };
  if (/sign in|đăng nhập/.test(body) && !document.querySelector('[role="textbox"], textarea, [contenteditable="true"]')) return { reason: "manual", message: "Gemini chưa đăng nhập. Hãy đăng nhập trong tab Gemini rồi resume." };
  if (/usage limit|you've reached your limit|đã đạt giới hạn|vượt quá giới hạn|try again later/.test(body)) return { reason: "quota", message: "Gemini báo giới hạn sử dụng. Job đã pause để bạn resume sau." };
  return null;
}

function composer() {
  return [...document.querySelectorAll('[role="textbox"], textarea, [contenteditable="true"]')]
    .filter((node) => node.getClientRects().length)
    .at(-1);
}

function responseNodes() {
  const selectors = ["message-content", "model-response", '[data-message-author-role="model"]'];
  for (const selector of selectors) {
    const nodes = [...document.querySelectorAll(selector)].filter((node) => node.innerText?.trim());
    if (nodes.length) return nodes;
  }
  return [];
}

function stopVisible() {
  return [...document.querySelectorAll("button")].some((button) => /stop|dừng|ngừng/.test(buttonText(button)) && button.getClientRects().length);
}

async function submitPrompt(prompt) {
  const blocked = classifyPage();
  if (blocked) throw Object.assign(new Error(blocked.message), blocked);
  const box = composer();
  if (!box) throw Object.assign(new Error("Không nhận diện được ô nhập Gemini; có thể UI đã thay đổi."), { reason: "ui_changed" });
  const before = responseNodes().length;
  box.focus();
  if (box instanceof HTMLTextAreaElement || box instanceof HTMLInputElement) {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(box), "value")?.set;
    setter?.call(box, prompt);
    box.dispatchEvent(new Event("input", { bubbles: true }));
  } else {
    box.replaceChildren(document.createTextNode(prompt));
    box.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: prompt }));
  }
  const send = [...document.querySelectorAll("button")].find((button) => /send|gửi/.test(buttonText(button)) && !button.disabled);
  if (send) send.click();
  else box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));

  return new Promise((resolve, reject) => {
    let sawGenerating = false;
    let quietTimer;
    const deadline = setTimeout(() => finish(new Error("Gemini không hoàn tất phản hồi trong thời hạn chờ.")), 240000);
    const observer = new MutationObserver(() => {
      const blockedNow = classifyPage();
      if (blockedNow) return finish(Object.assign(new Error(blockedNow.message), blockedNow));
      const nodes = responseNodes();
      const output = nodes.at(-1)?.innerText?.trim() || "";
      if (stopVisible()) sawGenerating = true;
      if (output && nodes.length > before && !stopVisible()) {
        clearTimeout(quietTimer);
        quietTimer = setTimeout(() => finish(null, output), 1400);
      }
      if (sawGenerating && !stopVisible() && output && nodes.length > before) {
        clearTimeout(quietTimer);
        quietTimer = setTimeout(() => finish(null, output), 400);
      }
    });
    const finish = (error, output) => {
      clearTimeout(deadline); clearTimeout(quietTimer); observer.disconnect();
      if (error) reject(error); else resolve(output);
    };
    observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
  });
}

function cleanResult(value) {
  return value.replace(/^```(?:text|markdown)?\s*/i, "").replace(/\s*```$/, "").trim();
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "ping") {
    sendResponse({ ready: true });
    return;
  }
  if (message.type !== "translate-chapter") return;
  (async () => {
    const { job } = message;
    const chapterPrompt = job.prompt_snapshot.replace("{{text}}", job.content);
    const translatedContent = cleanResult(await submitPrompt(chapterPrompt));
    if (translatedContent.length < Math.min(80, job.content.length * 0.15)) {
      throw Object.assign(new Error("Phản hồi quá ngắn, cần kiểm tra hoặc dịch lại."), { reason: "manual" });
    }
    const titlePrompt = job.prompt_snapshot.replace("{{text}}", job.title);
    const translatedTitle = cleanResult(await submitPrompt(titlePrompt));
    sendResponse({ ok: true, translation: { translated_title: translatedTitle, translated_content: translatedContent } });
  })().catch((error) => sendResponse({ ok: false, reason: error.reason || "transient", message: String(error.message || error) }));
  return true;
});
