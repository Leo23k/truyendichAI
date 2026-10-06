from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from app.ebooks import EbookParseError, parse_ebook


def test_txt_falls_back_to_one_chapter_and_escapes_markup() -> None:
    parsed = parse_ebook(b"<script>alert(1)</script> & hello", "novel.txt")
    assert parsed.title == "novel"
    assert len(parsed.chapters) == 1
    assert "&lt;script&gt;" in parsed.chapters[0].content


def test_txt_splits_chapter_headings_in_order() -> None:
    parsed = parse_ebook("第一章 开始\n正文一\n第二章 继续\n正文二".encode(), "book.txt")
    assert [chapter.title for chapter in parsed.chapters] == ["第一章 开始", "第二章 继续"]
    assert [chapter.number for chapter in parsed.chapters] == [1, 2]


def test_html_strips_active_content_and_event_attributes() -> None:
    parsed = parse_ebook(b"<title>Test</title><h1>One</h1><script>x</script><p onclick='x'>safe</p><iframe src='evil'></iframe>", "test.html")
    output = parsed.chapters[0].content
    assert "script" not in output and "iframe" not in output and "onclick" not in output
    assert "safe" in output


def test_epub_reads_chapters_from_spine_not_filename_sort() -> None:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OPS/content.opf"/></rootfiles></container>')
        archive.writestr("OPS/content.opf", '''<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>Book</dc:title><dc:creator>Author</dc:creator><dc:language>zh</dc:language></metadata><manifest><item id="late" href="z.xhtml" media-type="application/xhtml+xml"/><item id="early" href="a.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="late"/><itemref idref="early"/></spine></package>''')
        archive.writestr("OPS/z.xhtml", "<h1>First</h1><p>one</p>")
        archive.writestr("OPS/a.xhtml", "<h1>Second</h1><p>two</p>")
    parsed = parse_ebook(buffer.getvalue(), "book.epub")
    assert parsed.title == "Book" and parsed.author == "Author" and parsed.language == "zh"
    assert [chapter.title for chapter in parsed.chapters] == ["First", "Second"]


def test_rejects_unsafe_epub_member_paths() -> None:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.txt", "bad")
    with pytest.raises(EbookParseError, match="đường dẫn"):
        parse_ebook(buffer.getvalue(), "book.epub")


def test_pdf_is_preserved_as_fixed_layout_without_fake_chapters() -> None:
    parsed = parse_ebook(b"%PDF-1.7\n% fake minimal fixture", "manual.pdf")
    assert parsed.format == "pdf" and parsed.title == "manual" and parsed.chapters == ()


def test_pdf_rejects_wrong_file_signature() -> None:
    with pytest.raises(EbookParseError, match="PDF"):
        parse_ebook(b"not a pdf", "manual.pdf")
