import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.crawlers.base import BaseCrawler, BookInfo, ChapterInfo, clean_chapter_text, dedupe_chapters, extract_cover_url
from app.crawlers.errors import CrawlerError, SourceParseError, SourceRequestError

logger = logging.getLogger(__name__)
_CHAPTER_NUMBER = re.compile(r"\u7b2c\s*(\d+)\s*\u7ae0")


class FanqieCrawler(BaseCrawler):
    source_name = "fanqienovel"
    domains = ("fanqienovel.com", "www.fanqienovel.com")

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
            logger.warning("Fanqie returned HTTP %s for %s", exc.response.status_code, url)
            raise SourceRequestError("Kh\u00f4ng th\u1ec3 t\u1ea3i d\u1eef li\u1ec7u t\u1eeb Fanqie.") from exc
        except httpx.HTTPError as exc:
            raise SourceRequestError("Kh\u00f4ng th\u1ec3 k\u1ebft n\u1ed1i t\u1edbi Fanqie.") from exc

    @staticmethod
    def _book_id(url: str) -> str | None:
        match = re.search(r"/page/(\d+)", urlparse(url).path)
        return match.group(1) if match else None

    async def get_book_info(self, url: str) -> BookInfo:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        title_node = soup.select_one("h1")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        author_node = soup.select_one(".author-name-text, .author-name")
        description_node = soup.select_one(".page-abstract-content, .book-intro, .abstract")
        if not title:
            raise SourceParseError("Kh\u00f4ng t\u00ecm th\u1ea5y t\u00ean truy\u1ec7n tr\u00ean Fanqie.")
        return BookInfo(
            url,
            self.source_name,
            self._book_id(url),
            title,
            author_node.get_text(" ", strip=True) if author_node else None,
            description_node.get_text("\n", strip=True) if description_node else None,
            extract_cover_url(soup, url),
        )

    async def search(self, query: str) -> list[BookInfo]:
        query = query.strip()
        if not query:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=settings.crawler_timeout,
                trust_env=False,
                headers={"User-Agent": "com.ss.android.article.news/7.0.3 (Linux; U; Android 12; zh_CN; Pixel 6)"},
            ) as client:
                response = await client.get(
                    "https://novel.snssdk.com/api/novel/channel/homepage/search/search/v1/",
                    params={"device_platform": "android", "parent_enterfrom": "novel_channel_search.tab.", "offset": "0", "aid": "1967", "q": query},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceRequestError("Không thể tìm kiếm Fanqie lúc này.") from exc
        records = payload.get("data", {}).get("ret_data", []) if isinstance(payload, dict) else []
        candidates = [record for record in records if isinstance(record, dict) and str(record.get("book_id", "")).isdigit()]

        async def verify(record: dict) -> BookInfo | None:
            book_id = str(record["book_id"])
            source_url = f"https://fanqienovel.com/page/{book_id}"
            try:
                chapters = await self.get_chapters(source_url)
            except (CrawlerError, httpx.HTTPError):
                return None
            if not chapters:
                return None
            return BookInfo(
                source_url,
                self.source_name,
                book_id,
                str(record.get("title") or "").strip(),
                str(record.get("author") or "").strip() or None,
                str(record.get("abstract") or "").strip() or None,
                str(record.get("thumb_url") or "").strip() or None,
                len(chapters),
            )

        verified = await asyncio.gather(*(verify(record) for record in candidates[:10]))
        return [book for book in verified if book and book.title]

    async def get_chapters(self, url: str) -> list[ChapterInfo]:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        chapters: list[ChapterInfo] = []
        for index, link in enumerate(soup.select('a[href^="/reader/"]'), start=1):
            href = urljoin(url, link.get("href", ""))
            match = re.search(r"/reader/(\d+)", urlparse(href).path)
            title = link.get_text(" ", strip=True)
            if not match or not title:
                continue
            number_match = _CHAPTER_NUMBER.search(title)
            chapters.append(ChapterInfo(int(number_match.group(1)) if number_match else index, match.group(1), title, href))
        if not chapters:
            raise SourceParseError("Kh\u00f4ng t\u00ecm th\u1ea5y danh s\u00e1ch ch\u01b0\u01a1ng tr\u00ean Fanqie.")
        return dedupe_chapters(chapters)

    async def get_chapter_content(self, url: str) -> str:
        soup = BeautifulSoup(await self._get_html(url), "html.parser")
        content_node = soup.select_one(".muye-reader-content.noselect, .muye-reader-content, .reader-content")
        if not content_node:
            raise SourceParseError("Kh\u00f4ng t\u00ecm th\u1ea5y n\u1ed9i dung ch\u01b0\u01a1ng tr\u00ean Fanqie.")
        for node in content_node.select("script, style"):
            node.decompose()
        content = clean_chapter_text(content_node.get_text("\n", strip=True))
        if not content:
            raise SourceParseError("N\u1ed9i dung ch\u01b0\u01a1ng Fanqie tr\u1ed1ng ho\u1eb7c b\u1ecb kh\u00f3a.")
        return content


crawler = FanqieCrawler()
