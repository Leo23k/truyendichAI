import logging
import re
from urllib.parse import urljoin, urlparse

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


class Shuba69Crawler(BaseCrawler):
    source_name = "69shuba"
    domains = ("www.69shuba.com", "69shuba.com")

    async def _get_html(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=settings.crawler_timeout,
                follow_redirects=True,
                trust_env=False,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Referer": "https://www.69shuba.com/",
                },
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as exc:
            logger.warning("Source returned HTTP %s for %s", exc.response.status_code, url)
            raise SourceRequestError("Không thể tải dữ liệu từ website nguồn.") from exc
        except httpx.HTTPError as exc:
            logger.warning("Source request failed for %s: %s", url, exc)
            raise SourceRequestError("Không thể kết nối tới website nguồn.") from exc

    @staticmethod
    def _book_id(url: str) -> str | None:
        match = re.search(r"/book/(\d+)", urlparse(url).path)
        return match.group(1) if match else None

    async def get_book_info(self, url: str) -> BookInfo:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        title_node = soup.select_one("h1")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        cover_url = extract_cover_url(soup, url)
        text = soup.get_text(" ", strip=True)
        author_match = re.search(r"作者[:：]\s*([^\s]+)", text)
        author = author_match.group(1) if author_match else None
        description_node = soup.select_one(".intro, .desc, #intro")
        description = description_node.get_text("\n", strip=True) if description_node else None
        if not title:
            raise SourceParseError("Không tìm thấy tên truyện từ website nguồn.")
        return BookInfo(url, self.source_name, self._book_id(url), title, author, description, cover_url)

    async def get_chapters(self, url: str) -> list[ChapterInfo]:
        first_soup = BeautifulSoup(await self._get_html(url), "html.parser")
        book_id = self._book_id(url)
        page_urls = [url]
        if book_id:
            seen_pages = {url.rstrip("/")}
            for link in first_soup.select("a[href]"):
                href = urljoin(url, link.get("href", ""))
                path = urlparse(href).path
                if not re.search(rf"/book/{re.escape(book_id)}(?:[_-]\d+)?\.html?$", path):
                    continue
                normalized = href.rstrip("/")
                if normalized in seen_pages:
                    continue
                seen_pages.add(normalized)
                page_urls.append(href)
        for href in pagination_request_urls(first_soup, url):
            normalized = href.rstrip("/")
            if normalized not in {page.rstrip("/") for page in page_urls}:
                page_urls.append(href)
        chapters: list[ChapterInfo] = []
        for page_url in page_urls:
            soup = first_soup if page_url == url else BeautifulSoup(await self._get_html(page_url), "html.parser")
            for link in soup.select('a[href*="/txt/"]'):
                href = urljoin(page_url, link.get("href", ""))
                match = re.search(r"/txt/\d+/(\d+)", href)
                title = link.get_text(" ", strip=True)
                number_match = re.search(r"第\s*(\d+)\s*章", title)
                if href and match and number_match:
                    chapters.append(ChapterInfo(int(number_match.group(1)), match.group(1), title, href))
        if not chapters:
            raise SourceParseError("Không tìm thấy danh sách chương.")
        return dedupe_chapters(chapters)

    async def get_latest_chapters(self, url: str, limit: int = 40) -> list[ChapterInfo]:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        chapters: list[ChapterInfo] = []
        for link in soup.select('a[href*="/txt/"]'):
            href = urljoin(url, link.get("href", ""))
            match = re.search(r"/txt/\d+/(\d+)", href)
            title = link.get_text(" ", strip=True)
            number_match = re.search(r"第\s*(\d+)\s*章", title)
            if match and number_match:
                chapters.append(ChapterInfo(int(number_match.group(1)), match.group(1), title, href))
        if not chapters:
            raise SourceParseError("Không tìm thấy danh sách chương gần nhất.")
        return dedupe_chapters(chapters)[-max(1, limit):]

    async def get_chapter_content(self, url: str) -> str:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        content_node = soup.select_one("#txt_content, .txtnav, .content, .read-content")
        if not content_node:
            raise SourceParseError("Không tìm thấy nội dung chương.")
        for node in content_node.select("script, style"):
            node.decompose()
        content = clean_chapter_text(content_node.get_text("\n", strip=True))
        if not content:
            raise SourceParseError("Nội dung chương đang trống.")
        return content


crawler = Shuba69Crawler()
