"""Supabase-backed public novel data with a local development fallback."""

import json
import os
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
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


def load_novels() -> list[dict]:
    if _configured():
        try:
            novels = _all_rows("novels", {"select": "id,slug,title,author,cover_url,latest_chapter,updated_at", "order": "updated_at.desc"})
            translated = _all_rows("chapters", {"select": "novel_id", "translated": "eq.true"})
            totals = _all_rows("chapters", {"select": "novel_id"})
            translated_counts: dict[int, int] = {}
            total_counts: dict[int, int] = {}
            for row in translated:
                translated_counts[row["novel_id"]] = translated_counts.get(row["novel_id"], 0) + 1
            for row in totals:
                total_counts[row["novel_id"]] = total_counts.get(row["novel_id"], 0) + 1
            for novel in novels:
                novel["total_chapters"] = total_counts.get(novel["id"], 0)
                novel["translated_chapters"] = translated_counts.get(novel["id"], 0)
                cover_path = novel.get("cover_url")
                novel["cover"] = storage_url("covers", cover_path.removeprefix("covers/")) if cover_path else None
            return novels
        except CdnDataError:
            pass
    return _fallback_json("metadata.json").get("novels", [])


def load_cdn_json(path: str) -> dict:
    relative = _safe_path(path)
    if relative.as_posix() == "metadata.json":
        return {"novels": load_novels()}
    if _configured() and len(relative.parts) >= 4 and relative.parts[0] == "novels" and relative.parts[2] == "chapters":
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
                    "select": "chapter_number,title,source_url,translated,audio_available,content_path",
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
