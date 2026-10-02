import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession

from app.config import settings
from app.crawlers.base import BaseCrawler, BookInfo, ChapterInfo, clean_chapter_text, dedupe_chapters, extract_cover_url
from app.crawlers.errors import SourceParseError, SourceRequestError

logger = logging.getLogger(__name__)
_CHAPTER_NUMBER = re.compile(r"\u7b2c\s*([0-9\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u4e24\u5341\u767e\u5343]+)\s*\u7ae0")
_CHINESE_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "两": 2}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000}


def _chapter_number_from_title(title: str) -> int | None:
    match = _CHAPTER_NUMBER.search(title)
    if not match:
        return None
    value = match.group(1)
    if value.isdigit():
        return int(value)

    total = current = 0
    for char in value:
        if char in _CHINESE_DIGITS:
            current = _CHINESE_DIGITS[char]
        elif char in _CHINESE_UNITS:
            total += (current or 1) * _CHINESE_UNITS[char]
            current = 0
        else:
            return None
    return total + current


class HtmlNovelCrawler(BaseCrawler):
    chapter_selectors: tuple[str, ...] = ()
    content_selectors: tuple[str, ...] = ("#content", "#contents", ".content", ".article-content", ".read-content")
    preserve_source_chapter_order = False

    async def _get_html(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=settings.crawler_timeout,
                follow_redirects=True,
                trust_env=False,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36", "Accept-Language": "zh-CN,zh;q=0.9"},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                # Some older Chinese novel sites send a UTF-8 HTTP header while
                # their document itself declares GBK.  Trust the document's
                # declaration so CSS parsing and saved text are not mojibake.
                declared_charset = re.search(
                    rb"<meta[^>]+charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)",
                    response.content[:4096],
                    re.IGNORECASE,
                )
                if declared_charset:
                    try:
                        return response.content.decode(declared_charset.group(1).decode("ascii"), errors="replace")
                    except LookupError:
                        logger.warning("Unknown charset from %s: %r", url, declared_charset.group(1))
                return response.text
        except httpx.HTTPStatusError as exc:
            logger.warning("%s returned HTTP %s for %s", self.source_name, exc.response.status_code, url)
            raise SourceRequestError("Kh\u00f4ng th\u1ec3 t\u1ea3i d\u1eef li\u1ec7u t\u1eeb website ngu\u1ed3n.") from exc
        except httpx.HTTPError as exc:
            raise SourceRequestError("Kh\u00f4ng th\u1ec3 k\u1ebft n\u1ed1i t\u1edbi website ngu\u1ed3n.") from exc

    async def get_book_info(self, url: str) -> BookInfo:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        title_node = soup.select_one("h1") or soup.select_one(".bookname")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        text = soup.get_text("\n", strip=True)
        author_match = re.search(r"\u4f5c\u8005\s*[:\uff1a]?\s*([^\n\s]+)", text)
        description_node = soup.select_one("#intro, .intro, .description, .book-intro")
        if not title:
            raise SourceParseError("Kh\u00f4ng t\u00ecm th\u1ea5y t\u00ean truy\u1ec7n t\u1eeb website ngu\u1ed3n.")
        return BookInfo(url, self.source_name, self._book_id(url), title, author_match.group(1) if author_match else None, description_node.get_text("\n", strip=True) if description_node else None, extract_cover_url(soup, url))

    async def get_chapters(self, url: str) -> list[ChapterInfo]:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        links = []
        for selector in self.chapter_selectors:
            links.extend(soup.select(selector))
        if not links:
            links = soup.select("a[href]")
        chapters: list[ChapterInfo] = []
        for index, link in enumerate(links, start=1):
            href = urljoin(url, link.get("href", ""))
            if not self._is_chapter_url(href):
                continue
            title = link.get_text(" ", strip=True)
            if not title:
                continue
            number = _chapter_number_from_title(title)
            chapters.append(ChapterInfo(number if number is not None else index, self._chapter_id(href), title, href))
        if not chapters:
            raise SourceParseError("Kh\u00f4ng t\u00ecm th\u1ea5y danh s\u00e1ch ch\u01b0\u01a1ng.")
        return dedupe_chapters(chapters, sort_by_number=not self.preserve_source_chapter_order)

    async def get_chapter_content(self, url: str) -> str:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        content_node = next((soup.select_one(selector) for selector in self.content_selectors if soup.select_one(selector)), None)
        if not content_node:
            raise SourceParseError("Kh\u00f4ng t\u00ecm th\u1ea5y n\u1ed9i dung ch\u01b0\u01a1ng.")
        for node in content_node.select("script, style, nav, .pagination, .page"):
            node.decompose()
        content = clean_chapter_text(content_node.get_text("\n", strip=True))
        if not content:
            raise SourceParseError("N\u1ed9i dung ch\u01b0\u01a1ng tr\u1ed1ng.")
        return content


class PiaotianCrawler(HtmlNovelCrawler):
    source_name = "piaotian"
    domains = ("piaotian.com", "www.piaotian.com", "piaotia.com", "www.piaotia.com")
    chapter_selectors = ("#list a[href]", ".centent a[href]", ".chapterlist a[href]")
    content_selectors = ("#content", "#contents", ".content", ".yd_text2", ".read-content")
    preserve_source_chapter_order = True

    async def get_book_info(self, url: str) -> BookInfo:
        info = await super().get_book_info(url)
        # Piaotia puts this navigation label inside <h1>; it is not part of
        # the novel title and should never reach metadata translation.
        info.title = re.sub(r"\s*最新章节\s*$", "", info.title).strip()
        return info

    @staticmethod
    def _book_id(url: str) -> str | None:
        match = re.search(r"/(?:html|bookinfo)/\d+/(\d+)", urlparse(url).path)
        return match.group(1) if match else None

    def _is_chapter_url(self, url: str) -> bool:
        book_id = self._book_id(url)
        return bool(book_id and re.search(rf"/html/\d+/{re.escape(book_id)}/\d+\.html$", urlparse(url).path))

    @staticmethod
    def _chapter_id(url: str) -> str | None:
        match = re.search(r"/(\d+)\.html$", urlparse(url).path)
        return match.group(1) if match else None

    async def get_chapters(self, url: str) -> list[ChapterInfo]:
        """Follow Piaotia's book-info page to its complete table of contents."""
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        index_link = next(
            (
                urljoin(url, link["href"])
                for link in soup.select("a[href]")
                if re.search(r"/html/\d+/\d+/index\.html$", link["href"])
            ),
            None,
        )
        chapters = await super().get_chapters(index_link or url)
        # Piaotia's displayed "第…章" labels can restart or skip. The page
        # order is the only stable reader position, just as it is for XSW.
        return [
            ChapterInfo(index, chapter.source_chapter_id, chapter.title, chapter.url)
            for index, chapter in enumerate(chapters, start=1)
        ]

    async def get_chapter_content(self, url: str) -> str:
        html = await self._get_html(url)
        soup = BeautifulSoup(html, "html.parser")
        content_node = next((soup.select_one(selector) for selector in self.content_selectors if soup.select_one(selector)), None)
        if content_node:
            return await super().get_chapter_content(url)

        # Piaotia's reader has no content wrapper: the prose is bare HTML
        # between the top and bottom reader controls. Do not anchor this on
        # the chapter heading: some pages begin directly with prose.
        content_match = re.search(
            r'<div\s+class=["\']toplink["\'][^>]*>.*?</div>\s*(.*?)\s*<div\s+class=["\']bottomlink["\']',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if not content_match:
            raise SourceParseError("Không tìm thấy nội dung chương.")
        content = clean_chapter_text(BeautifulSoup(content_match.group(1), "html.parser").get_text("\n", strip=True))
        if not content:
            raise SourceParseError("Nội dung chương trống.")
        return content


class UukanshuCrawler(HtmlNovelCrawler):
    source_name = "uukanshu"
    domains = ("uukanshu.com", "www.uukanshu.com", "uukanshu.cc", "www.uukanshu.cc")
    chapter_selectors = ("#list-chapterAll dd a[href]", "#chapterList a[href]", ".chapter-list a[href]", "#list a[href]")
    content_selectors = (".readcotent", "#content", "#contents", ".content", ".article-content", ".read-content")

    async def _get_html(self, url: str) -> str:
        """Use a Chrome TLS fingerprint because uukanshu.cc blocks httpx."""
        try:
            async with AsyncSession(
                impersonate="chrome",
                headers={"Accept-Language": "zh-CN,zh;q=0.9"},
                trust_env=False,
                curl_options={
                    CurlOpt.PROXY: "",
                    CurlOpt.CONNECTTIMEOUT_MS: min(settings.crawler_timeout, 15) * 1000,
                    CurlOpt.TIMEOUT_MS: (settings.crawler_timeout + 5) * 1000,
                    CurlOpt.SERVER_RESPONSE_TIMEOUT_MS: min(settings.crawler_timeout, 15) * 1000,
                    CurlOpt.LOW_SPEED_LIMIT: 1,
                    CurlOpt.LOW_SPEED_TIME: 10,
                },
            ) as client:
                response = await client.get(url, timeout=settings.crawler_timeout)
                response.raise_for_status()
                return response.text
        except Exception as exc:
            logger.warning("%s request failed for %s: %s", self.source_name, url, exc)
            raise SourceRequestError("Không thể tải dữ liệu từ website nguồn.") from exc

    async def get_book_info(self, url: str) -> BookInfo:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        title_node = soup.select_one(".bookinfo h1, h1")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not title:
            raise SourceParseError("Không tìm thấy tên truyện từ website nguồn.")
        author_node = soup.select_one(".booktag a.red")
        description_node = soup.select_one(".bookintro, #intro, .intro, .description, .book-intro")
        return BookInfo(
            url,
            self.source_name,
            self._book_id(url),
            title,
            author_node.get_text(" ", strip=True) if author_node else None,
            description_node.get_text("\n", strip=True) if description_node else None,
            extract_cover_url(soup, url),
        )

    @staticmethod
    def _book_id(url: str) -> str | None:
        match = re.search(r"/(?:b|book)/(\d+)", urlparse(url).path)
        return match.group(1) if match else None

    def _is_chapter_url(self, url: str) -> bool:
        book_id = self._book_id(url)
        return bool(book_id and re.search(rf"/(?:b|book)/{re.escape(book_id)}/[^/]+\.html$", urlparse(url).path))

    @staticmethod
    def _chapter_id(url: str) -> str | None:
        match = re.search(r"/([^/]+)\.html$", urlparse(url).path)
        return match.group(1) if match else None


piaotian_crawler = PiaotianCrawler()
uukanshu_crawler = UukanshuCrawler()
