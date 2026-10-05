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


class TrxsCrawler(BaseCrawler):
    source_name = "trxs"
    domains = ("www.trxs.cc", "trxs.cc")

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
        match = re.search(r"/tongren/(\d+)(?:\.html|/)", urlparse(url).path)
        return match.group(1) if match else None

    async def get_book_info(self, url: str) -> BookInfo:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        title_node = soup.select_one("h1")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        title = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
        cover_url = extract_cover_url(soup, url)
        author_node = soup.find(string=re.compile(r"作者"))
        author = None
        if author_node:
            author = re.sub(r"^作者\s*[:：]?\s*", "", author_node.parent.get_text(" ", strip=True))
        description_node = soup.find(string=re.compile(r"简介"))
        description = None
        if description_node and description_node.parent:
            description = description_node.parent.parent.get_text("\n", strip=True)
        if not title:
            raise SourceParseError("Không tìm thấy tên truyện từ website nguồn.")
        return BookInfo(url, self.source_name, self._book_id(url), title, author, description, cover_url)

    async def get_chapters(self, url: str) -> list[ChapterInfo]:
        book_id = self._book_id(url)
        if not book_id:
            raise SourceParseError("URL truyện trxs.cc không hợp lệ.")
        first_soup = BeautifulSoup(await self._get_html(url), "html.parser")
        page_urls = [url]
        seen_pages = {url.rstrip("/")}
        for link in first_soup.select("a[href]"):
            href = urljoin(url, link.get("href", ""))
            path = urlparse(href).path
            if not re.search(rf"/tongren/{re.escape(book_id)}(?:\.html|/index(?:_\d+)?\.html)$", path):
                continue
            normalized = href.rstrip("/")
            if normalized in seen_pages:
                continue
            seen_pages.add(normalized)
            page_urls.append(href)
        for href in pagination_request_urls(first_soup, url):
            normalized = href.rstrip("/")
            if normalized in seen_pages:
                continue
            seen_pages.add(normalized)
            page_urls.append(href)
        chapters: list[ChapterInfo] = []
        pattern = re.compile(rf"/tongren/{re.escape(book_id)}/(\d+)\.html$")
        for page_url in page_urls:
            soup = first_soup if page_url == url else BeautifulSoup(await self._get_html(page_url), "html.parser")
            for link in soup.select("a[href]"):
                href = urljoin(page_url, link.get("href", ""))
                match = pattern.search(urlparse(href).path)
                if not match:
                    continue
                number = int(match.group(1))
                title = link.get_text(" ", strip=True) or f"第{number}节"
                chapters.append(ChapterInfo(number, str(number), title, href))
        if not chapters:
            raise SourceParseError("Không tìm thấy danh sách chương.")
        return dedupe_chapters(chapters)

    async def get_latest_chapters(self, url: str, limit: int = 40) -> list[ChapterInfo]:
        book_id = self._book_id(url)
        if not book_id:
            raise SourceParseError("URL truyện trxs.cc không hợp lệ.")
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        pattern = re.compile(rf"/tongren/{re.escape(book_id)}/(\d+)\.html$")
        chapters: list[ChapterInfo] = []
        for link in soup.select("a[href]"):
            href = urljoin(url, link.get("href", ""))
            match = pattern.search(urlparse(href).path)
            if match:
                number = int(match.group(1))
                chapters.append(ChapterInfo(number, str(number), link.get_text(" ", strip=True) or f"第{number}节", href))
        if not chapters:
            raise SourceParseError("Không tìm thấy danh sách chương gần nhất.")
        return dedupe_chapters(chapters)[-max(1, limit):]

    async def get_chapter_content(self, url: str) -> str:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        for selector in (".read_chapterDetail", ".contentbox", ".content", ".read-content", ".novel-content", "#content"):
            content_node = soup.select_one(selector)
            if content_node:
                break
        else:
            heading = soup.select_one("h1")
            content_node = heading.parent if heading else None
        if not content_node:
            raise SourceParseError("Không tìm thấy nội dung chương.")
        for node in content_node.select("script, style, nav, .page, .pagination"):
            node.decompose()
        content = content_node.get_text("\n", strip=True)
        content = re.sub(r"\n{3,}", "\n\n", content)
        content = clean_chapter_text(content)
        if not content:
            raise SourceParseError("Nội dung chương đang trống.")
        return content


crawler = TrxsCrawler()
