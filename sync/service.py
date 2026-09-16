"""Export locally processed novels and publish them with Git CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATA_REPO_URL = os.getenv("GITHUB_DATA_REPO_URL", "https://github.com/Leo23k/truyendichAI-data.git")
DATA_REPO_DIR = Path(os.getenv("GITHUB_DATA_REPO_DIR", ROOT / "temp" / "truyendichAI-data"))
DATABASE_PATH = Path(os.getenv("SYNC_DATABASE_PATH", ROOT / "data" / "app.db"))
AUDIO_DIR = Path(os.getenv("SYNC_AUDIO_DIR", ROOT / "data" / "audio"))
RETRIES = 3
logger = logging.getLogger(__name__)
app = FastAPI(title="LeoTruyen Sync")


class SyncError(RuntimeError):
    pass


def run_git(*args: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise SyncError("Git CLI is not installed or is not available in PATH.") from exc
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout).strip()
        raise SyncError(f"git {' '.join(args)} failed: {message}") from exc
    return result.stdout.strip()


def authenticated_repo_url() -> str:
    token = os.getenv("GITHUB_DATA_TOKEN")
    if token and DATA_REPO_URL.startswith("https://github.com/"):
        return DATA_REPO_URL.replace("https://", f"https://x-access-token:{token}@", 1)
    return DATA_REPO_URL


def slugify(value: str, book_id: int) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return f"{slug or 'novel'}-{book_id}"


def chapter_key(number: int) -> str:
    return f"{number:03d}"


def write_if_changed(path: Path, content: bytes) -> bool:
    if path.exists() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return True


def write_json(path: Path, payload: object) -> bool:
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    return write_if_changed(path, content)


def latest_audio(book_id: int, chapter_id: int, destination: Path) -> str | None:
    manifest = AUDIO_DIR / f"chapter-{chapter_id}-vi-VN-HoaiMyNeural-1_00.json"
    if not manifest.exists():
        return None
    try:
        segment_names = [item["filename"] for item in json.loads(manifest.read_text("utf-8"))["segments"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    segments = [AUDIO_DIR / name for name in segment_names]
    if not segments or not all(item.exists() and item.stat().st_size for item in segments):
        return None
    combined = b"".join(item.read_bytes() for item in segments)
    write_if_changed(destination, combined)
    return f"audio/{destination.name}"


def export_data() -> dict[str, int]:
    if not DATABASE_PATH.exists():
        raise SyncError(f"SQLite database was not found: {DATABASE_PATH}")
    output_root = DATA_REPO_DIR / "data"
    novels_root = output_root / "novels"
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    books = connection.execute("SELECT * FROM books ORDER BY id").fetchall()
    changed = chapters_written = 0
    metadata_novels: list[dict[str, object]] = []
    try:
        for book in books:
            slug = slugify(book["translated_title"] or book["title"], book["id"])
            novel_root = novels_root / slug
            chapter_rows = connection.execute(
                "SELECT * FROM chapters WHERE book_id = ? ORDER BY chapter_number", (book["id"],)
            ).fetchall()
            manifest_chapters: list[dict[str, object]] = []
            for chapter in chapter_rows:
                key = chapter_key(chapter["chapter_number"])
                has_content = bool(chapter["translated_content"] or chapter["original_content"])
                audio_url = latest_audio(book["id"], chapter["id"], novel_root / "audio" / f"{key}.mp3")
                synced = has_content
                if has_content:
                    payload = {
                        "number": chapter["chapter_number"],
                        "title": chapter["translated_title"] or chapter["title"],
                        "original_title": chapter["title"],
                        "content": chapter["translated_content"] or chapter["original_content"],
                        "translated": bool(chapter["translated_content"]),
                        "audio": audio_url,
                    }
                    if write_json(novel_root / "chapters" / f"{key}.json", payload):
                        changed += 1
                    chapters_written += 1
                manifest_chapters.append(
                    {
                        "number": chapter["chapter_number"],
                        "title": chapter["translated_title"] or chapter["title"],
                        "url": f"chapters/{key}.json" if synced else None,
                        "audio": audio_url,
                        "synced": synced,
                    }
                )
            latest = next((item["number"] for item in reversed(manifest_chapters) if item["synced"]), 0)
            manifest = {
                "slug": slug,
                "title": book["translated_title"] or book["title"],
                "author": book["translated_author"] or book["author"],
                "cover": book["cover_url"],
                "latest_chapter": latest,
                "chapters": manifest_chapters,
                "updated_at": book["updated_at"] or "",
            }
            if write_json(novel_root / "manifest.json", manifest):
                changed += 1
            metadata_novels.append({"slug": slug, "title": manifest["title"], "author": manifest["author"], "cover": manifest["cover"], "latest_chapter": latest})
    finally:
        connection.close()
    metadata_updated_at = max((book["updated_at"] or "" for book in books), default="")
    if write_json(output_root / "metadata.json", {"novels": metadata_novels, "updated_at": metadata_updated_at}):
        changed += 1
    return {"changed": changed, "chapters": chapters_written}


def prepare_repository() -> None:
    if (DATA_REPO_DIR / ".git").exists():
        try:
            run_git("rev-parse", "--verify", "HEAD", cwd=DATA_REPO_DIR)
            run_git("pull", "--ff-only", "origin", "main", cwd=DATA_REPO_DIR)
        except SyncError:
            run_git("checkout", "-b", "main", cwd=DATA_REPO_DIR)
        return
    DATA_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
    run_git("clone", authenticated_repo_url(), str(DATA_REPO_DIR))
    if not (DATA_REPO_DIR / ".git").exists():
        raise SyncError("GitHub data repository could not be cloned.")
    try:
        run_git("rev-parse", "--verify", "HEAD", cwd=DATA_REPO_DIR)
    except SyncError:
        run_git("checkout", "-b", "main", cwd=DATA_REPO_DIR)


def commit_and_push() -> bool:
    run_git("add", "data", cwd=DATA_REPO_DIR)
    if not run_git("status", "--porcelain", cwd=DATA_REPO_DIR):
        return False
    run_git("commit", "-m", "sync: publish novel data", cwd=DATA_REPO_DIR)
    for attempt in range(1, RETRIES + 1):
        try:
            run_git("push", "origin", "main", cwd=DATA_REPO_DIR)
            return True
        except SyncError:
            if attempt == RETRIES:
                raise
            run_git("pull", "--rebase", "origin", "main", cwd=DATA_REPO_DIR)
            time.sleep(attempt)
    return False


def sync_once() -> dict[str, int | bool]:
    prepare_repository()
    exported = export_data()
    pushed = commit_and_push()
    logger.info("GitHub data sync complete: %s pushed=%s", exported, pushed)
    return {**exported, "pushed": pushed}


sync_status: dict[str, object] = {"last_sync": None, "last_error": None}


def run_watch() -> None:
    interval = max(30, int(os.getenv("SYNC_INTERVAL", "60")))
    while True:
        try:
            sync_status["last_sync"] = sync_once()
            sync_status["last_error"] = None
        except SyncError as exc:
            logger.exception("Scheduled GitHub data sync failed")
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
    parser = argparse.ArgumentParser(description="Publish LeoTruyen data repository")
    parser.add_argument("command", choices=("sync", "watch"), default="sync", nargs="?")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        sync_once()
        if args.command == "sync":
            return
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
