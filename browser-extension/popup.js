const $ = (id) => document.getElementById(id);

async function send(message) {
  return chrome.runtime.sendMessage(message);
}

async function refresh() {
  const state = await send({ type: "get-state" });
  const connected = Number.isInteger(state?.appTabId);
  $("dot").classList.toggle("online", connected);
  $("state").textContent = state?.currentJob ? `Đang dịch chương ${state.currentJob}` : connected ? "Đã kết nối" : "Chưa kết nối";
  $("detail").textContent = state?.lastError || state?.appOrigin || "Mở Leo Truyện, rồi kết nối tab hiện tại.";
  $("message").textContent = "";
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
  } finally {
    $("connect").disabled = false;
    await refresh();
  }
});

$("openGemini").addEventListener("click", () => send({ type: "open-gemini" }));
$("healthCheck").addEventListener("click", async () => {
  $("healthCheck").disabled = true;
  $("message").textContent = "Đang kiểm tra tab Gemini…";
  try {
    const result = await send({ type: "health-check" });
    if (!result?.ok) throw new Error(result?.error || "Gemini chưa sẵn sàng.");
    $("message").textContent = "Gemini đã đăng nhập và ô nhập sẵn sàng.";
  } catch (error) {
    $("message").textContent = String(error?.message || error);
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
  }
  await refresh();
});

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "companion-state-changed") refresh();
});
refresh();
