const $ = (id) => document.getElementById(id);
const MESSAGE_TIMEOUT_MS = 14000;

async function send(message, timeoutMs = MESSAGE_TIMEOUT_MS) {
  let timer;
  try {
    return await Promise.race([
      chrome.runtime.sendMessage(message),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`Extension chưa phản hồi sau ${Math.round(timeoutMs / 1000)} giây. Hãy thử lại.`)), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

const GEMINI_PERMISSION = "https://gemini.google.com/*";

function log(code, message, tabId, url) {
  console.info("[GEMINI_COMPANION][POPUP]", JSON.stringify({ code, message, tabId: tabId ?? null, url: url ?? null, timestamp: new Date().toISOString() }));
}

function showHealthResult(result) {
  const check = (item) => item?.ok ? "OK" : item?.code || "FAILED";
  const dom = result?.gemini?.checks || {};
  const diagnostics = result?.browser?.diagnostics;
  const geminiCode = result?.gemini?.code || result?.browser?.code;
  const permissionCheckFailed = result?.browser?.code === "PERMISSION_CHECK_FAILED";
  const manifestState = diagnostics ? (diagnostics.manifestDeclared ? "có" : "thiếu") : permissionCheckFailed ? "chưa kiểm tra" : "OK";
  const runtimeState = diagnostics ? `API contains(): ${diagnostics.runtimeGranted ? "có" : "không"} · inject: ${diagnostics.actualAccess === true ? "được phép" : diagnostics.actualAccess === false ? "bị từ chối" : "chưa thử"}` : permissionCheckFailed ? "API contains(): chưa xác nhận" : "OK";
  $("message").textContent = [
    `Extension: v${diagnostics?.extensionVersion || chrome.runtime.getManifest().version} · ID ${diagnostics?.extensionId || chrome.runtime.id}`,
    `Backend: ${check(result?.backend)}${result?.backend?.message ? ` — ${result.backend.message}` : ""}`,
    `Quyền Gemini: ${check(result?.browser)} (${result?.browser?.host || GEMINI_PERMISSION})${result?.browser?.message ? ` — ${result.browser.message}` : ""}`,
    `Manifest host: ${manifestState} · ${runtimeState}`,
    diagnostics ? `Manifest hosts: ${diagnostics.manifestHosts.join(", ") || "(none)"}` : "",
    diagnostics ? `Runtime origins: ${diagnostics.runtimeOrigins.join(", ") || "(none)"}` : "",
    `Tab / content script: ${result?.gemini?.tabId ? `tab ${result.gemini.tabId}` : "không sẵn sàng"} / ${check(result?.gemini)}`,
    `Đăng nhập: ${dom.loginRequired ? "cần đăng nhập" : dom.loggedIn ? "OK" : "chưa xác định"} · Composer: ${dom.composer ? "OK" : "không"} · Nút gửi: ${dom.sendButton ? "OK" : "không"}`,
    `Chat mới: ${dom.newChat ? "OK" : "không"} · Vùng phản hồi: ${dom.responseContainer ? "OK" : "không"}`,
    `Mã tổng: ${result?.code || "UNKNOWN"}`,
    result?.gemini?.message ? `Chi tiết Gemini (${geminiCode || "FAILED"}): ${result.gemini.message}` : "",
    result?.gemini?.rawError ? `Lỗi gốc: ${result.gemini.rawError}` : "",
  ].filter(Boolean).join("\n");
  const permissionIssue = ["HOST_PERMISSION_REQUIRED", "HOST_PERMISSION_MISSING"].includes(geminiCode);
  $("grantPermission").hidden = !permissionIssue || !diagnostics?.manifestDeclared || !!diagnostics?.runtimeGranted;
  $("reloadGemini").hidden = !["INJECT_FAILED", "CONTENT_SCRIPT_NOT_READY", "CONTENT_SCRIPT_FILE_MISSING"].includes(geminiCode) || !diagnostics?.runtimeGranted;
  log(geminiCode || result?.code || "UNKNOWN", result?.gemini?.rawError || result?.gemini?.message || result?.backend?.message || "Health check completed", result?.gemini?.tabId, result?.gemini?.url);
}

async function refresh() {
  try {
    const state = await send({ type: "get-state" });
    const connected = Number.isInteger(state?.appTabId);
    $("dot").classList.toggle("online", connected);
    $("state").textContent = state?.currentJob ? `Đang dịch chương ${state.currentJob}` : connected ? "Đã kết nối" : "Chưa kết nối";
    $("detail").textContent = state?.lastError || state?.appOrigin || "Mở Leo Truyện, rồi kết nối tab hiện tại.";
  } catch (error) {
    log("STATE_REFRESH_FAILED", String(error?.message || error));
  }
}

$("connect").addEventListener("click", async () => {
  $("connect").disabled = true;
  $("message").textContent = "Đang kết nối với website…";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const result = await send({ type: "connect-app", tab }, 8000);
    if (!result?.ok) throw new Error(result?.error || "Kết nối thất bại.");
    $("message").textContent = "Đã kết nối. Hàng đợi Gemini Web sẽ chạy tuần tự.";
  } catch (error) {
    $("message").textContent = String(error?.message || error);
    log("CONNECT_FAILED", $("message").textContent);
  } finally {
    $("connect").disabled = false;
    void refresh();
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
    showHealthResult(await send({ type: "health-check" }));
  } catch (error) {
    $("message").textContent = String(error?.message || error);
    log("HEALTH_CHECK_FAILED", $("message").textContent);
  } finally {
    $("healthCheck").disabled = false;
  }
});
$("grantPermission").addEventListener("click", async () => {
  $("grantPermission").disabled = true;
  $("message").textContent = "Đang yêu cầu trình duyệt cấp quyền Gemini…";
  try {
    const granted = await chrome.permissions.request({ origins: [GEMINI_PERMISSION] });
    log(granted ? "PERMISSION_GRANTED" : "PERMISSION_DENIED", `User permission request result: ${granted}`);
    if (granted) showHealthResult(await send({ type: "health-check" }));
    else $("message").textContent = "Chưa cấp quyền Gemini. Chấp nhận yêu cầu của trình duyệt rồi thử lại.";
  } catch (error) {
    $("message").textContent = `Không thể yêu cầu quyền: ${String(error?.message || error)}. Mở phần cài đặt extension và cấp quyền truy cập Gemini.`;
    log("PERMISSION_REQUEST_FAILED", $("message").textContent);
  } finally {
    $("grantPermission").disabled = false;
  }
});
$("reloadGemini").addEventListener("click", async () => {
  $("reloadGemini").disabled = true;
  $("message").textContent = "Đang nạp lại tab Gemini hiện tại… Nội dung chưa gửi trong tab có thể mất.";
  try {
    const reload = await send({ type: "reload-gemini" });
    if (!reload?.ok) throw new Error(reload?.message || "Không thể nạp lại tab Gemini.");
    $("message").textContent = "Tab đã nạp lại. Đang kiểm tra quyền và content script…";
    showHealthResult(await send({ type: "health-check" }));
  } catch (error) {
    $("message").textContent = String(error?.message || error);
    log("TAB_RELOAD_FAILED", $("message").textContent);
  } finally {
    $("reloadGemini").disabled = false;
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
