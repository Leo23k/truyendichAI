"""Synchronize local SQLite novels to Supabase PostgreSQL and Storage."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import PROJECT_ROOT, settings
from app.translator.chunking import chunk_text
from app.translator.service import TranslationUnavailableError, translator
from app.crawlers.base import clean_chapter_text
from app.crawlers.bootstrap import registry
from app.crawlers.errors import SourceRequestError

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
AUDIO_DIR = Path(os.getenv("SYNC_AUDIO_DIR", settings.audio_path))
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_SECRET_KEY", "")
RETRIES = 3
CRAWL_CONCURRENCY = max(1, min(6, int(os.getenv("CRAWL_CONCURRENCY", "4"))))
logger = logging.getLogger(__name__)
app = FastAPI(title="LeoTruyen Sync")
PC_WORKER_TOKEN = os.getenv("PC_WORKER_TOKEN", "")


class SyncError(RuntimeError):
    pass


def slugify(value: str, book_id: int) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return f"{slug or 'novel'}-{book_id}"


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
        buckets = self.request("GET", "/storage/v1/bucket").json()
        existing = {bucket.get("id") for bucket in buckets if isinstance(bucket, dict)}
        for bucket in ("covers", "chapters", "audio"):
            if bucket not in existing:
                self.request("POST", "/storage/v1/bucket", json={"id": bucket, "name": bucket, "public": True})

    def upload_if_changed(self, bucket: str, path: str, content: bytes, content_type: str) -> bool:
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
        encoded = quote(path, safe="/")
        response = self.request("GET", f"/storage/v1/object/public/{bucket}/{encoded}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise SyncError(f"Stored {bucket}/{path} is not a JSON object.")
        return payload


def audio_bytes(chapter_id: int) -> bytes | None:
    manifest = AUDIO_DIR / f"chapter-{chapter_id}-vi-VN-HoaiMyNeural-1_00.json"
    if not manifest.exists():
        return None
    try:
        segments = [AUDIO_DIR / item["filename"] for item in json.loads(manifest.read_text("utf-8"))["segments"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not segments or not all(path.exists() and path.stat().st_size for path in segments):
        return None
    return b"".join(path.read_bytes() for path in segments)


def cover_bytes(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.get(url)
        response.raise_for_status()
        return response.content
    except httpx.HTTPError:
        logger.warning("Could not download cover: %s", url)
        return None


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


def prioritize_translation(remote: SupabaseSync, chapter_id: int, priority: int) -> bool:
    """Raise a queued chapter without interrupting the chunk already translating."""
    rows = remote.patch_rows(
        "translation_queue",
        {"chapter_id": f"eq.{chapter_id}", "status": "eq.queued"},
        {"priority": max(0, min(priority, 100)), "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return bool(rows)


def enqueue_remote_crawl(source_url: str) -> dict:
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
        return remote.insert("remote_crawl_jobs", {"source_url": source_url, "status": "queued"})
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
    if not rows:
        return None
    job = rows[0]
    claimed = remote.patch_rows(
        "remote_crawl_jobs",
        {"id": f"eq.{job['id']}", "status": "eq.queued"},
        {"status": "crawling", "error": None, "started_at": datetime.now(timezone.utc).isoformat(), "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return claimed[0] if claimed else None


def remote_job_is_running(remote: SupabaseSync, job_id: str) -> bool:
    return bool(remote.rows("remote_crawl_jobs", {"select": "id", "id": f"eq.{job_id}", "status": "in.(crawling,syncing,translating)", "limit": "1"}))


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
        {"select": "id,slug,latest_chapter", "id": f"eq.{chapter['novel_id']}", "limit": "1"},
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
    remote.upload_if_changed("chapters", json_path, payload, "application/json")
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
    if int(novel.get("latest_chapter") or 0) < int(chapter["chapter_number"]):
        remote.patch_rows(
            "novels",
            {"id": f"eq.{novel['id']}"},
            {"latest_chapter": chapter["chapter_number"], "updated_at": datetime.now(timezone.utc).isoformat()},
        )
    logger.info("Đã xuất bản chương %s: %s", chapter["chapter_number"], chapter["title"])


def run_translation_worker_once(remote: SupabaseSync | None = None) -> bool:
    remote = remote or SupabaseSync()
    job = claim_translation_job(remote)

    if not job:
        return False

    BATCH_SIZE = 4

    try:
        total_chunks = int(job["total_chunks"])
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
            translated_texts = translator.translate_batch(texts)

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

            if status == "completed":
                publish_completed_translation(remote, int(job["chapter_id"]))

            logger.info(
                "Đã dịch chương ID %s: hoàn tất %s/%s đoạn.",
                job["chapter_id"],
                next_index,
                total_chunks,
            )

            job["current_chunk"] = next_index

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


def sync_once() -> dict[str, int | bool]:
    if not DATABASE_PATH.exists():
        raise SyncError(f"SQLite database was not found: {DATABASE_PATH}")
    remote = SupabaseSync()
    remote.ensure_buckets()
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    counts = {"novels": 0, "chapters": 0, "queued": 0, "json_uploaded": 0, "audio_uploaded": 0, "covers_uploaded": 0}
    try:
        for book in connection.execute("SELECT * FROM books ORDER BY id").fetchall():
            slug = slugify(book["translated_title"] or book["title"], book["id"])
            cover_path = None
            cover = cover_bytes(book["cover_url"])
            if cover:
                cover_path = f"{slug}/cover.jpg"
                if remote.upload_if_changed("covers", cover_path, cover, "image/jpeg"):
                    counts["covers_uploaded"] += 1
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
                    "cover_url": f"covers/{cover_path}" if cover_path else None,
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
                audio = audio_bytes(chapter["id"])
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
                            "audio": f"audio/{slug}/{key}.mp3" if audio else None,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    if remote.upload_if_changed("chapters", json_path, payload, "application/json"):
                        counts["json_uploaded"] += 1
                audio_path = f"{slug}/{key}.mp3"
                if audio and remote.upload_if_changed("audio", audio_path, audio, "audio/mpeg"):
                    counts["audio_uploaded"] += 1
                chapter_payloads.append(
                    {
                        "novel_id": novel["id"],
                        "chapter_number": chapter["chapter_number"],
                        "title": chapter["translated_title"] or chapter["title"],
                        "source_url": chapter["url"],
                        "translated": translated,
                        "audio_available": bool(audio),
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


async def _crawl_and_translate_url(url: str, update_status, crawl_job_id: str | None = None) -> None:
    """Crawl directly into Supabase; SQLite is never used in this path."""
    translation_stop = asyncio.Event()
    translation_task: asyncio.Task[None] | None = None
    try:
        update_status(status="crawling")
        crawler = registry.get_for_url(url)
        info = await crawler.get_book_info(url)
        remote = SupabaseSync()
        logger.info("Bắt đầu crawl truyện: %s", info.title)
        translated_title = translator.translate(info.title)
        translated_author = translator.translate(info.author) if info.author else None
        novels = remote.rows("novels", {"select": "id,slug", "source_url": f"eq.{info.source_url}", "limit": "1"})
        if novels:
            novel = novels[0]
            remote.patch_rows("novels", {"id": f"eq.{novel['id']}"}, {"title": translated_title, "author": translated_author, "cover_url": info.cover_url, "deleted_at": None, "delete_after": None, "updated_at": datetime.now(timezone.utc).isoformat()})
        else:
            slug_id = uuid.uuid5(uuid.NAMESPACE_URL, info.source_url).int % 100000000
            novel = remote.insert("novels", {"slug": slugify(translated_title, slug_id), "title": translated_title, "author": translated_author, "cover_url": info.cover_url, "source_url": info.source_url, "latest_chapter": 0})

        update_status(book_id=novel["id"], title=translated_title)

        incoming = await crawler.get_chapters(url)
        existing = remote.all_rows("chapters", {"select": "id,source_url,translated", "novel_id": f"eq.{novel['id']}", "order": "source_url.asc"})
        existing_urls = {row["source_url"] for row in existing}
        new_rows = [{"novel_id": novel["id"], "chapter_number": item.chapter_number, "title": item.title, "source_url": item.url, "translated": False, "audio_available": False} for item in incoming if item.url not in existing_urls]
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
        restored_chapter_ids = restore_stored_chapters(remote, stored_without_queue, crawl_job_id)
        pending = [
            row
            for row in chapters
            if not row["translated"]
            and row["id"] not in queued
            and row["id"] not in restored_chapter_ids
            and not row.get("content_path")
        ]
        total_raw = len(pending)
        logger.info(
            "Đã đọc %s chương; có %s chương mới cần crawl và đưa vào hàng đợi dịch.",
            len(incoming), total_raw,
        )
        crawl_concurrency = 1 if crawler.source_name == "xsw" else CRAWL_CONCURRENCY
        logger.info("Crawl song song tối đa %s chương cùng lúc.", crawl_concurrency)
        update_status(status="crawling", raw_loaded=0, total_raw=total_raw)
        processed_job_ids = set(restored_chapter_ids)

        async def translate_while_crawling() -> None:
            """Keep the single GPU translation worker busy while crawling continues."""
            while not translation_stop.is_set():
                try:
                    translated = await asyncio.to_thread(run_translation_worker_once)
                    if not translated:
                        await asyncio.sleep(0.5)
                except Exception:
                    logger.exception("Worker dịch song song gặp lỗi tạm thời.")
                    await asyncio.sleep(1)

        def start_translation() -> None:
            nonlocal translation_task
            if translation_task is None:
                translation_task = asyncio.create_task(translate_while_crawling())

        if processed_job_ids:
            start_translation()

        semaphore = asyncio.Semaphore(crawl_concurrency)

        async def crawl_chapter(chapter: dict) -> tuple[dict, str]:
            async with semaphore:
                if crawl_job_id and not remote_job_is_running(remote, crawl_job_id):
                    raise SyncError("Job đã được tạm dừng hoặc xóa.")
                for attempt in range(1, 4):
                    try:
                        content = await asyncio.wait_for(
                            crawler.get_chapter_content(str(chapter["source_url"])),
                            timeout=settings.crawler_timeout + 5,
                        )
                        break
                    except (SourceRequestError, asyncio.TimeoutError) as exc:
                        if attempt == 3:
                            if isinstance(exc, asyncio.TimeoutError):
                                raise SourceRequestError("Không thể kết nối tới website nguồn.") from exc
                            raise
                        delay = attempt * 3
                        logger.warning(
                            "Nguồn %s tạm thời không phản hồi chương %s; thử lại sau %s giây.",
                            crawler.source_name, chapter["chapter_number"], delay,
                        )
                        await asyncio.sleep(delay)
            return chapter, clean_chapter_text(content)

        tasks = [asyncio.create_task(crawl_chapter(chapter)) for chapter in pending]
        for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
            if crawl_job_id and not remote_job_is_running(remote, crawl_job_id):
                for pending_task in tasks:
                    pending_task.cancel()
                return
            chapter, original = await task
            if not original:
                logger.warning("Bỏ qua chương %s vì không lấy được nội dung.", chapter["chapter_number"])
                continue
            key = chapter_key(int(chapter["chapter_number"]))
            payload = json.dumps({"number": chapter["chapter_number"], "title": chapter["title"], "original_title": chapter["title"], "original_content": original, "translated": False}, ensure_ascii=False).encode("utf-8")
            remote.upload_if_changed("chapters", f"{novel['slug']}/{key}.json", payload, "application/json")
            remote.patch_rows("chapters", {"id": f"eq.{chapter['id']}"}, {"content_path": f"chapters/{novel['slug']}/{key}.json"})
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
                start_translation()
            update_status(status="crawling", raw_loaded=completed, total_raw=total_raw, current_chapter=chapter["chapter_number"])
        logger.info("Đã tạo hoặc khôi phục hàng đợi dịch cho %s chương.", len(processed_job_ids))
        chapters_added = len(new_chapters)
        translation_stop.set()
        if translation_task:
            await translation_task
        update_status(status="translating", book_id=novel["id"], chapters_added=chapters_added, raw_loaded=total_raw, queued=len(processed_job_ids))
        while run_translation_worker_once(remote):
            if crawl_job_id and not remote_job_is_running(remote, crawl_job_id):
                return
        queue_states = remote.rows(
            "translation_queue",
            {"select": "chapter_id,status", "chapter_id": f"in.({','.join(str(chapter_id) for chapter_id in processed_job_ids)})"},
        ) if processed_job_ids else []
        incomplete = [row for row in queue_states if row.get("status") != "completed"]
        if incomplete or len(queue_states) != len(processed_job_ids):
            raise SyncError(f"Translation did not complete for {len(incomplete) or len(processed_job_ids) - len(queue_states)} newly crawled chapter(s).")
        update_status(status="completed", result={"book_id": novel["id"], "chapters_added": chapters_added, "raw_loaded": total_raw, "queued": len(processed_job_ids), "translated": len(processed_job_ids)})
    except Exception as exc:
        logger.exception("Worker crawl/dịch trên PC thất bại.")
        update_status(status="queued", error=str(exc))
    finally:
        translation_stop.set()
        if translation_task and not translation_task.done():
            await translation_task


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
        remote.patch_rows("remote_crawl_jobs", {"id": f"eq.{job['id']}"}, payload)

    try:
        logger.info("Bắt đầu job crawl: %s", job["source_url"])
        asyncio.run(_crawl_and_translate_url(str(job["source_url"]), update_status, str(job["id"])))
    except Exception as exc:
        logger.exception("Job crawl từ xa thất bại trước khi khởi chạy.")
        update_status(status="failed", error=str(exc))
    return True


def run_watch() -> None:
    interval = max(5, int(os.getenv("PC_WORKER_POLL_INTERVAL", "10")))
    with worker_process_lock() as acquired:
        if not acquired:
            logger.warning("Another PC worker is already running; exiting duplicate worker.")
            return
        while True:
            try:
                remote = SupabaseSync()
                process_remote_crawl_job_once(remote)
                while run_translation_worker_once(remote):
                    sync_status["last_error"] = None
            except SyncError as exc:
                logger.exception("Scheduled Supabase sync failed")
                sync_status["last_error"] = str(exc)
            time.sleep(interval)


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
    parser.add_argument("command", choices=("sync", "watch"), default="sync", nargs="?")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "watch":
        os.environ["SYNC_INTERVAL"] = str(max(30, args.interval))
        run_watch()
        return
    logger.info("Sync result: %s", sync_once())


if __name__ == "__main__":
    main()
