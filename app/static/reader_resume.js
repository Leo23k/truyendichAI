(function (root, factory) {
  const resume = factory();
  if (typeof module === "object" && module.exports) module.exports = resume;
  else {
    root.NovelReaderResume = resume;
    try { resume.start(root, root.document, root.localStorage); } catch { /* Resume must never block reading. */ }
  }
})(globalThis, function () {
  const STORAGE_KEY = "leotruyen.reader-resume.v1";
  const isReaderPath = pathname =>
    /^\/truyen\/[^/]+\/\d+\/?$/.test(pathname)
    || /^\/chapters\/[^/]+\/?$/.test(pathname)
    || /^\/ebooks\/[^/]+\/read\/\d+\/?$/.test(pathname);

  const normalizeReaderUrl = (value, origin) => {
    try {
      const url = new URL(String(value || ""), origin);
      if (url.origin !== origin || !isReaderPath(url.pathname)) return "";
      return `${url.pathname}${url.search}`;
    } catch {
      return "";
    }
  };

  const chooseResumeTarget = ({ pathname, referrer, savedUrl, mobileApp, origin }) => {
    if (pathname !== "/" || referrer || !mobileApp) return "";
    return normalizeReaderUrl(savedUrl, origin);
  };

  const start = (win, doc, storage) => {
    if (!win || !doc || !storage) return;
    const origin = win.location.origin;
    const matches = query => { try { return Boolean(win.matchMedia(query).matches); } catch { return false; } };
    const isMobileApp = Boolean(win.navigator?.standalone || matches("(display-mode: standalone)") || matches("(max-width: 767px)"));
    const currentUrl = () => `${win.location.pathname}${win.location.search}`;
    const saveCurrentReader = () => {
      if (isReaderPath(win.location.pathname)) storage.setItem(STORAGE_KEY, currentUrl());
      else storage.removeItem(STORAGE_KEY);
    };

    try {
      const savedUrl = storage.getItem(STORAGE_KEY);
      const target = chooseResumeTarget({
        pathname: win.location.pathname,
        referrer: doc.referrer,
        savedUrl,
        mobileApp: isMobileApp,
        origin,
      });
      if (target) {
        storage.removeItem(STORAGE_KEY);
        win.location.replace(target);
        return;
      }
      saveCurrentReader();
    } catch {
      // Resume is best-effort when browser storage is unavailable.
    }

    doc.addEventListener("visibilitychange", () => {
      if (doc.visibilityState === "hidden") {
        try { saveCurrentReader(); } catch { /* Ignore storage restrictions. */ }
      }
    });
    win.addEventListener("pagehide", () => {
      try { saveCurrentReader(); } catch { /* Ignore storage restrictions. */ }
    });
  };

  return { STORAGE_KEY, chooseResumeTarget, isReaderPath, normalizeReaderUrl, start };
});
