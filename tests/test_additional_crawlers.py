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
async def test_fanqie_search_returns_only_verified_crawlable_books(monkeypatch) -> None:
    crawler = FanqieCrawler()

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"ret_data": [{"book_id": "999", "title": "Night", "author": "Author", "abstract": "Intro", "thumb_url": "https://cover.example/book.jpg"}]}}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    async def chapters(_url: str):
        return [object(), object()]

    monkeypatch.setattr("app.crawlers.plugins.fanqie.httpx.AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(crawler, "get_chapters", chapters)
    results = await crawler.search("Night")

    assert len(results) == 1
    assert results[0].source_url == "https://fanqienovel.com/page/999"
    assert results[0].chapter_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crawler_type", "book_url", "chapter_url"),
    [
        (PiaotianCrawler, "https://www.piaotia.com/bookinfo/1/42.html", "https://www.piaotia.com/html/1/42/100.html"),
        (UukanshuCrawler, "https://uukanshu.cc/book/42/", "https://uukanshu.cc/book/42/100.html"),
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


@pytest.mark.asyncio
async def test_piaotia_uses_document_gbk_charset_when_header_is_wrong(monkeypatch) -> None:
    crawler = PiaotianCrawler()

    class Response:
        content = b'<meta http-equiv="Content-Type" content="text/html; charset=gbk"><h1>\xc6\xd5\xc2\xde</h1>'
        text = "mojibake"

        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("app.crawlers.plugins.text_sites.httpx.AsyncClient", lambda **_kwargs: Client())
    assert "普罗" in await crawler._get_html("https://www.piaotia.com/bookinfo/1/42.html")


@pytest.mark.asyncio
async def test_piaotia_reads_unwrapped_reader_content(monkeypatch) -> None:
    crawler = PiaotianCrawler()

    async def html(_url: str) -> str:
        return '<div class="toplink">Previous</div><br><br>&nbsp;&nbsp;第1章 Start<br><br>&nbsp;&nbsp;Paragraph one.<br><br>&nbsp;&nbsp;Paragraph two.<div class="bottomlink">Next</div>'

    monkeypatch.setattr(crawler, "_get_html", html)
    assert await crawler.get_chapter_content("https://www.piaotia.com/html/1/42/100.html") == "第1章 Start\nParagraph one.\nParagraph two."
