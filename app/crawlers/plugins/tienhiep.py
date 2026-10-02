import json
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.crawlers.base import BaseCrawler, BookInfo, ChapterInfo, clean_chapter_text, dedupe_chapters, extract_cover_url
from app.crawlers.errors import SourceParseError, SourceRequestError


class TienHiepCrawler(BaseCrawler):
    """Crawler for the Vietnamese-first Tiên Hiệp Lâu catalogue."""

    source_name = "tienhiep"
    source_type = "vi"
    domains = ("tienhiep.vercel.app",)
    _chapter_path = re.compile(r"/books/([^/]+)/chapters/(\d+)$")

    async def _get_html(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=settings.crawler_timeout, follow_redirects=True, trust_env=False, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "vi-VN,vi;q=0.9"}) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.text
        except httpx.HTTPError as exc:
            raise SourceRequestError("Không thể tải dữ liệu từ nguồn Truyện Việt.") from exc

    @staticmethod
    def _book_id(url: str) -> str:
        match = re.match(r"/books/([^/?#]+?)/?$", urlparse(url).path)
        if not match:
            raise SourceParseError("URL Tiên Hiệp Lâu phải có dạng /books/<slug>.")
        return match.group(1)

    @staticmethod
    def _book_json(soup: BeautifulSoup) -> dict:
        for node in soup.select('script[type="application/ld+json"]'):
            try:
                value = json.loads(node.string or "")
            except json.JSONDecodeError:
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, dict) and item.get("@type") == "Book":
                    return item
        return {}

    async def get_book_info(self, url: str) -> BookInfo:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        data = self._book_json(soup)
        title = str(data.get("name") or (soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else "")).strip()
        if not title:
            raise SourceParseError("Không tìm thấy tên truyện từ nguồn Truyện Việt.")
        author_data = data.get("author") if isinstance(data.get("author"), dict) else {}
        genres = data.get("genre") or []
        if isinstance(genres, str):
            genres = [item.strip() for item in genres.split(",") if item.strip()]
        return BookInfo(url, self.source_name, self._book_id(url), title, author_data.get("name"), data.get("description"), data.get("image") or extract_cover_url(soup, url), int(data.get("numberOfPages") or 0) or None, genres)

    async def get_chapters(self, url: str) -> list[ChapterInfo]:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        count = int(self._book_json(soup).get("numberOfPages") or 0)
        pages = max(1, (count + 99) // 100)
        book_id = self._book_id(url)
        chapters: list[ChapterInfo] = []
        for page in range(pages):
            try:
                payload = json.loads(await self._get_html(f"https://tienhiep.vercel.app/api/books/{book_id}/chapters?page={page}"))
                for item in payload.get("chapters", []):
                    number = int(item.get("chapter_number") or 0)
                    if number:
                        chapters.append(ChapterInfo(number, str(item.get("id") or number), str(item.get("title") or f"Chương {number}"), f"{url.rstrip('/')}/chapters/{number}"))
                continue
            except (json.JSONDecodeError, SourceRequestError, ValueError, AttributeError):
                # The public API is the primary source. Keep an SSR fallback
                # for a temporary API outage rather than failing a whole crawl.
                page_url = url if page == 0 else f"{url}?chaptersPage={page}"
                page_soup = soup if page == 0 else BeautifulSoup(await self._get_html(page_url), "html.parser")
                for link in page_soup.select("a[href]"):
                    href = urljoin(page_url, link.get("href", ""))
                    match = self._chapter_path.search(urlparse(href).path)
                    if not match:
                        continue
                    title = link.get_text(" ", strip=True)
                    if title:
                        chapters.append(ChapterInfo(int(match.group(2)), match.group(2), title, href))
        if not chapters:
            raise SourceParseError("Không tìm thấy danh sách chương từ nguồn Truyện Việt.")
        return dedupe_chapters(chapters)

    async def get_chapter_content(self, url: str) -> str:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        for selector in ("article", "[class*='whitespace-pre-wrap']", ".chapter-content", ".prose"):
            node = soup.select_one(selector)
            if node:
                for removable in node.select("script,style,nav,header,footer,button"):
                    removable.decompose()
                content = clean_chapter_text(node.get_text("\n", strip=True))
                if content:
                    return content
        raise SourceParseError("Không tìm thấy nội dung chương từ nguồn Truyện Việt.")

    async def search(self, query: str) -> list[BookInfo]:
        return []


crawler = TienHiepCrawler()
