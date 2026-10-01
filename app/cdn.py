"""Supabase-backed public novel data with a local development fallback."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

import httpx
from dotenv import load_dotenv


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
    if not SUPABASE_URL:
        raise CdnDataError("Supabase is not configured")
    relative = _safe_path(path)
    return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{quote(relative.as_posix())}"


def cdn_url(path: str) -> str:
    relative = _safe_path(path)
    parts = relative.parts
    if not _configured():
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
    if _configured():
        try:
            novels = _all_rows(
                "novels",
                {
                    "select": "id,slug,title,author,cover_url,latest_chapter,updated_at,source_url",
                    "deleted_at": "is.null",
                    "order": "updated_at.desc",
                },
            )
            chapter_counts = _all_rows("chapters", {"select": "novel_id,translated"})
            translated_counts: dict[int, int] = {}
            total_counts: dict[int, int] = {}
            for row in chapter_counts:
                total_counts[row["novel_id"]] = total_counts.get(row["novel_id"], 0) + 1
                if row.get("translated"):
                    translated_counts[row["novel_id"]] = translated_counts.get(row["novel_id"], 0) + 1
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
                novel["total_chapters"] = total_counts.get(novel["id"], 0)
                novel["translated_chapters"] = translated_counts.get(novel["id"], 0)
                novel["cover"] = _resolved_cover_url(novel.get("cover_url"), novel["slug"], fallback_covers)
                novel["source_name"] = _source_name(novel.get("source_url"))
            return novels
        except CdnDataError:
            pass
    return _fallback_json("metadata.json").get("novels", [])


def load_admin_novels() -> list[dict]:
    """Load editable novel fields using the service role for the admin panel."""
    try:
        return _service_rows("novels", {
            "select": "id,slug,title,author,description,original_title,source_url",
            "deleted_at": "is.null",
            "order": "title.asc",
        })
    except CdnDataError:
        # Keep the panel usable until the description migration has been applied.
        return _service_rows("novels", {
            "select": "id,slug,title,author,original_title,source_url",
            "deleted_at": "is.null",
            "order": "title.asc",
        })


def update_admin_novel(novel_id: int, *, slug: str, title: str, author: str, description: str) -> None:
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
    })


def load_synced_novel(slug: str) -> dict:
    if _configured():
        try:
            try:
                rows = _rest_rows(
                    "novels",
                    {"select": "id,slug,title,author,description,cover_url,latest_chapter,updated_at,source_url", "slug": f"eq.{slug}", "deleted_at": "is.null", "limit": "1"},
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
            "audio_available": False,
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


def delete_synced_novel_permanently(slug: str) -> None:
    _service_delete("novels", {"slug": f"eq.{slug}"})


def load_translation_progress(chapter_id: int) -> dict | None:
    queues = _service_rows("translation_queue", {"select": "status,current_chunk,total_chunks", "chapter_id": f"eq.{chapter_id}", "limit": "1"})
    if not queues:
        return None
    chunks = _service_rows("chapter_chunks", {"select": "chunk_index,translated_text,status", "chapter_id": f"eq.{chapter_id}", "order": "chunk_index.asc"})
    return {**queues[0], "chunks": chunks}


def load_cdn_json(path: str) -> dict:
    relative = _safe_path(path)
    if relative.as_posix() == "metadata.json":
        return {"novels": load_novels()}
    if _configured() and (
        (len(relative.parts) >= 4 and relative.parts[0] == "novels" and relative.parts[2] == "chapters")
        or (len(relative.parts) >= 3 and relative.parts[0] == "chapters")
    ):
        try:
            with httpx.Client(timeout=15, trust_env=False) as client:
                response = client.get(cdn_url(path), headers=_headers())
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
        except (httpx.HTTPError, ValueError):
            pass
    return _fallback_json(path)


def load_chapters_index(slug: str) -> list[dict]:
    if _configured():
        try:
            novels = _rest_rows("novels", {"select": "id", "slug": f"eq.{slug}", "limit": "1"})
            if not novels:
                raise CdnDataError("Novel was not found")
            return _all_rows(
                "chapters",
                {
                    "select": "id,chapter_number,title,source_url,translated,audio_available,content_path",
                    "novel_id": f"eq.{novels[0]['id']}",
                    "order": "chapter_number.asc",
                },
            )
        except CdnDataError:
            pass
    payload = _fallback_json(f"novels/{slug}/chapters_index.json")
    chapters = payload.get("chapters", [])
    if not isinstance(chapters, list):
        raise CdnDataError(f"Invalid chapters index: {slug}")
    return [chapter for chapter in chapters if isinstance(chapter, dict)]


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
            rows = _rest_rows("chapters", {"select": "id,chapter_number,title", **params, "order": f"chapter_number.{sort}", "limit": str(limit), "offset": str((page - 1) * limit)})
            return rows, total, page
        except CdnDataError:
            pass
    chapters = load_chapters_index(slug)
    chapters.sort(key=lambda item: int(item.get("chapter_number", 0)), reverse=sort == "desc")
    total = len(chapters)
    total_pages = max(1, (total + limit - 1) // limit)
    page = min(page, total_pages)
    offset = (page - 1) * limit
    return chapters[offset : offset + limit], total, page


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
    with httpx.Client(timeout=60, trust_env=False) as client:
        upload = client.post(
            f"{SUPABASE_URL}/storage/v1/object/chapters/{quote(json_path, safe='/')}",
            content=payload,
            headers={**headers, "x-upsert": "true", "Content-Type": "application/json"},
        )
        upload.raise_for_status()
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
