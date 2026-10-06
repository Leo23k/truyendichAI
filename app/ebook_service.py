"""Private ebook persistence. Every operation is scoped to the authenticated owner."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.ebooks import EbookParseError, ParsedEbook, parse_ebook
from app.storage import configured as b2_configured, presigned_download_url, upload as b2_upload
from sync.service import SupabaseSync, SyncError


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
