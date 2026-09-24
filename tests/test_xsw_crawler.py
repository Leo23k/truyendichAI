import re

import pytest

from app.crawlers.plugins.xsw import XswCrawler, chapter_number
from app.crawlers.base import ChapterInfo


BOOK_HTML = """
<html><body>
<meta property="og:image" content="//img.xsw.tw/og/cover.webp">
<h1>葬神棺</h1>
<img src="https://img.xsw.tw/1713/1713745/1713745s.jpg">
<div>作者：浮生一諾</div>
<div>小說簡介 億萬年前，混沌初開。</div>
<a href="/1713745/page-2.html">下一頁</a><a href="/1713745/page-146.html">尾頁</a>
<a href="/1713745/250143505.html">第1章 長安城</a>
<a href="/1713745/250143506.html">第2章 陳家</a>
</body></html>
"""

CHAPTER_HTML = """
<html><body><div class="content"><h1>第1章</h1><p>第一段內容。</p><p>第二段內容。</p><script>ignore</script></div></body></html>
"""

SEARCH_HTML = """
<html><body><div class="bookbox"><h4 class="bookname"><a href="/1681721/">玄鑒仙族</a></h4></div></body></html>
"""


@pytest.mark.asyncio
async def test_xsw_parser(monkeypatch) -> None:
    crawler = XswCrawler()

    async def html(url: str) -> str:
        return CHAPTER_HTML if url.endswith("250143505.html") else BOOK_HTML

    monkeypatch.setattr(crawler, "_get_html", html)
    info = await crawler.get_book_info("https://m.xsw.tw/1713745/")
    chapters = await crawler.get_chapters(info.source_url)
    content = await crawler.get_chapter_content(chapters[0].url)

    assert info.title == "葬神棺"
    assert info.author == "浮生一諾"
    assert info.cover_url == "https://img.xsw.tw/og/cover.webp"
    assert [chapter.chapter_number for chapter in chapters] == [1, 2]
    assert "第一段內容" in content
    assert "ignore" not in content


@pytest.mark.asyncio
async def test_xsw_search(monkeypatch) -> None:
    crawler = XswCrawler()

    async def html(url: str) -> str:
        return SEARCH_HTML

    monkeypatch.setattr(crawler, "_get_html", html)
    results = await crawler.search("玄鑒仙族")

    assert len(results) == 1
    assert results[0].title == "玄鑒仙族"
    assert results[0].source_url == "https://m.xsw.tw/1681721/"


@pytest.mark.asyncio
async def test_xsw_imports_all_paginated_chapters_without_tail_link(monkeypatch) -> None:
    crawler = XswCrawler()

    def page_html(page: int) -> str:
        start = (page - 1) * 25 + 1
        end = min(page * 25, 125)
        links = []
        for number in range(start, end + 1):
            source_id = 300000000 + number
            links.append(f'<a href="/999/{source_id}.html">第{number}章 測試</a>')
        if page == 3:
            links.append('<a href="/999/300000050.html">第50章 重複</a>')
        return "<html><body>" + "".join(links) + "</body></html>"

    async def html(url: str) -> str:
        if url.endswith("/999/"):
            return '<html><body><a href="/999/page-1.html">[正序]</a></body></html>'
        if url.endswith("/999/page-1.html"):
            return page_html(1)
        match = re.search(r"/999/page-(\d+)\.html$", url)
        if match:
            page = int(match.group(1))
            return page_html(page) if page <= 5 else "<html><body></body></html>"
        return BOOK_HTML

    monkeypatch.setattr(crawler, "_get_html", html)

    chapters = await crawler.get_chapters("https://m.xsw.tw/999/")

    assert len(chapters) == 125
    assert chapters[0].chapter_number == 1
    assert chapters[-1].chapter_number == 125
    assert len({chapter.source_chapter_id for chapter in chapters}) == 125


def test_xsw_chinese_chapter_number_parser() -> None:
    assert chapter_number("一") == 1
    assert chapter_number("二十") == 20
    assert chapter_number("四十八") == 48
    assert chapter_number("一千五百四十五") == 1545


@pytest.mark.asyncio
async def test_xsw_follows_load_more_ajax_url(monkeypatch) -> None:
    crawler = XswCrawler()
    first_page = """
    <html><body>
    <a href="/888/400000001.html">第1章 測試</a>
    <button data-next-url="/ajax/chapters/888?page=2">Load More</button>
    </body></html>
    """
    ajax_page = """
    <html><body>
    <a href="/888/400000002.html">第2章 測試</a>
    <a href="/888/400000003.html">第3章 測試</a>
    </body></html>
    """

    async def html(url: str) -> str:
        if url.endswith("/888/"):
            return '<html><body><a href="/888/page-1.html">[正序]</a></body></html>'
        if "/ajax/chapters/888" in url:
            return ajax_page
        if url.endswith("/888/page-1.html"):
            return first_page
        return "<html><body></body></html>"

    monkeypatch.setattr(crawler, "_get_html", html)

    chapters = await crawler.get_chapters("https://m.xsw.tw/888/")

    assert [chapter.chapter_number for chapter in chapters] == [1, 2, 3]


@pytest.mark.asyncio
async def test_xsw_assigns_unique_positions_to_split_chapters(monkeypatch) -> None:
    crawler = XswCrawler()

    async def html(_url: str) -> str:
        return "<html><body></body></html>"

    def parse_links(*_args):
        return [
            ChapterInfo(1, "400000001", "Part one", "https://m.xsw.tw/777/400000001.html"),
            ChapterInfo(1, "400000002", "Part two", "https://m.xsw.tw/777/400000002.html"),
        ]

    monkeypatch.setattr(crawler, "_get_html", html)
    monkeypatch.setattr(crawler, "_parse_chapter_links", parse_links)

    chapters = await crawler.get_chapters("https://m.xsw.tw/777/")

    assert [chapter.chapter_number for chapter in chapters] == [1, 2]
