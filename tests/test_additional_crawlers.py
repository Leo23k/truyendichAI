import pytest

from app.crawlers.plugins.fanqie import FanqieCrawler
from app.crawlers.plugins.text_sites import PiaotianCrawler, UukanshuCrawler


@pytest.mark.asyncio
async def test_fanqie_parses_server_rendered_book_and_reader(monkeypatch) -> None:
    crawler = FanqieCrawler()
    pages = {
        "book": """<h1>Fanqie Book</h1><span class='author-name-text'>Author</span><div class='page-abstract-content'>Intro</div><a href='/reader/111'>第1章 Start</a><a href='/reader/112'>第2章 Next</a>""",
        "chapter": "<div class='muye-reader-content noselect'><p>First paragraph.</p><p>Second paragraph.</p></div>",
    }

    async def html(url: str) -> str:
        return pages["chapter"] if "/reader/" in url else pages["book"]

    monkeypatch.setattr(crawler, "_get_html", html)
    info = await crawler.get_book_info("https://fanqienovel.com/page/999")
    chapters = await crawler.get_chapters(info.source_url)
    assert info.source_book_id == "999"
    assert [chapter.source_chapter_id for chapter in chapters] == ["111", "112"]
    assert await crawler.get_chapter_content(chapters[0].url) == "First paragraph.\nSecond paragraph."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crawler_type", "book_url", "chapter_url"),
    [
        (PiaotianCrawler, "https://www.piaotian.com/html/1/42/index.html", "https://www.piaotian.com/html/1/42/100.html"),
        (UukanshuCrawler, "https://www.uukanshu.com/b/42/", "https://www.uukanshu.com/b/42/100.html"),
    ],
)
async def test_html_site_crawlers_parse_book_chapter_and_content(monkeypatch, crawler_type, book_url, chapter_url) -> None:
    crawler = crawler_type()
    book_html = f"<h1>Test Book</h1><div id='intro'>Intro</div><div id='list'><a href='{chapter_url}'>第1章 Start</a></div>"
    chapter_html = "<div id='content'>Paragraph one.<br/>Paragraph two.</div>"

    async def html(url: str) -> str:
        return chapter_html if url == chapter_url else book_html

    monkeypatch.setattr(crawler, "_get_html", html)
    info = await crawler.get_book_info(book_url)
    chapters = await crawler.get_chapters(book_url)
    assert info.title == "Test Book"
    assert len(chapters) == 1
    assert await crawler.get_chapter_content(chapter_url) == "Paragraph one.\nParagraph two."
