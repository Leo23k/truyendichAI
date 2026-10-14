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
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const chapterId = link => link.dataset.chapterId || link.href.split("?")[0].split("/").pop();
  const storedChapter = chapter => get("chapters", chapter.id).then(found => found?.content ? found : get("chapters", chapter.chapterId)).then(found => found?.content ? found : undefined);
  const ownerId = () => localStorage.getItem("novel-reader-offline-owner") || "";

  const styles = `
    :root{color-scheme:dark;--off-bg:#1b1e22;--off-card:#252a30;--off-card2:#2b3036;--off-border:#343a40;--off-muted:#a9afb7;--off-accent:#D6A84F;--off-hover:#E3B963;--off-pressed:#B98A35}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--off-bg);color:#f4f5f5;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}button,input,select{font:inherit}a{color:inherit;text-decoration:none}button{color:inherit;cursor:pointer}
    .off-shell{max-width:1240px;min-height:100vh;margin:auto;padding:clamp(16px,3vw,36px) clamp(14px,3vw,30px) 110px}.off-app-header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:-4px 0 18px;padding-bottom:14px;border-bottom:1px solid var(--off-border)}.off-brand{display:inline-flex;align-items:center;gap:9px;font-size:1.05rem}.off-brand-mark{display:grid;place-items:center;width:30px;height:30px;border-radius:9px;background:var(--off-accent);color:#24201a;font-weight:850}.off-online-badge{color:var(--off-accent);font-size:.7rem;font-weight:750;letter-spacing:.06em}.off-top{display:flex;align-items:center;gap:12px;margin-bottom:20px}.off-top a,.off-btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:42px;padding:9px 14px;border:1px solid var(--off-border);border-radius:10px;background:var(--off-card);font-weight:650}.off-top a:hover,.off-btn:hover{border-color:var(--off-accent)}.off-kicker{color:var(--off-accent);font-size:.82rem;font-weight:700;letter-spacing:.04em}.off-title{margin:8px 0;font-size:clamp(1.55rem,4vw,2.35rem);line-height:1.2}.off-muted{color:var(--off-muted)}
    .off-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.off-card{min-width:0;overflow:hidden;border:1px solid var(--off-border);border-radius:12px;background:var(--off-card);transition:transform .16s,border-color .16s}.off-card:hover{transform:translateY(-2px);border-color:var(--off-accent)}.off-cover{display:block;position:relative;aspect-ratio:2/3;background:#30353b;overflow:hidden}.off-cover img{position:relative;z-index:1;width:100%;height:100%;object-fit:cover;display:block}.off-cover-fallback{position:absolute;inset:0;display:grid;place-items:center;color:var(--off-muted);font-size:.92rem}.off-card-body{padding:12px}.off-card-title{font-weight:750;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.off-card-meta{margin-top:5px;color:var(--off-muted);font-size:.86rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.off-progress{height:5px;margin-top:10px;border-radius:99px;background:#3a4046;overflow:hidden}.off-progress>i{display:block;height:100%;background:var(--off-accent)}.off-count{margin-top:7px;color:var(--off-muted);font-size:.78rem}.off-empty{grid-column:1/-1;padding:26px 18px;border:1px dashed var(--off-border);border-radius:12px;color:var(--off-muted);text-align:center}
    .off-hero{display:grid;grid-template-columns:minmax(130px,210px) 1fr;gap:clamp(18px,4vw,36px);align-items:center;padding:20px;border:1px solid var(--off-border);border-radius:16px;background:var(--off-card)}.off-hero .off-cover{border-radius:10px}.off-hero-copy{min-width:0}.off-actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:16px}.off-primary{background:var(--off-accent);border-color:var(--off-accent);color:#24201a}.off-primary:hover{background:var(--off-hover);border-color:var(--off-hover)}.off-detail-tools{display:grid;grid-template-columns:1fr auto;gap:10px;margin:24px 0 12px}.off-input,.off-select{width:100%;min-height:44px;padding:9px 12px;border:1px solid var(--off-border);border-radius:10px;background:var(--off-card);color:inherit}.off-input:focus,.off-select:focus{outline:2px solid rgba(214,168,79,.35);border-color:var(--off-accent)}.off-chapters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.off-chapter{padding:11px 13px;border:1px solid var(--off-border);border-radius:9px;background:var(--off-card);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.off-chapter:hover,.off-chapter.current{border-color:var(--off-accent);color:var(--off-accent)}
    .off-reader{max-width:900px;margin:0 auto;padding:82px 18px 190px}.off-reader-content{max-width:760px;margin:30px auto 0;font:clamp(18px,2vw,21px)/1.95 Georgia,"Noto Serif",serif;color:#deddd2;overflow-wrap:anywhere}.off-reader-content p{margin:0 0 1.25em;white-space:pre-wrap}.off-reader-content p.off-active{background:rgba(214,168,79,.1);border-left:3px solid var(--off-accent);padding:8px 12px;margin-left:-15px;border-radius:4px}.off-reader-title{font-size:clamp(1.35rem,3vw,1.8rem);line-height:1.4;margin:18px 0}.off-reader-toolbar{position:fixed;z-index:20;top:0;left:0;right:0;display:flex;align-items:center;gap:10px;padding:10px max(14px,calc((100vw - 900px)/2));background:rgba(27,30,34,.94);border-bottom:1px solid var(--off-border);backdrop-filter:blur(12px);transition:transform .2s}.off-reader-toolbar.is-hidden{transform:translateY(-110%)}.off-toolbar-name{min-width:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:.9rem}.off-progress-chip{position:fixed;z-index:21;top:10px;right:12px;padding:5px 9px;border:1px solid var(--off-border);border-radius:99px;background:var(--off-card);font-size:.78rem;font-variant-numeric:tabular-nums}.off-reader-toolbar .off-progress-chip{position:static;flex:none}.off-reader-player{position:fixed;z-index:22;left:50%;bottom:max(10px,env(safe-area-inset-bottom));transform:translateX(-50%);width:min(700px,calc(100% - 20px));padding:12px 14px;border:1px solid var(--off-border);border-radius:16px;background:rgba(37,42,48,.97);box-shadow:0 12px 38px #0008;backdrop-filter:blur(16px)}.off-player-top{display:flex;align-items:center;gap:8px}.off-player-title{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:.85rem}.off-player-state{color:var(--off-muted);font-size:.75rem}.off-player-state.playing{color:var(--off-accent)}.off-player-seek{width:100%;height:5px;margin:11px 0;accent-color:var(--off-accent)}.off-player-controls{display:flex;align-items:center;justify-content:center;gap:8px}.off-icon-btn{display:grid;place-items:center;width:40px;height:38px;border:1px solid transparent;border-radius:10px;background:transparent}.off-icon-btn:hover,.off-icon-btn[aria-pressed=true]{background:#393d42;border-color:var(--off-border);color:var(--off-accent)}.off-play{width:46px;height:42px;background:var(--off-accent);color:#24201a}.off-play:hover{background:var(--off-hover);color:#24201a}.off-player-extras{display:flex;justify-content:center;align-items:center;flex-wrap:wrap;gap:8px;margin-top:9px}.off-player-extras select,.off-player-extras button{min-height:32px;padding:4px 8px;border:1px solid var(--off-border);border-radius:8px;background:var(--off-card2);color:inherit;font-size:.78rem}.off-player-extras select{max-width:210px}.off-sheet{position:fixed;z-index:40;inset:0;display:grid;align-items:end;background:#0009}.off-sheet[hidden]{display:none}.off-sheet-panel{max-height:min(78vh,700px);overflow:auto;padding:18px;border:1px solid var(--off-border);border-radius:18px 18px 0 0;background:var(--off-card)}.off-sheet-head{display:flex;justify-content:space-between;align-items:center;position:sticky;top:-18px;padding:4px 0 12px;background:var(--off-card)}.off-sheet-list{display:grid;gap:7px}.off-sheet-item{text-align:left;padding:10px 12px;border:1px solid var(--off-border);border-radius:9px;background:var(--off-bg);color:inherit}.off-sheet-item.current{border-color:var(--off-accent);color:var(--off-accent)}.off-reader-tools{display:flex;gap:7px}.off-reader-tools .off-btn{min-height:36px;padding:6px 10px}
    @media(min-width:600px){.off-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.off-card-body{padding:14px}.off-chapters{grid-template-columns:repeat(3,minmax(0,1fr))}}
    @media(min-width:900px){.off-grid{grid-template-columns:repeat(5,minmax(0,1fr))}}
    @media(max-width:520px){.off-shell{padding-top:18px}.off-hero{grid-template-columns:104px 1fr;gap:14px;padding:13px}.off-actions .off-btn{flex:1;min-width:120px}.off-detail-tools{grid-template-columns:1fr}.off-chapters{grid-template-columns:1fr}.off-reader{padding:68px 18px 184px}.off-reader-content{font-size:19px;line-height:1.9}.off-reader-toolbar{gap:7px;padding:9px 10px}.off-reader-tools .off-btn{font-size:.78rem;padding:5px 8px}.off-progress-chip{right:10px}.off-reader-toolbar .off-progress-chip{display:none}.off-player-extras select{max-width:145px}.off-reader-player{padding:10px}.off-player-controls{gap:3px}}
    .off-reader-player{width:min(760px,calc(100% - 24px));padding:13px 16px}.off-player-top{min-height:24px}.off-player-title{font-size:.9rem;font-weight:650}.off-player-state{font-size:.68rem;white-space:nowrap}.off-player-seek{display:block;height:4px;margin:9px 0 10px;cursor:pointer}.off-player-controls{justify-content:flex-start;gap:7px}.off-player-controls .off-player-actions{display:flex;align-items:center;gap:7px;margin-left:auto}.off-player-actions .off-muted{display:flex;align-items:center;gap:6px;font-size:.72rem}.off-player-actions select{max-width:170px}.off-player-actions [data-voice-trigger]{min-height:36px}.off-player-actions [data-stop]{width:36px;height:36px;border-color:var(--off-border);background:var(--off-card2)}.off-player-state.error{color:#e68a8a}.off-player-state.playing{font-weight:650}.off-voice-sheet-label{display:grid;gap:7px;margin:10px 0;color:var(--off-muted);font-size:.86rem}.off-voice-sheet-label select{width:100%;min-height:44px;padding:8px 10px;border:1px solid var(--off-border);border-radius:9px;background:var(--off-bg);color:inherit}.off-voice-note{margin:8px 0 0;color:var(--off-muted);font-size:.78rem}.off-sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
    @media(max-width:520px){.off-reader-player{width:calc(100% - 16px);padding:10px 12px;border-radius:14px}.off-player-top{gap:6px}.off-player-title{font-size:.82rem}.off-player-state{max-width:42%;overflow:hidden;text-overflow:ellipsis;font-size:.66rem}.off-player-seek{margin:8px 0}.off-player-controls{display:grid;grid-template-columns:40px 46px 40px;justify-content:center;gap:5px}.off-player-controls>[data-prev-segment]{grid-column:1;grid-row:1}.off-player-controls>[data-play]{grid-column:2;grid-row:1}.off-player-controls>[data-next-segment]{grid-column:3;grid-row:1}.off-player-controls .off-player-actions{grid-column:1/-1;grid-row:2;gap:6px;justify-content:space-between;min-width:0;width:100%;margin-top:3px}.off-player-actions [data-voice-trigger]{min-height:34px;padding:5px 8px;font-size:.74rem}.off-player-actions label{font-size:0!important}.off-player-actions label select{max-width:74px;min-height:34px;padding:4px;font-size:.72rem}.off-player-actions [data-sleep]{width:76px;max-width:76px;min-height:34px;padding:3px;font-size:.68rem}.off-player-actions [data-stop]{width:34px;height:34px}}
    @media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}
  `;
  function shell(content, title = "Leo Truyện · Offline") {
    document.title = title;
    document.head.insertAdjacentHTML("beforeend", `<style id="offline-ui-styles">${styles}</style>`);
    document.body.innerHTML = content;
  }
  function cover(url, title, cls = "") {
    const image = url ? `<img src="${escape(url)}" alt="Bìa ${escape(title)}" loading="eager" onerror="this.remove()">` : "";
    return `<div class="off-cover ${cls}">${image}<div class="off-cover-fallback">${url ? "Không tải được bìa" : "Chưa có bìa"}</div></div>`;
  }
  function card(book, { ebook = false } = {}) {
    const saved = Number(ebook ? book.savedChapters : book.downloadedChapters) || 0;
    const total = Number(book.totalChapters) || 0;
    const percent = total ? Math.min(100, Math.round(saved / total * 100)) : 0;
    const author = book.author || "";
    return `<a class="off-card" href="${escape(book.bookUrl || (ebook ? `/ebooks/${book.slug}` : "/"))}">${cover(book.coverUrl, book.title)}<div class="off-card-body"><div class="off-card-title">${escape(book.title)}</div>${author ? `<div class="off-card-meta">${escape(author)}</div>` : ""}<div class="off-progress"><i style="width:${percent}%"></i></div><div class="off-count">${saved}/${total} chương đã lưu offline</div></div></a>`;
  }
  function empty(message) { return `<div class="off-empty">${escape(message)}</div>`; }
  function topLink(href, label) { return `<header class="off-app-header"><a class="off-brand" href="/"><span class="off-brand-mark">L</span><strong>Leo Truyện</strong></a><span class="off-online-badge">ĐANG NGOẠI TUYẾN</span></header><div class="off-top"><a href="${escape(href)}">← ${escape(label)}</a></div>`; }
  function fallbackPage() { shell(`<main class="off-shell"><h1 class="off-title">Không có bản offline</h1><p class="off-muted">Nội dung này chưa được tải xuống thiết bị.</p><a class="off-btn off-primary" href="/">Về thư viện offline</a></main>`); }

  async function renderOfflineHome() {
    const novels = await all("books"), account = ownerId();
    const ebooks = (await all("ebooks")).filter(book => !account || book.ownerId === account);
    const cards = [...novels.map(book => card(book)), ...ebooks.map(book => card(book, { ebook: true }))];
    shell(`<main class="off-shell">${topLink("/", "Thư viện offline")}<h1 class="off-title">Thư viện của bạn</h1><p class="off-muted">Các truyện và Ebook đã lưu trên thiết bị này.</p><section class="off-grid">${cards.join("") || empty("Chưa có truyện nào được lưu offline. Khi có mạng, mở truyện và chọn tải về thiết bị.")}</section></main>`);
  }
  async function renderOfflineEbookLibrary() {
    const account = ownerId(), books = (await all("ebooks")).filter(book => account && book.ownerId === account);
    const cards = books.map(book => card(book, { ebook: true })).join("");
    shell(`<main class="off-shell">${topLink("/", "Thư viện offline")}<h1 class="off-title">Ebook đã lưu</h1><p class="off-muted">${books.length} Ebook trên thiết bị này</p><section class="off-grid">${cards || empty("Chưa có Ebook offline trên thiết bị này.")}</section></main>`);
  }

  async function renderOfflineBook() {
    const book = (await all("books")).find(item => item.bookUrl === location.pathname);
    if (!book) return fallbackPage();
    const entries = (await get("chapterLists", book.id))?.chapters || [];
    const local = (await Promise.all(entries.map(async item => ({ item, chapter: await storedChapter(item) })))).filter(row => row.chapter);
    const rows = local.map(({ item }, index) => `<a class="off-chapter" href="${escape(item.url)}" data-chapter-search="${escape(`${index + 1} ${item.title}`.toLowerCase())}">${index + 1}. ${escape(item.title)}</a>`).join("");
    shell(`<main class="off-shell">${topLink("/", "Thư viện offline")}<section class="off-hero">${cover(book.coverUrl, book.title)}<div class="off-hero-copy"><span class="off-kicker">TRUYỆN ĐÃ TẢI</span><h1 class="off-title">${escape(book.title)}</h1><p class="off-muted">${local.length}/${book.totalChapters || entries.length} chương có thể đọc offline</p><div class="off-actions">${local[0] ? `<a class="off-btn off-primary" href="${escape(local[0].item.url)}">▶ Đọc truyện</a>` : ""}</div></div></section><div class="off-detail-tools"><input class="off-input" type="search" placeholder="Tìm chương đã lưu…" aria-label="Tìm chương"><select class="off-select" aria-label="Sắp xếp chương"><option value="asc">Cũ nhất trước</option><option value="desc">Mới nhất trước</option></select></div><div class="off-muted" data-chapter-count>${local.length} chương đã tải</div><section class="off-chapters" style="margin-top:10px">${rows || empty("Chưa có chương nào tải xong trên thiết bị.")}</section></main>`);
    const list = document.querySelector(".off-chapters"), search = document.querySelector('.off-detail-tools input'), sort = document.querySelector(".off-detail-tools select");
    const filter = () => { const q = search.value.trim().toLocaleLowerCase("vi"); [...list.querySelectorAll("[data-chapter-search]")].forEach(row => { row.hidden = !row.dataset.chapterSearch.includes(q); }); };
    search.addEventListener("input", filter);
    sort.addEventListener("change", () => { [...list.querySelectorAll(".off-chapter")].reverse().forEach(row => list.append(row)); });
  }

  function progressIdentity(owner, book, number, type) {
    return { position: `reader.${owner || "anonymous"}.reader.${type}.position.${book.id}.${number}`, last: `reader.${owner || "anonymous"}.reader.lastUrl.book.${book.id}` };
  }
  async function renderReader({ book, chapter, entries, index, owner, ebook }) {
    const chapterUrl = item => item.url;
    const body = chapter.content.split(/\n+/).filter(line => line.trim()).map(line => `<p>${escape(line)}</p>`).join("");
    const identity = progressIdentity(owner, book, ebook ? chapter.chapterNumber : chapter.chapterId || chapter.id, ebook ? "ebook" : "chapter");
    let savedPercent = 0; try { savedPercent = Number(JSON.parse(localStorage.getItem(identity.position) || "{}")?.percent) || 0; } catch { /* Ignore malformed legacy progress. */ }
    const percent = Math.max(0, Math.min(100, savedPercent));
    const chapterLabel = ebook ? `Chương ${chapter.chapterNumber}` : chapter.title;
    const prev = entries[index - 1], next = entries[index + 1];
    const chapterLink = (item, label) => item ? `<a class="off-btn" href="${escape(chapterUrl(item))}">${label}</a>` : "";
    const tocItems = entries.map((item, i) => `<a class="off-sheet-item ${i === index ? "current" : ""}" href="${escape(chapterUrl(item))}">${escape(ebook ? `${item.chapterNumber}. ${item.title}` : `${i + 1}. ${item.title}`)}</a>`).join("");
    shell(`<header class="off-reader-toolbar"><a class="off-btn" href="${escape(book.bookUrl)}" aria-label="Quay lại truyện">← <span class="off-toolbar-name">${escape(book.title)}</span></a><div class="off-reader-tools"><button class="off-btn" data-open-toc>Mục lục</button><button class="off-btn" data-font-down aria-label="Giảm cỡ chữ">A−</button><button class="off-btn" data-font-up aria-label="Tăng cỡ chữ">A+</button></div><span class="off-progress-chip" data-progress>0%</span></header><span class="off-progress-chip" data-progress-mobile>${percent}%</span><main class="off-reader"><div class="off-kicker">${ebook ? "EBOOK" : "TRUYỆN"} · ĐÃ LƯU OFFLINE</div><h1 class="off-reader-title">${escape(chapter.title || chapterLabel)}</h1><div class="off-muted">${escape(book.title)} · ${escape(chapterLabel)}</div><article class="off-reader-content" data-reader-text>${body}</article><nav class="off-actions" style="justify-content:space-between">${chapterLink(prev, "← Chương trước")}${chapterLink(next, "Chương sau →")}</nav></main><section class="off-reader-player" aria-label="Trình phát đọc bằng giọng nói"><div class="off-player-top"><div class="off-player-title">${escape(chapter.title || chapterLabel)}</div><span class="off-player-state" data-tts-status role="status" aria-live="polite">Sẵn sàng</span></div><input class="off-player-seek" type="range" min="0" max="${Math.max(0, document.querySelectorAll?.(".off-reader-content p").length - 1 || 0)}" value="0" aria-label="Vị trí đọc"><div class="off-player-controls"><button class="off-icon-btn" data-prev-segment aria-label="Đoạn trước">↶</button><button class="off-icon-btn off-play" data-play aria-label="Phát">▶</button><button class="off-icon-btn" data-stop aria-label="Dừng">■</button><button class="off-icon-btn" data-next-segment aria-label="Đoạn tiếp">↷</button>${chapterLink(prev, "‹")}${chapterLink(next, "›")}</div><div class="off-player-extras"><select data-voice aria-label="Giọng đọc"><option>Đang tìm giọng đọc…</option></select><label class="off-muted">Tốc độ <select data-rate aria-label="Tốc độ đọc"><option value="0.75">0.75×</option><option value="0.9">0.9×</option><option value="1" selected>1×</option><option value="1.1">1.1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="1.75">1.75×</option><option value="2">2×</option></select></label><select data-sleep aria-label="Hẹn giờ tắt"><option value="0">Hẹn giờ</option><option value="15">15 phút</option><option value="30">30 phút</option><option value="45">45 phút</option><option value="60">60 phút</option><option value="end">Hết chương</option></select></div></section><div class="off-sheet" data-toc-sheet hidden><section class="off-sheet-panel"><div class="off-sheet-head"><strong>Mục lục · ${escape(book.title)}</strong><button class="off-btn" data-close-toc>Đóng</button></div><div class="off-sheet-list">${tocItems}</div></section></div>` , `${chapter.title || chapterLabel} · ${book.title}`);

    const player = document.querySelector(".off-reader-player"), controls = player.querySelector(".off-player-controls"), extras = player.querySelector(".off-player-extras");
    const previousSegment = controls.querySelector("[data-prev-segment]"), playControl = controls.querySelector("[data-play]"), stopControl = controls.querySelector("[data-stop]"), nextSegment = controls.querySelector("[data-next-segment]");
    [...controls.querySelectorAll("a")].forEach(link => link.remove());
    const voiceSelect = extras.querySelector("[data-voice]"), rateLabel = extras.querySelector("label"), sleepSelect = extras.querySelector("[data-sleep]");
    const playerActions = document.createElement("div"); playerActions.className = "off-player-actions";
    const voiceTrigger = document.createElement("button"); voiceTrigger.type = "button"; voiceTrigger.className = "off-btn"; voiceTrigger.dataset.voiceTrigger = ""; voiceTrigger.setAttribute("aria-haspopup", "dialog"); voiceTrigger.setAttribute("aria-label", "Chọn giọng đọc offline"); voiceTrigger.textContent = "Giọng đọc";
    playerActions.append(voiceTrigger, rateLabel, sleepSelect, stopControl);
    controls.replaceChildren(previousSegment, playControl, nextSegment, playerActions); extras.remove();
    player.dataset.mobileExpanded = "false";
    const mobilePlayerToggle = document.createElement("button");
    mobilePlayerToggle.type = "button"; mobilePlayerToggle.className = "off-mobile-player-toggle"; mobilePlayerToggle.setAttribute("aria-label", "Mở trình phát đọc giọng nói"); mobilePlayerToggle.setAttribute("aria-expanded", "false");
    mobilePlayerToggle.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 14v-3a9 9 0 0 1 18 0v3"/><path d="M5 14h3v7H5a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2Zm11 0h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2h-3z"/></svg>';
    player.prepend(mobilePlayerToggle);
    if (!document.getElementById("offline-mobile-player-styles")) {
      const style = document.createElement("style"); style.id = "offline-mobile-player-styles";
      style.textContent = '.off-reader-player{left:auto;right:max(.75rem,env(safe-area-inset-right));bottom:max(.75rem,env(safe-area-inset-bottom));transform:none;width:min(760px,calc(100% - 24px));transition:transform .19s ease,opacity .19s ease}.off-reader-player.is-hidden{opacity:0;transform:translateY(calc(100% + 1.25rem));pointer-events:none}.off-reader-player[data-mobile-expanded="false"]{display:grid;place-items:center;width:3.25rem;height:3.25rem;padding:0;border-radius:50%;overflow:hidden}.off-reader-player[data-mobile-expanded="false"]>:not(.off-mobile-player-toggle){display:none!important}.off-mobile-player-toggle{position:absolute;inset:0;display:grid;place-items:center;width:100%;height:100%;border:0;border-radius:50%;background:transparent;color:var(--off-accent)}.off-mobile-player-toggle svg{width:1.5rem;height:1.5rem}.off-reader-player[data-mobile-expanded="true"] .off-mobile-player-toggle{top:auto;right:.35rem;bottom:calc(100% + .25rem);left:auto;width:2rem;height:2rem;border:1px solid var(--off-border);background:var(--off-card2)}';
      document.head.append(style);
    }
    mobilePlayerToggle.addEventListener("click", () => {
      const expanded = player.dataset.mobileExpanded !== "true";
      player.dataset.mobileExpanded = String(expanded); mobilePlayerToggle.setAttribute("aria-expanded", String(expanded));
      mobilePlayerToggle.setAttribute("aria-label", expanded ? "Thu gọn trình phát đọc giọng nói" : "Mở trình phát đọc giọng nói");
    });
    const voiceSheet = document.createElement("div"); voiceSheet.className = "off-sheet"; voiceSheet.hidden = true; voiceSheet.dataset.voiceSheet = "";
    voiceSheet.innerHTML = '<section class="off-sheet-panel" role="dialog" aria-modal="true" aria-label="Chọn giọng đọc"><div class="off-sheet-head"><strong>Giọng đọc offline</strong><button class="off-btn" type="button" data-close-voice>Đóng</button></div><label class="off-voice-sheet-label">Giọng Tiếng Việt trên thiết bị</label><p class="off-voice-note">Chỉ hiển thị các giọng Tiếng Việt đã cài trên thiết bị và có thể dùng offline.</p></section>';
    voiceSheet.querySelector(".off-voice-sheet-label").append(voiceSelect); document.body.append(voiceSheet);
    const closeVoiceSheet = () => { voiceSheet.hidden = true; voiceTrigger.focus(); };
    voiceTrigger.onclick = () => { voiceSheet.hidden = false; voiceSheet.querySelector("[data-close-voice]").focus(); };
    voiceSheet.querySelector("[data-close-voice]").onclick = closeVoiceSheet;
    voiceSheet.onclick = event => { if (event.target === voiceSheet) closeVoiceSheet(); };
    document.addEventListener("keydown", event => { if (event.key === "Escape" && !voiceSheet.hidden) closeVoiceSheet(); });

    const paragraphs = [...document.querySelectorAll(".off-reader-content p")];
    const seek = document.querySelector(".off-player-seek"); seek.max = String(Math.max(0, paragraphs.length - 1));
    const progressChip = document.querySelector("[data-progress]"), mobileProgress = document.querySelector("[data-progress-mobile]");
    const savePosition = () => {
      const maxScroll = Math.max(1, document.documentElement.scrollHeight - innerHeight), value = Math.max(0, Math.min(100, Math.round(scrollY / maxScroll * 100)));
      localStorage.setItem(identity.position, JSON.stringify({ chapter: chapter.chapterNumber || chapter.chapterId || chapter.id, percent: value, savedAt: Date.now() }));
      progressChip.textContent = mobileProgress.textContent = `${value}%`;
      localStorage.setItem(identity.last, location.pathname + location.search);
      localStorage.setItem(`reader.${owner || "anonymous"}.reader.lastReadAt.book.${book.id}`, String(Date.now()));
    };
    let saveTimer; addEventListener("scroll", () => { clearTimeout(saveTimer); saveTimer = setTimeout(savePosition, 350); }, { passive: true }); addEventListener("pagehide", savePosition, { once: true });
    if (percent) requestAnimationFrame(() => { scrollTo(0, Math.max(0, (document.documentElement.scrollHeight - innerHeight) * percent / 100)); savePosition(); }); else savePosition();
    document.querySelector("[data-font-down]").onclick = () => { const node = document.querySelector(".off-reader-content"), size = parseFloat(getComputedStyle(node).fontSize); node.style.fontSize = `${Math.max(15, size - 1)}px`; };
    document.querySelector("[data-font-up]").onclick = () => { const node = document.querySelector(".off-reader-content"), size = parseFloat(getComputedStyle(node).fontSize); node.style.fontSize = `${Math.min(32, size + 1)}px`; };
    const sheet = document.querySelector("[data-toc-sheet]"); document.querySelector("[data-open-toc]").onclick = () => sheet.hidden = false; document.querySelector("[data-close-toc]").onclick = () => sheet.hidden = true; sheet.onclick = event => { if (event.target === sheet) sheet.hidden = true; };
    let previousY = scrollY; addEventListener("scroll", () => { const toolbar = document.querySelector(".off-reader-toolbar"); if (scrollY < 80 || scrollY < previousY - 6) { toolbar.classList.remove("is-hidden"); player.classList.remove("is-hidden"); } else if (scrollY > previousY + 6) { toolbar.classList.add("is-hidden"); if (!status.classList.contains("playing")) { player.classList.add("is-hidden"); player.dataset.mobileExpanded = "false"; mobilePlayerToggle.setAttribute("aria-expanded", "false"); mobilePlayerToggle.setAttribute("aria-label", "Mở trình phát đọc giọng nói"); } } previousY = scrollY; }, { passive: true });
    document.addEventListener("click", event => { if (event.target.closest(".off-reader-player, .off-sheet")) return; document.querySelector(".off-reader-toolbar")?.classList.remove("is-hidden"); player.classList.remove("is-hidden"); });
    const provider = window.NovelTTS?.createDeviceProvider(), status = document.querySelector("[data-tts-status]"), play = document.querySelector("[data-play]"), rateSelect = document.querySelector("[data-rate]");
    const preferencesKey = `reader.${owner || "anonymous"}.offline.tts.preferences`;
    let preferences = {}; try { preferences = JSON.parse(localStorage.getItem(preferencesKey) || "{}"); } catch { /* Ignore malformed preferences. */ }
    rateSelect.value = String(Math.max(.75, Math.min(2, Number(preferences.rate) || 1)));
    const validSleepModes = new Set(["0", "15", "30", "45", "60", "end"]);
    sleepSelect.value = validSleepModes.has(String(preferences.sleepMode)) ? String(preferences.sleepMode) : "0";
    let sleepUntil = Number(preferences.sleepUntil) || 0;
    const autoplayKey = `reader.${owner || "anonymous"}.offline.tts.autoplay.${ebook ? "ebook" : "novel"}.${book.id}`;
    const pendingAutoplayUrl = sessionStorage.getItem(autoplayKey);
    const autoplayNextChapter = Boolean(pendingAutoplayUrl && new URL(pendingAutoplayUrl, location.href).pathname === location.pathname);
    const persistPreferences = () => localStorage.setItem(preferencesKey, JSON.stringify({ rate: Number(rateSelect.value), sleepMode: sleepSelect.value, sleepUntil, voice: voiceSelect.value }));
    const segments = [];
    for (const [paragraphIndex, node] of paragraphs.entries()) {
      const sentences = node.textContent.match(/[^.!?。！？]+[.!?。！？]?/gu) || [node.textContent]; let current = "";
      for (const sentence of sentences) { if (current.length + sentence.length > 850 && current) { segments.push({ text: current.trim(), paragraphIndex, node }); current = ""; } current += sentence; }
      if (current.trim()) segments.push({ text: current.trim(), paragraphIndex, node });
    }
    let segmentIndex = 0, timer = 0, sleepTimer = 0, asleepAtEnd = sleepSelect.value === "end", paused = false, offlineVoices = [];
    const markSegment = i => { segmentIndex = Math.max(0, Math.min(segments.length - 1, i)); const item = segments[segmentIndex]; if (!item) return; paragraphs.forEach(node => node.classList.remove("off-active")); item.node.classList.add("off-active"); seek.value = String(item.paragraphIndex); item.node.scrollIntoView({ block: "center", behavior: "smooth" }); };
    if (window.NativeTts) {
      const native = window.NativeTts, nativePosition = `reader.native.position.${location.pathname}`;
      let active = false, lastIndex = -1;
      const savedIndex = Number(localStorage.getItem(nativePosition));
      if (Number.isInteger(savedIndex) && savedIndex >= 0 && savedIndex < segments.length) segmentIndex = savedIndex;
      const sync = async () => {
        try {
          const state = await native.getState();
          active = state.chapterId === location.pathname && ["loading", "playing", "paused"].includes(state.status);
          if (state.chapterId !== location.pathname) return;
          if (state.index < segments.length && state.index !== lastIndex) {
            lastIndex = state.index;
            markSegment(state.index);
            localStorage.setItem(nativePosition, String(state.index));
          }
          status.classList.toggle("playing", state.status === "playing");
          play.textContent = state.status === "playing" ? "Ⅱ" : "▶";
          play.setAttribute("aria-label", state.status === "playing" ? "Tạm dừng" : "Phát");
          if (state.status === "error") status.textContent = state.error || "Android TTS gặp lỗi.";
          else if (state.status === "playing") status.textContent = `Đang đọc ${state.index + 1}/${state.total}`;
          else if (state.status === "paused") status.textContent = "Đã tạm dừng";
          else if (state.status === "loading") status.textContent = "Đang chuẩn bị giọng đọc…";
          else if (state.total && state.index >= state.total) { status.textContent = "Đã đọc xong"; localStorage.removeItem(nativePosition); }
        } catch (error) { status.textContent = error.message || "Không kết nối được Android TTS."; }
      };
      try {
        const response = await native.listVoices();
        offlineVoices = response.voices.filter(voice => voice.offline && voice.lang.toLowerCase().startsWith("vi"));
        voiceSelect.replaceChildren();
        if (offlineVoices.length) {
          offlineVoices.forEach(voice => voiceSelect.add(new Option(`${voice.name} · Offline`, voice.id)));
          voiceSelect.value = offlineVoices.some(voice => voice.id === preferences.voice) ? preferences.voice : offlineVoices[0].id;
          persistPreferences();
        } else {
          voiceSelect.add(new Option("Chưa có giọng vi-VN offline", ""));
          status.textContent = "Hãy cài giọng vi-VN offline trên thiết bị.";
          const install = document.createElement("button"); install.type = "button"; install.className = "off-btn"; install.textContent = "Tải giọng đọc";
          install.onclick = () => native.installVoiceData();
          voiceSheet.querySelector(".off-sheet-panel").append(install);
        }
      } catch (error) { status.textContent = error.message || "Không tìm được giọng Android TTS."; }
      play.onclick = async () => {
        try {
          if (active) {
            const state = await native.getState();
            if (state.status === "playing") await native.pause(); else await native.resume();
          } else {
            if (!offlineVoices.length || !segments.length) { status.textContent = "Cần giọng vi-VN offline và nội dung chương."; return; }
            await native.speak({ chapterId: location.pathname, title: chapter.title || chapterLabel, chunks: segments.map(item => item.text), startIndex: segmentIndex, voice: voiceSelect.value, rate: Number(rateSelect.value), sleepUntil });
          }
          await sync();
        } catch (error) { status.textContent = error.message || "Không phát được Android TTS."; }
      };
      stopControl.onclick = async () => { await native.stop(); active = false; status.classList.remove("playing"); status.textContent = "Đã dừng"; play.textContent = "▶"; };
      previousSegment.onclick = async () => { if (active) await native.previous(); else markSegment(Math.max(0, segmentIndex - 1)); await sync(); };
      nextSegment.onclick = async () => { if (active) await native.next(); else markSegment(Math.min(segments.length - 1, segmentIndex + 1)); await sync(); };
      seek.oninput = async () => {
        const i = segments.findIndex(item => item.paragraphIndex >= Number(seek.value));
        if (i < 0) return;
        if (active) await native.seek({ index: i }); else markSegment(i);
        await sync();
      };
      rateSelect.onchange = async () => { persistPreferences(); if (active) await native.setRate({ rate: Number(rateSelect.value) }); };
      voiceSelect.onchange = async () => { persistPreferences(); if (active) await native.setVoice({ voice: voiceSelect.value }); };
      sleepSelect.onchange = async () => { sleepUntil = Number(sleepSelect.value) ? Date.now() + Number(sleepSelect.value) * 60000 : 0; persistPreferences(); if (active) await native.setSleep({ until: sleepUntil }); };
      const polling = setInterval(sync, 500);
      addEventListener("pagehide", () => clearInterval(polling), { once: true });
      addEventListener("visibilitychange", () => { if (!document.hidden) sync(); });
      await sync();
      return;
    }
    const finish = () => { provider?.stop(); clearTimeout(timer); timer = 0; paused = false; play.textContent = "▶"; play.setAttribute("aria-label", "Phát"); status.textContent = "Đã đọc xong"; status.classList.remove("playing"); };
    if (Number(sleepSelect.value) > 0 && sleepUntil) {
      const remaining = sleepUntil - Date.now();
      if (remaining > 0) sleepTimer = setTimeout(() => { sleepSelect.value = "0"; sleepUntil = 0; asleepAtEnd = false; persistPreferences(); finish(); }, remaining);
      else { sleepSelect.value = "0"; sleepUntil = 0; persistPreferences(); }
    }
    const say = () => {
      if (!provider?.isSupported() || !segments.length) { status.textContent = "Trình duyệt chưa hỗ trợ TTS offline."; return; }
      if (!offlineVoices.length) { status.textContent = "Cài giọng Tiếng Việt dùng offline trên thiết bị để bật TTS."; return; }
      if (segmentIndex >= segments.length) { finish(); return; }
      const segment = segments[segmentIndex]; markSegment(segmentIndex); play.textContent = "Ⅱ"; play.setAttribute("aria-label", "Tạm dừng"); status.textContent = `Đang đọc ${segmentIndex + 1}/${segments.length}`; status.classList.add("playing"); player.classList.remove("is-hidden"); paused = false;
      provider.speak(segment.text, { lang: "vi-VN", voiceURI: voiceSelect.value || undefined, rate: Number(rateSelect.value) || 1, pitch: 1, volume: 1,
        onend: () => { segmentIndex += 1; if (segmentIndex < segments.length) timer = setTimeout(say, 90); else if (!asleepAtEnd && next) { status.textContent = "Đang chuyển chương…"; sessionStorage.setItem(autoplayKey, chapterUrl(next)); location.assign(chapterUrl(next)); } else finish(); },
        onerror: error => { status.textContent = error.message || "Không thể phát giọng đọc"; play.textContent = "▶"; status.classList.remove("playing"); },
      });
    };
    const seekTo = value => { const paragraphIndex = Number(value); const i = segments.findIndex(item => item.paragraphIndex >= paragraphIndex); if (i >= 0) { provider?.stop(); clearTimeout(timer); segmentIndex = i; markSegment(i); status.textContent = `Đoạn ${i + 1}/${segments.length}`; } };
    play.onclick = () => { if (paused) { paused = false; provider?.resume(); play.textContent = "Ⅱ"; status.textContent = "Đang đọc"; status.classList.add("playing"); } else if (provider?.isSpeaking()) { provider.pause(); paused = true; play.textContent = "▶"; status.textContent = "Đã tạm dừng"; status.classList.remove("playing"); } else say(); };
    document.querySelector("[data-stop]").onclick = finish;
    document.querySelector("[data-next-segment]").onclick = () => { provider?.stop(); clearTimeout(timer); segmentIndex = Math.min(segments.length - 1, segmentIndex + 1); say(); };
    document.querySelector("[data-prev-segment]").onclick = () => { provider?.stop(); clearTimeout(timer); segmentIndex = Math.max(0, segmentIndex - 1); say(); };
    seek.oninput = () => seekTo(seek.value);
    provider?.getVoices().then(voices => { offlineVoices = voices.filter(voice => voice.lang?.toLowerCase().startsWith("vi") && voice.localService); voiceSelect.replaceChildren(); if (!offlineVoices.length) { const option = new Option("Không có giọng Tiếng Việt offline", ""); voiceSelect.add(option); status.textContent = "Cài giọng Tiếng Việt dùng offline trên thiết bị để bật TTS."; return; } offlineVoices.forEach(voice => voiceSelect.add(new Option(`${voice.name} · Offline`, voice.voiceURI))); const preferredVoice = window.NovelTTS?.getPreferredDeviceVoice(offlineVoices)?.voiceURI; voiceSelect.value = offlineVoices.some(voice => voice.voiceURI === preferences.voice) ? preferences.voice : preferredVoice || offlineVoices[0].voiceURI; persistPreferences(); if (autoplayNextChapter) { sessionStorage.removeItem(autoplayKey); setTimeout(say, 150); } });
    rateSelect.addEventListener("change", persistPreferences);
    voiceSelect.addEventListener("change", persistPreferences);
    sleepSelect.onchange = () => { clearTimeout(sleepTimer); const value = sleepSelect.value; asleepAtEnd = value === "end"; sleepUntil = Number(value) ? Date.now() + Number(value) * 60_000 : 0; persistPreferences(); if (Number(value)) sleepTimer = setTimeout(() => { sleepSelect.value = "0"; sleepUntil = 0; asleepAtEnd = false; persistPreferences(); finish(); }, Number(value) * 60_000); };
    addEventListener("pagehide", () => { provider?.stop(); clearTimeout(sleepTimer); }, { once: true });
  }

  async function renderOfflineReader() {
    const chapter = await byUrl(location.pathname); if (!chapter) return fallbackPage();
    const book = await get("books", chapter.bookId), entries = (await get("chapterLists", chapter.bookId))?.chapters || [];
    const local = (await Promise.all(entries.map(async item => ({ item, chapter: await storedChapter(item) })))).filter(row => row.chapter);
    const index = local.findIndex(row => row.item.id === chapter.id || row.item.chapterId === chapter.chapterId);
    return renderReader({ book: book || { id: chapter.bookId, title: "Truyện offline", bookUrl: chapter.bookUrl }, chapter, entries: local.map(row => row.item), index, owner: ownerId(), ebook: false });
  }
  async function renderOfflineEbookDetail() {
    const account = ownerId(), slug = decodeURIComponent(location.pathname.split("/")[2] || "");
    const book = (await all("ebooks")).find(item => item.slug === slug && account && item.ownerId === account);
    if (!book) return renderOfflineEbookLibrary();
    const chapters = (await ebookChapters(book.id)).sort((a, b) => a.chapterNumber - b.chapterNumber);
    const rows = chapters.map(item => `<a class="off-chapter" href="${escape(item.url)}" data-chapter-search="${escape(`${item.chapterNumber} ${item.title}`.toLocaleLowerCase("vi"))}">${item.chapterNumber}. ${escape(item.title)}</a>`).join("");
    shell(`<main class="off-shell">${topLink("/ebooks", "Ebook offline")}<section class="off-hero">${cover(book.coverUrl, book.title)}<div class="off-hero-copy"><span class="off-kicker">EBOOK ĐÃ LƯU</span><h1 class="off-title">${escape(book.title)}</h1><p class="off-muted">${escape(book.author || "")} · ${chapters.length}/${book.totalChapters || chapters.length} chương offline</p><div class="off-actions">${chapters[0] ? `<a class="off-btn off-primary" href="${escape(chapters[0].url)}">▶ Đọc tiếp</a>` : ""}<span class="off-btn">${escape((book.format || "Ebook").toUpperCase())}</span></div></div></section><div class="off-detail-tools"><input class="off-input" type="search" placeholder="Tìm chương đã lưu…" aria-label="Tìm chương"><select class="off-select" aria-label="Sắp xếp chương"><option value="asc">Cũ nhất trước</option><option value="desc">Mới nhất trước</option></select></div><div class="off-muted">${chapters.length} chương có thể đọc offline</div><section class="off-chapters" style="margin-top:10px">${rows || empty("Chưa lưu chương nào.")}</section></main>`);
    const list = document.querySelector(".off-chapters"), search = document.querySelector('.off-detail-tools input'), sort = document.querySelector(".off-detail-tools select");
    search.addEventListener("input", () => { const q = search.value.trim().toLocaleLowerCase("vi"); list.querySelectorAll("[data-chapter-search]").forEach(row => row.hidden = !row.dataset.chapterSearch.includes(q)); });
    sort.addEventListener("change", () => [...list.querySelectorAll(".off-chapter")].reverse().forEach(row => list.append(row)));
  }
  async function renderOfflineEbookReader() {
    const account = ownerId(), match = location.pathname.match(/^\/ebooks\/([^/]+)\/read\/(\d+)/), slug = match?.[1] ? decodeURIComponent(match[1]) : "", number = Number(match?.[2]);
    const book = (await all("ebooks")).find(item => item.slug === slug && account && item.ownerId === account), chapter = book && await get("ebookChapters", `${book.id}:${number}`);
    if (!book || !chapter) return renderOfflineEbookLibrary();
    const entries = (await ebookChapters(book.id)).sort((a, b) => a.chapterNumber - b.chapterNumber), index = entries.findIndex(item => item.chapterNumber === number);
    return renderReader({ book, chapter, entries, index, owner: account, ebook: true });
  }

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
  async function setupBook(bookRoot) {
    const status = bookRoot.querySelector("[data-offline-status]"), download = bookRoot.querySelector("[data-offline-download]"), update = bookRoot.querySelector("[data-offline-update]"), del = bookRoot.querySelector("[data-offline-delete]");
    if (!status || !download || !update || !del) return;
    const id = bookRoot.dataset.bookId, title = bookRoot.dataset.bookTitle, coverUrl = bookRoot.querySelector(".book-cover img, img.book-cover")?.currentSrc || bookRoot.querySelector(".book-cover img, img.book-cover")?.src || "";
    let chapters = [...bookRoot.querySelectorAll("[data-offline-chapter]")].map((link, order) => { const sourceId = chapterId(link); return { id: `${id}:${sourceId}`, chapterId: sourceId, url: new URL(link.href).pathname, title: text(link.querySelector("span:last-child")) || text(link), bookId: id, bookUrl: location.pathname, order }; });
    const loadAllChapters = async () => { const url = bookRoot.dataset.offlineChaptersUrl; if (!url) return; const response = await fetch(url, { cache: "no-store" }); if (!response.ok) throw new Error("chapter list unavailable"); chapters = (await response.json()).map((chapter, order) => ({ id: `${id}:${chapter.id}`, chapterId: String(chapter.id), url: chapter.url, title: chapter.title || `Chương ${chapter.chapter_number}`, bookId: id, bookUrl: location.pathname, order })); };
    const count = async () => (await Promise.all(chapters.map(storedChapter))).filter(Boolean).length;
    const controls = (stored, busy = false, resumable = false) => { const complete = stored === chapters.length && chapters.length > 0, label = download.querySelector("span"); download.disabled = busy || complete; label.textContent = busy ? `Đang tải ${stored} / ${chapters.length}` : complete ? "✓ Đã tải toàn bộ" : (stored || resumable) ? "Tải tiếp tất cả chương" : "Tải tất cả chương"; update.hidden = !complete; del.hidden = !(stored || resumable); update.disabled = del.disabled = busy; };
    let stored = await count(), resume = Boolean(await get("books", id)), existingBook = await get("books", id);
    if (existingBook && coverUrl && !existingBook.coverUrl) await put("books", { ...existingBook, coverUrl });
    controls(stored, false, resume); if (stored || resume) status.textContent = stored === chapters.length ? `✓ Đã tải offline · ${stored} / ${chapters.length} chương` : `Đã tải ${stored} / ${chapters.length} chương. Có thể tải tiếp.`;
    const downloadMissing = async (updateOnly = false) => {
      const previousList = await get("chapterLists", id), knownChapterIds = new Set((previousList?.chapters || []).map(chapter => String(chapter.chapterId)));
      try { await loadAllChapters(); } catch { status.textContent = "Không tải được mục lục. Có thể thử lại sau."; return; }
      const local = await Promise.all(chapters.map(storedChapter)), pending = updateOnly ? chapters.filter(chapter => !knownChapterIds.has(String(chapter.chapterId))) : chapters.filter((chapter, index) => !local[index]);
      await put("chapterLists", { bookId: id, chapters, updatedAt: Date.now() }); await put("books", { ...(await get("books", id)), id, title, coverUrl, bookUrl: location.pathname, totalChapters: chapters.length, downloadedChapters: stored, updatedAt: Date.now(), status: "downloading" });
      if (!pending.length) { controls(stored); status.textContent = updateOnly ? "Bản offline đã có chương mới nhất." : status.textContent; return; }
      let cursor = 0, failed = false; resume = true; controls(stored, true, resume); const show = () => { status.textContent = `Đang tải ${stored} / ${chapters.length} chương`; controls(stored, true, resume); };
      await Promise.all(Array.from({ length: Math.min(4, pending.length) }, async () => { while (cursor < pending.length) { const chapter = pending[cursor++]; if (await fetchChapter(chapter)) { stored += 1; show(); } else failed = true; } }));
      await put("books", { ...(await get("books", id)), id, title, coverUrl, bookUrl: location.pathname, totalChapters: chapters.length, downloadedChapters: stored, updatedAt: Date.now(), status: stored === chapters.length ? "downloaded" : "partial" });
      resume = stored !== chapters.length; controls(stored, false, resume); status.textContent = stored === chapters.length ? `✓ Đã tải offline · ${stored} / ${chapters.length} chương` : `Đã tải ${stored} / ${chapters.length} chương.${failed ? " Tải thất bại. Có thể tiếp tục sau." : ""}`;
    };
    download.onclick = () => downloadMissing(false); update.onclick = () => downloadMissing(true);
    del.onclick = async () => { await removeBookChapters(id); await Promise.all([remove("chapterLists", id), remove("books", id)]); stored = 0; resume = false; controls(stored); status.textContent = "Đã xóa bản offline."; };
  }

  async function setupEbookDownload() {
    const button = document.querySelector("[data-offline-ebook-save]"); if (!button || !indexedDB) return;
    const status = document.querySelector("[data-offline-ebook-status]"), deleteButton = document.querySelector("[data-offline-ebook-delete]");
    const manifest = [...document.querySelectorAll("[data-offline-ebook-chapter]")].map(row => ({ number: Number(row.dataset.number), title: row.dataset.title, url: row.dataset.url, translated: row.dataset.translated === "true" }));
    const account = button.dataset.ownerId || ""; localStorage.setItem("novel-reader-offline-owner", account);
    const bookId = button.dataset.bookId, slug = button.dataset.slug; let busy = false;
    const existing = await get("ebooks", bookId);
    if (existing) { deleteButton.hidden = false; status.textContent = existing.status === "ready" ? "Đã lưu trên thiết bị · có thể đọc khi offline." : `Đã lưu ${existing.savedChapters || 0}/${existing.totalChapters} chương.`; }
    deleteButton?.addEventListener("click", async () => { if (busy) return; busy = true; deleteButton.disabled = true; try { for (const chapter of await ebookChapters(bookId)) await remove("ebookChapters", chapter.id); await remove("ebooks", bookId); status.textContent = "Đã xóa dữ liệu offline khỏi thiết bị."; deleteButton.hidden = true; button.textContent = "Lưu trên thiết bị"; } finally { busy = false; deleteButton.disabled = false; } });
    button.addEventListener("click", async () => {
      if (busy) return; busy = true; button.disabled = true;
      try {
        if (!manifest.length) throw new Error("Ebook chưa có danh sách chương để lưu.");
        const record = { ...(await get("ebooks", bookId)), id: bookId, ownerId: account, slug, title: button.dataset.title, author: button.dataset.author, format: button.dataset.format, coverUrl: button.dataset.coverUrl || "", bookUrl: `/ebooks/${encodeURIComponent(slug)}`, totalChapters: manifest.length, status: "downloading", updatedAt: Date.now() };
        await put("ebooks", record); const saved = new Set((await ebookChapters(bookId)).map(chapter => chapter.chapterNumber));
        let completed = saved.size, cursor = 0, failures = 0, quotaHit = false; status.textContent = `Đang lưu ${completed}/${manifest.length} chương…`;
        await Promise.all(Array.from({ length: 3 }, async () => { while (cursor < manifest.length) {
          const item = manifest[cursor++]; if (saved.has(item.number)) continue;
          try {
            const url = item.translated ? `${item.url}?mode=translated` : item.url;
            let response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
            if (!response.ok && item.translated) response = await fetch(item.url, { credentials: "same-origin", cache: "no-store" });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const page = new DOMParser().parseFromString(await response.text(), "text/html"), content = page.querySelector("#ebook-reader-content .reader-prose")?.innerText?.trim();
            if (!content) throw new Error("empty chapter");
            const title = page.querySelector("#ebook-reader-content h2")?.innerText?.trim() || item.title;
            await saveEbookChapter({ id: `${bookId}:${item.number}`, bookId, ownerId: account, chapterNumber: item.number, title, content, url: item.url, translated: item.translated, savedAt: Date.now() }); completed += 1;
          } catch (error) { failures += 1; if (error?.name === "QuotaExceededError") quotaHit = true; console.warn("Offline Ebook chapter download failed", item.number, error); }
          if (quotaHit) { cursor = manifest.length; status.textContent = "Thiết bị đã hết dung lượng lưu offline. Hãy xóa dữ liệu offline khác rồi thử tiếp."; break; }
          status.textContent = `Đang lưu ${completed}/${manifest.length} chương…`;
        } }));
        record.status = completed === manifest.length ? "ready" : "partial"; record.savedChapters = completed; record.updatedAt = Date.now(); await put("ebooks", record);
        status.textContent = record.status === "ready" ? `Đã lưu đủ ${completed} chương trên thiết bị.` : quotaHit ? `Đã lưu ${completed}/${manifest.length} chương; thiết bị hết dung lượng. Có thể tiếp tục sau khi giải phóng bộ nhớ.` : `Đã lưu ${completed}/${manifest.length} chương. ${failures} chương chưa tải được; có thể bấm để tiếp tục.`;
        button.textContent = record.status === "ready" ? "Đã lưu offline" : "Tải tiếp để đọc offline"; deleteButton.hidden = false;
      } catch (error) { status.textContent = error.message || "Không thể lưu Ebook trên thiết bị."; }
      finally { busy = false; button.disabled = false; }
    });
  }

  const setMediaSessionState = state => {
    try { if (navigator.mediaSession) navigator.mediaSession.playbackState = state; }
    catch (error) { console.warn("Media Session playback state unavailable", error); }
  };
  const configureMediaSession = (title, handlers) => {
    const session = navigator.mediaSession;
    if (!session) return;
    try { if (typeof window.MediaMetadata === "function") session.metadata = new window.MediaMetadata({title, artist:"Leo Truyện"}); }
    catch (error) { console.warn("Media Session metadata unavailable", error); }
    for (const [action, handler] of Object.entries(handlers)) {
      try { session.setActionHandler?.(action, handler); }
      catch (error) { console.warn(`Media Session ${action} action unavailable`, error); }
    }
  };
  const getInitialTtsMode = (storedMode, userAgent = navigator.userAgent, online = navigator.onLine) => {
    const mobile = /Android|iPhone|iPad|iPod|Mobile/i.test(userAgent) || navigator.userAgentData?.mobile;
    return mobile ? (online ? "server" : "device") : storedMode || (online ? "server" : "device");
  };
  const getPreferredDeviceVoice = (voices, userAgent = navigator.userAgent) => {
    const vietnamese = voices.filter(voice => voice.lang?.toLowerCase().startsWith("vi"));
    const edge = /Edg\//i.test(userAgent);
    const chrome = /Chrome\//i.test(userAgent) && !edge;
    if (edge) return vietnamese.find(voice => /nam\s*minh/i.test(`${voice.name} ${voice.voiceURI}`)) || vietnamese[0];
    if (chrome) return vietnamese.find(voice => /google/i.test(`${voice.name} ${voice.voiceURI}`) && /(?:tiếng\s*việt|vietnamese).*2|2.*(?:tiếng\s*việt|vietnamese)/i.test(`${voice.name} ${voice.voiceURI}`))
      || vietnamese.find(voice => /google/i.test(`${voice.name} ${voice.voiceURI}`)) || vietnamese[0];
    return vietnamese[0];
  };
  const createDeviceProvider = () => {
    const synth = window.speechSynthesis; let active = null, generation = 0, speaking = false;
    const supported = () => Boolean(synth && window.SpeechSynthesisUtterance);
    const getVoices = async () => { if (!supported()) return []; let voices = synth.getVoices(); if (voices.length) return voices; await new Promise(resolve => { const timer = setTimeout(resolve, 700); synth.addEventListener?.("voiceschanged", () => { clearTimeout(timer); resolve(); }, { once: true }); }); return synth.getVoices(); };
    return { isSupported: supported, isSpeaking: () => speaking, getVoices, speak(value, options = {}) { if (!supported()) { options.onerror?.(new Error("TTS không khả dụng trên trình duyệt này.")); return false; } if (!String(value || "").trim()) return false; const voices = synth.getVoices(), voice = voices.find(item => item.voiceURI === options.voiceURI) || (!options.voiceURI && voices.find(item => item.lang?.toLowerCase().startsWith("vi"))); if (!voice || !voice.lang?.toLowerCase().startsWith("vi")) { options.onerror?.(new Error("Không tìm thấy giọng đọc tiếng Việt trên thiết bị.")); return false; } synth.cancel(); const token = ++generation, utterance = new SpeechSynthesisUtterance(String(value)); active = utterance; utterance.voice = voice; utterance.lang = voice.lang; utterance.rate = Math.max(.5, Math.min(2, Number(options.rate) || 1)); utterance.pitch = Math.max(.5, Math.min(2, Number(options.pitch) || 1)); utterance.volume = Math.max(0, Math.min(1, Number(options.volume ?? 1))); utterance.onstart = event => { if (token === generation) { speaking = true; options.onstart?.(event); } }; utterance.onboundary = event => { if (token === generation) options.onboundary?.(event); }; utterance.onend = event => { if (token === generation) { speaking = false; active = null; options.onend?.(event); } }; utterance.onerror = event => { if (token === generation) { speaking = false; active = null; if (event.error !== "canceled" && event.error !== "interrupted") options.onerror?.(new Error(event.error || "Lỗi TTS thiết bị.")); } }; synth.speak(utterance); return true; }, pause() { if (supported()) { synth.pause(); speaking = false; } }, resume() { if (supported()) { synth.resume(); speaking = true; } }, stop() { generation++; speaking = false; active = null; if (supported()) synth.cancel(); } };
  };
  const createManager = ({ device, server, initialMode = "server" } = {}) => { let mode = initialMode === "device" ? "device" : "server"; const active = () => mode === "device" ? device : server; return { get mode() { return mode; }, setMode(value) { if (value !== "device" && value !== "server") throw new Error("TTS mode không hợp lệ"); if (mode !== value) { device?.stop?.(); server?.stop?.(); mode = value; } }, isSupported() { return Boolean(active()?.isSupported?.() ?? active()); }, getVoices() { return device?.getVoices?.() || Promise.resolve([]); }, speak(...args) { return active()?.speak?.(...args); }, pause() { return active()?.pause?.(); }, resume() { return active()?.resume?.(); }, stop() { device?.stop?.(); server?.stop?.(); } }; };
  document.addEventListener("DOMContentLoaded", () => { const book = document.querySelector("[data-offline-book]"); if (book && "indexedDB" in window) setupBook(book).catch(error => console.warn("Offline storage unavailable", error)); setupEbookDownload().catch(error => console.warn("Offline Ebook storage unavailable", error)); });
  window.NovelOffline = { renderOfflineReader, renderOfflineBook, renderOfflineHome, renderOfflineEbookLibrary, renderOfflineEbookDetail, renderOfflineEbookReader, store: { get, put, all, byUrl } };
  window.NovelTTS = { createDeviceProvider, createManager, configureMediaSession, getInitialTtsMode, getPreferredDeviceVoice, setMediaSessionState };
})();
