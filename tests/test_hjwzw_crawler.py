import pytest

from app.crawlers.plugins.hjwzw import HjwzwCrawler


BOOK = '''<h1>道君</h1><div>作者：躍千愁</div><a href="/Book/Read/36219,12549743">第一章 沒白來</a><a href="/Book/Read/36219,12549745">第二章 古人誠不欺我</a>'''
CHAPTER = '<div>' + 'nội dung chương ' * 80 + '</div>'


@pytest.mark.asyncio
async def test_hjwzw_parses_book_chapters_and_content(monkeypatch) -> None:
    crawler = HjwzwCrawler()

    async def html(url: str) -> str:
        return CHAPTER if '/Book/Read/' in url else BOOK

    monkeypatch.setattr(crawler, '_get_html', html)
    info = await crawler.get_book_info('https://tw.hjwzw.com/Book/36219')
    chapters = await crawler.get_chapters(info.source_url)
    assert info.title == '道君'
    assert info.author == '躍千愁'
    assert [chapter.chapter_number for chapter in chapters] == [1, 2]
    assert await crawler.get_chapter_content(chapters[0].url)
