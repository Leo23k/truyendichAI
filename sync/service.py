"""Synchronize local SQLite novels to Supabase PostgreSQL and Storage."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
import sys
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import PROJECT_ROOT, settings

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
logger = logging.getLogger(__name__)
app = FastAPI(title="LeoTruyen Sync")


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


def sync_once() -> dict[str, int | bool]:
    if not DATABASE_PATH.exists():
        raise SyncError(f"SQLite database was not found: {DATABASE_PATH}")
    remote = SupabaseSync()
    remote.ensure_buckets()
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    counts = {"novels": 0, "chapters": 0, "json_uploaded": 0, "audio_uploaded": 0, "covers_uploaded": 0}
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
            logger.info("Synced %s (%s chapters)", slug, len(chapter_rows))
    finally:
        connection.close()
    return {**counts, "pushed": True}


sync_status: dict[str, object] = {"last_sync": None, "last_error": None}


def run_watch() -> None:
    interval = max(30, int(os.getenv("SYNC_INTERVAL", "60")))
    while True:
        try:
            sync_status["last_sync"] = sync_once()
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish LeoTruyen data to Supabase")
    parser.add_argument("command", choices=("sync", "watch"), default="sync", nargs="?")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        logger.info("Sync result: %s", sync_once())
        if args.command == "sync":
            return
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
