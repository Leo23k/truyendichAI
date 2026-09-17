(() => {
  const buttons = document.querySelectorAll("[data-batch-translate-open]");
  if (!buttons.length) return;

  const dialog = document.createElement("div");
  dialog.className = "fixed inset-0 z-50 hidden items-center justify-center bg-stone-950/40 p-4";
  dialog.innerHTML = `
    <form data-batch-translate-form class="w-full max-w-md rounded-xl bg-white p-5 shadow-2xl" novalidate>
      <div class="flex items-start justify-between gap-4"><div><h2 class="text-xl font-semibold text-stone-950">Dịch nhiều chương</h2><p class="mt-1 text-sm text-stone-500">Chọn phạm vi chương cần xếp hàng dịch.</p></div><button type="button" data-batch-cancel class="icon-button text-stone-500" aria-label="Đóng">×</button></div>
      <label class="mt-5 block text-sm font-medium text-stone-700">Từ chương<input data-batch-from type="number" min="1" required class="mt-1 h-10 w-full rounded-md border border-stone-200 px-3"></label>
      <label class="mt-4 block text-sm font-medium text-stone-700">Đến chương<input data-batch-to type="number" min="1" required class="mt-1 h-10 w-full rounded-md border border-stone-200 px-3"></label>
      <fieldset class="mt-5"><legend class="text-sm font-medium text-stone-700">Giọng</legend><label class="mt-2 flex items-center gap-2 text-sm"><input type="radio" name="voice" value="vi-VN-NamMinhNeural" checked> Nam Minh</label><label class="mt-2 flex items-center gap-2 text-sm"><input type="radio" name="voice" value="vi-VN-HoaiMyNeural"> Hoài My</label></fieldset>
      <p data-batch-error class="mt-4 hidden rounded-md bg-red-50 px-3 py-2 text-sm text-red-700" role="alert"></p>
      <div class="mt-6 flex justify-end gap-3"><button type="button" data-batch-cancel class="h-10 rounded-md px-4 text-sm font-medium text-stone-600 hover:bg-stone-100">Hủy</button><button type="submit" class="h-10 rounded-md bg-teal-700 px-4 text-sm font-medium text-white hover:bg-teal-800">Bắt đầu dịch</button></div>
    </form>`;
  document.body.append(dialog);

  const form = dialog.querySelector("[data-batch-translate-form]");
  const from = dialog.querySelector("[data-batch-from]");
  const to = dialog.querySelector("[data-batch-to]");
  const error = dialog.querySelector("[data-batch-error]");
  let activeButton = null;

  const close = () => { dialog.classList.add("hidden"); dialog.classList.remove("flex"); };
  const showError = (message = "") => { error.textContent = message; error.classList.toggle("hidden", !message); };
  buttons.forEach((button) => button.addEventListener("click", () => {
    activeButton = button;
    const total = Number(button.dataset.batchTotal || 0);
    from.value = "1";
    to.value = String(total);
    to.max = String(total);
    showError();
    dialog.classList.remove("hidden"); dialog.classList.add("flex"); from.focus();
  }));
  dialog.querySelectorAll("[data-batch-cancel]").forEach((button) => button.addEventListener("click", close));
  dialog.addEventListener("click", (event) => { if (event.target === dialog) close(); });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const start = Number(from.value), end = Number(to.value), total = Number(activeButton?.dataset.batchTotal || 0);
    if (!Number.isInteger(start) || start < 1 || !Number.isInteger(end) || end > total || start > end) {
      showError(`Phạm vi phải từ 1 đến ${total}, và chương bắt đầu không được lớn hơn chương kết thúc.`);
      return;
    }
    form.dispatchEvent(new CustomEvent("batch-translate-submit", { bubbles: true, detail: { novelId: activeButton.dataset.batchNovelId, from: start, to: end, voice: form.elements.voice.value } }));
  });
})();
