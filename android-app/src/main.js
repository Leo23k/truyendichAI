import { CapacitorHttp, registerPlugin } from '@capacitor/core';

window.NativeTts = registerPlugin('NativeTts');

const site = 'https://leotruyen.vercel.app';
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const request = async (path, responseType = 'json') => {
  const response = await CapacitorHttp.get({ url: site + path, responseType, connectTimeout: 15000, readTimeout: 30000 });
  if (response.status !== 200) throw new Error(`HTTP ${response.status}`);
  return response.data;
};

async function openWebAppWhenOnline() {
  if (location.origin === site || location.pathname !== '/') return false;
  try {
    const response = await CapacitorHttp.get({
      url: `${site}/health`,
      responseType: 'json',
      connectTimeout: 5000,
      readTimeout: 5000,
    });
    if (response.status !== 200 || response.data?.status !== 'ok') return false;
    location.replace(`${site}/`);
    return true;
  } catch {
    return false;
  }
}

async function showHome() {
  await window.NovelOffline.renderOfflineHome();
  const link = document.createElement('a');
  link.className = 'off-btn off-primary';
  link.href = '/catalog';
  link.textContent = 'Tìm truyện để tải về';
  document.querySelector('.off-top')?.append(link);
}

async function showCatalog() {
  await window.NovelOffline.renderOfflineHome();
  const root = document.querySelector('.off-shell');
  root.innerHTML = '<div class="off-top"><a href="/">← Thư viện offline</a></div><h1 class="off-title">Truyện trên Leo Truyện</h1><p class="off-muted" data-status>Đang tải danh sách…</p><section class="off-grid"></section>';
  try {
    const payload = await request('/api/novels/metadata');
    const novels = payload.items || [];
    root.querySelector('[data-status]').textContent = `${novels.length} truyện · Chọn truyện để tải chương`;
    root.querySelector('.off-grid').innerHTML = novels.map(book => `<a class="off-card" href="/catalog/${encodeURIComponent(book.slug)}"><div class="off-cover">${book.cover ? `<img src="${escape(book.cover)}" alt="" loading="lazy">` : ''}<div class="off-cover-fallback">${escape(book.title)}</div></div><div class="off-card-body"><div class="off-card-title">${escape(book.title)}</div><div class="off-card-meta">${escape(book.author)}</div></div></a>`).join('');
    sessionStorage.setItem('android.catalog', JSON.stringify(novels));
  } catch (error) {
    root.querySelector('[data-status]').textContent = `Không tải được danh sách: ${error.message}`;
  }
}

async function showBook(slug) {
  await window.NovelOffline.renderOfflineHome();
  const root = document.querySelector('.off-shell');
  root.innerHTML = '<div class="off-top"><a href="/catalog">← Danh sách truyện</a></div><h1 class="off-title" data-title></h1><p class="off-muted" data-status>Đang tải mục lục…</p><section class="off-chapters"></section>';
  try {
    const books = JSON.parse(sessionStorage.getItem('android.catalog') || '[]');
    const book = books.find(item => item.slug === slug) || { slug, title: slug, id: slug };
    const chapters = await request(`/truyen/${encodeURIComponent(slug)}/offline-chapters`);
    const store = window.NovelOffline.store;
    await store.put('books', { id: String(book.id), title: book.title, author: book.author, coverUrl: book.cover || '', bookUrl: `/truyen/${slug}`, totalChapters: chapters.length, downloadedChapters: (await store.all('chapters')).filter(row => row.bookId === String(book.id)).length });
    await store.put('chapterLists', { bookId: String(book.id), chapters: chapters.map((row, index) => ({ id: `${book.id}:${row.id}`, chapterId: String(row.id), title: row.title, url: row.url, bookId: String(book.id), bookUrl: `/truyen/${slug}`, order: index })) });
    root.querySelector('[data-title]').textContent = book.title;
    root.querySelector('[data-status]').textContent = `${chapters.length} chương · Chạm vào chương để tải và đọc`;
    root.querySelector('.off-chapters').innerHTML = chapters.map(row => `<a class="off-chapter" href="${escape(row.url)}">${row.chapter_number}. ${escape(row.title)}</a>`).join('');
  } catch (error) {
    root.querySelector('[data-status]').textContent = `Không tải được mục lục: ${error.message}`;
  }
}

async function showChapter(path) {
  const store = window.NovelOffline.store;
  if (await store.byUrl(path)) return window.NovelOffline.renderOfflineReader();
  await window.NovelOffline.renderOfflineHome();
  const status = document.querySelector('.off-shell');
  status.innerHTML = '<p class="off-muted">Đang tải chương để đọc offline…</p>';
  try {
    const match = path.match(/^\/truyen\/([^/]+)\/(\d+)$/);
    const slug = decodeURIComponent(match[1]);
    const book = (await store.all('books')).find(row => row.bookUrl === `/truyen/${slug}`);
    if (!book) throw new Error('Mở mục lục truyện trước khi tải chương.');
    const entries = (await store.get('chapterLists', book.id))?.chapters || [];
    const entry = entries.find(row => row.url === path);
    if (!entry) throw new Error('Chương không có trong mục lục.');
    const html = await request(path, 'text');
    const page = new DOMParser().parseFromString(html, 'text/html');
    const content = [...page.querySelectorAll('[data-reader-content] p')].map(row => row.textContent.trim()).filter(Boolean).join('\n') || page.querySelector('[data-reader-content]')?.textContent?.trim();
    if (!content) throw new Error('Chương chưa có nội dung.');
    await store.put('chapters', { ...entry, content, savedAt: Date.now() });
    await store.put('books', { ...book, downloadedChapters: (await store.all('chapters')).filter(row => row.bookId === book.id).length });
    await window.NovelOffline.renderOfflineReader();
  } catch (error) {
    status.innerHTML = `<main class="off-shell"><p class="off-muted">${escape(error.message)} ${navigator.onLine ? '' : 'Hãy kết nối mạng để tải chương này.'}</p><a class="off-btn" href="/">Về thư viện offline</a></main>`;
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  if (await openWebAppWhenOnline()) return;
  const path = location.pathname;
  if (path === '/catalog') return showCatalog();
  if (path.startsWith('/catalog/')) return showBook(decodeURIComponent(path.slice('/catalog/'.length)));
  if (/^\/truyen\/[^/]+\/\d+$/.test(path)) return showChapter(path);
  if (path.startsWith('/truyen/')) return window.NovelOffline.renderOfflineBook();
  if (path === '/ebooks') return window.NovelOffline.renderOfflineEbookLibrary();
  if (/^\/ebooks\/[^/]+\/read\/\d+$/.test(path)) return window.NovelOffline.renderOfflineEbookReader();
  if (path.startsWith('/ebooks/')) return window.NovelOffline.renderOfflineEbookDetail();
  return showHome();
});
