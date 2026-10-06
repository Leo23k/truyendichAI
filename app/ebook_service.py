"""Private ebook persistence. Every operation is scoped to the authenticated owner."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

from app.ebooks import EbookParseError, ParsedEbook, parse_ebook
from app.ebook_export import build_ebook_export
from app.storage import configured as b2_configured, download as b2_download, presigned_download_url, upload as b2_upload
from app.translator.chunking import chunk_text
from sync.service import SupabaseSync, SyncError
from bs4 import BeautifulSoup


MAX_UPLOAD_BYTES = 100 * 1024 * 1024
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
    translation_inputs: dict[int, str] | None = None


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
    # CJK-only text can be Japanese or Korean; do not enqueue it as Chinese
    # unless the parser's language metadata explicitly identified Chinese.
    if any("\u3040" <= char <= "\u30ff" or "\uac00" <= char <= "\ud7af" for char in sample):
        return "unknown"
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


def upload_ebook(owner_id: str, filename: str, content: bytes, *, prepare_translation: bool = False) -> UploadedEbook:
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
    cover_path = None
    source_language = _language(parsed)
    try:
        b2_upload(original_path, content, {
            ".epub": "application/epub+zip", ".txt": "text/plain", ".html": "text/html", ".htm": "text/html", ".pdf": "application/pdf",
        }[extension], check_existing=False)
        uploaded_paths.append(original_path)
        if parsed.cover and parsed.cover_media_type:
            image_extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}[parsed.cover_media_type]
            cover_path = f"{root}/assets/cover{image_extension}"
            b2_upload(cover_path, parsed.cover, parsed.cover_media_type, check_existing=False)
            uploaded_paths.append(cover_path)
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
        if cover_path:
            book_payload["cover_path"] = cover_path
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
    translation_inputs = (
        {int(chapter.number): str(chapter.content) for chapter in parsed.chapters}
        if prepare_translation and source_language == "zh" else None
    )
    return UploadedEbook(ebook_id, parsed.title, len(parsed.chapters), source_language, translation_inputs)


def begin_ebook_upload(owner_id: str, filename: str, file_size: int, *, translate_if_chinese: bool = False) -> dict:
    """Reserve a private B2 object and return a short-lived, object-scoped PUT URL."""
    if not b2_configured():
        raise EbookServiceError("B2 private storage chưa được cấu hình.", 503)
    safe_name = _safe_filename(filename)
    extension = PurePosixPath(safe_name).suffix.lower()
    formats = {".epub": "epub", ".txt": "txt", ".html": "html", ".htm": "html", ".pdf": "pdf"}
    if extension not in formats:
        raise EbookServiceError("Chỉ hỗ trợ EPUB, TXT, HTML và PDF.")
    if file_size <= 0 or file_size > MAX_UPLOAD_BYTES:
        raise EbookServiceError("File rỗng hoặc vượt giới hạn upload 100 MB.", 413)
    content_types = {
        ".epub": "application/epub+zip", ".txt": "text/plain", ".html": "text/html",
        ".htm": "text/html", ".pdf": "application/pdf",
    }
    ebook_id = str(uuid.uuid4())
    owner_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32]
    object_path = f"ebooks/{owner_key}/{ebook_id}/original{extension}"
    try:
        from app.storage import client

        url = client().generate_presigned_url(
            "put_object",
            Params={
                "Bucket": os.environ["B2_BUCKET"], "Key": object_path,
                "ContentType": content_types[extension], "ContentLength": file_size,
            },
            ExpiresIn=900,
        )
        SupabaseSync().insert("ebook_ingestion_queue", {
            "id": ebook_id,
            "owner_id": owner_id,
            "file_name": safe_name,
            "source_format": formats[extension],
            "original_file_path": object_path,
            "expected_size": file_size,
            "translate_if_chinese": bool(translate_if_chinese),
            "status": "awaiting_upload",
        })
    except SyncError as exc:
        raise EbookServiceError("Không thể tạo phiên upload Ebook.", 503) from exc
    except Exception as exc:
        raise EbookServiceError("Không thể cấp quyền upload B2.", 503) from exc
    return {"upload_id": ebook_id, "upload_url": url, "content_type": content_types[extension]}


def complete_ebook_upload(owner_id: str, upload_id: str) -> None:
    remote = SupabaseSync()
    try:
        rows = remote.rows("ebook_ingestion_queue", {
            "select": "id,status", "id": f"eq.{upload_id}", "owner_id": f"eq.{owner_id}", "limit": "1",
        })
        if not rows:
            raise EbookServiceError("Không tìm thấy phiên upload.", 404)
        if rows[0]["status"] in {"queued", "processing", "completed"}:
            return
        changed = remote.patch_rows("ebook_ingestion_queue", {
            "id": f"eq.{upload_id}", "owner_id": f"eq.{owner_id}", "status": "eq.awaiting_upload",
        }, {"status": "queued", "updated_at": datetime.now(timezone.utc).isoformat(), "error": None})
        if not changed:
            raise EbookServiceError("Phiên upload không còn hợp lệ.", 409)
    except EbookServiceError:
        raise
    except SyncError as exc:
        raise EbookServiceError("Không thể đưa Ebook vào hàng đợi xử lý.", 503) from exc


def ebook_upload_status(owner_id: str, upload_id: str) -> dict:
    try:
        rows = SupabaseSync().rows("ebook_ingestion_queue", {
            "select": "id,status,source_language,translation_queued,error",
            "id": f"eq.{upload_id}", "owner_id": f"eq.{owner_id}", "limit": "1",
        })
    except SyncError as exc:
        raise EbookServiceError("Không thể kiểm tra trạng thái upload.", 503) from exc
    if not rows:
        raise EbookServiceError("Không tìm thấy phiên upload.", 404)
    row = rows[0]
    return {
        "status": row["status"], "ebook_id": upload_id if row["status"] == "completed" else None,
        "source_language": row.get("source_language"),
        "translation_queued": int(row.get("translation_queued") or 0), "error": row.get("error"),
    }


def process_ebook_ingestion_job(remote: SupabaseSync, job: dict, content: bytes) -> int:
    """Parse a B2-uploaded original in the PC worker, persist its index, and optionally queue zh chapters."""
    if len(content) != int(job["expected_size"]) or len(content) > MAX_UPLOAD_BYTES:
        raise EbookServiceError("Kích thước file B2 không khớp phiên upload.", 400)
    digest = hashlib.sha256(content).hexdigest()
    duplicates = remote.rows("ebook_books", {
        "select": "id", "owner_id": f"eq.{job['owner_id']}", "file_sha256": f"eq.{digest}", "limit": "1",
    })
    if duplicates:
        raise EbookServiceError("Ebook này đã có trong thư viện của bạn.", 409)
    try:
        parsed = parse_ebook(content, str(job["file_name"]))
    except EbookParseError as exc:
        raise EbookServiceError(str(exc), 400) from exc
    source_language = _language(parsed)
    ebook_id = str(job["id"])
    root = str(job["original_file_path"]).rsplit("/original", 1)[0]
    written_objects: list[str] = []
    chapter_rows = []
    try:
        cover_path = None
        if parsed.cover and parsed.cover_media_type:
            extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}[parsed.cover_media_type]
            cover_path = f"{root}/assets/cover{extension}"
            b2_upload(cover_path, parsed.cover, parsed.cover_media_type, check_existing=False)
            written_objects.append(cover_path)
        for start in range(0, len(parsed.chapters), CHAPTERS_PER_OBJECT):
            group = parsed.chapters[start:start + CHAPTERS_PER_OBJECT]
            shard_path = f"{root}/chapters/{start // CHAPTERS_PER_OBJECT:04d}.json"
            payload = {"format_version": 1, "chapters": [{
                "chapter_number": item.number, "title": item.title,
                "original_content": item.content, "translated_content": None, "translated": False,
            } for item in group]}
            b2_upload(shard_path, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json", check_existing=False)
            written_objects.append(shard_path)
            chapter_rows.extend({
                "ebook_id": ebook_id, "chapter_number": item.number, "title": item.title,
                "content_path": shard_path, "content_index": index,
                "translation_status": "pending" if source_language == "zh" else "not_needed",
            } for index, item in enumerate(group))
        book_payload = {
            "id": ebook_id, "owner_id": job["owner_id"], "file_name": job["file_name"],
            "file_sha256": digest, "original_file_path": job["original_file_path"],
            "source_format": job["source_format"], "source_language": source_language,
            "title": parsed.title[:500], "author": parsed.author[:255] or None,
            "description": parsed.description[:10000] or None, "status": "ready",
            "chapter_count": len(parsed.chapters),
        }
        if cover_path:
            book_payload["cover_path"] = cover_path
        created = remote.request("POST", "/rest/v1/ebook_books", json=book_payload, headers={"Prefer": "return=representation"}).json()
        if not isinstance(created, list) or not created:
            raise EbookServiceError("Supabase không xác nhận metadata Ebook.", 503)
        for offset in range(0, len(chapter_rows), 500):
            remote.request("POST", "/rest/v1/ebook_chapters", json=chapter_rows[offset:offset + 500], headers={"Prefer": "return=minimal"})
    except Exception:
        _rollback_objects(written_objects)
        try:
            remote.request("DELETE", "/rest/v1/ebook_books", params={"id": f"eq.{ebook_id}"})
        except Exception:
            pass
        raise
    if job.get("translate_if_chinese") and source_language == "zh" and parsed.chapters:
        raw_chapters = {int(item.number): str(item.content) for item in parsed.chapters}
        try:
            return enqueue_ebook_translation(str(job["owner_id"]), ebook_id, raw_chapters=raw_chapters, remote=remote)
        except EbookServiceError:
            return 0
    return 0


def list_ebooks(owner_id: str) -> list[dict]:
    try:
        remote = SupabaseSync()
        filters = {"owner_id": f"eq.{owner_id}", "order": "created_at.desc"}
        try:
            books = remote.all_rows("ebook_books", {**filters, "select": "id,title,author,source_format,source_language,status,chapter_count,created_at,updated_at,cover_path"})
        except SyncError:
            books = remote.all_rows("ebook_books", {**filters, "select": "id,title,author,source_format,source_language,status,chapter_count,created_at,updated_at"})
        if not books:
            return []
        ids = ",".join(str(book["id"]) for book in books)
        chapters = remote.all_rows("ebook_chapters", {
            "select": "ebook_id,translation_status", "ebook_id": f"in.({ids})",
        })
    except SyncError as exc:
        raise EbookServiceError("Không thể tải thư viện ebook. Kiểm tra kết nối Supabase và migration ebook_private_library.", 503) from exc
    stats = {str(book["id"]): {"translated": 0, "processing": 0, "pending": 0, "failed": 0} for book in books}
    for chapter in chapters:
        record = stats.get(str(chapter.get("ebook_id")))
        if record is None:
            continue
        status = str(chapter.get("translation_status") or "pending")
        key = "translated" if status == "translated" else "processing" if status == "processing" else "failed" if status == "failed" else "pending"
        record[key] += 1
    for book in books:
        progress = stats[str(book["id"])]
        book["cover_url"] = _private_cover_url(owner_id, str(book["id"]), book.get("cover_path"))
        book.update({
            "translated_chapters": progress["translated"],
            "processing_chapters": progress["processing"],
            "pending_chapters": progress["pending"],
            "failed_chapters": progress["failed"],
            "translation_percent": round(progress["translated"] * 100 / max(1, int(book.get("chapter_count") or 0))),
        })
    return books


def get_ebook_detail(owner_id: str, ebook_id: str) -> dict:
    remote = SupabaseSync()
    try:
        book_filters = {"id": f"eq.{ebook_id}", "owner_id": f"eq.{owner_id}", "limit": "1"}
        try:
            books = remote.rows("ebook_books", {**book_filters, "select": "id,title,author,description,source_format,source_language,status,chapter_count,created_at,updated_at,file_name,cover_path"})
        except SyncError:
            books = remote.rows("ebook_books", {**book_filters, "select": "id,title,author,description,source_format,source_language,status,chapter_count,created_at,updated_at,file_name"})
        if not books:
            raise EbookServiceError("Không tìm thấy ebook.", 404)
        book = books[0]
        chapters = remote.all_rows("ebook_chapters", {
            "select": "id,chapter_number,title,translation_status,translated_content_path",
            "ebook_id": f"eq.{ebook_id}", "order": "chapter_number.asc",
        })
        chapter_ids = ",".join(str(chapter["id"]) for chapter in chapters)
        queue = remote.all_rows("ebook_translation_queue", {
            "select": "ebook_chapter_id,status,current_chunk,total_chunks",
            "ebook_chapter_id": f"in.({chapter_ids})" if chapter_ids else "eq.0",
        })
    except EbookServiceError:
        raise
    except SyncError as exc:
        raise EbookServiceError("Không thể tải chi tiết ebook.", 503) from exc
    queue_by_chapter = {str(item["ebook_chapter_id"]): item for item in queue}
    for chapter in chapters:
        job = queue_by_chapter.get(str(chapter["id"]))
        chapter["queue"] = job
    translated = sum(chapter.get("translation_status") == "translated" for chapter in chapters)
    processing = sum(chapter.get("translation_status") == "processing" for chapter in chapters)
    book.update({
        "translated_chapters": translated,
        "processing_chapters": processing,
        "pending_chapters": sum(chapter.get("translation_status") == "pending" for chapter in chapters),
        "failed_chapters": sum(chapter.get("translation_status") == "failed" for chapter in chapters),
        "translation_percent": round(translated * 100 / max(1, int(book.get("chapter_count") or 0))),
    })
    book["cover_url"] = _private_cover_url(owner_id, ebook_id, book.get("cover_path"))
    return {"book": book, "chapters": chapters}


def _private_cover_url(owner_id: str, ebook_id: str, path: object) -> str | None:
    if not path:
        return None
    owner_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32]
    expected_prefix = f"ebooks/{owner_key}/{ebook_id}/assets/"
    value = str(path)
    if not value.startswith(expected_prefix):
        return None
    try:
        return presigned_download_url(value)
    except Exception:
        return None


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


def get_pdf_view_url(owner_id: str, ebook_id: str) -> str:
    try:
        rows = SupabaseSync().rows("ebook_books", {
            "select": "original_file_path,source_format", "id": f"eq.{ebook_id}",
            "owner_id": f"eq.{owner_id}", "limit": "1",
        })
        if not rows:
            raise EbookServiceError("Không tìm thấy ebook.", 404)
        row = rows[0]
        owner_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32]
        expected = f"ebooks/{owner_key}/{ebook_id}/original.pdf"
        if row.get("source_format") != "pdf" or row.get("original_file_path") != expected:
            raise EbookServiceError("Ebook này không phải PDF.", 404)
        return presigned_download_url(expected)
    except EbookServiceError:
        raise
    except SyncError as exc:
        raise EbookServiceError("Không thể mở PDF riêng tư.", 503) from exc


def _delete_b2_prefix(prefix: str) -> int:
    import os
    from app.storage import client

    s3 = client()
    bucket = os.environ["B2_BUCKET"]
    marker: dict[str, str] = {}
    deleted = 0
    while True:
        page = s3.list_object_versions(Bucket=bucket, Prefix=prefix, **marker)
        objects = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
        ]
        for offset in range(0, len(objects), 1000):
            response = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects[offset:offset + 1000], "Quiet": True})
            if response.get("Errors"):
                raise EbookServiceError("B2 không xoá được toàn bộ file/version của ebook.", 503)
            deleted += len(objects[offset:offset + 1000])
        if not page.get("IsTruncated"):
            return deleted
        marker = {
            "KeyMarker": page["NextKeyMarker"],
            "VersionIdMarker": page["NextVersionIdMarker"],
        }


def delete_ebook(owner_id: str, ebook_id: str) -> None:
    try:
        canonical_id = str(uuid.UUID(ebook_id))
    except (ValueError, AttributeError) as exc:
        raise EbookServiceError("ID ebook không hợp lệ.", 404) from exc
    remote = SupabaseSync()
    try:
        books = remote.rows("ebook_books", {
            "select": "id", "id": f"eq.{canonical_id}", "owner_id": f"eq.{owner_id}", "limit": "1",
        })
        if not books:
            raise EbookServiceError("Không tìm thấy ebook.", 404)
        owner_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32]
        prefix = f"ebooks/{owner_key}/{canonical_id}/"
        _delete_b2_prefix(prefix)
        response = remote.request(
            "DELETE", "/rest/v1/ebook_books",
            params={"id": f"eq.{canonical_id}", "owner_id": f"eq.{owner_id}"},
            headers={"Prefer": "return=representation"},
        )
        deleted_rows = response.json()
        if not isinstance(deleted_rows, list) or not deleted_rows:
            raise EbookServiceError("Không xoá được metadata ebook trên Supabase.", 503)
    except EbookServiceError:
        raise
    except Exception as exc:
        raise EbookServiceError("Không thể xoá ebook và dữ liệu lưu trữ.", 503) from exc


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

    return build_ebook_export(
        str(book.get("title") or "Ebook"), str(book.get("author") or ""),
        ebook_id, export_chapters, file_format, language,
    )


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


def enqueue_ebook_translation(
    owner_id: str,
    ebook_id: str,
    *,
    retranslate: bool = False,
    raw_chapters: dict[int, str] | None = None,
    remote: SupabaseSync | None = None,
) -> int:
    remote = remote or SupabaseSync()
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
            "select": "ebook_chapter_id,status,retry_count", "ebook_chapter_id": f"in.({','.join(str(row['id']) for row in chapters)})" if chapters else "eq.0",
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
        elif status == "failed" and queued and queued.get("status") == "failed":
            existing_chunks = remote.all_rows("ebook_translation_chunks", {
                "select": "chunk_index,status", "ebook_chapter_id": f"eq.{chapter_id}",
                "order": "chunk_index.asc",
            })
            if existing_chunks:
                completed = sum(row.get("status") == "completed" for row in existing_chunks)
                remote.patch_rows("ebook_translation_queue", {"ebook_chapter_id": f"eq.{chapter_id}", "status": "eq.failed"}, {
                    "status": "queued", "current_chunk": completed, "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                remote.patch_rows("ebook_chapters", {"id": f"eq.{chapter_id}"}, {"translation_status": "pending"})
                chapter_jobs.append({"ebook_chapter_id": chapter_id, "status": "queued", "current_chunk": completed, "total_chunks": len(existing_chunks), "retry_count": int(queued.get("retry_count") or 0)})
                continue
            remote.delete_rows("ebook_translation_queue", {"ebook_chapter_id": f"eq.{chapter_id}"})
            remote.patch_rows("ebook_chapters", {"id": f"eq.{chapter_id}"}, {"translation_status": "pending"})
        elif status != "pending" or queued:
            continue
        raw_html = (raw_chapters or {}).get(int(chapter["chapter_number"])) if not retranslate else None
        if raw_html is None:
            path = str(chapter.get("content_path") or "")
            if path not in shard_cache:
                shard_cache[path] = remote.download_json("ebooks", path.removeprefix("ebooks/"))
            items = shard_cache[path].get("chapters") or []
            index = int(chapter.get("content_index", -1))
            if index < 0 or index >= len(items):
                raise EbookServiceError("Ebook chapter source is unavailable.", 503)
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
