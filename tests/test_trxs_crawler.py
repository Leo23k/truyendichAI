import pytest

from app.crawlers.plugins.trxs import TrxsCrawler


BOOK_HTML = """
<html><body>
<h1>人在木叶，战场收尸十年(全本)</h1>
<img data-src="/d/file/tongren/20211009/cover.jpg">
<div>作者：骑驴三只脚</div>
<div>简介：这是一本简介。</div>
<a href="/tongren/6201/1.html">第1节</a>
<a href="/tongren/6201/2.html">第2节</a>
</body></html>
"""

CHAPTER_HTML = """
<html><body><main class="content"><h1>第1章</h1><p>第一段内容。</p><p>第二段内容。</p><script>ignore</script></main></body></html>
"""


@pytest.mark.asyncio
async def test_trxs_parser(monkeypatch) -> None:
    crawler = TrxsCrawler()

    async def html(url: str) -> str:
        return CHAPTER_HTML if "/1.html" in url else BOOK_HTML

    monkeypatch.setattr(crawler, "_get_html", html)
    info = await crawler.get_book_info("https://www.trxs.cc/tongren/6201.html")
    chapters = await crawler.get_chapters(info.source_url)
    content = await crawler.get_chapter_content(chapters[0].url)

    assert info.title == "人在木叶，战场收尸十年"
    assert info.author == "骑驴三只脚"
    assert info.cover_url == "https://www.trxs.cc/d/file/tongren/20211009/cover.jpg"
    assert [chapter.chapter_number for chapter in chapters] == [1, 2]
    assert "第一段内容" in content
    assert "ignore" not in content
