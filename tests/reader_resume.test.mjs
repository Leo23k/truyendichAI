import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
const { chooseResumeTarget, isReaderPath, normalizeReaderUrl, start, STORAGE_KEY } = require('../app/static/reader_resume.js');

test('recognizes novel and Ebook chapter reader paths only', () => {
  assert.equal(isReaderPath('/truyen/kiem-lai/12'), true);
  assert.equal(isReaderPath('/chapters/640700'), true);
  assert.equal(isReaderPath('/ebooks/kiem-lai/read/12'), true);
  assert.equal(isReaderPath('/ebooks/kiem-lai'), false);
  assert.equal(isReaderPath('/ebooks/kiem-lai/read-pdf'), false);
  assert.equal(isReaderPath('/'), false);
});

test('resumes to a saved reader only on mobile app startup at the library root', () => {
  const options = { pathname: '/', referrer: '', savedUrl: '/ebooks/kiem-lai/read/12?mode=translated', mobileApp: true, origin: 'https://leotruyen.test' };
  assert.equal(chooseResumeTarget(options), '/ebooks/kiem-lai/read/12?mode=translated');
  assert.equal(chooseResumeTarget({ ...options, referrer: 'https://leotruyen.test/truyen/kiem-lai/3' }), '');
  assert.equal(chooseResumeTarget({ ...options, mobileApp: false }), '');
  assert.equal(chooseResumeTarget({ ...options, pathname: '/ebooks' }), '');
});

test('rejects external or non-reader saved URLs', () => {
  assert.equal(normalizeReaderUrl('https://attacker.test/truyen/book/1', 'https://leotruyen.test'), '');
  assert.equal(normalizeReaderUrl('/truyen/book', 'https://leotruyen.test'), '');
});

test('stores the open chapter and clears it after entering a non-reader page', () => {
  const values = new Map();
  const storage = { getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
  const listeners = {};
  const win = { location: { origin: 'https://leotruyen.test', pathname: '/truyen/book/4', search: '?mode=original' }, navigator: {}, matchMedia: () => ({ matches: true }), addEventListener: (name, callback) => { listeners[name] = callback; } };
  const doc = { referrer: '', visibilityState: 'visible', addEventListener: (name, callback) => { listeners[name] = callback; } };
  start(win, doc, storage);
  assert.equal(storage.getItem(STORAGE_KEY), '/truyen/book/4?mode=original');
  doc.visibilityState = 'hidden';
  listeners.visibilitychange();
  assert.equal(storage.getItem(STORAGE_KEY), '/truyen/book/4?mode=original');
  win.location.pathname = '/'; win.location.search = '';
  doc.referrer = 'https://leotruyen.test/truyen/book/4';
  start(win, doc, storage);
  assert.equal(storage.getItem(STORAGE_KEY), null);
});

test('consumes and redirects to the saved reader on a standalone mobile app launch', () => {
  const values = new Map([[STORAGE_KEY, '/ebooks/book/read/8']]);
  const storage = { getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
  let redirected = '';
  const win = { location: { origin: 'https://leotruyen.test', pathname: '/', search: '', replace: value => { redirected = value; } }, navigator: { standalone: true }, matchMedia: () => ({ matches: false }), addEventListener() {} };
  const doc = { referrer: '', visibilityState: 'visible', addEventListener() {} };
  start(win, doc, storage);
  assert.equal(redirected, '/ebooks/book/read/8');
  assert.equal(storage.getItem(STORAGE_KEY), null);
});
