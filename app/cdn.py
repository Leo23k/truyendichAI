"""Supabase-backed public novel data with a local development fallback."""

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

import httpx
from dotenv import load_dotenv
from app.storage import cdn_object_url, configured as b2_configured, presigned_download_url, upload as b2_upload


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_SECRET_KEY", "")
LOCAL_SYNC_DATA_DIR = Path(
    os.getenv("SYNCED_DATA_PATH", ROOT / "temp" / "truyendichAI-data" / "data")
)
JSDELIVR_DATA_BASE_URL = os.getenv(
    "JSDELIVR_DATA_BASE_URL",
    "https://cdn.jsdelivr.net/gh/Leo23k/truyendichAI-data@main/data",
).rstrip("/")
_NOVELS_CACHE_TTL_SECONDS = 12.0
_novels_cache: tuple[float, list[dict]] | None = None
logger = logging.getLogger(__name__)


class CdnDataError(RuntimeError):
    pass


def _safe_path(path: str) -> PurePosixPath:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise CdnDataError("Invalid storage path")
    return relative


def _configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)


def storage_url(bucket: str, path: str) -> str:
    if b2_configured() and bucket != "covers":
        object_key = f"{bucket}/{_safe_path(path).as_posix()}"
        cdn_read_url = cdn_object_url(object_key) if bucket == "chapters" and os.getenv("CDN_READ_TOKEN") else None
        return cdn_read_url or presigned_download_url(object_key)
    if not SUPABASE_URL:
        raise CdnDataError("Supabase is not configured")
    relative = _safe_path(path)
    return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{quote(relative.as_posix())}"


def _supabase_storage_url(relative: PurePosixPath) -> str | None:
    parts = relative.parts
    if parts and parts[0] in {"chapters", "covers"}:
        return f"{SUPABASE_URL}/storage/v1/object/public/{parts[0]}/{quote('/'.join(parts[1:]))}"
    if len(parts) >= 4 and parts[0] == "novels" and parts[2] == "chapters":
        return f"{SUPABASE_URL}/storage/v1/object/public/chapters/{quote('/'.join((parts[1], *parts[3:])))}"
    return None


def cdn_url(path: str) -> str:
    relative = _safe_path(path)
    parts = relative.parts
    if not _configured() and not b2_configured():
        return f"{JSDELIVR_DATA_BASE_URL}/{quote(relative.as_posix())}"
    if len(parts) >= 4 and parts[0] == "novels" and parts[2] == "audio":
        return storage_url("audio", f"{parts[1]}/{'/'.join(parts[3:])}")
    if len(parts) >= 4 and parts[0] == "novels" and parts[2] == "chapters":
        return storage_url("chapters", f"{parts[1]}/{'/'.join(parts[3:])}")
    if parts and parts[0] in {"audio", "chapters", "covers"}:
        return storage_url(parts[0], "/".join(parts[1:]))
    raise CdnDataError(f"Unsupported storage path: {path}")


def _headers() -> dict[str, str]:
    headers = {"apikey": SUPABASE_ANON_KEY}
    if SUPABASE_ANON_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {SUPABASE_ANON_KEY}"
    return headers


def _service_headers() -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise CdnDataError("Supabase write access is not configured")
    headers = {"apikey": SUPABASE_SERVICE_KEY}
    if SUPABASE_SERVICE_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {SUPABASE_SERVICE_KEY}"
    return headers


def _service_rows(table: str, params: dict[str, str]) -> list[dict]:
    try:
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.get(f"{SUPABASE_URL}/rest/v1/{table}", params=params, headers=_service_headers())
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CdnDataError(f"Could not load Supabase {table}") from exc
    if not isinstance(payload, list):
        raise CdnDataError(f"Invalid Supabase {table} response")
    return [item for item in payload if isinstance(item, dict)]


def _service_patch(table: str, params: dict[str, str], payload: dict) -> None:
    try:
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.patch(
                f"{SUPABASE_URL}/rest/v1/{table}", params=params, json=payload,
                headers={**_service_headers(), "Prefer": "return=minimal"},
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CdnDataError(f"Could not update Supabase {table}") from exc


def _service_delete(table: str, params: dict[str, str]) -> None:
    try:
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.delete(
                f"{SUPABASE_URL}/rest/v1/{table}", params=params,
                headers={**_service_headers(), "Prefer": "return=minimal"},
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CdnDataError(f"Could not delete Supabase {table}") from exc


def _service_all_rows(table: str, params: dict[str, str]) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page = _service_rows(table, {**params, "limit": "1000", "offset": str(offset)})
        rows.extend(page)
        if len(page) < 1000:
            return rows
        offset += len(page)


def _service_delete_storage_objects(bucket: str, paths: list[str]) -> None:
    if not paths:
        return
    safe_paths = [_safe_path(path).as_posix() for path in paths]
    try:
        with httpx.Client(timeout=60, trust_env=False) as client:
            for start in range(0, len(safe_paths), 100):
                response = client.request(
                    "DELETE",
                    f"{SUPABASE_URL}/storage/v1/object/{quote(bucket)}",
                    json={"prefixes": safe_paths[start : start + 100]},
                    headers=_service_headers(),
                )
                response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CdnDataError(f"Could not delete Supabase {bucket} storage objects") from exc


def load_replacement_rules(user_id: str, book_id: str) -> list[dict]:
    return _service_rows("replacement_rules", {
        "select": "id,source_text,replacement_text,created_at,updated_at",
        "user_id": f"eq.{user_id}", "book_id": f"eq.{book_id}", "order": "created_at.asc",
    })


def save_replacement_rule(user_id: str, book_id: str, source_text: str, replacement_text: str, rule_id: int | None = None) -> dict:
    payload = {"source_text": source_text, "replacement_text": replacement_text, "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        with httpx.Client(timeout=30, trust_env=False) as client:
            if rule_id is not None:
                response = client.patch(f"{SUPABASE_URL}/rest/v1/replacement_rules", params={"id": f"eq.{rule_id}", "user_id": f"eq.{user_id}", "book_id": f"eq.{book_id}", "select": "id,source_text,replacement_text,created_at,updated_at"}, json=payload, headers={**_service_headers(), "Prefer": "return=representation"})
            else:
                response = client.post(f"{SUPABASE_URL}/rest/v1/replacement_rules?on_conflict=user_id,book_id,source_text", json={**payload, "user_id": user_id, "book_id": book_id}, headers={**_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"})
        response.raise_for_status()
        rows = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CdnDataError("Could not save replacement rule") from exc
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise CdnDataError("Replacement rule was not found")
    return rows[0]


def delete_replacement_rule(user_id: str, book_id: str, rule_id: int) -> None:
    _service_delete("replacement_rules", {"id": f"eq.{rule_id}", "user_id": f"eq.{user_id}", "book_id": f"eq.{book_id}"})


def apply_replacement_rules(text: str | None, rules: list[dict]) -> str:
    """Apply literal rules once, longest source first, without cascading replacements."""
    value = text or ""
    replacements = {str(rule.get("source_text")): str(rule.get("replacement_text") or "") for rule in rules if str(rule.get("source_text") or "")}
    if not replacements:
        return value
    pattern = re.compile("|".join(re.escape(source) for source in sorted(replacements, key=len, reverse=True)))
    return pattern.sub(lambda match: replacements[match.group(0)], value)


def _rest_rows(table: str, params: dict[str, str]) -> list[dict]:
    if not _configured():
        raise CdnDataError("Supabase is not configured")
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            response = client.get(f"{SUPABASE_URL}/rest/v1/{table}", params=params, headers=_headers())
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CdnDataError(f"Could not load Supabase {table}") from exc
    if not isinstance(payload, list):
        raise CdnDataError(f"Invalid Supabase {table} response")
    return [item for item in payload if isinstance(item, dict)]


def _rest_count(table: str, params: dict[str, str]) -> int:
    """Read PostgREST's exact count without downloading every matching row."""
    if not _configured():
        raise CdnDataError("Supabase is not configured")
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            response = client.get(
                f"{SUPABASE_URL}/rest/v1/{table}",
                params={**params, "limit": "1"},
                headers={**_headers(), "Prefer": "count=exact"},
            )
        response.raise_for_status()
        total = response.headers.get("content-range", "").partition("/")[2]
        return int(total)
    except (httpx.HTTPError, ValueError) as exc:
        raise CdnDataError(f"Could not count Supabase {table}") from exc


def _all_rows(table: str, params: dict[str, str]) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page = _rest_rows(table, {**params, "limit": "1000", "offset": str(offset)})
        rows.extend(page)
        if len(page) < 1000:
            return rows
        offset += len(page)


def _local_json(path: str) -> dict:
    try:
        payload = json.loads(LOCAL_SYNC_DATA_DIR.joinpath(*_safe_path(path).parts).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CdnDataError(f"Could not load local data: {path}") from exc
    if not isinstance(payload, dict):
        raise CdnDataError(f"Invalid local data: {path}")
    return payload


def _github_json(path: str) -> dict:
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            response = client.get(f"{JSDELIVR_DATA_BASE_URL}/{quote(_safe_path(path).as_posix())}")
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CdnDataError(f"Could not load GitHub fallback data: {path}") from exc
    if not isinstance(payload, dict):
        raise CdnDataError(f"Invalid GitHub fallback data: {path}")
    return payload


def _fallback_json(path: str) -> dict:
    try:
        return _local_json(path)
    except CdnDataError:
        return _github_json(path)


def _resolved_cover_url(cover_path: str | None, slug: str, fallback_covers: dict[str, str]) -> str | None:
    if cover_path:
        if cover_path.startswith(("http://", "https://")):
            return cover_path
        return storage_url("covers", cover_path.removeprefix("covers/"))
    return fallback_covers.get(slug)


def _source_name(source_url: str | None) -> str | None:
    hostname = urlparse(source_url or "").hostname
    if not hostname:
        return None
    return hostname.removeprefix("www.").removeprefix("m.")


def load_novels() -> list[dict]:
    global _novels_cache
    now = time.monotonic()
    if _novels_cache and now - _novels_cache[0] < _NOVELS_CACHE_TTL_SECONDS:
        return [dict(novel) for novel in _novels_cache[1]]

    if _configured():
        try:
            novels = _all_rows(
                "novels",
                {
                    "select": "id,slug,title,author,reading_status,cover_url,latest_chapter,updated_at,source_url,chapter_rows:chapters(count),translated_chapter_rows:chapters(count)",
                    "translated_chapter_rows.translated": "eq.true",
                    "deleted_at": "is.null",
                    "order": "updated_at.desc",
                },
            )
            fallback_covers: dict[str, str] = {}
            try:
                fallback_covers = {
                    item["slug"]: item["cover"]
                    for item in _fallback_json("metadata.json").get("novels", [])
                    if item.get("slug") and item.get("cover")
                }
            except CdnDataError:
                pass
            for novel in novels:
                chapter_rows = novel.pop("chapter_rows", [])
                translated_rows = novel.pop("translated_chapter_rows", [])
                novel["total_chapters"] = int(chapter_rows[0]["count"]) if chapter_rows else 0
                novel["translated_chapters"] = int(translated_rows[0]["count"]) if translated_rows else 0
                novel["cover"] = _resolved_cover_url(novel.get("cover_url"), novel["slug"], fallback_covers)
                novel["cover_fallback"] = fallback_covers.get(novel["slug"])
                novel["source_name"] = _source_name(novel.get("source_url"))
            _novels_cache = (time.monotonic(), novels)
            return [dict(novel) for novel in novels]
        except CdnDataError:
            pass
    novels = _fallback_json("metadata.json").get("novels", [])
    _novels_cache = (time.monotonic(), novels)
    return [dict(novel) for novel in novels]


def load_admin_novels() -> list[dict]:
    """Load editable novel fields using the service role for the admin panel."""
    select = "id,slug,title,author,description,genres,reading_status,source_type,cover_url,original_title,source_url,untranslated_chapter_rows:chapters(count)"
    try:
        rows = _service_rows("novels", {
            "select": select,
            "deleted_at": "is.null",
            "untranslated_chapter_rows.translated": "eq.false",
            "order": "title.asc",
        })
    except CdnDataError:
        # Keep the panel usable until the description migration has been applied.
        rows = _service_rows("novels", {
            "select": "id,slug,title,author,original_title,source_url,reading_status,untranslated_chapter_rows:chapters(count)",
            "deleted_at": "is.null",
            "untranslated_chapter_rows.translated": "eq.false",
            "order": "title.asc",
        })
    for novel in rows:
        counts = novel.pop("untranslated_chapter_rows", [])
        novel["untranslated_chapters"] = int(counts[0].get("count") or 0) if counts else 0
    return rows


def update_admin_novel(novel_id: int, *, slug: str, title: str, author: str, description: str, cover_url: str = "", genres: list[str] | None = None, reading_status: str = "ongoing") -> None:
    existing = _service_rows("novels", {"select": "id,slug", "id": f"eq.{novel_id}", "limit": "1"})
    if not existing:
        raise CdnDataError("Novel was not found")
    duplicate = _service_rows("novels", {"select": "id", "slug": f"eq.{slug}", "id": f"neq.{novel_id}", "limit": "1"})
    if duplicate:
        raise CdnDataError("Slug này đã được dùng bởi truyện khác.")
    _service_patch("novels", {"id": f"eq.{novel_id}"}, {
        "slug": slug,
        "title": title,
        "author": author or None,
        "description": description or None,
        "cover_url": cover_url or None,
        "genres": genres or [],
        "reading_status": reading_status,
    })


def upload_admin_novel_cover(slug: str, content: bytes, content_type: str) -> str:
    extensions = {"image/jpeg": (".jpg", b"\xff\xd8\xff"), "image/png": (".png", b"\x89PNG\r\n\x1a\n"), "image/webp": (".webp", b"RIFF"), "image/gif": (".gif", (b"GIF87a", b"GIF89a"))}
    image_type = extensions.get(content_type.lower().split(";", 1)[0].strip())
    if not content or len(content) > 15 * 1024 * 1024 or not image_type:
        raise CdnDataError("Ảnh bìa phải là JPEG, PNG, WebP hoặc GIF và không quá 15 MB.")
    extension, signature = image_type
    valid_signature = content.startswith(signature) if isinstance(signature, bytes) else content.startswith(signature)
    if content_type.lower().startswith("image/webp"):
        valid_signature = len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    if not valid_signature:
        raise CdnDataError("Nội dung tệp không khớp với định dạng ảnh đã chọn.")
    safe_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug).strip("-_") or "novel"
    path = f"{safe_slug}/uploaded-{uuid.uuid4().hex}{extension}"
    try:
        with httpx.Client(timeout=60, trust_env=False) as client:
            response = client.post(
                f"{SUPABASE_URL}/storage/v1/object/covers/{quote(path, safe='/')}",
                content=content,
                headers={**_service_headers(), "x-upsert": "true", "Content-Type": content_type},
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CdnDataError("Không thể tải ảnh bìa lên Supabase Storage.") from exc
    return f"{SUPABASE_URL}/storage/v1/object/public/covers/{quote(path, safe='/')}"


def update_admin_novel_source(novel_id: int, source_url: str) -> dict:
    rows = _service_rows("novels", {"select": "id,title,source_type", "id": f"eq.{novel_id}", "limit": "1"})
    if not rows:
        raise CdnDataError("Novel was not found")
    _service_patch("novels", {"id": f"eq.{novel_id}"}, {"source_url": source_url})
    return rows[0]


def load_admin_novel_chapters(novel_id: int) -> tuple[dict, list[dict]]:
    novels = _service_rows("novels", {"select": "id,slug,title", "id": f"eq.{novel_id}", "deleted_at": "is.null", "limit": "1"})
    if not novels:
        raise CdnDataError("Không tìm thấy truyện.")
    chapters = _service_all_rows("chapters", {
        "select": "id,chapter_number,title,source_url,translated,content_path,updated_at",
        "novel_id": f"eq.{novel_id}",
        "order": "chapter_number.asc,id.asc",
    })
    return novels[0], chapters


def _refresh_admin_novel_latest_chapter(novel_id: int) -> None:
    chapters = _service_rows("chapters", {"select": "chapter_number", "novel_id": f"eq.{novel_id}", "order": "chapter_number.desc", "limit": "1"})
    _service_patch("novels", {"id": f"eq.{novel_id}"}, {"latest_chapter": int(chapters[0]["chapter_number"]) if chapters else 0})


def save_admin_novel_chapter(novel_id: int, *, chapter_id: int | None, chapter_number: int, title: str, source_url: str) -> None:
    if chapter_number < 1 or not title or not source_url:
        raise CdnDataError("Số chương, tiêu đề và URL nguồn là bắt buộc.")
    parsed_url = urlparse(source_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise CdnDataError("URL nguồn phải là một địa chỉ http hoặc https hợp lệ.")
    novel, _ = load_admin_novel_chapters(novel_id)
    duplicate_number = _service_rows("chapters", {
        "select": "id", "novel_id": f"eq.{novel['id']}", "chapter_number": f"eq.{chapter_number}",
        "id": f"neq.{chapter_id}" if chapter_id else "gt.0", "limit": "1",
    })
    if duplicate_number:
        raise CdnDataError("Số chương này đã tồn tại trong truyện.")
    duplicate_url = _service_rows("chapters", {
        "select": "id", "novel_id": f"eq.{novel['id']}", "source_url": f"eq.{source_url}",
        "id": f"neq.{chapter_id}" if chapter_id else "gt.0", "limit": "1",
    })
    if duplicate_url:
        raise CdnDataError("URL nguồn này đã được dùng cho một chương khác.")
    payload = {"chapter_number": chapter_number, "title": title, "source_url": source_url}
    if chapter_id is None:
        try:
            with httpx.Client(timeout=30, trust_env=False) as client:
                response = client.post(f"{SUPABASE_URL}/rest/v1/chapters", json={**payload, "novel_id": novel_id, "translated": False}, headers={**_service_headers(), "Prefer": "return=minimal"})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CdnDataError("Không thể tạo chương.") from exc
    else:
        existing = _service_rows("chapters", {"select": "id", "id": f"eq.{chapter_id}", "novel_id": f"eq.{novel_id}", "limit": "1"})
        if not existing:
            raise CdnDataError("Không tìm thấy chương.")
        _service_patch("chapters", {"id": f"eq.{chapter_id}", "novel_id": f"eq.{novel_id}"}, payload)
    _refresh_admin_novel_latest_chapter(novel_id)


def delete_admin_novel_chapter(novel_id: int, chapter_id: int) -> None:
    existing = _service_rows("chapters", {"select": "id", "id": f"eq.{chapter_id}", "novel_id": f"eq.{novel_id}", "limit": "1"})
    if not existing:
        raise CdnDataError("Không tìm thấy chương.")
    _service_delete("chapters", {"id": f"eq.{chapter_id}", "novel_id": f"eq.{novel_id}"})
    remaining = _service_all_rows("chapters", {
        "select": "id,chapter_number", "novel_id": f"eq.{novel_id}", "order": "chapter_number.asc,id.asc",
    })
    for index, row in enumerate(remaining, start=1):
        if int(row.get("chapter_number") or 0) != index:
            _service_patch("chapters", {"id": f"eq.{row['id']}", "novel_id": f"eq.{novel_id}"}, {"chapter_number": index})
    _refresh_admin_novel_latest_chapter(novel_id)


def load_admin_chapter_content(novel_id: int, chapter_id: int) -> tuple[dict, dict, dict]:
    novel, _ = load_admin_novel_chapters(novel_id)
    chapters = _service_rows("chapters", {"select": "id,chapter_number,title,source_url,translated,content_path", "id": f"eq.{chapter_id}", "novel_id": f"eq.{novel_id}", "limit": "1"})
    if not chapters:
        raise CdnDataError("Không tìm thấy chương.")
    chapter = chapters[0]
    payload: dict = {}
    if chapter.get("content_path"):
        try:
            payload = load_cdn_json(str(chapter["content_path"]))
        except CdnDataError:
            pass
    return novel, chapter, payload


def save_admin_chapter_content(novel_id: int, chapter_id: int, *, original_title: str, translated_title: str, original_content: str, translated_content: str) -> None:
    novel, chapter, _ = load_admin_chapter_content(novel_id, chapter_id)
    if not original_title or not original_content:
        raise CdnDataError("Tiêu đề và nội dung bản gốc là bắt buộc.")
    if not b2_configured():
        raise CdnDataError("B2 chưa được cấu hình; không thể lưu nội dung chương.")
    translated = bool(translated_content.strip())
    key = f"{int(chapter['chapter_number']):03d}.json"
    storage_path = f"{novel['slug']}/{key}"
    payload = json.dumps({
        "number": chapter["chapter_number"], "title": translated_title or original_title,
        "original_title": original_title, "original_content": original_content,
        "translated_content": translated_content if translated else "",
        "content": translated_content if translated else original_content,
        "translated": translated, "audio": None,
    }, ensure_ascii=False).encode("utf-8")
    try:
        b2_upload(f"chapters/{storage_path}", payload, "application/json")
    except Exception as exc:
        raise CdnDataError("Không thể lưu nội dung chương.") from exc
    _service_patch("chapters", {"id": f"eq.{chapter_id}", "novel_id": f"eq.{novel_id}"}, {
        "title": translated_title or original_title, "translated": translated,
        "content_path": f"chapters/{storage_path}", "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    _service_delete("chapter_chunks", {"chapter_id": f"eq.{chapter_id}"})
    _service_delete("translation_queue", {"chapter_id": f"eq.{chapter_id}"})


def load_synced_novel(slug: str) -> dict:
    if _configured():
        try:
            try:
                rows = _rest_rows(
                    "novels",
                    {"select": "id,slug,title,author,description,genres,reading_status,cover_url,latest_chapter,updated_at,source_url", "slug": f"eq.{slug}", "deleted_at": "is.null", "limit": "1"},
                )
            except CdnDataError:
                rows = _rest_rows(
                    "novels",
                    {"select": "id,slug,title,author,cover_url,latest_chapter,updated_at,source_url", "slug": f"eq.{slug}", "deleted_at": "is.null", "limit": "1"},
                )
            if rows:
                novel = rows[0]
                novel["cover"] = _resolved_cover_url(novel.get("cover_url"), slug, {})
                novel["source_name"] = _source_name(novel.get("source_url"))
                return novel
        except CdnDataError:
            pass
    metadata = _fallback_json("metadata.json")
    novel = next((item for item in metadata.get("novels", []) if item.get("slug") == slug), None)
    if not isinstance(novel, dict):
        raise CdnDataError("Novel was not found")
    return novel


def get_synced_novel_for_management(slug: str) -> dict:
    rows = _service_rows(
        "novels",
        {"select": "id,slug,source_url", "slug": f"eq.{slug}", "limit": "1"},
    )
    if not rows:
        raise CdnDataError("Novel was not found")
    return rows[0]


def load_deleted_synced_novels() -> list[dict]:
    return _service_rows(
        "novels",
        {"select": "slug,title,deleted_at,delete_after", "deleted_at": "not.is.null", "order": "deleted_at.desc"},
    )


def load_translation_memory() -> list[dict]:
    return _service_rows(
        "translation_memory",
        {"select": "id,source_text,replacement_text", "order": "source_text.asc"},
    )


def save_translation_memory(source_text: str, replacement_text: str) -> None:
    try:
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.post(
                f"{SUPABASE_URL}/rest/v1/translation_memory?on_conflict=source_text",
                json={"source_text": source_text, "replacement_text": replacement_text},
                headers={**_service_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CdnDataError("Could not save Supabase translation memory") from exc


def delete_translation_memory(entry_id: int) -> None:
    _service_delete("translation_memory", {"id": f"eq.{entry_id}"})


def add_synced_chapters(slug: str, chapters: list[object]) -> int:
    novel = get_synced_novel_for_management(slug)
    existing_rows = _service_rows("chapters", {"select": "source_url", "novel_id": f"eq.{novel['id']}"})
    existing_urls = {row.get("source_url") for row in existing_rows}
    payload = [
        {
            "novel_id": novel["id"],
            "chapter_number": chapter.chapter_number,
            "title": chapter.title,
            "source_url": chapter.url,
            "translated": False,
        }
        for chapter in chapters
        if chapter.url not in existing_urls
    ]
    if not payload:
        return 0
    try:
        with httpx.Client(timeout=60, trust_env=False) as client:
            response = client.post(
                f"{SUPABASE_URL}/rest/v1/chapters?on_conflict=novel_id,source_url",
                json=payload,
                headers={**_service_headers(), "Prefer": "resolution=ignore-duplicates,return=minimal"},
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CdnDataError("Could not add Supabase chapters") from exc
    return len(payload)


def set_synced_novel_deletion(slug: str, *, deleted_at: str | None, delete_after: str | None) -> None:
    _service_patch(
        "novels",
        {"slug": f"eq.{slug}"},
        {"deleted_at": deleted_at, "delete_after": delete_after},
    )


def _delete_synced_novel_permanently(params: dict[str, str]) -> None:
    novels = _service_rows("novels", {"select": "id", **params})
    if not novels:
        return
    novel_ids = [int(novel["id"]) for novel in novels]
    chapters = _service_all_rows(
        "chapters",
        {"select": "content_path", "novel_id": f"in.({','.join(str(novel_id) for novel_id in novel_ids)})"},
    )
    paths = sorted({str(chapter["content_path"]).removeprefix("chapters/") for chapter in chapters if str(chapter.get("content_path") or "").startswith("chapters/")})
    # Remove Storage first: if it fails, retain database rows so the reader
    # cannot be left pointing to files that were silently lost.
    _service_delete_storage_objects("chapters", paths)
    _service_delete("novels", params)


def delete_synced_novel_permanently(slug: str) -> None:
    _delete_synced_novel_permanently({"slug": f"eq.{slug}"})


def delete_synced_novel_permanently_by_id(novel_id: int) -> None:
    _delete_synced_novel_permanently({"id": f"eq.{novel_id}"})


def load_translation_progress(chapter_id: int) -> dict | None:
    queues = _service_rows("translation_queue", {"select": "status,current_chunk,total_chunks", "chapter_id": f"eq.{chapter_id}", "limit": "1"})
    if not queues:
        return None
    chunks = _service_rows("chapter_chunks", {"select": "chunk_index,translated_text,status", "chapter_id": f"eq.{chapter_id}", "order": "chunk_index.asc"})
    return {**queues[0], "chunks": chunks}


def load_user_reading_progress(user_id: str, book_id: str | None = None) -> list[dict]:
    params = {"select": "novel_id,chapter_number,scroll_position,updated_at", "user_id": f"eq.{user_id}", "order": "updated_at.desc"}
    rows = _service_rows("reading_progress", params)
    if not rows:
        return []
    novel_ids = sorted({int(row["novel_id"]) for row in rows})
    novels = _service_rows("novels", {"select": "id,slug", "id": f"in.({','.join(str(novel_id) for novel_id in novel_ids)})"})
    slugs = {int(novel["id"]): str(novel["slug"]) for novel in novels}
    return [{**row, "book_id": slugs[int(row["novel_id"])]} for row in rows if slugs.get(int(row["novel_id"])) and (book_id is None or slugs[int(row["novel_id"])] == book_id)]


def save_user_reading_progress(user_id: str, book_id: str, chapter_number: int, scroll_position: float) -> None:
    novels = _service_rows("novels", {"select": "id", "slug": f"eq.{book_id}", "limit": "1"})
    if not novels:
        raise CdnDataError("Novel was not found")
    payload = {
        "user_id": user_id,
        "novel_id": int(novels[0]["id"]),
        "chapter_number": chapter_number,
        "scroll_position": max(0, min(100, scroll_position)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            response = client.post(
                f"{SUPABASE_URL}/rest/v1/reading_progress?on_conflict=user_id,novel_id",
                json=payload,
                headers={**_service_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CdnDataError("Could not save reading progress") from exc


def load_cdn_json(path: str) -> dict:
    relative = _safe_path(path)
    if relative.as_posix() == "metadata.json":
        return {"novels": load_novels()}
    if (_configured() or b2_configured()) and (
        (len(relative.parts) >= 4 and relative.parts[0] == "novels" and relative.parts[2] == "chapters")
        or (len(relative.parts) >= 3 and relative.parts[0] == "chapters")
    ):
        urls = [cdn_url(path)]
        fallback_url = _supabase_storage_url(relative) if b2_configured() else None
        if fallback_url:
            urls.append(fallback_url)
        cdn_base = os.getenv("CDN_BASE_URL", "").strip().rstrip("/")
        for url in urls:
            with httpx.Client(timeout=15, trust_env=False) as client:
                try:
                    if SUPABASE_URL and url.startswith(f"{SUPABASE_URL}/"):
                        headers = _headers()
                    elif cdn_base and url.startswith(f"{cdn_base}/") and os.getenv("CDN_READ_TOKEN"):
                        headers = {"X-CDN-Read-Token": os.environ["CDN_READ_TOKEN"]}
                    else:
                        headers = {}
                    response = client.get(url, headers=headers)
                    response.raise_for_status()
                    payload = response.json()
                    if isinstance(payload, dict):
                        return payload
                    if url == urls[0] and fallback_url:
                        logger.warning("Primary chapter read returned a non-object for %s; trying Supabase Storage", relative)
                except (httpx.HTTPError, ValueError) as exc:
                    if url == urls[0] and fallback_url:
                        logger.warning("Primary chapter read failed for %s (%s); trying Supabase Storage", relative, type(exc).__name__)
                    continue
    return _fallback_json(path)


def load_chapters_index(slug: str) -> list[dict]:
    if _configured():
        try:
            novels = _rest_rows("novels", {"select": "id,source_type", "slug": f"eq.{slug}", "limit": "1"})
            if not novels:
                raise CdnDataError("Novel was not found")
            chapters = _all_rows(
                "chapters",
                {
                    "select": "id,chapter_number,title,source_url,translated,content_path",
                    "novel_id": f"eq.{novels[0]['id']}",
                    "order": "chapter_number.asc",
                },
            )
            source_type = str(novels[0].get("source_type") or "zh")
            # Chapter IDs remain stable, while the public sequence is compact
            # after an admin deletion (e.g. 1, 2, 4 becomes 1, 2, 3).
            return [{**chapter, "chapter_number": index, "source_type": source_type} for index, chapter in enumerate(chapters, start=1)]
        except CdnDataError:
            pass
    payload = _fallback_json(f"novels/{slug}/chapters_index.json")
    chapters = payload.get("chapters", [])
    if not isinstance(chapters, list):
        raise CdnDataError(f"Invalid chapters index: {slug}")
    return [{**chapter, "chapter_number": index} for index, chapter in enumerate((chapter for chapter in chapters if isinstance(chapter, dict)), start=1)]


def load_chapters_page(slug: str, page: int = 1, sort: str = "asc", limit: int = 50) -> tuple[list[dict], int, int]:
    """Fetch one ordered table-of-contents page without loading every chapter."""
    page, limit = max(1, page), max(1, min(limit, 100))
    sort = "desc" if sort == "desc" else "asc"
    if _configured():
        try:
            novels = _rest_rows("novels", {"select": "id", "slug": f"eq.{slug}", "limit": "1"})
            if not novels:
                raise CdnDataError("Novel was not found")
            params = {"novel_id": f"eq.{novels[0]['id']}"}
            total = _rest_count("chapters", params)
            total_pages = max(1, (total + limit - 1) // limit)
            page = min(page, total_pages)
            offset = (page - 1) * limit
            rows = _rest_rows("chapters", {"select": "id,chapter_number,title", **params, "order": f"chapter_number.{sort}", "limit": str(limit), "offset": str(offset)})
            rows = [{**row, "chapter_number": (offset + index if sort == "asc" else total - offset - index + 1)} for index, row in enumerate(rows, start=1)]
            return rows, total, page
        except CdnDataError:
            pass
    chapters = load_chapters_index(slug)
    chapters.sort(key=lambda item: int(item.get("chapter_number", 0)), reverse=sort == "desc")
    total = len(chapters)
    total_pages = max(1, (total + limit - 1) // limit)
    page = min(page, total_pages)
    offset = (page - 1) * limit
    rows = chapters[offset : offset + limit]
    return [{**row, "chapter_number": (offset + index if sort == "asc" else total - offset - index + 1)} for index, row in enumerate(rows, start=1)], total, page


def publish_translated_chapter(
    *,
    slug: str,
    chapter_number: int,
    title: str,
    original_title: str,
    source_url: str,
    original_content: str,
    content: str,
) -> None:
    if not b2_configured():
        raise CdnDataError("B2 chưa được cấu hình; không thể lưu bản dịch chương.")
    headers = _service_headers()
    key = f"{chapter_number:03d}"
    json_path = f"{slug}/{key}.json"
    payload = json.dumps(
        {
            "number": chapter_number,
            "title": title,
            "original_title": original_title,
            "original_content": original_content,
            "translated_content": content,
            "content": content,
            "translated": True,
            "audio": None,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    b2_upload(f"chapters/{json_path}", payload, "application/json")
    with httpx.Client(timeout=60, trust_env=False) as client:
        novels = client.get(
            f"{SUPABASE_URL}/rest/v1/novels",
            params={"select": "id,latest_chapter", "slug": f"eq.{slug}", "limit": "1"},
            headers=headers,
        )
        novels.raise_for_status()
        rows = novels.json()
        if not rows:
            raise CdnDataError("Novel was not found")
        novel = rows[0]
        chapter_update = client.patch(
            f"{SUPABASE_URL}/rest/v1/chapters",
            params={"novel_id": f"eq.{novel['id']}", "source_url": f"eq.{source_url}"},
            json={
                "title": title,
                "translated": True,
                "content_path": f"chapters/{json_path}",
            },
            headers={**headers, "Prefer": "return=minimal"},
        )
        chapter_update.raise_for_status()
        if int(novel.get("latest_chapter") or 0) < chapter_number:
            novel_update = client.patch(
                f"{SUPABASE_URL}/rest/v1/novels",
                params={"id": f"eq.{novel['id']}"},
                json={"latest_chapter": chapter_number},
                headers={**headers, "Prefer": "return=minimal"},
            )
            novel_update.raise_for_status()
