"""Synchronize local SQLite novels to Supabase PostgreSQL and Storage."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
from html import escape as escape_html
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from urllib.parse import quote, urlparse

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import PROJECT_ROOT, settings
from app.storage import cdn_object_url, configured as b2_configured, download as b2_download, upload as b2_upload
from app.translator.chunking import chunk_text
from app.translator.glossary import GlossaryEntry
from app.translator.preprocess import traditional_to_simplified
from app.translator.service import TranslationUnavailableError, glossary_for_source_url, translator
from app.crawlers.base import clean_chapter_text
from app.crawlers.bootstrap import registry
from app.crawlers.errors import SourceParseError, SourceRequestError

load_dotenv(ROOT / ".env")
def default_database_path() -> Path:
    local_app_db = ROOT / "data" / "app.db"
    if local_app_db.exists() and not os.getenv("VERCEL"):
        return local_app_db
    if settings.database_path:
        path = Path(settings.database_path)
        return path if path.is_absolute() else PROJECT_ROOT / path
    if settings.database_url.startswith("sqlite:///./"):
        return PROJECT_ROOT / settings.database_url.removeprefix("sqlite:///./")
    if os.getenv("VERCEL"):
        return settings.data_path / "database.db"
    return ROOT / "data" / "app.db"


DATABASE_PATH = Path(os.getenv("SYNC_DATABASE_PATH", default_database_path()))
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_SECRET_KEY", "")
RETRIES = 3
CRAWL_CONCURRENCY = max(1, min(6, int(os.getenv("CRAWL_CONCURRENCY", "4"))))
logger = logging.getLogger(__name__)
app = FastAPI(title="LeoTruyen Sync")
PC_WORKER_TOKEN = os.getenv("PC_WORKER_TOKEN", "")


class SyncError(RuntimeError):
    pass


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-") or "novel"


def unique_slug(value: str, existing_slugs: set[str]) -> str:
    base = slugify(value)
    candidate = base
    suffix = 1
    while candidate in existing_slugs:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def chapter_key(number: int) -> str:
    return f"{number:03d}"


class SupabaseSync:
    def __init__(self) -> None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise SyncError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required for sync.")
        self.headers = {"apikey": SUPABASE_SERVICE_KEY}
        if SUPABASE_SERVICE_KEY.startswith("eyJ"):
            self.headers["Authorization"] = f"Bearer {SUPABASE_SERVICE_KEY}"

    def request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        error: Exception | None = None
        detail = ""
        request_headers = {**self.headers, **dict(kwargs.pop("headers", {}))}
        for attempt in range(1, RETRIES + 1):
            try:
                with httpx.Client(timeout=60, trust_env=False) as client:
                    response = client.request(method, f"{SUPABASE_URL}{path}", headers=request_headers, **kwargs)
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                error = exc
                if isinstance(exc, httpx.HTTPStatusError):
                    detail = f" ({exc.response.status_code}: {exc.response.text[:300]})"
                if attempt < RETRIES:
                    time.sleep(attempt)
        raise SyncError(f"Supabase request failed: {path}{detail}") from error

    def upsert(self, table: str, payload: dict, conflict: str) -> dict:
        response = self.request(
            "POST",
            f"/rest/v1/{table}?on_conflict={quote(conflict)}",
            json=payload,
            headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=representation"},
        )
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            raise SyncError(f"Supabase did not return {table} upsert result.")
        return rows[0]

    def upsert_many(self, table: str, payloads: list[dict], conflict: str) -> None:
        for start in range(0, len(payloads), 500):
            self.request(
                "POST",
                f"/rest/v1/{table}?on_conflict={quote(conflict)}",
                json=payloads[start : start + 500],
                headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            )

    def rows(self, table: str, params: dict[str, str]) -> list[dict]:
        payload = self.request("GET", f"/rest/v1/{table}", params=params).json()
        if not isinstance(payload, list):
            raise SyncError(f"Supabase did not return rows for {table}.")
        return [row for row in payload if isinstance(row, dict)]

    def rpc_rows(self, function: str, payload: dict) -> list[dict]:
        """Run an internal database function and require its table result."""
        result = self.request("POST", f"/rest/v1/rpc/{quote(function)}", json=payload).json()
        if not isinstance(result, list):
            raise SyncError(f"Supabase did not return rows for RPC {function}.")
        return [row for row in result if isinstance(row, dict)]

    def all_rows(self, table: str, params: dict[str, str]) -> list[dict]:
        """Read every matching row instead of PostgREST's first 1,000 rows."""
        page_size = 1000
        offset = 0
        collected: list[dict] = []
        while True:
            page = self.rows(table, {**params, "limit": str(page_size), "offset": str(offset)})
            collected.extend(page)
            if len(page) < page_size:
                return collected
            offset += page_size

    def insert_many_ignore(self, table: str, payloads: list[dict], conflict: str) -> None:
        for start in range(0, len(payloads), 500):
            self.request(
                "POST",
                f"/rest/v1/{table}?on_conflict={quote(conflict)}",
                json=payloads[start : start + 500],
                headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=minimal"},
            )

    def patch_rows(self, table: str, params: dict[str, str], payload: dict) -> list[dict]:
        response = self.request(
            "PATCH",
            f"/rest/v1/{table}",
            params=params,
            json=payload,
            headers={**self.headers, "Prefer": "return=representation"},
        )
        rows = response.json()
        return rows if isinstance(rows, list) else []

    def delete_rows(self, table: str, params: dict[str, str]) -> None:
        self.request("DELETE", f"/rest/v1/{table}", params=params, headers={**self.headers, "Prefer": "return=minimal"})

    def insert(self, table: str, payload: dict) -> dict:
        response = self.request(
            "POST",
            f"/rest/v1/{table}",
            json=payload,
            headers={**self.headers, "Prefer": "return=representation"},
        )
        rows = response.json()
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise SyncError(f"Supabase did not return {table} insert result.")
        return rows[0]

    def ensure_buckets(self) -> None:
        if b2_configured():
            return
        buckets = self.request("GET", "/storage/v1/bucket").json()
        existing = {bucket.get("id") for bucket in buckets if isinstance(bucket, dict)}
        for bucket in ("covers", "chapters", "audio"):
            if bucket not in existing:
                self.request("POST", "/storage/v1/bucket", json={"id": bucket, "name": bucket, "public": True})

    def upload_if_changed(self, bucket: str, path: str, content: bytes, content_type: str, *, check_existing: bool = True) -> bool:
        if b2_configured() and bucket != "covers":
            return b2_upload(f"{bucket}/{path}", content, content_type, check_existing=check_existing)
        encoded = quote(path, safe="/")
        public_path = f"/storage/v1/object/public/{bucket}/{encoded}"
        try:
            with httpx.Client(timeout=60, trust_env=False) as client:
                existing = client.get(f"{SUPABASE_URL}{public_path}", headers=self.headers)
            if existing.status_code == 200 and existing.content == content:
                return False
        except httpx.HTTPError:
            pass
        self.request(
            "POST",
            f"/storage/v1/object/{bucket}/{encoded}",
            content=content,
            headers={**self.headers, "x-upsert": "true", "Content-Type": content_type},
        )
        return True

    def download_json(self, bucket: str, path: str) -> dict:
        if b2_configured():
            object_key = f"{bucket}/{path}"
            cdn_token = os.getenv("CDN_READ_TOKEN", "")
            cdn_url = cdn_object_url(object_key) if bucket == "chapters" and cdn_token else None
            try:
                if cdn_url:
                    with httpx.Client(timeout=30, trust_env=False) as client:
                        response = client.get(cdn_url, headers={"X-CDN-Read-Token": cdn_token})
                        response.raise_for_status()
                        payload = response.json()
                else:
                    payload = json.loads(b2_download(object_key).decode("utf-8"))
            except httpx.HTTPError as exc:
                raise SyncError(f"Could not download CDN object {object_key}: {exc}") from exc
            except (BotoCoreError, ClientError) as exc:
                raise SyncError(f"Could not download B2 object {object_key}: {exc}") from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SyncError(f"Stored object {object_key} is not valid UTF-8 JSON.") from exc
            if not isinstance(payload, dict):
                raise SyncError(f"Stored {bucket}/{path} is not a JSON object.")
            return payload
        encoded = quote(path, safe="/")
        response = self.request("GET", f"/storage/v1/object/public/{bucket}/{encoded}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise SyncError(f"Stored {bucket}/{path} is not a JSON object.")
        return payload


_COVER_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


def download_cover(url: str | None) -> tuple[bytes, str, str] | None:
    if not url:
        return None
    try:
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        extension = _COVER_TYPES.get(content_type)
        if not extension or not response.content or len(response.content) > 20 * 1024 * 1024:
            logger.warning("Skipping unsupported or oversized cover image: %s", url)
            return None
        return response.content, content_type, extension
    except (httpx.HTTPError, ValueError):
        logger.warning("Could not download cover: %s", url)
        return None


def cover_bytes(url: str | None) -> bytes | None:
    """Compatibility helper returning downloaded cover bytes only."""
    result = download_cover(url)
    return result[0] if result else None


def store_novel_cover(remote: SupabaseSync, slug: str, url: str | None) -> str | None:
    downloaded = download_cover(url)
    if not downloaded:
        return None
    safe_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug).strip("-_") or "novel"
    content, content_type, extension = downloaded
    path = f"{safe_slug}/cover{extension}"
    try:
        remote.upload_if_changed("covers", path, content, content_type)
    except Exception:
        logger.exception("Could not store downloaded cover for slug=%s", safe_slug)
        return None
    return f"covers/{path}"


def source_cover_url(source_url: str | None) -> str | None:
    if not source_url:
        return None
    try:
        crawler = registry.get_for_url(source_url)
        info = asyncio.run(crawler.get_book_info(source_url))
        return info.cover_url
    except Exception:
        logger.warning("Could not recover cover URL from source: %s", source_url, exc_info=True)
        return None


def migrate_remote_cover_urls(remote: SupabaseSync | None = None) -> dict[str, int]:
    """Restore direct source image URLs for covers previously replaced by object paths."""
    recovered_urls = {
        "da-vo-cuong": "https://6ed7f732781d5145ac367958b98f24ef.cdn.bubble.io/cdn-cgi/image/w%3D256%2Ch%3D341%2Cf%3Dauto%2Cdpr%3D2.5%2Cfit%3Dcover%2Cq%3D75/f1729177579401x492835757942738050/600%20%2820%29.webp",
        "xich-tam-tuan-thien": "https://so1.360tres.com/t01d83620acf52d09d1.jpg",
        "kiem-lai": "https://cdn.novelupdates.com/images/2019/04/1496234539057.jpg",
        "chu-nhan-pho-la": "https://bookcover.yuewen.com/qdbimg/349573/1038229163/600",
        "kiem-chuc-dai-hoang": "https://imgpai.thepaper.cn/newpai/image/1780297679162_3rKWAp_1780303822446.png?x-oss-process=image%2Fresize%2Cw_1024",
        "huyen-giam-tien-toc-ban-cu": "https://q8.itc.cn/q_70/images03/20240511/0939405ed5c04e0fb8a0fcc8f5a9b495.jpeg",
    }
    remote = remote or SupabaseSync()
    counts = {"scanned": 0, "restored": 0, "skipped": 0, "unavailable": 0}
    novels = remote.all_rows("novels", {"select": "id,slug,cover_url,source_url", "order": "id.asc"})
    for novel in novels:
        counts["scanned"] += 1
        cover_url = str(novel.get("cover_url") or "").strip()
        is_placeholder = any(marker in cover_url.lower() for marker in ("noimg", "nocover"))
        if urlparse(cover_url).scheme in {"http", "https"} and not is_placeholder:
            counts["skipped"] += 1
            continue
        source_url = source_cover_url(str(novel.get("source_url") or "")) if not is_placeholder else None
        source_url = source_url or recovered_urls.get(str(novel.get("slug") or ""))
        if not source_url:
            counts["unavailable"] += 1
            continue
        try:
            remote.patch_rows("novels", {"id": f"eq.{novel['id']}"}, {"cover_url": source_url})
            counts["restored"] += 1
        except Exception:
            counts["unavailable"] += 1
            logger.exception("Could not restore source cover URL for novel_id=%s", novel.get("id"))
    return counts


def enqueue_untranslated_chapters(remote: SupabaseSync, chapters: list[dict], crawl_job_id: str | None = None, priority: int = 0) -> int:
    candidates = [row for row in chapters if row.get("id") and row.get("original_content") and not row.get("translated_content")]
    if not candidates:
        return 0
    ids = ",".join(str(row["id"]) for row in candidates)
    existing = {row.get("chapter_id") for row in remote.rows("translation_queue", {"select": "chapter_id", "chapter_id": f"in.({ids})"})}
    chunks: list[dict] = []
    jobs: list[dict] = []
    for chapter in candidates:
        if chapter["id"] in existing:
            continue
        parts = chunk_text(str(chapter["original_content"]))
        if not parts:
            continue
        chunks.extend({"chapter_id": chapter["id"], "chunk_index": index, "original_text": part, "status": "queued"} for index, part in enumerate(parts))
        jobs.append({"chapter_id": chapter["id"], "status": "queued", "priority": max(0, min(priority, 100)), "current_chunk": 0, "total_chunks": len(parts), "retry_count": 0, "crawl_job_id": crawl_job_id})
    if not jobs:
        return 0
    remote.insert_many_ignore("chapter_chunks", chunks, "chapter_id,chunk_index")
    remote.insert_many_ignore("translation_queue", jobs, "chapter_id")
    return len(jobs)


def restore_stored_chapters(remote: SupabaseSync, chapters: list[dict], crawl_job_id: str | None = None) -> set[int]:
    """Recreate translation work after a crawl was interrupted post-upload."""
    restored: list[dict] = []
    for chapter in chapters:
        content_path = str(chapter.get("content_path") or "")
        if not content_path.startswith("chapters/"):
            continue
        try:
            payload = remote.download_json("chapters", content_path.removeprefix("chapters/"))
        except (SyncError, ValueError):
            logger.warning("Could not restore stored chapter %s.", chapter["chapter_number"])
            continue
        original = str(payload.get("original_content") or "").strip()
        if original:
            restored.append({"id": chapter["id"], "original_content": original, "translated_content": None})
    enqueue_untranslated_chapters(remote, restored, crawl_job_id)
    return {int(chapter["id"]) for chapter in restored}


def claim_translation_job(remote: SupabaseSync) -> dict | None:
    rows = remote.rows("translation_queue", {"select": "*", "status": "eq.queued", "order": "priority.desc,created_at.asc", "limit": "1"})
    if not rows:
        return None
    job = rows[0]
    claimed = remote.patch_rows("translation_queue", {"id": f"eq.{job['id']}", "status": "eq.queued"}, {"status": "translating", "updated_at": datetime.now(timezone.utc).isoformat()})
    return claimed[0] if claimed else None


def reclaim_stale_translation_jobs(remote: SupabaseSync) -> int:
    """Return abandoned translating jobs to the queue after a worker interruption."""
    stale_after = max(600, int(os.getenv("TRANSLATION_JOB_STALE_SECONDS", "900")))
    stale_before = (datetime.now(timezone.utc) - timedelta(seconds=stale_after)).isoformat()
    reclaimed = remote.patch_rows(
        "translation_queue",
        {"status": "eq.translating", "updated_at": f"lt.{stale_before}"},
        {"status": "queued", "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    if reclaimed:
        logger.warning("Đã nhận lại %s job dịch bị kẹt quá %s giây.", len(reclaimed), stale_after)
    return len(reclaimed)


def complete_crawl_job_when_translation_finishes(remote: SupabaseSync, crawl_job_id: str) -> bool:
    """Close a crawl job once its independent translation queue is empty."""
    active = remote.rows(
        "translation_queue",
        {"select": "id", "crawl_job_id": f"eq.{crawl_job_id}", "status": "in.(queued,translating,paused,failed)", "limit": "1"},
    )
    if active:
        return False
    completed = remote.patch_rows(
        "remote_crawl_jobs",
        {"id": f"eq.{crawl_job_id}", "status": "eq.translating"},
        {"status": "completed", "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return bool(completed)


def prioritize_translation(remote: SupabaseSync, chapter_id: int, priority: int) -> bool:
    """Raise a queued chapter without interrupting the chunk already translating."""
    rows = remote.patch_rows(
        "translation_queue",
        {"chapter_id": f"eq.{chapter_id}", "status": "eq.queued"},
        {"priority": max(0, min(priority, 100)), "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return bool(rows)


def enqueue_remote_crawl(source_url: str, source_type: str = "zh", result: dict | None = None) -> dict:
    """Persist a production crawl request for the outbound-only PC worker."""
    remote = SupabaseSync()
    active = remote.rows(
        "remote_crawl_jobs",
        {
            "select": "*",
            "source_url": f"eq.{source_url}",
            "status": "in.(queued,crawling,syncing,translating)",
            "order": "created_at.asc",
            "limit": "1",
        },
    )
    if active:
        return active[0]
    try:
        return remote.insert("remote_crawl_jobs", {"source_url": source_url, "source_type": source_type, "status": "queued", "result": result or {}})
    except SyncError:
        active = remote.rows(
            "remote_crawl_jobs",
            {"select": "*", "source_url": f"eq.{source_url}", "status": "in.(queued,crawling,syncing,translating)", "limit": "1"},
        )
        if active:
            return active[0]
        raise


def claim_remote_crawl_job(remote: SupabaseSync) -> dict | None:
    rows = remote.rows(
        "remote_crawl_jobs",
        {"select": "*", "status": "eq.queued", "order": "created_at.asc", "limit": "1"},
    )
    claim_filter = {"status": "eq.queued"}
    if not rows:
        # A PC restart cannot resume a row left in `crawling` by the previous
        # process. Reclaim it only after its progress heartbeat has gone stale.
        stale_after = max(90, int(os.getenv("CRAWL_JOB_STALE_SECONDS", "180")))
        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=stale_after)).isoformat()
        rows = remote.rows(
            "remote_crawl_jobs",
            {
                "select": "*",
                "status": "eq.crawling",
                "updated_at": f"lt.{stale_before}",
                "order": "updated_at.asc",
                "limit": "1",
            },
        )
        if not rows:
            return None
        claim_filter = {"status": "eq.crawling", "updated_at": f"lt.{stale_before}"}
        logger.warning("Nhận lại job crawl bị kẹt quá %s giây: %s", stale_after, rows[0]["id"])
    job = rows[0]
    claimed = remote.patch_rows(
        "remote_crawl_jobs",
        {"id": f"eq.{job['id']}", **claim_filter},
        {"status": "crawling", "error": None, "started_at": datetime.now(timezone.utc).isoformat(), "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return claimed[0] if claimed else None


def remote_job_is_running(remote: SupabaseSync, job_id: str) -> bool:
    # Pausing a crawl must not interrupt translation work already in its queue.
    return bool(remote.rows("remote_crawl_jobs", {"select": "id", "id": f"eq.{job_id}", "status": "in.(crawling,syncing,translating,completed,paused)", "limit": "1"}))


def remote_crawl_is_running(remote: SupabaseSync, job_id: str) -> bool:
    return bool(remote.rows("remote_crawl_jobs", {"select": "id", "id": f"eq.{job_id}", "status": "in.(crawling,syncing)", "limit": "1"}))


def remote_crawl_is_paused(remote: SupabaseSync, job_id: str) -> bool:
    return bool(remote.rows("remote_crawl_jobs", {"select": "id", "id": f"eq.{job_id}", "status": "eq.paused", "limit": "1"}))


AUTO_GLOSSARY_MIN_CHAPTERS = 2
AUTO_GLOSSARY_MAX_CANDIDATES = 12
_AUTO_ALIAS_PATTERN = re.compile(r"(?:\u4eba\u79f0|\u53f7\u79f0|\u5916\u53f7|\u7ef0\u53f7|\u81ea\u53f7|\u540d\u4e3a)[\"\u201c\u300c\u300e]([\u3400-\u9fff]{2,8})[\"\u201d\u300d\u300f]")
_AUTO_SECT_PATTERN = re.compile(r"(?=([\u3400-\u9fff]{2,8}(?:\u5b97\u95e8|\u5c71\u5e84|\u4ed9\u5e9c|\u9053\u5bab|\u5251\u9601|\u4e66\u9662|\u5b97|\u95e8|\u6d3e|\u5e9c|\u5bab|\u9601|\u697c|\u5bfa|\u89c2|\u5802|\u9662)))")
_AUTO_TECHNIQUE_PATTERN = re.compile(r"(?:\u4fee\u70bc|\u65bd\u5c55|\u8fd0\u8f6c|\u4fee\u884c|\u4e60\u5f97|\u638c\u63e1|\u53c2\u609f)([\u3400-\u9fff]{2,10}(?:\u529f\u6cd5|\u5fc3\u6cd5|\u5251\u6cd5|\u5200\u6cd5|\u62f3\u6cd5|\u638c\u6cd5|\u8eab\u6cd5|\u8bc0|\u7ecf|\u529f|\u672f|\u5178|\u5f55|\u7bc7))")
_AUTO_SECT_VALUE_RE = re.compile(r"^[\u3400-\u9fff]{2,8}(?:\u5b97\u95e8|\u5c71\u5e84|\u4ed9\u5e9c|\u9053\u5bab|\u5251\u9601|\u4e66\u9662|\u5b97|\u95e8|\u6d3e|\u5e9c|\u5bab|\u9601|\u697c|\u5bfa|\u89c2|\u5802|\u9662)$")
_AUTO_TECHNIQUE_VALUE_RE = re.compile(r"^[\u3400-\u9fff]{2,10}(?:\u529f\u6cd5|\u5fc3\u6cd5|\u5251\u6cd5|\u5200\u6cd5|\u62f3\u6cd5|\u638c\u6cd5|\u8eab\u6cd5|\u8bc0|\u7ecf|\u529f|\u672f|\u5178|\u5f55|\u7bc7)$")
_AUTO_ALIAS_VALUE_RE = re.compile(r"^[\u3400-\u9fff]{2,8}(?:\u5ba2|\u541b|\u4ed9|\u9b54|\u738b|\u5723|\u5c0a|\u5e1d|\u5b50|\u5a18|\u7237)$")
_AUTO_NAME_PATTERN = re.compile(r"(?=([\u3400-\u9fff]{2,4}?)(?:先生|道友|师兄|师姐|前辈|真人|姑娘|公子|大人|老祖|掌门))")
_COMMON_NAME_FALSE_POSITIVES = {"王朝", "王者", "李代桃僵", "张口", "张望", "赵国"}
_NON_NAME_PREFIXES = frozenset("和与及但而或若在从向被把给将对于由为让使令跟同至")
_COMMON_SURNAMES = frozenset("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣邓郁单杭洪包诸左石崔吉龚程嵇邢滑裴陆荣翁荀羊於惠甄曲封储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲台从鄂索咸籍赖卓蔺屠蒙池乔阴郁胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边扈燕冀浦尚温庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公")


_AUTO_GLOSSARY_REJECT_PARTS = ("\u90a3\u4f4d", "\u90a3\u4e24\u4f4d", "\u90a3\u540d", "\u8fd9\u4f4d", "\u6b64\u4eba", "\u67d0\u4f4d", "\u4e00\u4f4d", "\u7684", "\u4eec", "\u5bb6\u4e8c", "\u9ebb\u70e6", "\u51fa\u8eab", "\u6765\u81ea", "\u8eab")


def _is_safe_auto_glossary_term(candidate: str, *, person: bool = False) -> bool:
    if not 2 <= len(candidate) <= 12 or any(not "\u3400" <= char <= "\u9fff" for char in candidate):
        return False
    if any(part in candidate for part in _AUTO_GLOSSARY_REJECT_PARTS):
        return False
    return not person or candidate[0] in _COMMON_SURNAMES


def is_allowed_auto_glossary_entry(candidate: str) -> bool:
    """Validate stored auto entries without trusting old extraction rules."""
    candidate = traditional_to_simplified(candidate)
    return (
        (_is_safe_auto_glossary_term(candidate, person=True) and len(candidate) <= 4)
        or (_is_safe_auto_glossary_term(candidate) and bool(_AUTO_ALIAS_VALUE_RE.fullmatch(candidate)))
        or (_is_safe_auto_glossary_term(candidate) and bool(_AUTO_SECT_VALUE_RE.fullmatch(candidate)))
        or (_is_safe_auto_glossary_term(candidate) and bool(_AUTO_TECHNIQUE_VALUE_RE.fullmatch(candidate)))
    )


def _auto_name_candidates(source: str) -> set[str]:
    """Learn only named people, personal aliases, sects, and named techniques."""
    source = traditional_to_simplified(source)
    candidates: set[str] = set()
    for match in _AUTO_NAME_PATTERN.finditer(source):
        candidate = traditional_to_simplified(match.group(1))
        if candidate[0] not in _NON_NAME_PREFIXES and candidate not in _COMMON_NAME_FALSE_POSITIVES and _is_safe_auto_glossary_term(candidate, person=True):
            candidates.add(candidate)
    candidates.update(candidate for candidate in _AUTO_ALIAS_PATTERN.findall(source) if _is_safe_auto_glossary_term(candidate))
    candidates.update(candidate for candidate in _AUTO_SECT_PATTERN.findall(source) if _is_safe_auto_glossary_term(candidate))
    candidates.update(candidate for candidate in _AUTO_TECHNIQUE_PATTERN.findall(source) if _is_safe_auto_glossary_term(candidate))
    # A suffix of a longer matched name (e.g. 文铭 in 杜文铭) is not a separate entity.
    full_names = {candidate for candidate in candidates if not any(other != candidate and other.endswith(candidate) for other in candidates)}
    return set(sorted(full_names, key=lambda value: (-len(value), value))[:AUTO_GLOSSARY_MAX_CANDIDATES])


def _auto_glossary_entries(remote: SupabaseSync, novel_id: int) -> list[GlossaryEntry]:
    """Load only already-confirmed, per-novel entries; failures never block MT."""
    try:
        rows = remote.rows(
            "novel_auto_glossary",
            {"select": "source_text,replacement_text", "novel_id": f"eq.{novel_id}", "status": "eq.auto_confirmed"},
        )
    except SyncError:
        logger.warning("Auto glossary unavailable for novel %s; continuing without it.", novel_id)
        return []
    return [GlossaryEntry(str(row["source_text"]), str(row["replacement_text"])) for row in rows if row.get("source_text") and row.get("replacement_text")]


def _learn_auto_glossary(remote: SupabaseSync, novel_id: int, original_content: str) -> None:
    """Observe names across chapters, but only protect them after two observations."""
    candidates = _auto_name_candidates(original_content)
    if not candidates:
        return
    try:
        existing = {
            str(row["source_text"]): row
            for row in remote.rows(
                "novel_auto_glossary",
                {"select": "source_text,replacement_text,appearances,status", "novel_id": f"eq.{novel_id}"},
            )
        }
        now = datetime.now(timezone.utc).isoformat()
        updates: list[dict] = []
        for source in candidates:
            row = existing.get(source, {})
            if row.get("status") == "auto_confirmed":
                continue
            appearances = int(row.get("appearances") or 0) + 1
            target = row.get("replacement_text")
            status = "candidate"
            if appearances >= AUTO_GLOSSARY_MIN_CHAPTERS and not target:
                target = translator.translate(source)
                if not target or any("\u3400" <= char <= "\u9fff" for char in target) or len(target) > 80:
                    target = None
                else:
                    status = "auto_confirmed"
            updates.append({"novel_id": novel_id, "source_text": source, "replacement_text": target, "appearances": appearances, "status": status, "updated_at": now})
        if updates:
            remote.upsert_many("novel_auto_glossary", updates, "novel_id,source_text")
    except (SyncError, TranslationUnavailableError, ValueError):
        logger.exception("Auto glossary observation failed for novel %s; translation output was kept unchanged.", novel_id)


def publish_completed_translation(remote: SupabaseSync, chapter_id: int) -> None:
    """Publish a fully translated queue item and expose it to the reader."""
    logger.info("Đang xuất bản bản dịch của chương ID %s...", chapter_id)
    chapters = remote.rows(
        "chapters",
        {
            "select": "id,novel_id,chapter_number,title,source_url",
            "id": f"eq.{chapter_id}",
            "limit": "1",
        },
    )
    if not chapters:
        raise SyncError(f"Chapter {chapter_id} was not found while publishing translation.")
    chapter = chapters[0]
    novels = remote.rows(
        "novels",
        {"select": "id,slug,title,latest_chapter", "id": f"eq.{chapter['novel_id']}", "limit": "1"},
    )
    if not novels:
        raise SyncError(f"Novel for chapter {chapter_id} was not found while publishing translation.")
    novel = novels[0]
    chunks = remote.rows(
        "chapter_chunks",
        {
            "select": "chunk_index,original_text,translated_text,status",
            "chapter_id": f"eq.{chapter_id}",
            "order": "chunk_index.asc",
        },
    )
    if not chunks or any(row.get("status") != "completed" or not row.get("translated_text") for row in chunks):
        raise SyncError(f"Chapter {chapter_id} has incomplete translated chunks.")
    original_content = "\n\n".join(str(row["original_text"]) for row in chunks)
    translated_content = "\n\n".join(str(row["translated_text"]) for row in chunks)
    original_title = str(chapter["title"])
    translated_title = translator.translate(original_title)
    key = chapter_key(int(chapter["chapter_number"]))
    json_path = f"{novel['slug']}/{key}.json"
    payload = json.dumps(
        {
            "number": chapter["chapter_number"],
            "title": translated_title,
            "original_title": original_title,
            "original_content": original_content,
            "translated_content": translated_content,
            "content": translated_content,
            "translated": True,
            "audio": None,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    remote.upload_if_changed("chapters", json_path, payload, "application/json", check_existing=False)
    remote.patch_rows(
        "chapters",
        {"id": f"eq.{chapter_id}"},
        {
            "title": translated_title,
            "translated": True,
            "content_path": f"chapters/{json_path}",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    try:
        discoveries = remote.rows(
            "chapter_discoveries",
            {
                "select": "id,novel_id,chapter_number,chapter_title,source_url,notification_after_translation",
                "novel_id": f"eq.{chapter['novel_id']}",
                "source_identity": f"eq.url:{str(chapter['source_url']).rstrip('/')}",
                "notification_after_translation": "eq.true",
                "limit": "1",
            },
        )
        if discoveries:
            from app.notification_service import notify_chapter_discovery

            notify_chapter_discovery(
                remote,
                {**discoveries[0], "chapter_title": translated_title},
                {**novel, "title": novel.get("title") or "Truyện"},
                update_existing=True,
            )
            remote.patch_rows(
                "chapter_discoveries",
                {"id": f"eq.{discoveries[0]['id']}"},
                {"notification_after_translation": False},
            )
    except Exception:
        logger.exception("Could not notify followers after translating chapter %s", chapter_id)
    if int(novel.get("latest_chapter") or 0) < int(chapter["chapter_number"]):
        remote.patch_rows(
            "novels",
            {"id": f"eq.{novel['id']}"},
            {"latest_chapter": chapter["chapter_number"], "updated_at": datetime.now(timezone.utc).isoformat()},
        )
    _learn_auto_glossary(remote, int(novel["id"]), original_content)
    # The published chapter JSON is the durable raw/translated source. Chunks
    # are temporary worker state and can be recreated from that JSON later.
    remote.delete_rows("chapter_chunks", {"chapter_id": f"eq.{chapter_id}"})
    remote.delete_rows("translation_queue", {"chapter_id": f"eq.{chapter_id}"})
    logger.info("Đã xuất bản chương %s: %s", chapter["chapter_number"], chapter["title"])


def run_translation_worker_once(remote: SupabaseSync | None = None) -> bool:
    remote = remote or SupabaseSync()
    reclaim_stale_translation_jobs(remote)
    job = claim_translation_job(remote)

    if not job:
        return False

    BATCH_SIZE = 4

    try:
        chapter_rows = remote.rows(
            "chapters",
            {"select": "id,source_url,novel_id,content_path,title", "id": f"eq.{job['chapter_id']}", "limit": "1"},
        )
        source_url = str(chapter_rows[0].get("source_url") or "") if chapter_rows else ""
        novel_id = int(chapter_rows[0]["novel_id"]) if chapter_rows and chapter_rows[0].get("novel_id") else 0
        glossary = [*glossary_for_source_url(source_url), *_auto_glossary_entries(remote, novel_id)] if novel_id else glossary_for_source_url(source_url)
        total_chunks = int(job["total_chunks"])
        if total_chunks == 0:
            if not chapter_rows:
                raise SyncError(f"Chapter {job['chapter_id']} was not found while rebuilding chunks.")
            content_path = str(chapter_rows[0].get("content_path") or "")
            if not content_path.startswith("chapters/"):
                raise SyncError(f"Chapter {job['chapter_id']} has no stored raw content.")
            stored = remote.download_json("chapters", content_path.removeprefix("chapters/"))
            original_content = clean_chapter_text(str(stored.get("original_content") or ""))
            original_title = str(stored.get("original_title") or "").strip()
            if original_title and original_title != str(chapter_rows[0].get("title") or "").strip():
                remote.patch_rows(
                    "chapters",
                    {"id": f"eq.{job['chapter_id']}"},
                    {"title": original_title, "updated_at": datetime.now(timezone.utc).isoformat()},
                )
            parts = chunk_text(original_content)
            if not parts:
                raise SyncError(f"Chapter {job['chapter_id']} has empty stored raw content.")
            remote.insert_many_ignore(
                "chapter_chunks",
                [{"chapter_id": job["chapter_id"], "chunk_index": index, "original_text": part, "status": "queued"} for index, part in enumerate(parts)],
                "chapter_id,chunk_index",
            )
            total_chunks = len(parts)
            remote.patch_rows(
                "translation_queue",
                {"id": f"eq.{job['id']}", "status": "eq.translating"},
                {"current_chunk": 0, "total_chunks": total_chunks, "updated_at": datetime.now(timezone.utc).isoformat()},
            )
            job["total_chunks"] = total_chunks
        completed_chunks = remote.rows(
            "chapter_chunks",
            {"select": "chunk_index", "chapter_id": f"eq.{job['chapter_id']}", "status": "eq.completed"},
        )
        current_chunk = sum(row.get("status") == "completed" for row in completed_chunks)
        if current_chunk != int(job.get("current_chunk") or 0):
            remote.patch_rows(
                "translation_queue", {"id": f"eq.{job['id']}", "status": "eq.translating"},
                {"current_chunk": current_chunk, "updated_at": datetime.now(timezone.utc).isoformat()},
            )
            job["current_chunk"] = current_chunk
        logger.info("Bắt đầu dịch chương ID %s (%s đoạn).", job["chapter_id"], total_chunks)

        while int(job["current_chunk"]) < total_chunks:
            crawl_job_id = job.get("crawl_job_id")
            if crawl_job_id and not remote_job_is_running(remote, str(crawl_job_id)):
                remote.patch_rows("translation_queue", {"id": f"eq.{job['id']}", "status": "eq.translating"}, {"status": "paused", "updated_at": datetime.now(timezone.utc).isoformat()})
                return False
            current = int(job["current_chunk"])

            # Lấy tối đa 4 chunk liên tiếp
            rows = remote.rows(
                "chapter_chunks",
                {
                    "select": "id,original_text,chunk_index",
                    "chapter_id": f"eq.{job['chapter_id']}",
                    "status": "eq.queued",
                    "order": "chunk_index.asc",
                    "limit": str(BATCH_SIZE),
                },
            )

            if not rows:
                raise SyncError(
                    f"Missing queued chunk for chapter {job['chapter_id']}"
                )

            logger.info(
                "Đang dịch chương ID %s: đoạn %s-%s/%s (%s đoạn trong batch).",
                job["chapter_id"],
                current + 1,
                current + len(rows),
                total_chunks,
                len(rows),
            )

            # Gom nhiều chunk gửi GPU một lần
            texts = [str(r["original_text"]) for r in rows]
            translated_texts = translator.translate_batch(texts, glossary)

            # Lưu kết quả
            for row, translated in zip(rows, translated_texts):
                remote.patch_rows(
                    "chapter_chunks",
                    {"id": f"eq.{row['id']}"},
                    {
                        "translated_text": translated,
                        "status": "completed",
                    },
                )

            next_index = current + len(rows)
            status = "completed" if next_index >= total_chunks else "translating"

            remote.patch_rows(
                "translation_queue",
                {"id": f"eq.{job['id']}"},
                {
                    "current_chunk": next_index,
                    "status": status,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            logger.info(
                "Đã dịch chương ID %s: hoàn tất %s/%s đoạn.",
                job["chapter_id"],
                next_index,
                total_chunks,
            )

            job["current_chunk"] = next_index

        publish_completed_translation(remote, int(job["chapter_id"]))
        if job.get("crawl_job_id"):
            complete_crawl_job_when_translation_finishes(remote, str(job["crawl_job_id"]))
        logger.info("Hoàn tất toàn bộ chương ID %s.", job["chapter_id"])
        return True

    except (TranslationUnavailableError, SyncError):
        logger.exception(
            "Dịch thất bại cho chương ID %s.",
            job["chapter_id"],
        )

        remote.patch_rows(
            "translation_queue",
            {"id": f"eq.{job['id']}"},
            {
                "status": "failed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        return False


def reclaim_stale_ebook_translation_jobs(remote: SupabaseSync) -> int:
    stale_after = max(600, int(os.getenv("TRANSLATION_JOB_STALE_SECONDS", "900")))
    stale_before = (datetime.now(timezone.utc) - timedelta(seconds=stale_after)).isoformat()
    rows = remote.patch_rows(
        "ebook_translation_queue",
        {"status": "eq.translating", "updated_at": f"lt.{stale_before}"},
        {"status": "queued", "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return len(rows)


def run_ebook_ingestion_worker_once(remote: SupabaseSync | None = None) -> bool:
    """Consume one direct-to-B2 upload and parse it outside the Vercel request path."""
    remote = remote or SupabaseSync()
    stale_after = max(600, int(os.getenv("TRANSLATION_JOB_STALE_SECONDS", "900")))
    stale_before = (datetime.now(timezone.utc) - timedelta(seconds=stale_after)).isoformat()
    remote.patch_rows(
        "ebook_ingestion_queue", {"status": "eq.processing", "updated_at": f"lt.{stale_before}"},
        {"status": "queued", "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    queued = remote.rows("ebook_ingestion_queue", {
        "select": "*", "status": "eq.queued", "order": "created_at.asc", "limit": "1",
    })
    if not queued:
        return False
    job = queued[0]
    claimed = remote.patch_rows("ebook_ingestion_queue", {
        "id": f"eq.{job['id']}", "status": "eq.queued",
    }, {"status": "processing", "updated_at": datetime.now(timezone.utc).isoformat(), "error": None})
    if not claimed:
        return False
    job = claimed[0]
    try:
        from app.ebook_service import process_ebook_ingestion_job

        original = b2_download(str(job["original_file_path"]))
        translated_count = process_ebook_ingestion_job(remote, job, original)
        books = remote.rows("ebook_books", {
            "select": "source_language", "id": f"eq.{job['id']}", "limit": "1",
        })
        remote.patch_rows("ebook_ingestion_queue", {"id": f"eq.{job['id']}", "status": "eq.processing"}, {
            "status": "completed", "source_language": books[0]["source_language"] if books else "unknown",
            "translation_queued": translated_count, "error": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        logger.exception("Ebook ingestion failed for job %s.", job.get("id"))
        remote.patch_rows("ebook_ingestion_queue", {"id": f"eq.{job['id']}", "status": "eq.processing"}, {
            "status": "failed", "error": str(exc)[:1000], "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    return True


def run_ebook_translation_worker_once(remote: SupabaseSync | None = None) -> bool:
    """Translate one owner-scoped ebook chapter using the existing MT service."""
    remote = remote or SupabaseSync()
    reclaim_stale_ebook_translation_jobs(remote)
    queued = remote.rows("ebook_translation_queue", {
        "select": "*", "status": "eq.queued", "order": "created_at.asc", "limit": "1",
    })
    if not queued:
        return False
    job = queued[0]
    claimed = remote.patch_rows("ebook_translation_queue", {
        "id": f"eq.{job['id']}", "status": "eq.queued",
    }, {"status": "translating", "updated_at": datetime.now(timezone.utc).isoformat()})
    if not claimed:
        return False
    job = claimed[0]
    chapter_id = str(job["ebook_chapter_id"])
    try:
        chapters = remote.rows("ebook_chapters", {
            "select": "id,ebook_id,chapter_number,title,translation_status",
            "id": f"eq.{chapter_id}", "limit": "1",
        })
        if not chapters:
            raise SyncError(f"Ebook chapter {chapter_id} was not found.")
        chapter = chapters[0]
        books = remote.rows("ebook_books", {
            "select": "id,owner_id,source_language", "id": f"eq.{chapter['ebook_id']}", "limit": "1",
        })
        if not books or books[0].get("source_language") != "zh":
            raise SyncError(f"Ebook {chapter['ebook_id']} is missing or is not a Chinese source.")
        remote.patch_rows("ebook_chapters", {"id": f"eq.{chapter_id}"}, {
            "translation_status": "processing", "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        total_chunks = int(job.get("total_chunks") or 0)
        current_chunk = int(job.get("current_chunk") or 0)
        completed_chunks = remote.all_rows("ebook_translation_chunks", {
            "select": "chunk_index,status", "ebook_chapter_id": f"eq.{chapter_id}", "status": "eq.completed",
        })
        completed_count = sum(row.get("status") == "completed" for row in completed_chunks)
        if completed_count != current_chunk:
            current_chunk = completed_count
            remote.patch_rows("ebook_translation_queue", {"id": f"eq.{job['id']}", "status": "eq.translating"}, {
                "current_chunk": current_chunk, "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        while current_chunk < total_chunks:
            rows = remote.rows("ebook_translation_chunks", {
                "select": "id,chunk_index,original_text", "ebook_chapter_id": f"eq.{chapter_id}",
                "status": "eq.queued", "order": "chunk_index.asc", "limit": "4",
            })
            if not rows:
                raise SyncError(f"Queued ebook translation chunks are missing for chapter {chapter_id}.")
            translated = translator.translate_batch([str(row["original_text"]) for row in rows])
            if len(translated) != len(rows):
                raise SyncError("Translator returned an unexpected ebook chunk count.")
            for row, result in zip(rows, translated):
                remote.patch_rows("ebook_translation_chunks", {"id": f"eq.{row['id']}"}, {
                    "translated_text": result, "status": "completed",
                })
            current_chunk += len(rows)
            status = "completed" if current_chunk >= total_chunks else "translating"
            remote.patch_rows("ebook_translation_queue", {"id": f"eq.{job['id']}"}, {
                "current_chunk": current_chunk, "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            job["current_chunk"] = current_chunk
        chunks = remote.all_rows("ebook_translation_chunks", {
            "select": "chunk_index,translated_text,status", "ebook_chapter_id": f"eq.{chapter_id}",
            "order": "chunk_index.asc",
        })
        if not chunks or any(row.get("status") != "completed" or not row.get("translated_text") for row in chunks):
            raise SyncError(f"Ebook translation is incomplete for chapter {chapter_id}.")
        translated_text = "\n\n".join(str(row["translated_text"]) for row in chunks)
        owner_key = hashlib.sha256(str(books[0]["owner_id"]).encode("utf-8")).hexdigest()[:32]
        chapter_number = int(chapter["chapter_number"])
        translation_path = f"ebooks/{owner_key}/{chapter['ebook_id']}/translations/{chapter_number:06d}.json"
        output = json.dumps({
            "ebook_id": str(chapter["ebook_id"]), "chapter_number": chapter_number,
            "translated_content": f"<p>{escape_html(translated_text).replace(chr(10), '<br/>')}</p>",
        }, ensure_ascii=False).encode("utf-8")
        b2_upload(translation_path, output, "application/json", check_existing=False)
        remote.patch_rows("ebook_chapters", {"id": f"eq.{chapter_id}"}, {
            "translation_status": "translated", "translated_content_path": translation_path,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        remote.delete_rows("ebook_translation_chunks", {"ebook_chapter_id": f"eq.{chapter_id}"})
        remote.delete_rows("ebook_translation_queue", {"ebook_chapter_id": f"eq.{chapter_id}"})
        return True
    except Exception:
        logger.exception("Ebook translation failed for chapter %s", chapter_id)
        remote.patch_rows("ebook_translation_queue", {"id": f"eq.{job['id']}"}, {
            "status": "failed", "retry_count": int(job.get("retry_count") or 0) + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        remote.patch_rows("ebook_chapters", {"id": f"eq.{chapter_id}"}, {
            "translation_status": "failed", "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return False


def sync_once() -> dict[str, int | bool]:
    if not DATABASE_PATH.exists():
        raise SyncError(f"SQLite database was not found: {DATABASE_PATH}")
    remote = SupabaseSync()
    remote.ensure_buckets()
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    counts = {"novels": 0, "chapters": 0, "queued": 0, "json_uploaded": 0, "audio_uploaded": 0, "covers_uploaded": 0}
    try:
        remote_novels = remote.all_rows("novels", {"select": "slug,source_url,cover_url", "order": "slug.asc"})
        slugs_by_source = {str(item.get("source_url")): str(item["slug"]) for item in remote_novels if item.get("source_url") and item.get("slug")}
        remote_by_source = {str(item.get("source_url")): item for item in remote_novels if item.get("source_url")}
        used_slugs = {str(item["slug"]) for item in remote_novels if item.get("slug")}
        for book in connection.execute("SELECT * FROM books ORDER BY id").fetchall():
            source_url = str(book["source_url"] or "")
            slug = slugs_by_source.get(source_url) or unique_slug(book["translated_title"] or book["title"], used_slugs)
            used_slugs.add(slug)
            current_novel = remote_by_source.get(source_url, {})
            local_cover = str(book["cover_url"] or "").strip()
            remote_cover = str(current_novel.get("cover_url") or "")
            cover_url = remote_cover if remote_cover.startswith("covers/") else None
            if not cover_url and local_cover.startswith("covers/"):
                cover_url = local_cover
            if not cover_url and local_cover:
                cover_url = local_cover
            if not cover_url:
                cover_url = remote_cover or None
            chapter_rows = connection.execute(
                "SELECT * FROM chapters WHERE book_id = ? ORDER BY chapter_number", (book["id"],)
            ).fetchall()
            latest = max((row["chapter_number"] for row in chapter_rows if row["translated_content"]), default=0)
            novel = remote.upsert(
                "novels",
                {
                    "slug": slug,
                    "title": book["translated_title"] or book["title"],
                    "author": book["translated_author"] or book["author"],
                    "cover_url": cover_url,
                    "source_url": book["source_url"],
                    "latest_chapter": latest,
                    "updated_at": book["updated_at"],
                    "deleted_at": book["deleted_at"],
                    "delete_after": book["delete_after"],
                },
                "slug",
            )
            counts["novels"] += 1
            chapter_payloads: list[dict] = []
            for chapter in chapter_rows:
                key = chapter_key(chapter["chapter_number"])
                original_content = chapter["original_content"]
                translated_content = chapter["translated_content"]
                translated = bool(chapter["translated_content"])
                json_path = f"{slug}/{key}.json"
                if original_content or translated_content:
                    payload = json.dumps(
                        {
                            "number": chapter["chapter_number"],
                            "title": chapter["translated_title"] or chapter["title"],
                            "original_title": chapter["title"],
                            "original_content": original_content,
                            "translated_content": translated_content,
                            "content": translated_content or original_content,
                            "translated": translated,
                            "audio": None,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    if remote.upload_if_changed("chapters", json_path, payload, "application/json"):
                        counts["json_uploaded"] += 1
                chapter_payloads.append(
                    {
                        "novel_id": novel["id"],
                        "chapter_number": chapter["chapter_number"],
                        "title": chapter["translated_title"] or chapter["title"],
                        "source_url": chapter["url"],
                        "translated": translated,
                        "content_path": f"chapters/{json_path}" if original_content or translated_content else None,
                        "updated_at": chapter["updated_at"],
                    }
                )
                counts["chapters"] += 1
            remote.upsert_many("chapters", chapter_payloads, "novel_id,source_url")
            remote_chapters = remote.rows("chapters", {"select": "id,source_url", "novel_id": f"eq.{novel['id']}"})
            ids_by_url = {row.get("source_url"): row.get("id") for row in remote_chapters}
            counts["queued"] += enqueue_untranslated_chapters(remote, [
                {"id": ids_by_url.get(chapter["url"]), "original_content": chapter["original_content"], "translated_content": chapter["translated_content"]}
                for chapter in chapter_rows
            ])
            logger.info("Synced %s (%s chapters)", slug, len(chapter_rows))
    finally:
        connection.close()
    return {**counts, "pushed": True}


sync_status: dict[str, object] = {"last_sync": None, "last_error": None}
pc_crawl_jobs: dict[str, dict[str, object]] = {}


def record_crawled_chapter_discovery(remote: SupabaseSync, chapter: dict, novel: dict) -> dict | None:
    """Record a new chapter for notification after translation completes."""
    source_url = str(chapter.get("source_url") or "").strip()
    if not source_url:
        return None
    response = remote.request(
        "POST",
        "/rest/v1/chapter_discoveries?on_conflict=novel_id,source_identity",
        json={
            "novel_id": int(novel["id"]),
            "source_identity": "url:" + source_url.rstrip("/"),
            "chapter_number": max(1, int(chapter.get("chapter_number") or 1)),
            "chapter_title": str(chapter.get("title") or "Chapter")[:500],
            "source_url": source_url,
            "crawled_at": datetime.now(timezone.utc).isoformat(),
            "notification_after_translation": True,
        },
        headers={"Prefer": "resolution=ignore-duplicates,return=representation"},
    )
    inserted = response.json()
    if isinstance(inserted, list) and inserted:
        return inserted[0]
    identity = "url:" + source_url.rstrip("/")
    existing = remote.patch_rows(
        "chapter_discoveries",
        {"novel_id": f"eq.{novel['id']}", "source_identity": f"eq.{identity}"},
        {"crawled_at": datetime.now(timezone.utc).isoformat(), "notification_after_translation": True},
    )
    return existing[0] if existing else None


def _existing_novel_refresh_patch(info, source_type: str) -> dict:
    """Refresh crawl-derived fields without replacing editor-managed artwork or synopsis."""
    return {
        "genres": info.genres or [],
        "source_type": source_type,
        "deleted_at": None,
        "delete_after": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@contextmanager
def worker_process_lock():
    """Keep a second PC worker from claiming crawl or translation work."""
    lock_path = ROOT / "temp" / "supabase-worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        handle.seek(0)
        handle.write("0")
        handle.flush()
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield True
    except OSError:
        yield False
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


async def _crawl_and_translate_url(
    url: str,
    update_status,
    crawl_job_id: str | None = None,
    source_type: str = "zh",
    source_update_novel_id: int | None = None,
) -> None:
    """Crawl directly into Supabase; SQLite is never used in this path."""
    try:
        update_status(status="crawling")
        crawler = registry.get_for_url(url)
        if getattr(crawler, "source_type", "zh") != source_type:
            raise SyncError("Loại nguồn của job không khớp crawler.")
        is_vietnamese = source_type == "vi"
        info = await crawler.get_book_info(url)
        remote = SupabaseSync()
        logger.info("Bắt đầu crawl truyện: %s", info.title)
        novels = remote.rows(
            "novels", {"select": "id,slug,title,author,cover_url", "id": f"eq.{source_update_novel_id}", "limit": "1"},
        ) if source_update_novel_id else remote.rows(
            "novels", {"select": "id,slug,title,author,cover_url", "source_url": f"eq.{info.source_url}", "limit": "1"},
        )
        was_existing_novel = bool(novels)
        if novels:
            novel = novels[0]
            translated_title = str(novel.get("title") or info.title)
            translated_author = novel.get("author") or info.author
            novel_patch = _existing_novel_refresh_patch(info, source_type)
            remote.patch_rows("novels", {"id": f"eq.{novel['id']}"}, novel_patch)
        else:
            translated_title = info.title if is_vietnamese else translator.translate_metadata(info.title)
            translated_author = info.author if is_vietnamese else (translator.translate_metadata(info.author) if info.author else None)
            existing_slugs = {str(item["slug"]) for item in remote.all_rows("novels", {"select": "slug", "order": "slug.asc"}) if item.get("slug")}
            novel_slug = unique_slug(translated_title, existing_slugs)
            novel = remote.insert("novels", {"slug": novel_slug, "title": translated_title, "author": translated_author, "cover_url": info.cover_url, "genres": info.genres or [], "source_type": source_type, "source_url": info.source_url, "latest_chapter": 0})

        update_status(book_id=novel["id"], title=translated_title)

        # Crawlers may expose the same logical chapter through multiple URLs.
        # Keep one canonical source per number: storage and reader navigation
        # use the number as identity, so accepting both would overwrite raw JSON.
        incoming = await crawler.get_chapters(url)
        incoming_by_number = {}
        for item in incoming:
            incoming_by_number.setdefault(int(item.chapter_number), item)
        incoming = list(incoming_by_number.values())
        existing = remote.all_rows("chapters", {"select": "id,chapter_number,title,source_url,translated", "novel_id": f"eq.{novel['id']}", "order": "source_url.asc"})
        existing_by_number = {int(row["chapter_number"]): row for row in existing}
        updated_source_ids: set[int] = set()
        if source_update_novel_id:
            for item in incoming:
                current = existing_by_number.get(int(item.chapter_number))
                if current:
                    updated_source_ids.add(int(current["id"]))
                    if current.get("source_url") != item.url:
                        remote.patch_rows("chapters", {"id": f"eq.{current['id']}"}, {"source_url": item.url})
        new_rows = [
            # A source-language flag is not proof that the chapter JSON has
            # reached B2 yet. Mark it ready only after upload + content_path patch.
            {"novel_id": novel["id"], "chapter_number": item.chapter_number, "title": item.title, "source_url": item.url, "translated": False}
            for item in incoming
            if int(item.chapter_number) not in existing_by_number
        ]
        remote.insert_many_ignore("chapters", new_rows, "novel_id,source_url")
        chapters = remote.all_rows("chapters", {"select": "id,chapter_number,title,source_url,translated,content_path", "novel_id": f"eq.{novel['id']}", "order": "chapter_number.asc"})
        new_source_urls = {row["source_url"] for row in new_rows}
        new_chapters = [row for row in chapters if row.get("source_url") in new_source_urls]
        chapter_ids = {row["id"] for row in chapters}
        queued = {
            row["chapter_id"]
            for row in remote.all_rows("translation_queue", {"select": "chapter_id", "order": "chapter_id.asc"})
            if row.get("chapter_id") in chapter_ids
        }
        stored_without_queue = [row for row in chapters if not row["translated"] and row.get("content_path") and row["id"] not in queued]
        restored_chapter_ids = set() if is_vietnamese else restore_stored_chapters(remote, stored_without_queue, crawl_job_id)
        pending = [
            row
            for row in chapters
            if (is_vietnamese or not row["translated"] or int(row["id"]) in updated_source_ids)
            and (source_update_novel_id or row["id"] not in queued)
            and (source_update_novel_id or row["id"] not in restored_chapter_ids)
            and (source_update_novel_id or not row.get("content_path"))
        ]
        total_raw = len(pending)
        logger.info(
            "Đã đọc %s chương; có %s chương mới cần crawl và đưa vào hàng đợi dịch.",
            len(incoming), total_raw,
        )
        crawl_concurrency = 1 if crawler.source_name == "xsw" else (3 if crawler.source_name == "uukanshu" else CRAWL_CONCURRENCY)
        logger.info("Crawl song song tối đa %s chương cùng lúc.", crawl_concurrency)
        update_status(status="crawling", raw_loaded=0, total_raw=total_raw)
        processed_job_ids = set(restored_chapter_ids)

        semaphore = asyncio.Semaphore(crawl_concurrency)

        async def crawl_chapter(chapter: dict) -> tuple[dict, str]:
            async with semaphore:
                if crawl_job_id and not remote_crawl_is_running(remote, crawl_job_id):
                    return chapter, ""
                max_attempts = 1 if crawler.source_name == "uukanshu" else 3
                for attempt in range(1, max_attempts + 1):
                    try:
                        content = await asyncio.wait_for(
                            crawler.get_chapter_content(str(chapter["source_url"])),
                            timeout=settings.crawler_timeout + 5,
                        )
                        break
                    except (SourceRequestError, asyncio.TimeoutError) as exc:
                        if attempt == max_attempts:
                            logger.warning("Bỏ qua chương %s từ nguồn %s sau %s lần thử: %s", chapter["chapter_number"], crawler.source_name, attempt, exc)
                            return chapter, ""
                        delay = attempt * 3
                        logger.warning(
                            "Nguồn %s tạm thời không phản hồi chương %s; thử lại sau %s giây.",
                            crawler.source_name, chapter["chapter_number"], delay,
                        )
                        await asyncio.sleep(delay)
                    except SourceParseError as exc:
                        logger.warning(
                            "Bỏ qua chương %s từ nguồn %s vì nội dung không hợp lệ: %s",
                            chapter["chapter_number"], crawler.source_name, exc,
                        )
                        return chapter, ""
            return chapter, clean_chapter_text(content)

        tasks = [asyncio.create_task(crawl_chapter(chapter)) for chapter in pending]
        for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
            if crawl_job_id and not remote_crawl_is_running(remote, crawl_job_id):
                for pending_task in tasks:
                    pending_task.cancel()
                return
            chapter, original = await task
            if crawl_job_id and not remote_crawl_is_running(remote, crawl_job_id):
                for pending_task in tasks:
                    pending_task.cancel()
                return
            if not original:
                logger.warning("Bỏ qua chương %s vì không lấy được nội dung.", chapter["chapter_number"])
                continue
            previous_payload: dict = {}
            content_path = str(chapter.get("content_path") or "")
            if source_update_novel_id and content_path.startswith("chapters/"):
                try:
                    previous_payload = remote.download_json("chapters", content_path.removeprefix("chapters/"))
                except SyncError:
                    logger.warning("Could not read the prior raw content for chapter %s.", chapter["chapter_number"])
            if source_update_novel_id and previous_payload and clean_chapter_text(str(previous_payload.get("original_content") or "")) == original:
                if str(previous_payload.get("original_title") or "") != str(chapter["title"]):
                    translated_title = translator.translate(str(chapter["title"]))
                    previous_payload.update({"number": chapter["chapter_number"], "title": translated_title, "original_title": chapter["title"]})
                    remote.upload_if_changed(
                        "chapters",
                        f"{novel['slug']}/{chapter_key(int(chapter['chapter_number']))}.json",
                        json.dumps(previous_payload, ensure_ascii=False).encode("utf-8"),
                        "application/json",
                        check_existing=False,
                    )
                    remote.patch_rows("chapters", {"id": f"eq.{chapter['id']}"}, {"title": translated_title})
                update_status(status="crawling", raw_loaded=completed, total_raw=total_raw, current_chapter=chapter["chapter_number"])
                continue
            key = chapter_key(int(chapter["chapter_number"]))
            payload = json.dumps({"number": chapter["chapter_number"], "title": chapter["title"], "original_title": chapter["title"], "original_content": original, "translated_content": original if is_vietnamese else None, "content": original if is_vietnamese else None, "translated": is_vietnamese}, ensure_ascii=False).encode("utf-8")
            remote.upload_if_changed("chapters", f"{novel['slug']}/{key}.json", payload, "application/json", check_existing=False)
            remote.patch_rows("chapters", {"id": f"eq.{chapter['id']}"}, {"content_path": f"chapters/{novel['slug']}/{key}.json", "translated": is_vietnamese})
            remote.patch_rows(
                "chapter_discoveries",
                {
                    "novel_id": f"eq.{novel['id']}",
                    "source_identity": f"eq.url:{str(chapter['source_url']).rstrip('/')}",
                    "crawled_at": "is.null",
                },
                {"crawled_at": datetime.now(timezone.utc).isoformat()},
            )
            if was_existing_novel and chapter.get("source_url") in new_source_urls:
                try:
                    discovery = record_crawled_chapter_discovery(remote, chapter, novel)
                except Exception:
                    discovery = None
                    logger.exception(
                        "Could not record notification discovery for novel_id=%s chapter=%s",
                        novel["id"], chapter["chapter_number"],
                    )
            else:
                discovery = None
            if discovery and not is_vietnamese:
                try:
                    from app.notification_service import notify_chapter_discovery

                    notify_chapter_discovery(remote, discovery, novel, crawler_only=True)
                except Exception:
                    logger.exception("Could not send source-only crawl notification for chapter %s", chapter["id"])
            if is_vietnamese:
                if discovery:
                    try:
                        from app.notification_service import notify_chapter_discovery

                        notify_chapter_discovery(
                            remote, {**discovery, "chapter_title": chapter["title"]}, novel,
                            update_existing=True,
                        )
                        remote.patch_rows(
                            "chapter_discoveries",
                            {"id": f"eq.{discovery['id']}"},
                            {"notification_after_translation": False},
                        )
                    except Exception:
                        logger.exception("Could not notify followers for translated chapter %s", chapter["id"])
                update_status(status="crawling", raw_loaded=completed, total_raw=total_raw, current_chapter=chapter["chapter_number"])
                continue
            if source_update_novel_id:
                remote.delete_rows("chapter_chunks", {"chapter_id": f"eq.{chapter['id']}"})
                remote.delete_rows("translation_queue", {"chapter_id": f"eq.{chapter['id']}"})
            parts = chunk_text(original)
            if not parts:
                logger.warning("Bỏ qua chương %s vì không lấy được nội dung.", chapter["chapter_number"])
                continue
            logger.info(
                "Đã crawl %s/%s: chương %s (%s ký tự, %s đoạn dịch).",
                completed, total_raw, chapter["chapter_number"], len(original), len(parts),
            )
            if enqueue_untranslated_chapters(remote, [{"id": chapter["id"], "original_content": original, "translated_content": None}], crawl_job_id, 100 if chapter["source_url"] in new_source_urls else 0):
                processed_job_ids.add(int(chapter["id"]))
            update_status(status="crawling", raw_loaded=completed, total_raw=total_raw, current_chapter=chapter["chapter_number"])
        if is_vietnamese:
            remote.patch_rows("novels", {"id": f"eq.{novel['id']}"}, {"latest_chapter": max((row["chapter_number"] for row in chapters), default=0), "source_type": "vi"})
            update_status(status="completed", result={"book_id": novel["id"], "chapters_added": len(new_chapters), "raw_loaded": total_raw, "source_type": "vi"})
            return
        logger.info("Đã tạo hoặc khôi phục hàng đợi dịch cho %s chương.", len(processed_job_ids))
        chapters_added = len(new_chapters)
        result = {
            "book_id": novel["id"],
            "chapters_added": chapters_added,
            "raw_loaded": total_raw,
            "queued": len(processed_job_ids),
        }
        if processed_job_ids:
            update_status(status="translating", **result)
            return
        update_status(status="completed", result={**result, "translated": 0})
    except Exception as exc:
        if crawl_job_id and remote_crawl_is_paused(remote, crawl_job_id):
            logger.info("Job crawl %s đã được tạm dừng.", crawl_job_id)
            return
        logger.exception("Worker crawl/dịch trên PC thất bại.")
        update_status(status="queued", error=str(exc))


def process_remote_crawl_job_once(remote: SupabaseSync | None = None) -> bool:
    remote = remote or SupabaseSync()
    job = claim_remote_crawl_job(remote)
    if not job:
        logger.info("Chưa có job crawl nào trong hàng đợi.")
        return False
    logger.info("Đã nhận job crawl %s.", job["id"])


    job_result = dict(job.get("result") or {})

    def update_status(**values: object) -> None:
        payload = {key: values[key] for key in ("status", "error", "result") if key in values}
        details = {key: value for key, value in values.items() if key not in {"status", "error", "result"}}
        if isinstance(payload.get("result"), dict):
            job_result.update(payload["result"])
        if details:
            job_result.update(details)
        if job_result:
            payload["result"] = job_result
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        params = {"id": f"eq.{job['id']}"}
        if payload.get("status") in {"crawling", "syncing", "translating", "completed", "queued"}:
            params["status"] = "eq.crawling"
        remote.patch_rows("remote_crawl_jobs", params, payload)

    try:
        logger.info("Bắt đầu job crawl: %s", job["source_url"])
        source_update_novel_id = job_result.get("source_update_novel_id")
        asyncio.run(_crawl_and_translate_url(
            str(job["source_url"]), update_status, str(job["id"]), str(job.get("source_type") or "zh"),
            int(source_update_novel_id) if source_update_novel_id else None,
        ))
    except Exception as exc:
        logger.exception("Job crawl từ xa thất bại trước khi khởi chạy.")
        update_status(status="failed", error=str(exc))
    return True


def _run_translation_watch(stop_event: threading.Event, idle_delay: float) -> None:
    """Consume translation_queue independently from the crawl worker."""
    logger.info("Worker dịch riêng đã khởi động.")
    while not stop_event.is_set():
        try:
            if run_translation_worker_once():
                sync_status["last_error"] = None
                continue
            if run_ebook_translation_worker_once():
                sync_status["last_error"] = None
                continue
        except SyncError as exc:
            logger.exception("Worker dịch gặp lỗi khi đọc hàng đợi.")
            sync_status["last_error"] = str(exc)
        stop_event.wait(idle_delay)


def _run_ebook_ingestion_watch(stop_event: threading.Event, idle_delay: float) -> None:
    """Keep ebook imports moving independently of a busy novel translation queue."""
    logger.info("Ebook ingestion worker started.")
    while not stop_event.is_set():
        try:
            if run_ebook_ingestion_worker_once():
                continue
        except SyncError as exc:
            logger.exception("Ebook ingestion worker failed while reading the queue.")
            sync_status["last_error"] = str(exc)
        stop_event.wait(idle_delay)


def translation_worker_concurrency() -> int:
    """Keep GPU translation parallelism bounded and configurable on the PC."""
    return max(1, min(4, int(os.getenv("TRANSLATION_WORKER_CONCURRENCY", "2"))))


def run_watch() -> None:
    interval = max(5, int(os.getenv("PC_WORKER_POLL_INTERVAL", "10")))
    with worker_process_lock() as acquired:
        if not acquired:
            logger.warning("Another PC worker is already running; exiting duplicate worker.")
            return
        translator_stop = threading.Event()
        translator_threads = [
            threading.Thread(
                target=_run_translation_watch,
                args=(translator_stop, min(interval, 2)),
                daemon=True,
                name=f"leotruyen-translator-{index + 1}",
            )
            for index in range(translation_worker_concurrency())
        ]
        ebook_ingestion_thread = threading.Thread(
            target=_run_ebook_ingestion_watch,
            args=(translator_stop, min(interval, 2)),
            daemon=True,
            name="leotruyen-ebook-ingestion",
        )
        for translator_thread in translator_threads:
            translator_thread.start()
        ebook_ingestion_thread.start()
        try:
            while True:
                try:
                    process_remote_crawl_job_once()
                except SyncError as exc:
                    logger.exception("Scheduled crawl worker failed")
                    sync_status["last_error"] = str(exc)
                time.sleep(interval)
        finally:
            translator_stop.set()
            for translator_thread in translator_threads:
                translator_thread.join(timeout=5)
            ebook_ingestion_thread.join(timeout=5)


@app.on_event("startup")
def start_watch() -> None:
    if os.getenv("SYNC_AUTOSTART", "false").lower() == "true":
        threading.Thread(target=run_watch, daemon=True, name="leotruyen-sync").start()


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", **sync_status}


@app.post("/sync")
def trigger_sync() -> dict[str, int | bool]:
    try:
        result = sync_once()
        sync_status["last_sync"] = result
        sync_status["last_error"] = None
        return result
    except SyncError as exc:
        sync_status["last_error"] = str(exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class CrawlRequest(BaseModel):
    url: str


async def _crawl_and_translate(job_id: str, url: str) -> None:
    await _crawl_and_translate_url(url, lambda **values: pc_crawl_jobs[job_id].update(values))


@app.post("/crawl", status_code=202)
async def crawl_and_translate(payload: CrawlRequest, background_tasks: BackgroundTasks, x_worker_token: str | None = Header(default=None)) -> dict[str, object]:
    if not PC_WORKER_TOKEN or x_worker_token != PC_WORKER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid PC worker token")
    job_id = str(uuid.uuid4())
    pc_crawl_jobs[job_id] = {"status": "queued"}
    background_tasks.add_task(_crawl_and_translate, job_id, payload.url)
    return {"job_id": job_id, "status": "queued"}


@app.get("/crawl/{job_id}")
def crawl_status(job_id: str, x_worker_token: str | None = Header(default=None)) -> dict[str, object]:
    if not PC_WORKER_TOKEN or x_worker_token != PC_WORKER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid PC worker token")
    if job_id not in pc_crawl_jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return pc_crawl_jobs[job_id]


@app.post("/priority")
def prioritize(payload: dict[str, int], x_worker_token: str | None = Header(default=None)) -> dict[str, bool]:
    if not PC_WORKER_TOKEN or x_worker_token != PC_WORKER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid PC worker token")
    chapter_id = int(payload.get("chapter_id", 0))
    priority = int(payload.get("priority", 0))
    if chapter_id < 1:
        raise HTTPException(status_code=400, detail="Invalid chapter id")
    return {"updated": prioritize_translation(SupabaseSync(), chapter_id, priority)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish LeoTruyen data to Supabase")
    parser.add_argument("command", choices=("sync", "watch", "covers"), default="sync", nargs="?")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "watch":
        os.environ["SYNC_INTERVAL"] = str(max(30, args.interval))
        run_watch()
        return
    if args.command == "covers":
        logger.info("Cover migration result: %s", migrate_remote_cover_urls())
        return
    logger.info("Sync result: %s", sync_once())


if __name__ == "__main__":
    main()
