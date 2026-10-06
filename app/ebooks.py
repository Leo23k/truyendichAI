"""Safe, format-specific ebook parsing into a small normalized representation.

This module deliberately does not persist data or enqueue translations. Those
operations belong to the authenticated ebook-ingestion layer, not the parsers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from pathlib import PurePosixPath
import re
from typing import Protocol
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from bs4 import BeautifulSoup


MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_FILES = 10_000
MAX_CHAPTERS = 5_000


class EbookParseError(ValueError):
    """Raised when an ebook is invalid, unsafe, or unsupported."""


@dataclass(frozen=True)
class EbookChapter:
    number: int
    title: str
    content: str


@dataclass(frozen=True)
class ParsedEbook:
    title: str
    author: str = ""
    language: str = ""
    description: str = ""
    chapters: tuple[EbookChapter, ...] = field(default_factory=tuple)
    format: str = ""


class EbookParser(Protocol):
    format: str

    def parse(self, content: bytes, filename: str) -> ParsedEbook: ...


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return content.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue
    raise EbookParseError("Không thể nhận diện encoding của file văn bản.")


def _clean_html(source: str) -> tuple[str, str]:
    soup = BeautifulSoup(source, "html.parser")
    for node in soup(["script", "style", "iframe", "object", "embed", "form", "svg"]):
        node.decompose()
    root = soup.body or soup
    # Convert only a small formatting allowlist to safe, self-contained HTML.
    allowed = {"p", "br", "em", "strong", "b", "i", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li"}

    def render(node) -> str:
        if getattr(node, "name", None) is None:
            return escape(str(node), quote=False)
        children = "".join(render(child) for child in node.children)
        name = str(node.name).lower()
        if name not in allowed:
            return children
        if name == "br":
            return "<br/>"
        return f"<{name}>{children}</{name}>"

    safe_html = "".join(render(child) for child in root.children).strip()
    text = root.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return safe_html, title or text.splitlines()[0][:200] if text else title


def _split_headings(source: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(source, "html.parser")
    for node in soup(["script", "style", "iframe", "object", "embed", "form", "svg"]):
        node.decompose()
    body = soup.body or soup
    headings = body.find_all(re.compile(r"^h[1-3]$"))
    if len(headings) < 2:
        safe, title = _clean_html(str(body))
        return [(title or "Nội dung", safe)] if safe else []
    chunks: list[tuple[str, str]] = []
    current_title = ""
    current: list[str] = []
    for node in body.descendants:
        if getattr(node, "name", None) in {"h1", "h2", "h3"}:
            if current_title or current:
                chunks.append((current_title or f"Chương {len(chunks) + 1}", "\n".join(current)))
            current_title = node.get_text(" ", strip=True)[:500] or f"Chương {len(chunks) + 1}"
            current = []
        elif getattr(node, "name", None) in {"p", "blockquote", "li"}:
            text = node.get_text(" ", strip=True)
            if text:
                current.append(text)
    if current_title or current:
        chunks.append((current_title or f"Chương {len(chunks) + 1}", "\n".join(current)))
    return [(title, escape(content, quote=False).replace("\n", "<br/>")) for title, content in chunks if content.strip()]


class TxtParser:
    format = "txt"
    _chapter_pattern = re.compile(r"(?im)^\s*((?:第\s*[\d一二三四五六七八九十百千零〇两]+\s*[章节回卷集部].*)|(?:chapter\s+\d+.*)|(?:chương\s+\d+.*))\s*$")

    def parse(self, content: bytes, filename: str = "") -> ParsedEbook:
        text = _decode_text(content).lstrip("\ufeff").strip()
        if not text:
            raise EbookParseError("File TXT không có nội dung.")
        if len(content) > MAX_ARCHIVE_BYTES:
            raise EbookParseError("File ebook vượt quá giới hạn 100 MB.")
        matches = list(self._chapter_pattern.finditer(text))
        segments: list[tuple[str, str]] = []
        if len(matches) >= 2:
            if text[:matches[0].start()].strip():
                segments.append(("Mở đầu", text[:matches[0].start()].strip()))
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
                segments.append((match.group(1).strip(), text[match.end():end].strip()))
        else:
            segments = [(PurePosixPath(filename).stem or "Nội dung", text)]
        chapters = tuple(EbookChapter(i, title[:500], escape(body, quote=False).replace("\n", "<br/>")) for i, (title, body) in enumerate(segments, 1) if body)
        if not chapters:
            raise EbookParseError("Không trích xuất được nội dung chương.")
        if len(chapters) > MAX_CHAPTERS:
            raise EbookParseError("Ebook có quá nhiều chương (tối đa 5000).");
        return ParsedEbook(PurePosixPath(filename).stem or "Ebook", chapters=chapters, format=self.format)


class HtmlParser:
    format = "html"

    def parse(self, content: bytes, filename: str = "") -> ParsedEbook:
        if not content or len(content) > MAX_ARCHIVE_BYTES:
            raise EbookParseError("File HTML rỗng hoặc vượt quá giới hạn 100 MB.")
        source = _decode_text(content)
        soup = BeautifulSoup(source, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else PurePosixPath(filename).stem
        segments = _split_headings(source)
        chapters = tuple(EbookChapter(i, name[:500], body) for i, (name, body) in enumerate(segments, 1))
        if not chapters:
            raise EbookParseError("Không trích xuất được nội dung từ HTML.")
        return ParsedEbook(title=title or "Ebook", chapters=chapters, format=self.format)


def _archive_path(base: str, path: str) -> str:
    joined = PurePosixPath(base).parent / PurePosixPath(path)
    normalized = PurePosixPath()
    parts: list[str] = []
    for part in joined.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise EbookParseError("EPUB chứa đường dẫn vượt khỏi thư mục lưu trữ.")
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


class EpubParser:
    format = "epub"

    def parse(self, content: bytes, filename: str = "") -> ParsedEbook:
        if not content or len(content) > MAX_ARCHIVE_BYTES:
            raise EbookParseError("File EPUB rỗng hoặc vượt quá giới hạn 100 MB.")
        try:
            with ZipFile(__import__("io").BytesIO(content)) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ARCHIVE_FILES or sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
                    raise EbookParseError("EPUB vượt quá giới hạn giải nén an toàn.")
                for info in infos:
                    path = PurePosixPath(info.filename)
                    if path.is_absolute() or ".." in path.parts:
                        raise EbookParseError("EPUB chứa đường dẫn file không an toàn.")
                container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
                rootfile = next((node.attrib.get("full-path") for node in container.iter() if node.tag.endswith("rootfile")), None)
                if not rootfile or rootfile not in archive.namelist():
                    raise EbookParseError("Không tìm thấy package document trong EPUB.")
                package = ElementTree.fromstring(archive.read(rootfile))
                metadata = next((node for node in package if node.tag.endswith("metadata")), None)
                manifest_node = next((node for node in package if node.tag.endswith("manifest")), None)
                spine_node = next((node for node in package if node.tag.endswith("spine")), None)
                if metadata is None or manifest_node is None or spine_node is None:
                    raise EbookParseError("Cấu trúc EPUB thiếu metadata, manifest hoặc spine.")
                metadata_values = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in metadata}
                manifest = {node.attrib.get("id", ""): node.attrib for node in manifest_node}
                base = rootfile
                chapters: list[EbookChapter] = []
                for itemref in spine_node:
                    idref = itemref.attrib.get("idref")
                    item = manifest.get(idref or "")
                    href = item.get("href") if item else None
                    if not href:
                        continue
                    path = _archive_path(base, href)
                    if path not in archive.namelist():
                        continue
                    mime = item.get("media-type", "")
                    if mime not in {"application/xhtml+xml", "text/html"} and not path.lower().endswith((".xhtml", ".html", ".htm")):
                        continue
                    source = archive.read(path).decode("utf-8", errors="replace")
                    safe_html, heading = _clean_html(source)
                    if not BeautifulSoup(safe_html, "html.parser").get_text(" ", strip=True):
                        continue
                    chapters.append(EbookChapter(len(chapters) + 1, heading or f"Chương {len(chapters) + 1}", safe_html))
                    if len(chapters) > MAX_CHAPTERS:
                        raise EbookParseError("EPUB có quá nhiều chương (tối đa 5000).")
        except EbookParseError:
            raise
        except (BadZipFile, KeyError, ElementTree.ParseError, OSError) as exc:
            raise EbookParseError("File EPUB bị hỏng hoặc không đúng định dạng.") from exc
        if not chapters:
            raise EbookParseError("Không tìm thấy nội dung chương theo thứ tự đọc của EPUB.")
        return ParsedEbook(
            title=metadata_values.get("title") or PurePosixPath(filename).stem or "Ebook",
            author=metadata_values.get("creator", ""),
            language=metadata_values.get("language", ""),
            description=metadata_values.get("description", ""),
            chapters=tuple(chapters),
            format=self.format,
        )


PARSERS: dict[str, EbookParser] = {".epub": EpubParser(), ".txt": TxtParser(), ".html": HtmlParser(), ".htm": HtmlParser()}


def parse_ebook(content: bytes, filename: str) -> ParsedEbook:
    extension = PurePosixPath(filename).suffix.lower()
    parser = PARSERS.get(extension)
    if parser is None:
        raise EbookParseError("Định dạng chưa hỗ trợ. Hiện parser lõi nhận EPUB, TXT và HTML.")
    return parser.parse(content, filename)
