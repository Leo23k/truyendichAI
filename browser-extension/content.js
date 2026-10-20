const readBody = () => (document.body?.innerText || "").toLowerCase();
const buttonText = (button) => `${button.getAttribute("aria-label") || ""} ${button.innerText || ""} ${button.title || ""}`.toLowerCase();

chrome.runtime.sendMessage({ type: "gemini-ready" }).catch(() => {});

function classifyPage() {
  const body = readBody().slice(-2000);
  if (/captcha|verify it's you|xác minh bạn|verification/.test(body)) return { reason: "manual", message: "Gemini yêu cầu xác minh/CAPTCHA. Hãy xử lý trong tab Gemini rồi resume." };
  if (/sign in|đăng nhập/.test(body) && !document.querySelector('[role="textbox"], textarea, [contenteditable="true"]')) return { reason: "manual", message: "Gemini chưa đăng nhập. Hãy đăng nhập trong tab Gemini rồi resume." };
  if (/usage limit|you've reached your limit|đã đạt giới hạn|vượt quá giới hạn|try again later/.test(body)) return { reason: "quota", message: "Gemini báo giới hạn sử dụng. Job đã pause để bạn resume sau." };
  if (/prompt is too long|input is too long|message is too long|exceeds (the )?(context|token) limit/.test(body)) return { reason: "too_long", message: "Chương vượt giới hạn một lần gửi; sẽ thử dịch theo đoạn." };
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

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "ping") {
    sendResponse({ ready: true });
    return;
  }
  if (message.type !== "translate-chapter") return;
  (async () => {
    const { job } = message;
    const translatedContent = await translateWholeChapter(job);
    const titlePrompt = job.prompt_snapshot.replace("{{text}}", job.title);
    const translatedTitle = cleanResult(await submitPrompt(titlePrompt));
    if (!translatedTitle || translatedTitle === job.title) {
      throw Object.assign(new Error("Tên chương chưa được dịch; job đã pause để kiểm tra."), { reason: "manual" });
    }
    sendResponse({ ok: true, translation: { translated_title: translatedTitle, translated_content: translatedContent } });
  })().catch((error) => sendResponse({ ok: false, reason: error.reason || "transient", message: String(error.message || error) }));
  return true;
});
