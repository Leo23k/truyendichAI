import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.crawlers.base import BaseCrawler, BookInfo, ChapterInfo, clean_chapter_text, dedupe_chapters, extract_cover_url
from app.crawlers.errors import SourceParseError, SourceRequestError
from app.crawlers.plugins.xsw import CHAPTER_TITLE_RE, CHINESE_DIGITS, chapter_number


class HjwzwCrawler(BaseCrawler):
    source_name = "hjwzw"
    domains = ("tw.hjwzw.com",)

    async def _get_html(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=settings.crawler_timeout, follow_redirects=True, trust_env=False, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-TW,zh;q=0.9"}) as client:
                response = await client.get(url)
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError as exc:
            raise SourceRequestError("Không thể tải dữ liệu từ website nguồn.") from exc
        except httpx.HTTPError as exc:
            raise SourceRequestError("Không thể kết nối tới website nguồn.") from exc

    @staticmethod
    def _book_id(url: str) -> str | None:
        match = re.search(r"/Book/(\d+)", urlparse(url).path)
        return match.group(1) if match else None

    @staticmethod
    def _chapter_number(raw: str) -> int:
        if raw.isdigit():
            return int(raw)
        if raw and all(char in CHINESE_DIGITS for char in raw):
            return int("".join(str(CHINESE_DIGITS[char]) for char in raw))
        return chapter_number(raw)

    async def get_book_info(self, url: str) -> BookInfo:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        title_node = soup.select_one("h1")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not title:
            raise SourceParseError("Không tìm thấy tên truyện từ website nguồn.")
        text = soup.get_text("\n", strip=True)
        author = re.search(r"作者\s*[:：]?\s*([^\n\s]+)", text)
        return BookInfo(url, self.source_name, self._book_id(url), title, author.group(1) if author else None, None, extract_cover_url(soup, url))

    async def get_chapters(self, url: str) -> list[ChapterInfo]:
        book_id = self._book_id(url)
        if not book_id:
            raise SourceParseError("URL truyện không hợp lệ.")
        soup = BeautifulSoup(await self._get_html(urljoin(url, f"/Book/Chapter/{book_id}")), "html.parser")
        chapters: list[ChapterInfo] = []
        for link in soup.select("a[href]"):
            href = urljoin(url, link.get("href", ""))
            match = re.search(rf"/Book/Read/{re.escape(book_id)},(\d+)$", urlparse(href).path)
            number = CHAPTER_TITLE_RE.search(link.get_text(" ", strip=True))
            if match and number:
                chapters.append(ChapterInfo(self._chapter_number(number.group(1)), match.group(1), link.get_text(" ", strip=True), href))
        if not chapters:
            raise SourceParseError("Không tìm thấy danh sách chương.")
        return dedupe_chapters(chapters)

    async def get_chapter_content(self, url: str) -> str:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        candidates = [node for node in soup.select("div") if len(node.get_text(" ", strip=True)) > 300]
        if not candidates:
            raise SourceParseError("Không tìm thấy nội dung chương.")
        content = clean_chapter_text(max(candidates, key=lambda node: len(node.get_text(" ", strip=True))).get_text("\n", strip=True))
        if not content:
            raise SourceParseError("Nội dung chương đang trống.")
        return content


crawler = HjwzwCrawler()
