import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.crawlers.base import BaseCrawler, BookInfo, ChapterInfo, clean_chapter_text, dedupe_chapters, extract_cover_url
from app.crawlers.errors import SourceParseError, SourceRequestError

logger = logging.getLogger(__name__)
_CHAPTER_NUMBER = re.compile(r"\u7b2c\s*(\d+)\s*\u7ae0")


class HtmlNovelCrawler(BaseCrawler):
    chapter_selectors: tuple[str, ...] = ()
    content_selectors: tuple[str, ...] = ("#content", "#contents", ".content", ".article-content", ".read-content")

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
            number_match = _CHAPTER_NUMBER.search(title)
            chapters.append(ChapterInfo(int(number_match.group(1)) if number_match else index, self._chapter_id(href), title, href))
        if not chapters:
            raise SourceParseError("Kh\u00f4ng t\u00ecm th\u1ea5y danh s\u00e1ch ch\u01b0\u01a1ng.")
        return dedupe_chapters(chapters)

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
    domains = ("piaotian.com", "www.piaotian.com")
    chapter_selectors = ("#list a[href]", ".centent a[href]", ".chapterlist a[href]")
    content_selectors = ("#content", "#contents", ".content", ".yd_text2", ".read-content")

    @staticmethod
    def _book_id(url: str) -> str | None:
        match = re.search(r"/html/\d+/(\d+)", urlparse(url).path)
        return match.group(1) if match else None

    def _is_chapter_url(self, url: str) -> bool:
        book_id = self._book_id(url)
        return bool(book_id and re.search(rf"/html/\d+/{re.escape(book_id)}/\d+\.html$", urlparse(url).path))

    @staticmethod
    def _chapter_id(url: str) -> str | None:
        match = re.search(r"/(\d+)\.html$", urlparse(url).path)
        return match.group(1) if match else None


class UukanshuCrawler(HtmlNovelCrawler):
    source_name = "uukanshu"
    domains = ("uukanshu.com", "www.uukanshu.com")
    chapter_selectors = ("#chapterList a[href]", ".chapter-list a[href]", "#list a[href]")

    @staticmethod
    def _book_id(url: str) -> str | None:
        match = re.search(r"/b/(\d+)", urlparse(url).path)
        return match.group(1) if match else None

    def _is_chapter_url(self, url: str) -> bool:
        book_id = self._book_id(url)
        return bool(book_id and re.search(rf"/b/{re.escape(book_id)}/[^/]+\.html$", urlparse(url).path))

    @staticmethod
    def _chapter_id(url: str) -> str | None:
        match = re.search(r"/([^/]+)\.html$", urlparse(url).path)
        return match.group(1) if match else None


piaotian_crawler = PiaotianCrawler()
uukanshu_crawler = UukanshuCrawler()
