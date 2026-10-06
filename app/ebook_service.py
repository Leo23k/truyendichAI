"""Private ebook persistence. Every operation is scoped to the authenticated owner."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from html import escape as escape_html
from io import BytesIO
from dataclasses import dataclass
from pathlib import PurePosixPath
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from app.ebooks import EbookParseError, ParsedEbook, parse_ebook
from app.storage import configured as b2_configured, download as b2_download, presigned_download_url, upload as b2_upload
from app.translator.chunking import chunk_text
from sync.service import SupabaseSync, SyncError
from bs4 import BeautifulSoup


MAX_UPLOAD_BYTES = 25 * 1024 * 1024
CHAPTERS_PER_OBJECT = 25
_SAFE_NAME = re.compile(r"[^\w .()\[\]-]+", re.UNICODE)


class EbookServiceError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class UploadedEbook:
    id: str
    title: str
    chapter_count: int
    source_language: str


def _safe_filename(filename: str) -> str:
    name = PurePosixPath(filename.replace("\\", "/")).name
    name = _SAFE_NAME.sub("_", name).strip(" .")[:180]
    return name or "ebook"


def _language(parsed: ParsedEbook) -> str:
    language = parsed.language.lower().split("-", 1)[0]
    if language in {"zh", "zho", "chi", "cmn"}:
        return "zh"
    if language in {"vi", "vie"}:
        return "vi"
    sample = " ".join(chapter.content for chapter in parsed.chapters[:3])[:12000]
    cjk = sum("\u3400" <= char <= "\u9fff" for char in sample)
    letters = sum(char.isalpha() for char in sample)
    if letters and cjk / letters >= 0.12:
        return "zh"
    if any(char in "ăâđêôơưĂÂĐÊÔƠƯ" for char in sample):
        return "vi"
    return "unknown"


def _rollback_objects(paths: list[str]) -> None:
    try:
        import os
        from app.storage import client

        bucket = os.environ["B2_BUCKET"]
        for path in paths:
            client().delete_object(Bucket=bucket, Key=path)
    except Exception:
        # Orphan cleanup is best-effort; never hide the original persistence error.
        pass


def upload_ebook(owner_id: str, filename: str, content: bytes) -> UploadedEbook:
    if not b2_configured():
        raise EbookServiceError("B2 private storage chưa được cấu hình.", 503)
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise EbookServiceError("File rỗng hoặc vượt quá giới hạn upload 25 MB.", 413)
    safe_name = _safe_filename(filename)
    digest = hashlib.sha256(content).hexdigest()
    remote = SupabaseSync()
    try:
        duplicates = remote.rows("ebook_books", {
            "select": "id", "owner_id": f"eq.{owner_id}", "file_sha256": f"eq.{digest}", "limit": "1",
        })
    except SyncError as exc:
        raise EbookServiceError("Chưa có bảng ebook trên Supabase. Hãy chạy migration trước.", 503) from exc
    if duplicates:
        raise EbookServiceError("Bạn đã tải ebook này lên trước đó.", 409)
    try:
        parsed = parse_ebook(content, safe_name)
    except EbookParseError as exc:
        raise EbookServiceError(str(exc)) from exc
    ebook_id = str(uuid.uuid4())
    owner_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32]
    root = f"ebooks/{owner_key}/{ebook_id}"
    extension = PurePosixPath(safe_name).suffix.lower()
    original_path = f"{root}/original{extension}"
    uploaded_paths: list[str] = []
    source_language = _language(parsed)
    try:
        b2_upload(original_path, content, {
            ".epub": "application/epub+zip", ".txt": "text/plain", ".html": "text/html", ".htm": "text/html",
        }[extension], check_existing=False)
        uploaded_paths.append(original_path)
        chapter_rows = []
        for start in range(0, len(parsed.chapters), CHAPTERS_PER_OBJECT):
            group = parsed.chapters[start:start + CHAPTERS_PER_OBJECT]
            shard_path = f"{root}/chapters/{start // CHAPTERS_PER_OBJECT:04d}.json"
            payload = {
                "format_version": 1,
                "chapters": [{
                    "chapter_number": chapter.number,
                    "title": chapter.title,
                    "original_content": chapter.content,
                    "translated_content": None,
                    "translated": False,
                } for chapter in group],
            }
            b2_upload(shard_path, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json", check_existing=False)
            uploaded_paths.append(shard_path)
            for index, chapter in enumerate(group):
                chapter_rows.append({
                    "ebook_id": ebook_id,
                    "chapter_number": chapter.number,
                    "title": chapter.title,
                    "content_path": shard_path,
                    "content_index": index,
                    "translation_status": "pending" if source_language == "zh" else "not_needed",
                })
        book_payload = {
            "id": ebook_id,
            "owner_id": owner_id,
            "file_name": safe_name,
            "file_sha256": digest,
            "original_file_path": original_path,
            "source_format": parsed.format,
            "source_language": source_language,
            "title": parsed.title[:500],
            "author": parsed.author[:255] or None,
            "description": parsed.description[:10000] or None,
            "status": "ready",
            "chapter_count": len(parsed.chapters),
        }
        created = remote.request(
            "POST", "/rest/v1/ebook_books", json=book_payload,
            headers={"Prefer": "return=representation"},
        ).json()
        if not isinstance(created, list) or not created:
            raise EbookServiceError("Supabase không xác nhận được ebook đã lưu.", 503)
        for offset in range(0, len(chapter_rows), 500):
            remote.request(
                "POST", "/rest/v1/ebook_chapters", json=chapter_rows[offset:offset + 500],
                headers={"Prefer": "return=minimal"},
            )
    except EbookServiceError:
        _rollback_objects(uploaded_paths)
        raise
    except Exception as exc:
        _rollback_objects(uploaded_paths)
        try:
            remote.request("DELETE", "/rest/v1/ebook_books", params={"id": f"eq.{ebook_id}"})
        except Exception:
            pass
        raise EbookServiceError("Không thể lưu ebook. File B2 đã được dọn nếu có thể.", 503) from exc
    return UploadedEbook(ebook_id, parsed.title, len(parsed.chapters), source_language)


def list_ebooks(owner_id: str) -> list[dict]:
    try:
        books = SupabaseSync().all_rows("ebook_books", {
            "select": "id,title,author,source_format,source_language,status,chapter_count,created_at",
            "owner_id": f"eq.{owner_id}", "order": "created_at.desc",
        })
    except SyncError as exc:
        raise EbookServiceError("Không thể tải thư viện ebook. Kiểm tra kết nối Supabase và migration ebook_private_library.", 503) from exc
    return books


def get_original_download(owner_id: str, ebook_id: str) -> tuple[str, str]:
    try:
        rows = SupabaseSync().rows("ebook_books", {
            "select": "original_file_path,file_name", "id": f"eq.{ebook_id}",
            "owner_id": f"eq.{owner_id}", "limit": "1",
        })
        if not rows:
            raise EbookServiceError("Không tìm thấy ebook.", 404)
        row = rows[0]
        filename = str(row["file_name"])
        return presigned_download_url(str(row["original_file_path"]), download_filename=filename), filename
    except SyncError as exc:
        raise EbookServiceError("Không thể tạo liên kết tải ebook.", 503) from exc


def export_ebook(owner_id: str, ebook_id: str, file_format: str, language: str = "vi") -> tuple[bytes, str, str]:
    """Build a private owner-scoped EPUB, TXT, or self-contained HTML export."""
    file_format = file_format.lower()
    if file_format not in {"epub", "txt", "html"} or language not in {"vi", "zh", "bilingual"}:
        raise EbookServiceError("Äá»‹nh dáº¡ng hoáº·c ngÃ´n ngá»¯ export khÃ´ng há»£p lá»‡.")
    remote = SupabaseSync()
    try:
        books = remote.rows("ebook_books", {
            "select": "id,title,author,source_language", "id": f"eq.{ebook_id}",
            "owner_id": f"eq.{owner_id}", "limit": "1",
        })
        if not books:
            raise EbookServiceError("KhÃ´ng tÃ¬m tháº¥y ebook.", 404)
        book = books[0]
        chapters = remote.all_rows("ebook_chapters", {
            "select": "chapter_number,title,content_path,content_index,translated_content_path,translation_status",
            "ebook_id": f"eq.{ebook_id}", "order": "chapter_number.asc",
        })
        owner_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32]
        prefix = f"ebooks/{owner_key}/{ebook_id}/"
        shard_cache: dict[str, dict] = {}
        export_chapters = []
        for chapter in chapters:
            path = str(chapter.get("content_path") or "")
            if not path.startswith(prefix + "chapters/"):
                raise EbookServiceError("ÄÆ°á»ng dáº«n chÆ°Æ¡ng khÃ´ng há»£p lá»‡.", 503)
            if path not in shard_cache:
                shard_cache[path] = remote.download_json("ebooks", path.removeprefix("ebooks/"))
            items = shard_cache[path].get("chapters") or []
            index = int(chapter.get("content_index", -1))
            if index < 0 or index >= len(items):
                raise EbookServiceError("KhÃ´ng Ä‘á»c Ä‘Æ°á»£c ná»™i dung chÆ°Æ¡ng.", 503)
            original = str(items[index].get("original_content") or "")
            translated = ""
            translated_path = str(chapter.get("translated_content_path") or "")
            if translated_path and chapter.get("translation_status") == "translated":
                if not translated_path.startswith(prefix + "translations/"):
                    raise EbookServiceError("ÄÆ°á»ng dáº«n báº£n dá»‹ch khÃ´ng há»£p lá»‡.", 503)
                translated = str(json.loads(b2_download(translated_path).decode("utf-8")).get("translated_content") or "")
            if language == "vi" and book.get("source_language") == "zh" and not translated:
                raise EbookServiceError("CÃ³ chÆ°Æ¡ng chÆ°a dá»‹ch; hÃ£y chá»n báº£n gá»‘c hoáº·c song ngá»¯.", 409)
            if language == "vi":
                content = translated or original
            elif language == "zh":
                content = original
            else:
                content = f"<section><h2>Tiáº¿ng Trung</h2>{original}<h2>Tiáº¿ng Viá»‡t</h2>{translated or '<p>ChÆ°a dá»‹ch</p>'}</section>"
            export_chapters.append((int(chapter["chapter_number"]), str(chapter["title"]), content))
        export_chapters.sort(key=lambda item: item[0])
    except EbookServiceError:
        raise
    except Exception as exc:
        raise EbookServiceError("KhÃ´ng thá»ƒ Ä‘á»c dá»¯ liá»‡u Ä‘á»ƒ export ebook.", 503) from exc

    title = str(book.get("title") or "Ebook")
    author = str(book.get("author") or "")
    filename = _safe_filename(title) or "ebook"
    if file_format == "txt":
        sections = [title, author, ""]
        for number, chapter_title, content in export_chapters:
            sections.extend([f"ChÆ°Æ¡ng {number}: {chapter_title}", BeautifulSoup(content, "html.parser").get_text("\n", strip=True), ""])
        return "\n".join(sections).encode("utf-8"), "text/plain; charset=utf-8", f"{filename}.txt"
    if file_format == "html":
        body = "".join(f"<section><h2>{escape_html(chapter_title)}</h2>{content}</section>" for _, chapter_title, content in export_chapters)
        document = f"<!doctype html><html lang=\"vi\"><meta charset=\"utf-8\"><title>{escape_html(title)}</title><h1>{escape_html(title)}</h1><p>{escape_html(author)}</p>{body}</html>"
        return document.encode("utf-8"), "text/html; charset=utf-8", f"{filename}.html"

    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>', compress_type=ZIP_DEFLATED)
        manifest_items, spine_items, nav_items = [], [], []
        for index, (_, chapter_title, content) in enumerate(export_chapters, 1):
            chapter_name = f"chapter-{index:05d}.xhtml"
            safe_title = escape_html(chapter_title)
            xhtml = f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{safe_title}</title></head><body><h1>{safe_title}</h1>{content}</body></html>'
            archive.writestr(f"OEBPS/{chapter_name}", xhtml, compress_type=ZIP_DEFLATED)
            manifest_items.append(f'<item id="chapter{index}" href="{chapter_name}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chapter{index}"/>')
            nav_items.append(f'<li><a href="{chapter_name}">{safe_title}</a></li>')
        archive.writestr("OEBPS/nav.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>' + "".join(nav_items) + "</ol></nav></body></html>", compress_type=ZIP_DEFLATED)
        manifest_items.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        metadata = f'<dc:title>{escape_html(title)}</dc:title><dc:language>{"vi" if language != "zh" else "zh"}</dc:language>'
        if author:
            metadata += f'<dc:creator>{escape_html(author)}</dc:creator>'
        opf = f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">{metadata}<dc:identifier id="bookid">{escape_html(ebook_id)}</dc:identifier></metadata><manifest>{"".join(manifest_items)}</manifest><spine>{"".join(spine_items)}</spine></package>'
        archive.writestr("OEBPS/content.opf", opf, compress_type=ZIP_DEFLATED)
    return output.getvalue(), "application/epub+zip", f"{filename}.epub"


def get_ebook_chapter(owner_id: str, ebook_id: str, chapter_number: int, *, mode: str = "original") -> dict:
    if chapter_number < 1:
        raise EbookServiceError("Số chương không hợp lệ.", 404)
    remote = SupabaseSync()
    try:
        books = remote.rows("ebook_books", {
            "select": "id,title,author,source_language,source_format,chapter_count,original_file_path",
            "id": f"eq.{ebook_id}", "owner_id": f"eq.{owner_id}", "limit": "1",
        })
        if not books:
            raise EbookServiceError("Không tìm thấy ebook.", 404)
        book = books[0]
        chapters = remote.rows("ebook_chapters", {
            "select": "chapter_number,title,content_path,content_index,translation_status,translated_content_path",
            "ebook_id": f"eq.{ebook_id}", "chapter_number": f"eq.{chapter_number}", "limit": "1",
        })
        if not chapters:
            raise EbookServiceError("Không tìm thấy chương ebook.", 404)
        chapter = chapters[0]
        owner_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32]
        expected_prefix = f"ebooks/{owner_key}/{ebook_id}/chapters/"
        content_path = str(chapter.get("content_path") or "")
        if not content_path.startswith(expected_prefix):
            raise EbookServiceError("Đường dẫn nội dung ebook không hợp lệ.", 503)
        shard = json.loads(b2_download(content_path).decode("utf-8"))
        items = shard.get("chapters") if isinstance(shard, dict) else None
        index = int(chapter.get("content_index", -1))
        if not isinstance(items, list) or index < 0 or index >= len(items):
            raise EbookServiceError("Nội dung chương ebook không đồng bộ.", 503)
        payload = items[index]
        if int(payload.get("chapter_number", 0)) != chapter_number:
            raise EbookServiceError("Mục lục và nội dung chương không khớp.", 503)
        previous = remote.rows("ebook_chapters", {
            "select": "chapter_number,title", "ebook_id": f"eq.{ebook_id}",
            "chapter_number": f"lt.{chapter_number}", "order": "chapter_number.desc", "limit": "1",
        })
        following = remote.rows("ebook_chapters", {
            "select": "chapter_number,title", "ebook_id": f"eq.{ebook_id}",
            "chapter_number": f"gt.{chapter_number}", "order": "chapter_number.asc", "limit": "1",
        })
        content = str(payload.get("original_content") or "")
        translated = False
        translated_path = str(chapter.get("translated_content_path") or "")
        expected_translation_prefix = f"ebooks/{owner_key}/{ebook_id}/translations/"
        if mode == "translated" and chapter.get("translation_status") == "translated":
            if not translated_path.startswith(expected_translation_prefix):
                raise EbookServiceError("ÄÆ°á»ng dáº«n báº£n dá»‹ch ebook khÃ´ng há»£p lá»‡.", 503)
            translation = json.loads(b2_download(translated_path).decode("utf-8"))
            if int(translation.get("chapter_number", 0)) != chapter_number:
                raise EbookServiceError("Báº£n dá»‹ch khÃ´ng khá»›p vá»›i chÆ°Æ¡ng ebook.", 503)
            content = str(translation.get("translated_content") or content)
            translated = bool(translation.get("translated_content"))
    except EbookServiceError:
        raise
    except (SyncError, ValueError, KeyError, TypeError) as exc:
        raise EbookServiceError("Không thể tải nội dung chương ebook.", 503) from exc
    return {
        "book": book,
        "chapter": chapter,
        "content": content,
        "translated": translated,
        "previous": previous[0] if previous else None,
        "following": following[0] if following else None,
    }


def enqueue_ebook_translation(owner_id: str, ebook_id: str, *, retranslate: bool = False) -> int:
    remote = SupabaseSync()
    try:
        books = remote.rows("ebook_books", {
            "select": "id,source_language", "id": f"eq.{ebook_id}",
            "owner_id": f"eq.{owner_id}", "limit": "1",
        })
        if not books:
            raise EbookServiceError("Không tìm thấy ebook.", 404)
        if books[0].get("source_language") != "zh":
            raise EbookServiceError("Chỉ ebook tiếng Trung có thể đưa vào pipeline dịch hiện tại.", 409)
        chapters = remote.all_rows("ebook_chapters", {
            "select": "id,chapter_number,title,content_path,content_index,translation_status,translated_content_path",
            "ebook_id": f"eq.{ebook_id}", "order": "chapter_number.asc",
        })
        queue_rows = remote.all_rows("ebook_translation_queue", {
            "select": "ebook_chapter_id,status", "ebook_chapter_id": f"in.({','.join(str(row['id']) for row in chapters)})" if chapters else "eq.0",
        })
    except SyncError as exc:
        raise EbookServiceError("Không thể truy cập hàng đợi ebook. Hãy chạy migration ebook_translation_queue.", 503) from exc

    queue_by_chapter = {str(row.get("ebook_chapter_id")): row for row in queue_rows}
    chapter_jobs: list[dict] = []
    chunk_rows: list[dict] = []
    shard_cache: dict[str, dict] = {}
    for chapter in chapters:
        chapter_id = str(chapter["id"])
        status = str(chapter.get("translation_status") or "pending")
        queued = queue_by_chapter.get(chapter_id)
        if retranslate:
            if status not in {"translated", "failed", "unsupported"}:
                continue
            remote.delete_rows("ebook_translation_chunks", {"ebook_chapter_id": f"eq.{chapter_id}"})
            remote.delete_rows("ebook_translation_queue", {"ebook_chapter_id": f"eq.{chapter_id}"})
            remote.patch_rows("ebook_chapters", {"id": f"eq.{chapter_id}"}, {"translation_status": "pending", "translated_content_path": None})
        elif status != "pending" or queued:
            continue
        path = str(chapter.get("content_path") or "")
        if path not in shard_cache:
            shard_cache[path] = remote.download_json("ebooks", path.removeprefix("ebooks/"))
        items = shard_cache[path].get("chapters") or []
        index = int(chapter.get("content_index", -1))
        if index < 0 or index >= len(items):
            raise EbookServiceError(f"Không đọc được dữ liệu chương {chapter.get('chapter_number')}.", 503)
        raw_html = str(items[index].get("original_content") or "")
        text = BeautifulSoup(raw_html, "html.parser").get_text("\n", strip=True)
        parts = chunk_text(text)
        if not parts:
            remote.patch_rows("ebook_chapters", {"id": f"eq.{chapter_id}"}, {"translation_status": "failed"})
            continue
        chunk_rows.extend({"ebook_chapter_id": chapter_id, "chunk_index": chunk_index, "original_text": part, "status": "queued"} for chunk_index, part in enumerate(parts))
        chapter_jobs.append({"ebook_chapter_id": chapter_id, "status": "queued", "current_chunk": 0, "total_chunks": len(parts), "retry_count": 0})

    if chunk_rows:
        try:
            remote.insert_many_ignore("ebook_translation_chunks", chunk_rows, "ebook_chapter_id,chunk_index")
            remote.insert_many_ignore("ebook_translation_queue", chapter_jobs, "ebook_chapter_id")
        except SyncError as exc:
            raise EbookServiceError("Không thể thêm ebook vào hàng đợi dịch.", 503) from exc
    return len(chapter_jobs)
