"""Small export adapters shared by uploaded ebooks and crawled novels."""
from __future__ import annotations

import base64
from html import escape
from io import BytesIO
from pathlib import PurePath
import re
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from bs4 import BeautifulSoup


def _safe_content(source: str) -> str:
    soup = BeautifulSoup(source, "html.parser")
    for node in soup(["script", "style", "iframe", "object", "embed", "form", "svg"]):
        node.decompose()
    allowed = {"p", "br", "em", "strong", "b", "i", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li"}
    for node in list(soup.find_all(True)):
        if node.name not in allowed:
            node.unwrap()
            continue
        for attr in list(node.attrs):
            del node.attrs[attr]
    if not soup.find(["p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol"]):
        paragraphs = [part.strip() for part in re.split(r"\r?\n+", soup.get_text("\n")) if part.strip()]
        return "".join(f"<p>{escape(paragraph)}</p>" for paragraph in paragraphs)
    return str(soup)


def build_ebook_export(title: str, author: str, identifier: str, chapters: list[tuple[int, str, str]], file_format: str, language: str, cover: tuple[bytes, str] | None = None) -> tuple[bytes, str, str]:
    title = title or "Ebook"
    filename = PurePath(title.replace("/", "_").replace("\\", "_")).name[:120] or "ebook"
    chapters = [(number, chapter_title, _safe_content(content)) for number, chapter_title, content in chapters]
    cover_media_types = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
    cover_type = cover[1].lower() if cover and cover[0] and cover[1].lower() in cover_media_types else None
    cover_data = cover[0] if cover_type else None
    if file_format == "txt":
        sections = [title, author, ""]
        for number, chapter_title, content in chapters:
            sections.extend([f"Chương {number}: {chapter_title}", BeautifulSoup(content, "html.parser").get_text("\n", strip=True), ""])
        return "\n".join(sections).encode("utf-8"), "text/plain; charset=utf-8", f"{filename}.txt"
    if file_format == "html":
        cover_html = f'<img alt="Bìa {escape(title)}" src="data:{cover_type};base64,{base64.b64encode(cover_data).decode("ascii")}">' if cover_data else ""
        body = "".join(f"<section><h2>{escape(chapter_title)}</h2>{content}</section>" for _, chapter_title, content in chapters)
        document = f"<!doctype html><html lang=\"vi\"><meta charset=\"utf-8\"><title>{escape(title)}</title><h1>{escape(title)}</h1>{cover_html}<p>{escape(author)}</p>{body}</html>"
        return document.encode("utf-8"), "text/html; charset=utf-8", f"{filename}.html"

    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>', compress_type=ZIP_DEFLATED)
        manifest, spine, toc = [], [], []
        if cover_data:
            cover_path = f"images/cover{cover_media_types[cover_type]}"
            archive.writestr(f"OEBPS/{cover_path}", cover_data, compress_type=ZIP_DEFLATED)
            archive.writestr("OEBPS/cover.xhtml", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body><img alt="Bìa {escape(title)}" src="{cover_path}"/></body></html>', compress_type=ZIP_DEFLATED)
            manifest.extend([
                f'<item id="cover-image" href="{cover_path}" media-type="{cover_type}" properties="cover-image"/>',
                '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>',
            ])
            spine.append('<itemref idref="cover"/>')
        for index, (_, chapter_title, content) in enumerate(chapters, 1):
            path = f"chapter-{index:05d}.xhtml"
            safe_title = escape(chapter_title)
            xhtml = f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{safe_title}</title></head><body><h1>{safe_title}</h1>{content}</body></html>'
            archive.writestr(f"OEBPS/{path}", xhtml, compress_type=ZIP_DEFLATED)
            manifest.append(f'<item id="chapter{index}" href="{path}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="chapter{index}"/>')
            toc.append(f'<li><a href="{path}">{safe_title}</a></li>')
        archive.writestr("OEBPS/nav.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>' + "".join(toc) + "</ol></nav></body></html>", compress_type=ZIP_DEFLATED)
        manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        metadata = f'<dc:title>{escape(title)}</dc:title><dc:language>{"vi" if language != "zh" else "zh"}</dc:language>'
        if cover_data:
            metadata += '<meta name="cover" content="cover-image"/>'
        if author:
            metadata += f'<dc:creator>{escape(author)}</dc:creator>'
        opf = f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">{metadata}<dc:identifier id="bookid">{escape(identifier)}</dc:identifier></metadata><manifest>{"".join(manifest)}</manifest><spine>{"".join(spine)}</spine></package>'
        archive.writestr("OEBPS/content.opf", opf, compress_type=ZIP_DEFLATED)
    return output.getvalue(), "application/epub+zip", f"{filename}.epub"
