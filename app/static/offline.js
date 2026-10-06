(() => {
  const DB_NAME = "novel-reader-offline", DB_VERSION = 2;
  const openDb = () => new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => { const db = request.result;
      if (!db.objectStoreNames.contains("books")) db.createObjectStore("books", { keyPath: "id" });
      if (!db.objectStoreNames.contains("chapterLists")) db.createObjectStore("chapterLists", { keyPath: "bookId" });
      if (!db.objectStoreNames.contains("chapters")) { const s = db.createObjectStore("chapters", { keyPath: "id" }); s.createIndex("url", "url", { unique: true }); s.createIndex("bookId", "bookId", { unique: false }); }
      if (!db.objectStoreNames.contains("ebooks")) db.createObjectStore("ebooks", { keyPath: "id" });
      if (!db.objectStoreNames.contains("ebookChapters")) { const s = db.createObjectStore("ebookChapters", { keyPath: "id" }); s.createIndex("bookId", "bookId", { unique: false }); }
    }; request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
  });
  const tx = async (name, mode, action) => { const db = await openDb(); return new Promise((resolve, reject) => { const t = db.transaction(name, mode), result = action(t.objectStore(name)); t.oncomplete = () => resolve(result?.result); t.onerror = t.onabort = () => reject(t.error); }).finally(() => db.close()); };
  const get = (name, key) => tx(name, "readonly", s => s.get(key));
  const put = (name, value) => tx(name, "readwrite", s => s.put(value));
  const remove = (name, key) => tx(name, "readwrite", s => s.delete(key));
  const all = name => tx(name, "readonly", s => s.getAll());
  const byUrl = url => tx("chapters", "readonly", s => s.index("url").get(url));
  const removeBookChapters = async bookId => Promise.all((await all("chapters")).filter(chapter => chapter.bookId === bookId).map(chapter => remove("chapters", chapter.id)));
  const ebookChapters = bookId => tx("ebookChapters", "readonly", s => s.index("bookId").getAll(bookId));
  const saveEbookChapter = value => tx("ebookChapters", "readwrite", s => s.put(value));
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
    let chapters = [...book.querySelectorAll("[data-offline-chapter]")].map((link, order) => { const sourceId = chapterId(link); return { id: `${id}:${sourceId}`, chapterId: sourceId, url: new URL(link.href).pathname, title: text(link.querySelector("span:last-child")) || text(link), bookId: id, bookUrl: location.pathname, order }; });
    const loadAllChapters = async () => { const url = book.dataset.offlineChaptersUrl; if (!url) return; const response = await fetch(url, { cache: "no-store" }); if (!response.ok) throw new Error("chapter list unavailable"); chapters = (await response.json()).map((chapter, order) => ({ id: `${id}:${chapter.id}`, chapterId: String(chapter.id), url: chapter.url, title: chapter.title || `Chương ${chapter.chapter_number}`, bookId: id, bookUrl: location.pathname, order })); };
    const count = async () => (await Promise.all(chapters.map(storedChapter))).filter(Boolean).length;
    const controls = (stored, busy = false, resumable = false) => { const complete = stored === chapters.length && chapters.length > 0, label = download.querySelector("span"); download.disabled = busy || complete; label.textContent = busy ? `Đang tải ${stored} / ${chapters.length}` : complete ? "✓ Đã tải toàn bộ" : (stored || resumable) ? "Tải tiếp tất cả chương" : "Tải tất cả chương"; update.hidden = !complete; del.hidden = !(stored || resumable); update.disabled = del.disabled = busy; };
    let stored = await count(), resume = Boolean(await get("books", id)); controls(stored, false, resume); if (stored || resume) status.textContent = stored === chapters.length ? `✓ Đã tải offline · ${stored} / ${chapters.length} chương` : `Đã tải ${stored} / ${chapters.length} chương. Có thể tải tiếp.`;
    const downloadMissing = async (updateOnly = false) => {
      const previousList = await get("chapterLists", id);
      const knownChapterIds = new Set((previousList?.chapters || []).map(chapter => String(chapter.chapterId)));
      try { await loadAllChapters(); } catch (error) { status.textContent = "Không tải được mục lục. Có thể thử lại sau."; return; }
      const local = await Promise.all(chapters.map(storedChapter)), pending = updateOnly ? chapters.filter(chapter => !knownChapterIds.has(String(chapter.chapterId))) : chapters.filter((chapter, index) => !local[index]);
      await put("chapterLists", { bookId: id, chapters, updatedAt: Date.now() }); await put("books", { id, title, bookUrl: location.pathname, totalChapters: chapters.length, downloadedChapters: stored, updatedAt: Date.now(), status: "downloading" });
      if (!pending.length) { controls(stored); status.textContent = updateOnly ? "Bản offline đã có chương mới nhất." : status.textContent; return; }
      let cursor = 0, failed = false; resume = true; controls(stored, true, resume); const show = () => { status.textContent = `Đang tải ${stored} / ${chapters.length} chương`; controls(stored, true, resume); };
      await Promise.all(Array.from({ length: Math.min(4, pending.length) }, async () => { while (cursor < pending.length) { const chapter = pending[cursor++]; if (await fetchChapter(chapter)) { stored += 1; show(); } else failed = true; } }));
      await put("books", { id, title, bookUrl: location.pathname, totalChapters: chapters.length, downloadedChapters: stored, updatedAt: Date.now(), status: stored === chapters.length ? "downloaded" : "partial" });
      resume = stored !== chapters.length; controls(stored, false, resume); status.textContent = stored === chapters.length ? `✓ Đã tải offline · ${stored} / ${chapters.length} chương` : `Đã tải ${stored} / ${chapters.length} chương.${failed ? " Tải thất bại. Có thể tiếp tục sau." : ""}`;
    };
    download.onclick = () => downloadMissing(false);
    update.onclick = () => downloadMissing(true);
    del.onclick = async () => { await removeBookChapters(id); await Promise.all([remove("chapterLists", id), remove("books", id)]); stored = 0; resume = false; controls(stored); status.textContent = "Đã xóa bản offline."; };
  }

  const currentOfflineOwner = () => localStorage.getItem("novel-reader-offline-owner") || "";
  const offlineEbookUrl = slug => `/ebooks/${encodeURIComponent(slug)}`;
  async function setupEbookDownload() {
    const button = document.querySelector("[data-offline-ebook-save]"); if (!button || !indexedDB) return;
    const status = document.querySelector("[data-offline-ebook-status]");
    const deleteButton=document.querySelector("[data-offline-ebook-delete]");
    const manifest = [...document.querySelectorAll("[data-offline-ebook-chapter]")].map(row => ({ number: Number(row.dataset.number), title: row.dataset.title, url: row.dataset.url }));
    const ownerId = button.dataset.ownerId || "";
    localStorage.setItem("novel-reader-offline-owner", ownerId);
    const bookId = button.dataset.bookId, slug = button.dataset.slug;
    let busy = false;
    const existing = await get("ebooks", bookId);
    if (existing) {deleteButton.hidden=false;status.textContent = existing.status === "ready" ? "Đã lưu trên thiết bị · có thể đọc khi offline." : `Đã lưu ${existing.savedChapters||0}/${existing.totalChapters} chương.`;}
    deleteButton?.addEventListener("click",async()=>{if(busy)return;busy=true;deleteButton.disabled=true;try{for(const chapter of await ebookChapters(bookId))await remove("ebookChapters",chapter.id);await remove("ebooks",bookId);status.textContent="Đã xóa dữ liệu offline khỏi thiết bị.";deleteButton.hidden=true;button.textContent="Lưu trên thiết bị";}finally{busy=false;deleteButton.disabled=false;}});
    button.addEventListener("click", async () => {
      if (busy) return;
      busy = true; button.disabled = true;
      try {
        if (!manifest.length) throw new Error("Ebook chưa có danh sách chương để lưu.");
        const record = { id: bookId, ownerId, slug, title: button.dataset.title, author: button.dataset.author, format: button.dataset.format, bookUrl: offlineEbookUrl(slug), totalChapters: manifest.length, status: "downloading", updatedAt: Date.now() };
        await put("ebooks", record);
        const saved = new Set((await ebookChapters(bookId)).map(chapter => chapter.chapterNumber));
        let completed = saved.size, cursor = 0, failures = 0, quotaHit = false;
        status.textContent = `Đang lưu ${completed}/${manifest.length} chương…`;
        await Promise.all(Array.from({length: 3}, async () => {
          while (cursor < manifest.length) {
            const item = manifest[cursor++]; if (saved.has(item.number)) continue;
            try {
              const response = await fetch(item.url, {credentials: "same-origin", cache: "no-store"});
              if (!response.ok) throw new Error(`HTTP ${response.status}`);
              const page = new DOMParser().parseFromString(await response.text(), "text/html");
              const content = page.querySelector("#ebook-reader-content .reader-prose")?.innerText?.trim();
              if (!content) throw new Error("empty chapter");
              const title = page.querySelector("#ebook-reader-content h2")?.innerText?.trim() || item.title;
              await saveEbookChapter({id: `${bookId}:${item.number}`, bookId, ownerId, chapterNumber: item.number, title, content, url: item.url, savedAt: Date.now()});
              completed += 1;
            } catch (error) { failures += 1; if(error?.name==="QuotaExceededError")quotaHit=true; console.warn("Offline Ebook chapter download failed", item.number, error); }
            if(quotaHit){cursor=manifest.length;status.textContent="Thiết bị đã hết dung lượng lưu offline. Hãy xóa dữ liệu offline khác rồi thử tiếp.";break;}
            status.textContent = `Đang lưu ${completed}/${manifest.length} chương…`;
          }
        }));
        record.status = completed === manifest.length ? "ready" : "partial";
        record.savedChapters = completed; record.updatedAt = Date.now(); await put("ebooks", record);
        status.textContent = record.status === "ready" ? `Đã lưu đủ ${completed} chương trên thiết bị.` : quotaHit ? `Đã lưu ${completed}/${manifest.length} chương; thiết bị hết dung lượng. Có thể tiếp tục sau khi giải phóng bộ nhớ.` : `Đã lưu ${completed}/${manifest.length} chương. ${failures} chương chưa tải được; có thể bấm để tiếp tục.`;
        button.textContent = record.status === "ready" ? "Đã lưu offline" : "Tải tiếp để đọc offline";
      } catch (error) { status.textContent = error.message || "Không thể lưu Ebook trên thiết bị."; }
      finally { busy = false; button.disabled = false; }
    });
  }

  async function renderOfflineEbookLibrary() {
    const ownerId = currentOfflineOwner();
    const books = (await all("ebooks")).filter(book => ownerId && book.ownerId === ownerId);
    const items = books.map(book => `<a href="${escape(book.bookUrl)}" style="display:block;padding:1rem;border:1px solid #343a40;border-radius:.75rem;background:#22262a;color:#f4f5f5;text-decoration:none"><strong>${escape(book.title)}</strong><span style="display:block;color:#a9afb7;margin-top:.35rem">${Number(book.savedChapters || 0)}/${Number(book.totalChapters || 0)} chương đã lưu</span></a>`).join("");
    document.body.innerHTML = `<main style="max-width:64rem;margin:0 auto;padding:2rem 1rem;font:16px system-ui;color:#f4f5f5;background:#1b1e22;min-height:100vh"><p style="color:#35aaa0">Đang ngoại tuyến</p><h1>Thư viện Ebook</h1><p style="color:#a9afb7">Ebook đã lưu trên thiết bị này.</p><section style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem">${items || '<p style="color:#a9afb7">Chưa có Ebook offline. Khi có mạng, mở Ebook và chọn “Lưu trên thiết bị”.</p>'}</section></main>`;
  }
  async function renderOfflineEbookDetail() {
    const ownerId = currentOfflineOwner(), slug = decodeURIComponent(location.pathname.split("/")[2] || "");
    const book = (await all("ebooks")).find(item => item.slug === slug && ownerId && item.ownerId === ownerId);
    if (!book) return renderOfflineEbookLibrary();
    const chapters = (await ebookChapters(book.id)).sort((a,b) => a.chapterNumber-b.chapterNumber);
    const rows = chapters.map(item => `<li><a href="${escape(item.url)}" style="color:#35aaa0">${item.chapterNumber}. ${escape(item.title)}</a></li>`).join("");
    document.body.innerHTML = `<main style="max-width:52rem;margin:0 auto;padding:2rem 1rem;font:16px system-ui;color:#f4f5f5;background:#1b1e22;min-height:100vh"><a href="/ebooks" style="color:#35aaa0">← Thư viện Ebook</a><p style="color:#35aaa0;margin-top:2rem">Đang ngoại tuyến</p><h1>${escape(book.title)}</h1><p style="color:#a9afb7">${chapters.length}/${book.totalChapters} chương đã lưu</p><ol style="line-height:2">${rows}</ol></main>`;
  }
  async function renderOfflineEbookReader() {
    const ownerId = currentOfflineOwner(), match = location.pathname.match(/^\/ebooks\/([^/]+)\/read\/(\d+)/);
    const slug = match?.[1] ? decodeURIComponent(match[1]) : "", number = Number(match?.[2]);
    const book = (await all("ebooks")).find(item => item.slug === slug && ownerId && item.ownerId === ownerId);
    const chapter = book && await get("ebookChapters", `${book.id}:${number}`);
    if (!chapter) return renderOfflineEbookLibrary();
    const html = chapter.content.split(/\n+/).filter(Boolean).map(line => `<p>${escape(line)}</p>`).join("");
    document.body.innerHTML = `<main style="max-width:48rem;margin:0 auto;padding:1.5rem 1rem 8rem;font:18px/1.85 Georgia,serif;color:#deddd2;background:#1d211f;min-height:100vh"><a href="${escape(book.bookUrl)}" style="color:#35aaa0">← ${escape(book.title)}</a><p style="color:#a9afb7;font:13px system-ui;margin-top:1.5rem">Đang ngoại tuyến · Ebook đã lưu trên thiết bị</p><h1 style="font:700 1.7rem/1.4 system-ui">${escape(chapter.title)}</h1><article data-offline-tts-text>${html}</article><div style="position:sticky;bottom:1rem;padding:1rem;background:#292d32;border-radius:1rem;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.6rem;align-items:center"><select data-offline-tts-voice aria-label="Giọng đọc thiết bị" style="grid-column:1/-1;min-height:2.5rem"></select><label>Tốc độ <input data-offline-tts-rate type="range" min=".5" max="2" step=".05" value="1"></label><label>Cao độ <input data-offline-tts-pitch type="range" min=".5" max="1.5" step=".05" value="1"></label><button data-offline-tts-play aria-label="Đọc bằng giọng thiết bị">▶ Đọc / Tiếp tục</button><button data-offline-tts-pause aria-label="Tạm dừng">Ⅱ Tạm dừng</button><button data-offline-tts-stop aria-label="Dừng đọc">■ Dừng</button><span data-offline-tts-status role="status" aria-live="polite">Thiết bị</span></div></main>`;
    document.body.innerHTML = `<main style="max-width:48rem;margin:0 auto;padding:1.5rem 1rem 8rem;font:18px/1.85 Georgia,serif;color:#deddd2;background:#1d211f;min-height:100vh"><a href="${escape(book.bookUrl)}" style="color:#35aaa0">← ${escape(book.title)}</a><p style="color:#a9afb7;font:13px system-ui;margin-top:1.5rem">Đang ngoại tuyến · Ebook đã lưu trên thiết bị</p><h1 style="font:700 1.7rem/1.4 system-ui">${escape(chapter.title)}</h1><article data-offline-tts-text>${html}</article><div style="position:sticky;bottom:1rem;padding:1rem;background:#292d32;border-radius:1rem;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.6rem;align-items:center"><select data-offline-tts-voice aria-label="Giọng đọc thiết bị" style="grid-column:1/-1;min-height:2.5rem"></select><label>Tốc độ <input data-offline-tts-rate type="range" min=".5" max="2" step=".05" value="1"></label><label>Cao độ <input data-offline-tts-pitch type="range" min=".5" max="1.5" step=".05" value="1"></label><button data-offline-tts-play aria-label="Đọc bằng giọng thiết bị">▶ Đọc / Tiếp tục</button><button data-offline-tts-pause aria-label="Tạm dừng">Ⅱ Tạm dừng</button><button data-offline-tts-stop aria-label="Dừng đọc">■ Dừng</button><span data-offline-tts-status role="status" aria-live="polite">Thiết bị</span></div></main>`;
    const progressKey=`reader.${ownerId}.reader.ebook.position.${book.id}.${number}`;
    const lastUrlKey=`reader.${ownerId}.reader.lastUrl.book.${book.id}`;
    localStorage.setItem(lastUrlKey,location.pathname+location.search);localStorage.setItem(`reader.${ownerId}.reader.lastReadAt.book.${book.id}`,String(Date.now()));
    try{const saved=JSON.parse(localStorage.getItem(progressKey)||"null");if(saved?.percent)requestAnimationFrame(()=>scrollTo(0,Math.max(0,(document.documentElement.scrollHeight-innerHeight)*saved.percent/100)));}catch{}
    let saveTimer;const savePosition=()=>{const percent=Math.max(0,Math.min(100,Math.round(scrollY/Math.max(1,document.documentElement.scrollHeight-innerHeight)*100)));localStorage.setItem(progressKey,JSON.stringify({chapter:number,percent,savedAt:Date.now()}));};
    addEventListener("scroll",()=>{clearTimeout(saveTimer);saveTimer=setTimeout(savePosition,500);},{passive:true});addEventListener("pagehide",savePosition,{once:true});
    const provider = window.NovelTTS?.createDeviceProvider();
    const chunks = [...document.querySelectorAll("[data-offline-tts-text] p")].map(node => ({text: node.innerText.trim(), node})).filter(item => item.text);
    let index = 0, rate = 1, volume = 1, pitch = 1;
    const status = document.querySelector("[data-offline-tts-status]");
    const voiceSelect=document.querySelector("[data-offline-tts-voice]");
    provider?.getVoices().then(voices=>{const local=voices.filter(voice=>voice.lang?.toLowerCase().startsWith("vi"));voiceSelect.replaceChildren(...local.map(voice=>{const option=document.createElement("option");option.value=voice.voiceURI;option.textContent=`${voice.name} · ${voice.lang} · ${voice.localService?"Local":"Network/không rõ"}`;return option;}));if(!local.length)status.textContent="Không có giọng tiếng Việt trên thiết bị.";});
    const speak = () => { if (!provider || !chunks.length) { status.textContent = "Thiết bị chưa hỗ trợ TTS."; return; } if (index >= chunks.length) index = 0; const current = chunks[index]; current.node.scrollIntoView({block:"center",behavior:"smooth"}); provider.speak(current.text,{lang:"vi-VN",voiceURI:voiceSelect.value||undefined,rate:Number(document.querySelector("[data-offline-tts-rate]").value)||rate,pitch:Number(document.querySelector("[data-offline-tts-pitch]").value)||pitch,volume,onstart:()=>status.textContent=`Đang đọc ${index+1}/${chunks.length}`,onend:()=>{index++;if(index<chunks.length)speak();else status.textContent="Đã đọc xong";},onerror:error=>status.textContent=error.message||"Lỗi đọc"}); };
    document.querySelector("[data-offline-tts-play]").onclick = () => provider?.isSpeaking() ? provider.resume() : speak();
    document.querySelector("[data-offline-tts-pause]").onclick = () => {provider?.pause();status.textContent="Đã tạm dừng";};
    document.querySelector("[data-offline-tts-stop]").onclick = () => { provider?.stop(); index=0; status.textContent="Đã dừng"; };
    window.addEventListener("pagehide", () => provider?.stop(), {once:true});
  }

  const createDeviceProvider = () => {
    const synth = window.speechSynthesis; let active = null, generation = 0, speaking = false;
    const supported = () => Boolean(synth && window.SpeechSynthesisUtterance);
    const getVoices = async () => {
      if (!supported()) return [];
      let voices = synth.getVoices(); if (voices.length) return voices;
      await new Promise(resolve => { const timer = setTimeout(resolve, 700); synth.addEventListener?.("voiceschanged", () => { clearTimeout(timer); resolve(); }, {once:true}); });
      return synth.getVoices();
    };
    return {isSupported:supported, isSpeaking:()=>speaking, getVoices, speak(text, options={}) { if(!supported()) { options.onerror?.(new Error("TTS không khả dụng trên trình duyệt này.")); return false; } if(!String(text||"").trim()) return false; const voices=synth.getVoices(); const voice=voices.find(item=>item.voiceURI===options.voiceURI)||(!options.voiceURI&&voices.find(item=>item.lang?.toLowerCase().startsWith("vi"))); if(!voice||!voice.lang?.toLowerCase().startsWith("vi")){options.onerror?.(new Error("Không tìm thấy giọng đọc tiếng Việt trên thiết bị."));return false;} synth.cancel(); const token=++generation, utterance=new SpeechSynthesisUtterance(String(text)); active=utterance; utterance.voice=voice; utterance.lang=voice.lang; utterance.rate=Math.max(.5,Math.min(2,Number(options.rate)||1)); utterance.pitch=Math.max(.5,Math.min(2,Number(options.pitch)||1)); utterance.volume=Math.max(0,Math.min(1,Number(options.volume??1))); utterance.onstart=event=>{if(token===generation){speaking=true;options.onstart?.(event);}}; utterance.onboundary=event=>{if(token===generation)options.onboundary?.(event);}; utterance.onend=event=>{if(token===generation){speaking=false;active=null;options.onend?.(event);}}; utterance.onerror=event=>{if(token===generation){speaking=false;active=null;if(event.error!=="canceled"&&event.error!=="interrupted")options.onerror?.(new Error(event.error||"Lỗi TTS thiết bị."));}}; synth.speak(utterance); return true; }, pause(){if(supported()){synth.pause();speaking=false;}}, resume(){if(supported()){synth.resume();speaking=true;}}, stop(){generation++;speaking=false;active=null;if(supported())synth.cancel();} };
  };
  const createManager = ({device, server, initialMode="server"}={}) => {
    let mode = initialMode === "device" ? "device" : "server";
    const active = () => mode === "device" ? device : server;
    return {get mode(){return mode;},setMode(value){if(value!=="device"&&value!=="server")throw new Error("TTS mode không hợp lệ");if(mode!==value){device?.stop?.();server?.stop?.();mode=value;}},isSupported(){return Boolean(active()?.isSupported?.() ?? active());},getVoices(){return device?.getVoices?.()||Promise.resolve([]);},speak(...args){return active()?.speak?.(...args);},pause(){return active()?.pause?.();},resume(){return active()?.resume?.();},stop(){device?.stop?.();server?.stop?.();}};
  };
  document.addEventListener("DOMContentLoaded", () => { const book = document.querySelector("[data-offline-book]"); if (book && "indexedDB" in window) setupBook(book).catch(error => console.warn("Offline storage unavailable", error)); setupEbookDownload().catch(error => console.warn("Offline Ebook storage unavailable", error)); });
  window.NovelOffline = { renderOfflineReader, renderOfflineBook, renderOfflineHome, renderOfflineEbookLibrary, renderOfflineEbookDetail, renderOfflineEbookReader };
  window.NovelTTS = { createDeviceProvider, createManager };
})();
