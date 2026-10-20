import logging
import re
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.crawlers.base import (
    BaseCrawler,
    BookInfo,
    ChapterInfo,
    clean_chapter_text,
    dedupe_chapters,
    extract_cover_url,
    pagination_request_urls,
)
from app.crawlers.errors import SourceParseError, SourceRequestError

logger = logging.getLogger(__name__)

CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000, "萬": 10000, "万": 10000}
CHAPTER_TITLE_RE = re.compile(
    r"(?:\*\*\s*)?(?:正文\s*)?(?:第|Thứ|Chương)\s*([0-9零〇一二两兩三四五六七八九十百千万萬]+)\s*(?:章|chương)?",
    re.IGNORECASE,
)


class XswCrawler(BaseCrawler):
    source_name = "xsw"
    domains = ("m.xsw.tw", "www.xsw.tw", "xsw.tw")

    async def _get_html(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=settings.crawler_timeout,
                follow_redirects=True,
                trust_env=False,
                headers={"User-Agent": "Mozilla/5.0 MyNovelReader/1.0"},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                header = response.content[:4096].decode("ascii", errors="ignore")
                charset_match = re.search(r"charset\s*=\s*['\"]?([\w-]+)", header, re.IGNORECASE)
                encoding = charset_match.group(1) if charset_match else "utf-8"
                if encoding.lower() in {"big5", "big5-hkscs"}:
                    try:
                        return response.content.decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                try:
                    return response.content.decode(encoding, errors="replace")
                except LookupError:
                    return response.content.decode("utf-8", errors="replace")
        except httpx.HTTPStatusError as exc:
            logger.warning("Source returned HTTP %s for %s", exc.response.status_code, url)
            raise SourceRequestError("Không thể tải dữ liệu từ website nguồn.") from exc
        except httpx.HTTPError as exc:
            logger.warning("Source request failed for %s: %s", url, exc)
            raise SourceRequestError("Không thể kết nối tới website nguồn.") from exc

    @staticmethod
    def _book_id(url: str) -> str | None:
        match = re.search(r"/(\d+)/(?:page-\d+\.html)?/?$", urlparse(url).path)
        return match.group(1) if match else None

    async def get_book_info(self, url: str) -> BookInfo:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        book_id = self._book_id(url)
        title_node = soup.select_one("h1") or soup.select_one("h2")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not title:
            title_match = re.search(r"書名\s*[:：]?\s*([^\n]+)", soup.get_text("\n"))
            title = title_match.group(1).strip() if title_match else ""
        cover_url = extract_cover_url(soup, url)
        page_text = soup.get_text("\n", strip=True)
        author_match = re.search(r"作者\s*[:：]?\s*([^\n]+)", page_text)
        author = author_match.group(1).strip() if author_match else None
        description = None
        description_match = re.search(r"小說簡介\s*([^\n].*?)(?=第\d+章|查看目錄|加入書架|$)", page_text, re.DOTALL)
        if description_match:
            description = re.sub(r"\s+", " ", description_match.group(1)).strip()
        if not title:
            raise SourceParseError("Không tìm thấy tên truyện từ website nguồn.")
        return BookInfo(url, self.source_name, book_id, title, author, description, cover_url)

    async def search(self, query: str) -> list[BookInfo]:
        search_url = "https://m.xsw.tw/modules/article/wap_search.php?searchkey=" + quote_plus(query.strip())
        soup = BeautifulSoup(await self._get_html(search_url), "html.parser")
        results: list[BookInfo] = []
        seen: set[str] = set()
        for box in soup.select(".bookbox"):
            link = box.select_one(".bookname a[href]") or box.select_one("h4 a[href]")
            if not link:
                continue
            href = urljoin(search_url, link.get("href", ""))
            if not re.fullmatch(r"https?://(?:m\.)?xsw\.tw/\d+/?", href) or href in seen:
                continue
            title = link.get_text(" ", strip=True)
            if not title:
                continue
            title = link.get_text(" ", strip=True)
            author_node = box.select_one(".author")
            author = None
            if author_node:
                author = re.sub(r"^作者\s*[:：]?\s*", "", author_node.get_text(" ", strip=True)).strip() or None
            description_node = box.select_one(".intro_line")
            description = None
            if description_node:
                description = re.sub(r"^簡介\s*[:：]?\s*", "", description_node.get_text(" ", strip=True)).strip() or None
            cover_url = extract_cover_url(box, search_url)
            seen.add(href)
            results.append(BookInfo(href, self.source_name, self._book_id(href), title, author, description, cover_url))
        if results:
            return results

        for link in soup.select('a[href]'):
            href = urljoin(search_url, link.get("href", ""))
            if not re.fullmatch(r"https?://(?:m\.)?xsw\.tw/\d+/?", href) or href in seen:
                continue
            title = link.get_text(" ", strip=True)
            if not title:
                continue
            seen.add(href)
            results.append(BookInfo(href, self.source_name, self._book_id(href), title, None, None, None))
        return results

    async def get_chapters(self, url: str) -> list[ChapterInfo]:
        book_id = self._book_id(url)
        if not book_id:
            raise SourceParseError("URL truyện xsw.tw không hợp lệ.")
        book_soup = BeautifulSoup(await self._get_html(url), "html.parser")
        canonical_book_id = self._canonical_book_id(book_soup) or book_id
        book_id = canonical_book_id
        first_url = f"https://m.xsw.tw/{book_id}/page-1.html"
        first_soup = BeautifulSoup(await self._get_html(first_url), "html.parser")
        page_numbers = [1]
        for link in first_soup.select('a[href*="page-"]'):
            match = re.search(r"/\d+/page-(\d+)(?:-\d+)?\.html", link.get("href", ""))
            if match:
                page_numbers.append(int(match.group(1)))
        chapters: list[ChapterInfo] = []
        chapter_pattern = re.compile(rf"/{re.escape(book_id)}/(\d+)\.html$")

        max_page = max(page_numbers)
        discovered_paginated_pages = max_page > 1
        for page_number in range(1, max_page + 1):
            page_url = first_url if page_number == 1 else f"https://m.xsw.tw/{book_id}/page-{page_number}.html"
            soup = first_soup if page_number == 1 else BeautifulSoup(await self._get_html(page_url), "html.parser")
            chapters.extend(self._parse_chapter_links(soup, page_url, chapter_pattern))

        if not discovered_paginated_pages:
            seen_count = len(dedupe_chapters(chapters))
            page_number = 2
            while True:
                page_url = f"https://m.xsw.tw/{book_id}/page-{page_number}.html"
                try:
                    soup = BeautifulSoup(await self._get_html(page_url), "html.parser")
                except SourceRequestError:
                    break
                chapters.extend(self._parse_chapter_links(soup, page_url, chapter_pattern))
                unique_count = len(dedupe_chapters(chapters))
                if unique_count == seen_count:
                    break
                seen_count = unique_count
                page_number += 1

        seen_page_urls = {
            (first_url if page_number == 1 else f"https://m.xsw.tw/{book_id}/page-{page_number}.html").rstrip("/")
            for page_number in range(1, max_page + 1)
        }
        queued_page_urls = [href for href in pagination_request_urls(first_soup, first_url) if "page-" not in href]
        while queued_page_urls:
            page_url = queued_page_urls.pop(0)
            normalized = page_url.rstrip("/")
            if normalized in seen_page_urls:
                continue
            seen_page_urls.add(normalized)
            try:
                soup = BeautifulSoup(await self._get_html(page_url), "html.parser")
            except SourceRequestError:
                continue
            chapters.extend(self._parse_chapter_links(soup, page_url, chapter_pattern))
            queued_page_urls.extend(href for href in pagination_request_urls(soup, page_url) if "page-" not in href)

        if not chapters:
            raise SourceParseError("Không tìm thấy danh sách chương.")
        # XSW lists newest entries first and can insert late chapters whose
        # displayed number is lower than surrounding entries. Preserve source
        # order instead of sorting by that display number. Reader positions
        # increase oldest-to-newest, so reverse the source listing before
        # assigning unique positions (also handles split/repeated chapter labels).
        ordered = list(reversed(dedupe_chapters(chapters, sort_by_number=False)))
        return [
            ChapterInfo(index, chapter.source_chapter_id, chapter.title, chapter.url)
            for index, chapter in enumerate(ordered, start=1)
        ]

    async def get_latest_chapters(self, url: str, limit: int = 40) -> list[ChapterInfo]:
        book_id = self._book_id(url)
        if not book_id:
            raise SourceParseError("URL truyện xsw.tw không hợp lệ.")
        book_soup = BeautifulSoup(await self._get_html(url), "html.parser")
        book_id = self._canonical_book_id(book_soup) or book_id
        first_url = f"https://m.xsw.tw/{book_id}/page-1.html"
        soup = BeautifulSoup(await self._get_html(first_url), "html.parser")
        pattern = re.compile(rf"/{re.escape(book_id)}/(\d+)\.html$")
        chapters = self._parse_chapter_links(soup, first_url, pattern)
        if not chapters:
            raise SourceParseError("Không tìm thấy danh sách chương gần nhất.")
        return chapters[:max(1, limit)]

    @staticmethod
    def _parse_chapter_links(soup: BeautifulSoup, page_url: str, chapter_pattern: re.Pattern[str]) -> list[ChapterInfo]:
        chapters: list[ChapterInfo] = []
        for link in soup.select("a[href]"):
            href = urljoin(page_url, link.get("href", ""))
            match = chapter_pattern.search(urlparse(href).path)
            title = link.get_text(" ", strip=True)
            number = chapter_number_from_title(title)
            if not match or number is None:
                continue
            source_id = match.group(1)
            chapters.append(ChapterInfo(number, source_id, title, href))
        return chapters

    @staticmethod
    def _canonical_book_id(soup: BeautifulSoup) -> str | None:
        for link in soup.select('a[href*="page-1.html"]'):
            text = link.get_text(" ", strip=True)
            href = link.get("href", "")
            if text not in {"[正序]", "正序"}:
                continue
            match = re.search(r"/(\d+)/page-1\.html", href)
            if match:
                return match.group(1)
        return None

    async def get_chapter_content(self, url: str) -> str:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        content_node = soup.select_one(".content")
        if not content_node:
            raise SourceParseError("Không tìm thấy nội dung chương.")
        for node in content_node.select("script, style, nav, .pagination"):
            node.decompose()
        content = re.sub(r"\n{3,}", "\n\n", content_node.get_text("\n", strip=True))
        content = clean_chapter_text(content)
        if not content:
            raise SourceParseError("Nội dung chương đang trống.")
        return content


crawler = XswCrawler()


def chapter_number(raw: str) -> int:
    if raw.isdigit():
        return int(raw)
    total = 0
    section = 0
    number = 0
    for char in raw:
        if char in CHINESE_DIGITS:
            number = CHINESE_DIGITS[char]
            continue
        unit = CHINESE_UNITS.get(char)
        if unit is None:
            continue
        if unit >= 10000:
            section = (section + number) * unit
            total += section
            section = 0
        else:
            section += (number or 1) * unit
        number = 0
    return total + section + number


def chapter_number_from_title(title: str) -> int | None:
    match = CHAPTER_TITLE_RE.search(title)
    if not match:
        return None
    return chapter_number(match.group(1))
