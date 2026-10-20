const $ = (id) => document.getElementById(id);

async function send(message) {
  return chrome.runtime.sendMessage(message);
}

function log(code, message, tabId, url) {
  console.info("[GEMINI_COMPANION][POPUP]", JSON.stringify({ code, message, tabId: tabId ?? null, url: url ?? null, timestamp: new Date().toISOString() }));
}

async function refresh() {
  const state = await send({ type: "get-state" });
  const connected = Number.isInteger(state?.appTabId);
  $("dot").classList.toggle("online", connected);
  $("state").textContent = state?.currentJob ? `Đang dịch chương ${state.currentJob}` : connected ? "Đã kết nối" : "Chưa kết nối";
  $("detail").textContent = state?.lastError || state?.appOrigin || "Mở Leo Truyện, rồi kết nối tab hiện tại.";
}

$("connect").addEventListener("click", async () => {
  $("connect").disabled = true;
  $("message").textContent = "Đang kết nối với website…";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const result = await send({ type: "connect-app", tab });
    if (!result?.ok) throw new Error(result?.error || "Kết nối thất bại.");
    $("message").textContent = "Đã kết nối. Hàng đợi Gemini Web sẽ chạy tuần tự.";
  } catch (error) {
    $("message").textContent = String(error?.message || error);
    log("CONNECT_FAILED", $("message").textContent);
  } finally {
    $("connect").disabled = false;
    await refresh();
  }
});

$("openGemini").addEventListener("click", async () => {
  const result = await send({ type: "open-gemini" });
  if (!result?.ok) $("message").textContent = result?.message || "Không mở được Gemini Web.";
});
$("healthCheck").addEventListener("click", async () => {
  $("healthCheck").disabled = true;
  $("message").textContent = "Đang kiểm tra tab Gemini…";
  try {
    const result = await send({ type: "health-check" });
    const check = (item) => item?.ok ? "OK" : item?.code || "FAILED";
    const dom = result?.gemini?.checks || {};
    $("message").textContent = [
      `Backend: ${check(result?.backend)}`,
      `Quyền Gemini: ${check(result?.browser)} (${result?.browser?.host || "https://gemini.google.com/*"})`,
      `Tab / content script: ${result?.gemini?.tabId ? `tab ${result.gemini.tabId}` : "không sẵn sàng"} / ${result?.browser?.injected ? "đã inject" : check(result?.browser)}`,
      `Đăng nhập: ${dom.loggedIn ? "OK" : "không"} · Composer: ${dom.composer ? "OK" : "không"} · Nút gửi: ${dom.sendButton ? "OK" : "không"}`,
      `Chat mới: ${dom.newChat ? "OK" : "không"} · Vùng phản hồi: ${dom.responseContainer ? "OK" : "không"}`,
      `Mã: ${result?.code || "UNKNOWN"}${result?.gemini?.message ? ` — ${result.gemini.message}` : ""}`,
    ].join("\n");
    log(result?.code || "UNKNOWN", result?.gemini?.message || "Health check completed", result?.gemini?.tabId, result?.gemini?.url);
  } catch (error) {
    $("message").textContent = String(error?.message || error);
    log("HEALTH_CHECK_FAILED", $("message").textContent);
  } finally {
    $("healthCheck").disabled = false;
  }
});
$("resume").addEventListener("click", async () => {
  try {
    const result = await send({ type: "resume-jobs" });
    if (!result?.ok) throw new Error(result?.error || "Không thể resume.");
    $("message").textContent = `Đã đưa ${result.resumed} job vào hàng đợi.`;
  } catch (error) {
    $("message").textContent = String(error?.message || error);
    log("RESUME_FAILED", $("message").textContent);
  }
  await refresh();
});

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "companion-state-changed") refresh();
});
refresh();
