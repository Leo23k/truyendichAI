(() => {
  const DB_NAME = "novel-reader-offline", DB_VERSION = 1;
  const openDb = () => new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => { const db = request.result;
      if (!db.objectStoreNames.contains("books")) db.createObjectStore("books", { keyPath: "id" });
      if (!db.objectStoreNames.contains("chapterLists")) db.createObjectStore("chapterLists", { keyPath: "bookId" });
      if (!db.objectStoreNames.contains("chapters")) { const s = db.createObjectStore("chapters", { keyPath: "id" }); s.createIndex("url", "url", { unique: true }); s.createIndex("bookId", "bookId", { unique: false }); }
    }; request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
  });
  const tx = async (name, mode, action) => { const db = await openDb(); return new Promise((resolve, reject) => { const t = db.transaction(name, mode), result = action(t.objectStore(name)); t.oncomplete = () => resolve(result?.result); t.onerror = t.onabort = () => reject(t.error); }).finally(() => db.close()); };
  const get = (name, key) => tx(name, "readonly", s => s.get(key));
  const put = (name, value) => tx(name, "readwrite", s => s.put(value));
  const remove = (name, key) => tx(name, "readwrite", s => s.delete(key));
  const all = name => tx(name, "readonly", s => s.getAll());
  const byUrl = url => tx("chapters", "readonly", s => s.index("url").get(url));
  const removeBookChapters = async bookId => Promise.all((await all("chapters")).filter(chapter => chapter.bookId === bookId).map(chapter => remove("chapters", chapter.id)));
  const text = node => node?.textContent?.trim() || "";
  const escape = value => String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const chapterId = link => link.dataset.chapterId || link.href.split("?")[0].split("/").pop();
  const storedChapter = chapter => get("chapters", chapter.id).then(found => found?.content ? found : get("chapters", chapter.chapterId)).then(found => found?.content ? found : undefined);

  async function renderOfflineReader() {
    const chapter = await byUrl(location.pathname);
    if (!chapter) return missing();
    const entries = (await get("chapterLists", chapter.bookId))?.chapters || [];
    const index = entries.findIndex(item => item.id === chapter.id || item.chapterId === chapter.chapterId);
    const nav = (entry, label) => entry ? `<a href="${escape(entry.url)}" style="color:#0f766e">${label}</a>` : `<span style="color:#a8a29e">${label}</span>`;
    const content = chapter.content.split(/\n+/).filter(Boolean).map(line => `<p>${escape(line)}</p>`).join("");
    document.title = chapter.title;
    document.body.innerHTML = `<main style="max-width:48rem;margin:0 auto;padding:2rem 1.25rem 5rem;font:18px/1.85 Georgia,serif;color:#302d28;background:#faf8f3;min-height:100vh"><a href="${escape(chapter.bookUrl)}" style="font:14px system-ui;color:#0f766e">← Quay lại truyện</a><h1 style="font:600 1.5rem/1.35 system-ui;margin:2rem 0">${escape(chapter.title)}</h1><article>${content}</article><nav style="display:flex;justify-content:space-between;gap:1rem;margin-top:3rem;font:14px system-ui">${nav(entries[index - 1], "← Chương trước")}${nav(entries[index + 1], "Chương sau →")}</nav></main>`;
  }
  function missing() { document.body.innerHTML = '<main style="max-width:42rem;margin:4rem auto;padding:0 1.25rem;font-family:system-ui"><h1>Không có bản offline</h1><p>Chương này chưa được tải để đọc offline.</p><a href="/">Về thư viện</a></main>'; }
  async function renderOfflineBook() {
    const book = (await all("books")).find(item => item.bookUrl === location.pathname);
    const chapters = book ? (await get("chapterLists", book.id))?.chapters || [] : [];
    const list = chapters.map(item => `<li><a href="${escape(item.url)}">${escape(item.title)}</a></li>`).join("") || "<li>Chưa có chương nào được tải offline.</li>";
    document.body.innerHTML = `<main style="max-width:42rem;margin:3rem auto;padding:0 1.25rem;font-family:system-ui"><p style="color:#0f766e">Đang đọc offline</p><h1>${escape(book?.title || "Truyện offline")}</h1><p>${chapters.length} chương đã lưu trên thiết bị.</p><ol style="line-height:2;padding-left:1.5rem">${list}</ol></main>`;
  }
  async function renderOfflineHome() { const books = await all("books"); const items = books.map(book => `<li><a href="${escape(book.bookUrl)}">${escape(book.title)}</a></li>`).join("") || "<li>Chưa có truyện nào được tải offline.</li>"; document.body.innerHTML = `<main style="max-width:42rem;margin:4rem auto;padding:0 1.25rem;font-family:system-ui"><h1>Leo Truyen</h1><p>Bạn đang offline. Truyện đã tải:</p><ul style="line-height:2">${items}</ul></main>`; }

  async function fetchChapter(chapter) {
    for (let attempt = 0; attempt < 2; attempt += 1) try {
      const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 25000);
      const response = await fetch(chapter.url, { cache: "no-store", signal: controller.signal }).finally(() => clearTimeout(timeout)); if (!response.ok) throw new Error(String(response.status));
      const page = new DOMParser().parseFromString(await response.text(), "text/html"), content = text(page.querySelector("[data-reader-content]"));
      if (!content) throw new Error("empty chapter");
      await put("chapters", { ...chapter, title: text(page.querySelector("h2")) || chapter.title, content, savedAt: Date.now() }); return true;
    } catch (error) { if (!attempt) await new Promise(resolve => setTimeout(resolve, 500)); else console.warn("Offline chapter download failed", chapter.id, error); }
    return false;
  }
  async function setupBook(book) {
    const status = book.querySelector("[data-offline-status]"), download = book.querySelector("[data-offline-download]"), update = book.querySelector("[data-offline-update]"), del = book.querySelector("[data-offline-delete]");
    if (!status || !download || !update || !del) return;
    const id = book.dataset.bookId, title = book.dataset.bookTitle;
    const chapters = [...book.querySelectorAll("[data-offline-chapter]")].map((link, order) => { const sourceId = chapterId(link); return { id: `${id}:${sourceId}`, chapterId: sourceId, url: new URL(link.href).pathname, title: text(link.querySelector("span:last-child")) || text(link), bookId: id, bookUrl: location.pathname, order }; });
    const count = async () => (await Promise.all(chapters.map(storedChapter))).filter(Boolean).length;
    const controls = (stored, busy = false, resumable = false) => { const complete = stored === chapters.length && chapters.length > 0, label = download.querySelector("span"); download.disabled = busy || complete; label.textContent = busy ? `Đang tải ${stored} / ${chapters.length}` : complete ? "✓ Đã tải offline" : (stored || resumable) ? "Tải tiếp" : "Tải để đọc offline"; update.hidden = del.hidden = !(stored || resumable); update.disabled = del.disabled = busy; };
    let stored = await count(), resume = Boolean(await get("books", id)); controls(stored, false, resume); if (stored || resume) status.textContent = stored === chapters.length ? `✓ Đã tải offline · ${stored} / ${chapters.length} chương` : `Đã tải ${stored} / ${chapters.length} chương. Có thể tải tiếp.`;
    const downloadMissing = async () => {
      const local = await Promise.all(chapters.map(storedChapter)), pending = chapters.filter((c, i) => !local[i]);
      await put("chapterLists", { bookId: id, chapters, updatedAt: Date.now() }); await put("books", { id, title, bookUrl: location.pathname, totalChapters: chapters.length, downloadedChapters: stored, updatedAt: Date.now(), status: "downloading" });
      if (!pending.length) { controls(stored); return; }
      let cursor = 0, failed = false; resume = true; controls(stored, true, resume); const show = () => { status.textContent = `Đang tải ${stored} / ${chapters.length} chương`; controls(stored, true, resume); };
      await Promise.all(Array.from({ length: Math.min(4, pending.length) }, async () => { while (cursor < pending.length) { const chapter = pending[cursor++]; if (await fetchChapter(chapter)) { stored += 1; show(); } else failed = true; } }));
      await put("books", { id, title, bookUrl: location.pathname, totalChapters: chapters.length, downloadedChapters: stored, updatedAt: Date.now(), status: stored === chapters.length ? "downloaded" : "partial" });
      resume = stored !== chapters.length; controls(stored, false, resume); status.textContent = stored === chapters.length ? `✓ Đã tải offline · ${stored} / ${chapters.length} chương` : `Đã tải ${stored} / ${chapters.length} chương.${failed ? " Tải thất bại. Có thể tiếp tục sau." : ""}`;
    };
    download.onclick = update.onclick = downloadMissing;
    del.onclick = async () => { await removeBookChapters(id); await Promise.all([remove("chapterLists", id), remove("books", id)]); stored = 0; resume = false; controls(stored); status.textContent = "Đã xóa bản offline."; };
  }
  document.addEventListener("DOMContentLoaded", () => { const book = document.querySelector("[data-offline-book]"); if (book && "indexedDB" in window) setupBook(book).catch(error => console.warn("Offline storage unavailable", error)); });
  window.NovelOffline = { renderOfflineReader, renderOfflineBook, renderOfflineHome };
})();
