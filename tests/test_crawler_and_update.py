from datetime import datetime

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.crawlers.base import BaseCrawler, BookInfo, ChapterInfo, clean_chapter_text, extract_cover_url
from app.crawlers.plugins.shuba69 import Shuba69Crawler
from app.database import Base
from app.models import Book, Chapter
from app.services.book_service import BookService


BOOK_HTML = """
<html><body>
<h1>测试小说</h1>
<img src="/files/article/image/12/12345/12345s.jpg">
<div class="intro">这是简介</div>
<div><a href="/txt/12345/101">第1章 开始</a><a href="/txt/12345/102">第2章 继续</a></div>
</body></html>
"""


@pytest.mark.asyncio
async def test_shuba_parser(monkeypatch) -> None:
    crawler = Shuba69Crawler()
    monkeypatch.setattr(crawler, "_get_html", lambda url: _html())

    info = await crawler.get_book_info("https://www.69shuba.com/book/12345.htm")
    chapters = await crawler.get_chapters(info.source_url)

    assert info.title == "测试小说"
    assert info.source_book_id == "12345"
    assert info.cover_url == "https://www.69shuba.com/files/article/image/12/12345/12345s.jpg"
    assert len(chapters) == 2
    assert chapters[0].source_chapter_id == "101"


async def _html() -> str:
    return BOOK_HTML


def test_clean_chapter_text_removes_navigation_footer() -> None:
    content = clean_chapter_text(
        "第5章 仙緣難得\n"
        "正文第一段。\n"
        "(本章完)\n"
        ">>章節報錯<<\n"
        "上一章\n"
        "目錄\n"
        "下一章\n"
        "首頁\n"
    )

    assert content == "第5章 仙緣難得\n正文第一段。"


def test_extract_cover_url_priority_and_absolute_urls() -> None:
    soup = BeautifulSoup(
        """
        <html><head>
          <meta property="og:image" content="//cdn.example.com/og.webp">
          <meta name="twitter:image" content="/twitter.jpg">
          <script type="application/ld+json">{"image": "/jsonld.jpg"}</script>
        </head><body><img data-src="/lazy.jpg"><img src="/plain.jpg"></body></html>
        """,
        "html.parser",
    )

    assert extract_cover_url(soup, "https://site.example/book/1") == "https://cdn.example.com/og.webp"


def test_extract_cover_url_fallbacks() -> None:
    twitter = BeautifulSoup('<meta name="twitter:image" content="/twitter.jpg"><img src="/plain.jpg">', "html.parser")
    json_ld = BeautifulSoup('<script type="application/ld+json">{"image": {"url": "/jsonld.jpg"}}</script>', "html.parser")
    data_src = BeautifulSoup('<img data-src="/lazy.jpg">', "html.parser")
    embedded = BeautifulSoup("<script>window.__BOOK__={cover:'/embedded.jpg'}</script>", "html.parser")

    assert extract_cover_url(twitter, "https://site.example/book/1") == "https://site.example/twitter.jpg"
    assert extract_cover_url(json_ld, "https://site.example/book/1") == "https://site.example/jsonld.jpg"
    assert extract_cover_url(data_src, "https://site.example/book/1") == "https://site.example/lazy.jpg"
    assert extract_cover_url(embedded, "https://site.example/book/1") == "https://site.example/embedded.jpg"


class FakeCrawler(BaseCrawler):
    source_name = "test"
    domains = ("example.com",)

    def __init__(self) -> None:
        self.chapter_count = 2

    async def get_book_info(self, url: str) -> BookInfo:
        return BookInfo(url, self.source_name, "book-1", "原题", "作者", None, None)

    async def get_chapters(self, url: str) -> list[ChapterInfo]:
        return [ChapterInfo(index, str(index), f"第{index}章", f"https://example.com/{index}") for index in range(1, self.chapter_count + 1)]

    async def get_chapter_content(self, url: str) -> str:
        return "内容"


@pytest.mark.asyncio
async def test_update_adds_only_new_chapters() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    crawler = FakeCrawler()
    service = BookService(session, crawler)

    book, added = await service.import_book("https://example.com/book")
    assert added == 2
    session.scalar(select(Chapter).where(Chapter.chapter_number == 1)).title = "bản cũ đã sửa"
    session.commit()

    crawler.chapter_count = 5
    assert await service.update_book(book) == 3
    assert await service.update_book(book) == 0

    chapters = session.scalars(select(Chapter).order_by(Chapter.chapter_number)).all()
    assert len(chapters) == 5
    assert chapters[0].title == "bản cũ đã sửa"
    assert isinstance(book.last_checked_at, datetime)
