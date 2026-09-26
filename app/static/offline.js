(() => {
  const DB_NAME = "novel-reader-offline";
  const DB_VERSION = 1;
  const openDb = () => new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains("books")) db.createObjectStore("books", { keyPath: "id" });
      if (!db.objectStoreNames.contains("chapterLists")) db.createObjectStore("chapterLists", { keyPath: "bookId" });
      if (!db.objectStoreNames.contains("chapters")) {
        const chapters = db.createObjectStore("chapters", { keyPath: "id" });
        chapters.createIndex("url", "url", { unique: true });
        chapters.createIndex("bookId", "bookId", { unique: false });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const request = async (store, mode, action) => {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, mode);
      const result = action(tx.objectStore(store));
      tx.oncomplete = () => resolve(result?.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    }).finally(() => db.close());
  };
  const get = (store, key) => request(store, "readonly", objectStore => objectStore.get(key));
  const put = (store, value) => request(store, "readwrite", objectStore => objectStore.put(value));
  const remove = (store, key) => request(store, "readwrite", objectStore => objectStore.delete(key));
  const byUrl = url => request("chapters", "readonly", objectStore => objectStore.index("url").get(url));
  const chapterId = href => href.match(/\/chapters\/([^/?#]+)/)?.[1] || "";
  const text = value => value?.textContent?.trim() || "";

  async function renderOfflineReader() {
    const chapter = await byUrl(location.pathname);
    if (!chapter) {
      document.body.innerHTML = '<main style="max-width:42rem;margin:4rem auto;padding:0 1.25rem;font-family:system-ui"><h1>Không có bản offline</h1><p>Chương này chưa được tải để đọc offline.</p><a href="/">Về thư viện</a></main>';
      return;
    }
    document.title = chapter.title;
    const paragraphs = chapter.content.split(/\n+/).filter(Boolean).map(line => `<p>${escapeHtml(line)}</p>`).join("");
    document.body.innerHTML = `<main style="max-width:48rem;margin:0 auto;padding:2rem 1.25rem 5rem;font:18px/1.85 Georgia,serif;color:#302d28;background:#faf8f3;min-height:100vh"><a href="${escapeAttribute(chapter.bookUrl)}" style="font:14px system-ui;color:#0f766e">← Quay lại truyện</a><h1 style="font:600 1.5rem/1.35 system-ui;margin:2rem 0">${escapeHtml(chapter.title)}</h1><article>${paragraphs}</article></main>`;
  }
  const escapeHtml = value => String(value).replace(/[&<>"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char]);
  const escapeAttribute = escapeHtml;

  async function setupBookDownload(book) {
    const status = book.querySelector("[data-offline-status]");
    const download = book.querySelector("[data-offline-download]");
    const update = book.querySelector("[data-offline-update]");
    const deleteButton = book.querySelector("[data-offline-delete]");
    const id = book.dataset.bookId;
    const title = book.dataset.bookTitle;
    const chapters = [...book.querySelectorAll("[data-offline-chapter]")].map(link => ({ id: link.dataset.chapterId || chapterId(link.href), url: new URL(link.href).pathname, title: text(link.querySelector("span:last-child")) || text(link), bookId: id, bookUrl: location.pathname }));
    const existing = await get("chapterLists", id);
    const setDownloaded = downloaded => {
      download.hidden = downloaded;
      update.hidden = !downloaded;
      deleteButton.hidden = !downloaded;
    };
    const existingCount = existing ? (await Promise.all(chapters.map(chapter => get("chapters", chapter.id)))).filter(Boolean).length : 0;
    setDownloaded(existingCount > 0);
    if (existingCount === chapters.length && chapters.length) status.textContent = "✓ Đã tải offline";
    else if (existingCount) status.textContent = `Đã tải ${existingCount} / ${chapters.length} chương. Có thể bấm Cập nhật để tiếp tục.`;
    const downloadMissing = async () => {
      const known = await Promise.all(chapters.map(chapter => get("chapters", chapter.id)));
      const pending = chapters.filter((chapter, index) => !known[index]);
      await put("books", { id, title, updatedAt: Date.now(), status: pending.length ? "downloading" : "downloaded" });
      await put("chapterLists", { bookId: id, chapters, updatedAt: Date.now() });
      let completed = chapters.length - pending.length;
      const showProgress = () => { status.textContent = pending.length ? `Đang tải ${completed} / ${chapters.length} chương` : "✓ Đã tải offline"; };
      showProgress();
      download.disabled = update.disabled = true;
      let cursor = 0;
      const worker = async () => {
        while (cursor < pending.length) {
          const chapter = pending[cursor++];
          try {
            const response = await fetch(chapter.url, { cache: "no-store" });
            if (!response.ok) throw new Error(String(response.status));
            const page = new DOMParser().parseFromString(await response.text(), "text/html");
            const content = text(page.querySelector("[data-reader-content]"));
            if (!content) throw new Error("empty chapter");
            await put("chapters", { ...chapter, title: text(page.querySelector("h2")) || chapter.title, content, savedAt: Date.now() });
            completed += 1;
            showProgress();
          } catch (error) {
            console.warn("Offline chapter download failed", chapter.id, error);
          }
        }
      };
      await Promise.all(Array.from({ length: Math.min(4, pending.length) }, worker));
      await put("books", { id, title, updatedAt: Date.now(), status: "downloaded" });
      download.disabled = update.disabled = false;
      const remaining = (await Promise.all(chapters.map(chapter => get("chapters", chapter.id)))).filter(Boolean).length;
      status.textContent = remaining === chapters.length ? "✓ Đã tải offline" : `Đã tải ${remaining} / ${chapters.length} chương. Có thể bấm Cập nhật để tiếp tục.`;
      setDownloaded(remaining > 0);
    };
    download.addEventListener("click", downloadMissing);
    update.addEventListener("click", downloadMissing);
    deleteButton.addEventListener("click", async () => {
      await Promise.all(chapters.map(chapter => remove("chapters", chapter.id)));
      await remove("chapterLists", id);
      await remove("books", id);
      status.textContent = "Đã xóa bản offline.";
      setDownloaded(false);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const book = document.querySelector("[data-offline-book]");
    if (book && "indexedDB" in window) setupBookDownload(book).catch(error => console.warn("Offline storage unavailable", error));
  });
  window.NovelOffline = { renderOfflineReader };
})();
