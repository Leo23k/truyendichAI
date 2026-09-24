import asyncio
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import threading
import uuid
from types import SimpleNamespace

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.templating import Jinja2Templates

from app.config import settings
from app.cdn import (
    CdnDataError,
    add_synced_chapters,
    cdn_url,
    delete_synced_novel_permanently,
    delete_translation_memory as delete_supabase_translation_memory,
    get_synced_novel_for_management,
    load_cdn_json,
    load_chapters_index,
    load_deleted_synced_novels,
    load_translation_progress,
    load_translation_memory,
    publish_translated_chapter,
    save_translation_memory,
    set_synced_novel_deletion,
)
from app.database import SessionLocal, create_tables, get_db
from app.crawlers.base import clean_chapter_text
from app.crawlers.bootstrap import registry
from app.crawlers.errors import CrawlerError
from app.models import Book
from app.models import Chapter
from app.search import search_books as run_search
from app.services.book_service import BookService
from app.translator.service import TranslationUnavailableError, translator
from app.tts.edge_tts_service import (
    DEFAULT_EDGE_VOICE,
    EdgeTTSUnavailableError,
    estimate_duration_seconds,
    normalize_rate,
    normalize_voice,
    split_chapter_text,
    synthesize_chapter_playlist,
    synthesize_edge_tts,
    stream_edge_tts,
)
from sync.service import SyncError, SupabaseSync, enqueue_remote_crawl, prioritize_translation, sync_once
from app.worker_control import worker_manager


BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="My Novel Reader")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TTSRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str = DEFAULT_EDGE_VOICE
    rate: float = Field(default=1.0, ge=0.5, le=2.0)


class TranslateRequest(BaseModel):
    chapter_id: int | None = None
    source_url: str | None = None
    synced_slug: str | None = None
    synced_chapter_number: int | None = None
    synced_title: str | None = None


@app.post("/api/translation-priority/{chapter_id}")
async def request_translation_priority(chapter_id: int, priority: int = 100) -> dict[str, bool]:
    if os.getenv("VERCEL"):
        try:
            return {"updated": prioritize_translation(SupabaseSync(), chapter_id, priority)}
        except SyncError:
            return {"updated": False}
    if not settings.pc_worker_url or not settings.pc_worker_token:
        return {"updated": False}
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(
                f"{settings.pc_worker_url.rstrip('/')}/priority",
                headers={"X-Worker-Token": settings.pc_worker_token},
                json={"chapter_id": chapter_id, "priority": priority},
            )
            response.raise_for_status()
        return {"updated": bool(response.json().get("updated"))}
    except (httpx.HTTPError, ValueError, KeyError):
        return {"updated": False}


translation_jobs: dict[str, dict[str, object]] = {}
translation_jobs_lock = threading.Lock()
BOOK_RESTORE_WINDOW = timedelta(days=3)


def _sync_supabase_background() -> None:
    try:
        result = sync_once()
        logger.info("Supabase sync after crawl: %s", result)
    except SyncError as exc:
        logger.warning("Supabase sync after crawl skipped: %s", exc)
    except Exception:
        logger.exception("Supabase sync after crawl failed")


def _queue_supabase_sync() -> None:
    threading.Thread(target=_sync_supabase_background, daemon=True, name="leotruyen-sync-once").start()


def _update_translation_job(job_id: str, **values: object) -> None:
    with translation_jobs_lock:
        translation_jobs[job_id].update(values)


def _purge_expired_deleted_books(db: Session) -> None:
    expired_books = db.scalars(
        select(Book).where(Book.delete_after.is_not(None), Book.delete_after <= datetime.utcnow())
    ).all()
    if not expired_books:
        return
    for book in expired_books:
        db.delete(book)
    db.commit()


def _run_translation_job(
    job_id: str,
    chapter_id: int | None,
    source_url: str | None = None,
    synced_slug: str | None = None,
    synced_chapter_number: int | None = None,
    synced_title: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        chapter = db.get(Chapter, chapter_id) if chapter_id else None
        if chapter is None:
            if not source_url or not synced_slug or synced_chapter_number is None:
                raise RuntimeError("Chương chưa có bản gốc trên máy này.")
            _update_translation_job(job_id, status="loading", progress=20)
            try:
                payload = load_cdn_json(f"novels/{synced_slug}/chapters/{synced_chapter_number:03d}.json")
                original_content = payload.get("original_content") or payload.get("content") or ""
            except CdnDataError:
                original_content = ""
            if not original_content:
                crawler = registry.get_for_url(source_url)
                original_content = asyncio.run(crawler.get_chapter_content(source_url))
            original_content = clean_chapter_text(original_content)
            original_title = synced_title or f"Chương {synced_chapter_number}"

            _update_translation_job(job_id, status="translating", progress=45)
            translated_content = translator.translate(original_content, db)
            translated_title = translator.translate(original_title, db)

            _update_translation_job(job_id, status="saving", progress=85)
            publish_translated_chapter(
                slug=synced_slug,
                chapter_number=synced_chapter_number,
                title=translated_title,
                original_title=original_title,
                source_url=source_url,
                original_content=original_content,
                content=translated_content,
            )
            _update_translation_job(job_id, status="completed", progress=100)
            return
        if not chapter.original_content:
            raise RuntimeError("Chương chưa có bản gốc trên máy này.")

        _update_translation_job(job_id, status="translating", progress=35)
        chapter.original_content = clean_chapter_text(chapter.original_content)
        chapter.translated_content = translator.translate(chapter.original_content, db)
        if not chapter.translated_title:
            chapter.translated_title = translator.translate(chapter.title, db)

        _update_translation_job(job_id, status="saving", progress=85)
        db.commit()
        _queue_supabase_sync()
        _update_translation_job(job_id, status="completed", progress=100)
    except TranslationUnavailableError as exc:
        _update_translation_job(job_id, status="failed", progress=100, error="Translator chưa kết nối với PC.")
    except Exception as exc:
        logger.exception("Translation job %s failed", job_id)
        _update_translation_job(job_id, status="failed", progress=100, error=str(exc))
    finally:
        db.close()


@app.on_event("startup")
def startup() -> None:
    create_tables()


@app.get("/", name="library")
def library(request: Request):
    """The library is Supabase-backed in every environment."""
    deleted_synced_novels = []
    try:
        deleted_synced_novels = load_deleted_synced_novels()
    except CdnDataError:
        pass
    try:
        metadata = load_cdn_json("metadata.json")
        synced_novels = metadata.get("novels", [])
    except CdnDataError:
        synced_novels = []
    return templates.TemplateResponse(
        request=request, name="library.html", context={"books": [], "deleted_books": [], "deleted_synced_novels": deleted_synced_novels, "synced_novels": synced_novels}
    )


def _source_name(source_url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(source_url).netloc.removeprefix("www.").removeprefix("m.") or source_url


def _crawl_job_cards() -> list[dict]:
    remote = SupabaseSync()
    jobs = remote.rows("remote_crawl_jobs", {"select": "id,source_url,status,error,result,started_at,created_at,updated_at", "status": "in.(queued,crawling,syncing,translating,paused,failed)", "order": "updated_at.desc"})
    cards: list[dict] = []
    for job in jobs:
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        book_id = result.get("book_id")
        novel = remote.rows("novels", {"select": "id,slug,title", "id": f"eq.{book_id}", "limit": "1"}) if book_id else remote.rows("novels", {"select": "id,slug,title", "source_url": f"eq.{job['source_url']}", "limit": "1"})
        novel = novel[0] if novel else {}
        if novel and not book_id:
            result["book_id"] = novel.get("id")
        chapters = remote.all_rows("chapters", {"select": "id,chapter_number,translated", "novel_id": f"eq.{novel['id']}", "order": "chapter_number.desc"}) if novel else []
        queues = remote.rows("translation_queue", {"select": "status,chapter_id", "crawl_job_id": f"eq.{job['id']}"})
        states = {state: sum(1 for row in queues if row.get("status") == state) for state in ("queued", "translating", "completed", "failed", "paused")}
        if not queues and chapters:
            # Jobs created before crawl_job_id existed still own this novel by result.book_id.
            states["completed"] = sum(bool(row.get("translated")) for row in chapters)
            states["queued"] = len(chapters) - states["completed"]
        current = [row for row in chapters if row.get("id") in {queue.get("chapter_id") for queue in queues if queue.get("status") == "translating"}]
        job.update({"result": result, "title": result.get("title") or novel.get("title") or _source_name(str(job["source_url"])), "source_name": _source_name(str(job["source_url"])), "novel": novel, "crawl_total": result.get("total_raw"), "crawl_done": result.get("raw_loaded", 0), "queue": states, "current_chapters": [row.get("chapter_number") for row in current[:3]]})
        cards.append(job)
    return cards


def _worker_statuses() -> dict[str, dict[str, object]]:
    """Vercel cannot see localhost on the PC; use queue activity as a safe signal."""
    workers = worker_manager.statuses()
    if not os.getenv("VERCEL"):
        return workers
    remote = SupabaseSync()
    crawling = remote.rows("remote_crawl_jobs", {"select": "id", "status": "in.(crawling,syncing)", "limit": "1"})
    translating_jobs = remote.rows("remote_crawl_jobs", {"select": "id", "status": "eq.translating", "limit": "1"})
    translating = remote.rows("translation_queue", {"select": "id", "status": "eq.translating", "limit": "1"})
    if crawling:
        workers["crawler"] = {"name": "crawler", "state": "running", "pid": None, "managed": False, "error": None, "detail": "Đang xử lý job trên PC (theo Supabase)."}
    if translating or translating_jobs:
        workers["translator"] = {"name": "translator", "state": "running", "pid": None, "managed": False, "error": None, "detail": "Đang dịch trên PC (theo hàng đợi Supabase)."}
    return workers


@app.get("/jobs")
def crawl_jobs(request: Request):
    try:
        return templates.TemplateResponse(request=request, name="jobs.html", context={"jobs": _crawl_job_cards(), "workers": _worker_statuses()})
    except SyncError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/jobs/list")
def crawl_jobs_list(request: Request):
    return templates.TemplateResponse(request=request, name="jobs.html", context={"jobs": _crawl_job_cards(), "workers": _worker_statuses(), "partial": True})


@app.get("/jobs/workers/status")
def worker_status() -> dict[str, dict[str, object]]:
    return _worker_statuses()


@app.post("/jobs/workers/{target}/{action}")
def control_workers(target: str, action: str, request: Request):
    if target not in {"all", "crawler", "translator"} or action not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=404, detail="Thao tác worker không hợp lệ.")
    ok, message = worker_manager.act(target, action)
    response = templates.TemplateResponse(request=request, name="jobs.html", context={"jobs": _crawl_job_cards(), "workers": _worker_statuses(), "toast": message, "toast_error": not ok})
    return response


@app.post("/jobs/{job_id}/{action}")
def control_crawl_job(job_id: str, action: str, request: Request):
    if action not in {"pause", "resume", "retry", "delete"}:
        raise HTTPException(status_code=404, detail="Thao tác không hợp lệ.")
    remote = SupabaseSync()
    jobs = remote.rows("remote_crawl_jobs", {"select": "id,status,result,source_url", "id": f"eq.{job_id}", "limit": "1"})
    if not jobs:
        raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình.")
    job = jobs[0]
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if action == "delete":
        remote.patch_rows("remote_crawl_jobs", {"id": f"eq.{job_id}"}, {"status": "deleting"})
        remote.patch_rows("translation_queue", {"crawl_job_id": f"eq.{job_id}", "status": "in.(queued,translating,paused)"}, {"status": "paused"})
        book_id = result.get("book_id")
        if book_id:
            remote.delete_rows("novels", {"id": f"eq.{book_id}"})
        remote.delete_rows("remote_crawl_jobs", {"id": f"eq.{job_id}"})
        message = "Đã xóa tiến trình và dữ liệu truyện liên quan."
    elif action == "pause":
        remote.patch_rows("remote_crawl_jobs", {"id": f"eq.{job_id}", "status": "in.(queued,crawling,syncing,translating)"}, {"status": "paused"})
        remote.patch_rows("translation_queue", {"crawl_job_id": f"eq.{job_id}", "status": "eq.queued"}, {"status": "paused"})
        message = "Đã tạm dừng tiến trình."
    else:
        target = "queued" if action in {"resume", "retry"} else "queued"
        remote.patch_rows("remote_crawl_jobs", {"id": f"eq.{job_id}"}, {"status": target, "error": None})
        remote.patch_rows("translation_queue", {"crawl_job_id": f"eq.{job_id}", "status": "in.(paused,failed)"}, {"status": "queued"})
        message = "Đã tiếp tục tiến trình." if action == "resume" else "Đã khởi động lại tiến trình."
    response = templates.TemplateResponse(request=request, name="jobs.html", context={"jobs": _crawl_job_cards(), "workers": _worker_statuses(), "partial": True, "toast": message})
    response.headers["HX-Trigger"] = "jobsChanged"
    return response


@app.get("/synced/{slug}")
def synced_book(slug: str, request: Request):
    try:
        metadata = load_cdn_json("metadata.json")
        novel = next((item for item in metadata.get("novels", []) if item.get("slug") == slug), None)
        chapters = load_chapters_index(slug)
        if not novel:
            raise CdnDataError("Novel was not found")
    except CdnDataError:
        raise HTTPException(status_code=404, detail="Truyện chưa được đồng bộ.") from None
    return templates.TemplateResponse(
        request=request,
        name="synced_book_detail.html",
        context={"novel": novel, "chapters": chapters},
    )


@app.get("/synced/{slug}/{chapter_number}")
def read_synced_chapter(slug: str, chapter_number: int, request: Request):
    key = f"{chapter_number:03d}"
    try:
        entries = load_chapters_index(slug)
        position = next((index for index, item in enumerate(entries) if item.get("chapter_number") == chapter_number), None)
        if position is None:
            return HTMLResponse("Chương này chưa được đồng bộ.", status_code=404)
        current = entries[position]
        progress = load_translation_progress(int(current["id"])) if current.get("id") else None
        if not current.get("translated"):
            payload = {}
            if current.get("content_path"):
                try:
                    payload = load_cdn_json(f"novels/{slug}/chapters/{key}.json")
                except CdnDataError:
                    payload = {}
            original_content = payload.get("original_content") or payload.get("content") or ""
            translated_parts = [str(item.get("translated_text") or "Đang dịch đoạn tiếp theo...") for item in (progress or {}).get("chunks", [])]
            progressive = bool(progress and translated_parts)
            chapter = SimpleNamespace(
                id=f"cdn-{slug}-{chapter_number}",
                book_id=slug,
                chapter_number=chapter_number,
                title=payload.get("original_title") or current.get("title"),
                translated_title=current.get("title"),
                source_url=current.get("source_url"),
                original_content=original_content,
                translated_content="\n\n".join(translated_parts) if progressive else "",
            )
            return templates.TemplateResponse(
                request=request,
                name="reader.html",
                context={
                    "chapter": chapter,
                    "previous": entries[position - 1] if position else None,
                    "following": entries[position + 1] if position + 1 < len(entries) else None,
                    "mode": "translated",
                    "showing_translation": progressive,
                    "display_title": current.get("title"),
                    "display_content": chapter.translated_content if progressive else original_content,
                    "translation_error": None if progressive else "Chương này chưa được dịch. Hãy đồng bộ hoặc dịch trước.",
                    "translation_progress": progress,
                    "original_url": current.get("source_url"),
                    "synced_reader": True,
                    "chapter_list": entries,
                    "back_url": f"/synced/{slug}",
                    "previous_url": f"/synced/{slug}/{entries[position - 1]['chapter_number']}" if position else "",
                    "following_url": f"/synced/{slug}/{entries[position + 1]['chapter_number']}" if position + 1 < len(entries) else "",
                    "audio_manifest_url": f"/synced/{slug}/{chapter_number}/audio/manifest",
                    "audio_stream_url": f"/synced/{slug}/{chapter_number}/audio/tts/stream",
                    "queue_chapter_id": current.get("id"),
                },
            )
        payload = load_cdn_json(f"novels/{slug}/chapters/{key}.json")
    except CdnDataError:
        return HTMLResponse("Chương này chưa được đồng bộ.", status_code=404)
    previous = entries[position - 1] if position else None
    following = entries[position + 1] if position + 1 < len(entries) else None
    original_content = payload.get("original_content") or payload.get("content")
    translated_content = payload.get("translated_content") or payload.get("content")
    showing_translation = request.query_params.get("mode", "translated") != "original" and bool(translated_content)
    translated_title = current.get("title") or payload.get("title")
    chapter = SimpleNamespace(
        id=f"cdn-{slug}-{chapter_number}",
        book_id=slug,
        chapter_number=chapter_number,
        title=payload.get("original_title") or payload.get("title"),
        translated_title=translated_title,
        source_url=current.get("source_url"),
        original_content=original_content,
        translated_content=translated_content,
    )
    return templates.TemplateResponse(
        request=request,
        name="reader.html",
        context={
            "chapter": chapter,
            "previous": previous,
            "following": following,
            "mode": "translated" if showing_translation else "original",
            "showing_translation": showing_translation,
            "display_title": translated_title if showing_translation else chapter.title,
            "display_content": translated_content if showing_translation else original_content,
            "translation_error": None,
            "original_url": current.get("source_url"),
            "synced_reader": True,
            "chapter_list": entries,
            "back_url": f"/synced/{slug}",
            "previous_url": f"/synced/{slug}/{previous['chapter_number']}" if previous else "",
            "following_url": f"/synced/{slug}/{following['chapter_number']}" if following else "",
            "audio_manifest_url": f"/synced/{slug}/{chapter_number}/audio/manifest",
            "audio_stream_url": f"/synced/{slug}/{chapter_number}/audio/tts/stream",
            "queue_chapter_id": current.get("id"),
            "translation_progress": progress,
        },
    )


@app.post("/synced/{slug}/update")
async def update_synced_book(slug: str):
    try:
        novel = get_synced_novel_for_management(slug)
        source_url = novel.get("source_url")
        if not source_url:
            raise CdnDataError("Truyện chưa có URL nguồn để cập nhật. Hãy đồng bộ lại từ máy local.")
        enqueue_remote_crawl(str(source_url))
        return RedirectResponse(f"/synced/{slug}", status_code=303)
    except CrawlerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CdnDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/synced/{slug}/delete")
def delete_synced_book(slug: str):
    try:
        now = datetime.utcnow()
        set_synced_novel_deletion(
            slug,
            deleted_at=now.isoformat(),
            delete_after=(now + BOOK_RESTORE_WINDOW).isoformat(),
        )
        return RedirectResponse("/", status_code=303)
    except CdnDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/synced/{slug}/restore")
def restore_synced_book(slug: str):
    try:
        set_synced_novel_deletion(slug, deleted_at=None, delete_after=None)
        return RedirectResponse(f"/synced/{slug}", status_code=303)
    except CdnDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/synced/{slug}/delete-permanently")
def delete_synced_book_permanently(slug: str):
    try:
        delete_synced_novel_permanently(slug)
        return RedirectResponse("/?restore=1", status_code=303)
    except CdnDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/synced/{slug}/{chapter_number}/audio/manifest")
async def synced_audio_manifest(
    slug: str,
    chapter_number: int,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: float = 1.0,
):
    key = f"{chapter_number:03d}"
    try:
        chapter = load_cdn_json(f"novels/{slug}/chapters/{key}.json")
        entry = next(item for item in load_chapters_index(slug) if int(item["chapter_number"]) == chapter_number)
        progress = load_translation_progress(int(entry["id"])) if entry.get("id") else None
        partial_text = "\n\n".join(str(item.get("translated_text") or "") for item in (progress or {}).get("chunks", []))
        translated_content = str(chapter.get("translated_content") or chapter.get("content") or "")
        if partial_text.strip():
            translated_content = partial_text
        chunks = split_chapter_text(translated_content)
        selected_voice, selected_rate = normalize_voice(voice), normalize_rate(rate)
        return {
            "voice": selected_voice,
            "rate": selected_rate,
            "segments": [
                {"index": index, "url": f"/synced/{slug}/{chapter_number}/audio/segments/{index}?voice={selected_voice}&rate={selected_rate}", "duration": estimate_duration_seconds(chunk, selected_rate)}
                for index, chunk in enumerate(chunks)
            ],
        }
    except CdnDataError:
        raise HTTPException(status_code=404, detail="Chương này chưa được đồng bộ.") from None
    except (EdgeTTSUnavailableError, KeyError, StopIteration, TypeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Không thể tạo playlist audio.") from exc


@app.get("/synced/{slug}/{chapter_number}/audio/segments/{index}")
async def synced_audio_segment(slug: str, chapter_number: int, index: int, voice: str = DEFAULT_EDGE_VOICE, rate: float = 1.0):
    key = f"{chapter_number:03d}"
    try:
        chapter = load_cdn_json(f"novels/{slug}/chapters/{key}.json")
        entry = next(item for item in load_chapters_index(slug) if int(item["chapter_number"]) == chapter_number)
        progress = load_translation_progress(int(entry["id"])) if entry.get("id") else None
        partial_text = "\n\n".join(str(item.get("translated_text") or "") for item in (progress or {}).get("chunks", []))
        translated_content = str(chapter.get("translated_content") or chapter.get("content") or "")
        if partial_text.strip():
            translated_content = partial_text
        chunks = split_chapter_text(translated_content)
    except (CdnDataError, KeyError, StopIteration, TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Không tìm thấy đoạn audio.") from None
    if index < 0 or index >= len(chunks):
        raise HTTPException(status_code=404, detail="Không tìm thấy đoạn audio.")
    try:
        audio = await synthesize_edge_tts(chunks[index], voice=normalize_voice(voice), rate=normalize_rate(rate), chapter_id=int(entry["id"]))
        return FileResponse(audio.path, media_type="audio/mpeg", filename=audio.path.name)
    except EdgeTTSUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/synced/{slug}/{chapter_number}/audio/tts/stream")
async def stream_synced_chapter_audio(
    slug: str,
    chapter_number: int,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: float = 1.0,
) -> StreamingResponse:
    key = f"{chapter_number:03d}"
    try:
        chapter = load_cdn_json(f"novels/{slug}/chapters/{key}.json")
    except CdnDataError:
        raise HTTPException(status_code=404, detail="Chương này chưa được đồng bộ.") from None
    translated_content = str(chapter.get("translated_content") or chapter.get("content") or "")
    try:
        entry = next(item for item in load_chapters_index(slug) if int(item["chapter_number"]) == chapter_number)
        progress = load_translation_progress(int(entry["id"])) if entry.get("id") else None
        partial_text = "\n\n".join(str(item.get("translated_text") or "") for item in (progress or {}).get("chunks", []))
        if partial_text.strip():
            translated_content = partial_text
    except (CdnDataError, KeyError, StopIteration, TypeError, ValueError):
        pass
    chunks = split_chapter_text(translated_content)
    if not chunks:
        raise HTTPException(status_code=400, detail="Hãy dịch chương trước khi nghe.")
    return StreamingResponse(
        stream_edge_tts(chunks[0], voice=normalize_voice(voice), rate=normalize_rate(rate)),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/books/preview")
async def book_preview(request: Request, url: str, db: Session = Depends(get_db)):
    try:
        crawler = registry.get_for_url(url)
        info = await crawler.get_book_info(url)
        return templates.TemplateResponse(request=request, name="book_preview.html", context={"book": info})
    except CrawlerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("Book preview failed for %s", url)
        raise HTTPException(status_code=500, detail="Không thể tải thông tin truyện lúc này.") from None


@app.get("/books/search")
async def search_books(request: Request, query: str, db: Session = Depends(get_db)):
    query = query.strip()
    try:
        results = await run_search(query, db)
        return templates.TemplateResponse(request=request, name="search_results.html", context={"results": results, "query": query})
    except Exception:
        logger.exception("Book search failed for %s", query)
        raise HTTPException(status_code=500, detail="Không thể tìm kiếm truyện lúc này.") from None


@app.get("/books/{book_id}")
def book_detail(book_id: int, request: Request, db: Session = Depends(get_db)):
    _purge_expired_deleted_books(db)
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện.")
    return templates.TemplateResponse(request=request, name="book_detail.html", context={"book": book})


@app.post("/books/import")
async def import_book(request: Request, url: str = Form(...), db: Session = Depends(get_db)):
    try:
        crawler = registry.get_for_url(url)
        book, chapters_added = await BookService(db, crawler).import_book(url.strip())
        if book.deleted_at:
            book.deleted_at = None
            book.delete_after = None
            db.commit()
        _queue_supabase_sync()
        if request.headers.get("HX-Request"):
            return HTMLResponse(status_code=204, headers={"HX-Redirect": f"/books/{book.id}"})
        return {"book_id": book.id, "chapters_added": chapters_added}
    except CrawlerError as exc:
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request=request,
                name="partials/message.html",
                context={"message": str(exc), "error": True},
                status_code=400,
            )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("Book import failed for %s", url)
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request=request,
                name="partials/message.html",
                context={"message": "Không thể nhập truyện lúc này.", "error": True},
                status_code=500,
            )
        raise HTTPException(status_code=500, detail="Không thể nhập truyện lúc này.") from None


@app.post("/books/lookup")
async def lookup_books(request: Request, query: str = Form(...), db: Session = Depends(get_db)):
    query = query.strip()
    try:
        if query.startswith(("http://", "https://")):
            if os.getenv("VERCEL"):
                job = enqueue_remote_crawl(query)
                request.state.crawl_job_id = str(job["id"])
                if request.headers.get("HX-Request"):
                    return templates.TemplateResponse(request=request, name="partials/message.html", context={"message": "PC đang crawl và dịch truyện. Hãy quay lại thư viện sau ít phút.", "error": False})
                return RedirectResponse("/", status_code=303)
            crawler = registry.get_for_url(query)
            book, _ = await BookService(db, crawler).import_book(query)
            if book.deleted_at:
                book.deleted_at = None
                book.delete_after = None
                db.commit()
            _queue_supabase_sync()
            if request.headers.get("HX-Request"):
                return templates.TemplateResponse(
                    request=request,
                    name="partials/message.html",
                    context={
                        "message": f"Đã nhập truyện “{book.translated_title or book.title}” thành công.",
                        "book": book,
                        "error": False,
                    },
                )
            return RedirectResponse(f"/books/{book.id}", status_code=303)

        results = await run_search(query, db)
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request=request,
                name="partials/search_results.html",
                context={"results": results, "query": query},
            )
        return templates.TemplateResponse(request=request, name="search_results.html", context={"results": results, "query": query})
    except CrawlerError as exc:
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request=request,
                name="partials/message.html",
                context={"message": str(exc), "error": True},
                status_code=400,
            )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("Book lookup failed for %s", query)
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request=request,
                name="partials/message.html",
                context={"message": "Không thể tìm kiếm truyện lúc này.", "error": True},
                status_code=500,
            )
        raise HTTPException(status_code=500, detail="Không thể tìm kiếm truyện lúc này.") from None


@app.post("/books/{book_id}/update")
async def update_book(book_id: int, request: Request, db: Session = Depends(get_db)):
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện.")
    if book.deleted_at:
        raise HTTPException(status_code=409, detail="Hãy khôi phục truyện trước khi cập nhật.")
    try:
        crawler = registry.get_for_url(book.source_url)
        chapters_added = await BookService(db, crawler).update_book(book)
        _queue_supabase_sync()
        if request.headers.get("HX-Request"):
            return HTMLResponse(status_code=204, headers={"HX-Redirect": f"/books/{book.id}"})
        return RedirectResponse(f"/books/{book.id}", status_code=303)
    except CrawlerError as exc:
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request=request,
                name="partials/message.html",
                context={"message": str(exc), "error": True},
                status_code=400,
            )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("Book update failed for %s", book_id)
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request=request,
                name="partials/message.html",
                context={"message": "Không thể cập nhật truyện lúc này.", "error": True},
                status_code=500,
            )
        raise HTTPException(status_code=500, detail="Không thể cập nhật truyện lúc này.") from None


@app.post("/books/{book_id}/delete")
def delete_book(book_id: int, db: Session = Depends(get_db)):
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện.")
    now = datetime.utcnow()
    book.deleted_at = now
    book.delete_after = now + BOOK_RESTORE_WINDOW
    db.commit()
    return RedirectResponse(f"/books/{book.id}", status_code=303)


@app.post("/books/{book_id}/restore")
def restore_book(book_id: int, db: Session = Depends(get_db)):
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện.")
    book.deleted_at = None
    book.delete_after = None
    db.commit()
    return RedirectResponse(f"/books/{book.id}", status_code=303)


@app.post("/books/{book_id}/delete-permanently")
def delete_book_permanently(book_id: int, db: Session = Depends(get_db)):
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện.")
    db.delete(book)
    db.commit()
    return RedirectResponse("/?restore=1", status_code=303)


@app.get("/chapters/{chapter_id}")
async def read_chapter(chapter_id: int, request: Request, db: Session = Depends(get_db)):
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương.")
    if not chapter.original_content:
        crawler = registry.get_for_url(chapter.url)
        chapter.original_content = await crawler.get_chapter_content(chapter.url)
    if chapter.original_content:
        chapter.original_content = clean_chapter_text(chapter.original_content)
    translation_error = None
    db.commit()
    _queue_supabase_sync()
    previous = db.scalar(select(Chapter).where(Chapter.book_id == chapter.book_id, Chapter.chapter_number < chapter.chapter_number).order_by(Chapter.chapter_number.desc()))
    following = db.scalar(select(Chapter).where(Chapter.book_id == chapter.book_id, Chapter.chapter_number > chapter.chapter_number).order_by(Chapter.chapter_number))
    chapter_list = db.scalars(
        select(Chapter).where(Chapter.book_id == chapter.book_id).order_by(Chapter.chapter_number)
    ).all()
    mode = request.query_params.get("mode", "translated")
    showing_translation = mode != "original" and bool(chapter.translated_content)
    return templates.TemplateResponse(
        request=request,
        name="reader.html",
        context={
            "chapter": chapter,
            "previous": previous,
            "following": following,
            "mode": "translated" if showing_translation else "original",
            "showing_translation": showing_translation,
            "display_title": chapter.translated_title if showing_translation and chapter.translated_title else chapter.title,
            "display_content": chapter.translated_content if showing_translation else chapter.original_content,
            "translation_error": translation_error,
            "chapter_list": chapter_list,
        },
    )


@app.get("/chapters/{chapter_id}/edit")
def edit_chapter(chapter_id: int, request: Request, db: Session = Depends(get_db)):
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương.")
    return templates.TemplateResponse(request=request, name="edit_translation.html", context={"chapter": chapter})


@app.post("/chapters/{chapter_id}/edit")
def save_chapter_translation(chapter_id: int, translated_content: str = Form(...), db: Session = Depends(get_db)):
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương.")
    chapter.translated_content = translated_content
    db.commit()
    _queue_supabase_sync()
    return RedirectResponse(f"/chapters/{chapter.id}", status_code=303)


@app.post("/chapters/{chapter_id}/translate")
def translate_chapter(chapter_id: int, db: Session = Depends(get_db)):
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương.")
    if not chapter.original_content:
        raise HTTPException(status_code=400, detail="Hãy mở chương để tải bản gốc trước.")
    try:
        chapter.original_content = clean_chapter_text(chapter.original_content)
        chapter.translated_content = translator.translate(chapter.original_content, db)
        db.commit()
        _queue_supabase_sync()
        return RedirectResponse(f"/chapters/{chapter.id}", status_code=303)
    except TranslationUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/chapters/{chapter_id}/retranslate")
def retranslate_chapter(chapter_id: int, db: Session = Depends(get_db)):
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương.")
    if not chapter.original_content:
        raise HTTPException(status_code=400, detail="Hãy mở chương để tải bản gốc trước.")
    try:
        chapter.original_content = clean_chapter_text(chapter.original_content)
        chapter.translated_content = translator.translate(chapter.original_content, db)
        chapter.translated_title = translator.translate(chapter.title, db)
        db.commit()
        _queue_supabase_sync()
        return RedirectResponse(f"/chapters/{chapter.id}", status_code=303)
    except TranslationUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/translate")
def queue_translation(payload: TranslateRequest, db: Session = Depends(get_db)) -> dict[str, object]:
    chapter = db.get(Chapter, payload.chapter_id) if payload.chapter_id else None
    if chapter is None and payload.source_url:
        chapter = db.scalar(select(Chapter).where(Chapter.url == payload.source_url))
    if chapter is None:
        if not payload.source_url or not payload.synced_slug or payload.synced_chapter_number is None:
            raise HTTPException(
                status_code=404,
                detail="Chương này chỉ có trên CDN hoặc chưa có bản gốc trên máy.",
            )
    elif not chapter.original_content:
        raise HTTPException(status_code=409, detail="Hãy mở chương trên Local PC để tải bản gốc trước.")

    job_id = uuid.uuid4().hex
    with translation_jobs_lock:
        translation_jobs[job_id] = {"status": "queued", "progress": 5, "error": None}
    threading.Thread(
        target=_run_translation_job,
        args=(
            job_id,
            chapter.id if chapter else None,
            payload.source_url,
            payload.synced_slug,
            payload.synced_chapter_number,
            payload.synced_title,
        ),
        daemon=True,
    ).start()
    return {"job_id": job_id, **translation_jobs[job_id]}


@app.get("/translate/{job_id}")
def translation_status(job_id: str) -> dict[str, object]:
    with translation_jobs_lock:
        job = translation_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình dịch.")
        return {"job_id": job_id, **job}


@app.get("/translation-memory")
def translation_memory(request: Request):
    try:
        entries = load_translation_memory()
    except CdnDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return templates.TemplateResponse(request=request, name="translation_memory.html", context={"entries": entries})


@app.post("/translation-memory")
def add_translation_memory(source_text: str = Form(...), replacement_text: str = Form(...)):
    try:
        save_translation_memory(source_text.strip(), replacement_text.strip())
    except CdnDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse("/translation-memory", status_code=303)


@app.post("/translation-memory/{entry_id}/delete")
def delete_translation_memory(entry_id: int):
    try:
        delete_supabase_translation_memory(entry_id)
    except CdnDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse("/translation-memory", status_code=303)

@app.post("/api/tts")
async def api_tts(payload: TTSRequest) -> dict[str, str]:
    try:
        audio = await synthesize_edge_tts(
            payload.text,
            voice=payload.voice,
            rate=payload.rate,
        )
        return {"audio_url": audio.url}
    except EdgeTTSUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/tts/audio/{filename}")
def tts_audio_file(filename: str):
    path = (settings.audio_path / filename).resolve()
    audio_root = settings.audio_path.resolve()
    if audio_root not in path.parents or not path.exists() or path.suffix.lower() != ".mp3":
        raise HTTPException(status_code=404, detail="Không tìm thấy audio.")
    return FileResponse(path, media_type="audio/mpeg", filename=filename)


@app.get("/chapters/{chapter_id}/audio/manifest")
async def chapter_audio_manifest(
    chapter_id: int,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: float = 1.0,
    db: Session = Depends(get_db),
):
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="KhÃ´ng tÃ¬m tháº¥y chÆ°Æ¡ng.")
    if not chapter.translated_content:
        raise HTTPException(status_code=400, detail="HÃ£y dá»‹ch chÆ°Æ¡ng trÆ°á»›c khi sá»­ dá»¥ng chá»©c nÄƒng nghe.")
    chunks = split_chapter_text(chapter.translated_content)
    selected_voice, selected_rate = normalize_voice(voice), normalize_rate(rate)
    # This is metadata only: synthesis is intentionally deferred to each
    # segment request so opening a chapter warms only segment zero.
    return {
        "chapter_id": chapter.id,
        "voice": selected_voice,
        "rate": selected_rate,
        "segments": [
            {"index": index, "url": f"/chapters/{chapter.id}/audio/segments/{index}?voice={selected_voice}&rate={selected_rate}", "duration": estimate_duration_seconds(chunk, selected_rate)}
            for index, chunk in enumerate(chunks)
        ],
    }


@app.get("/chapters/{chapter_id}/audio/segments/{index}")
async def chapter_audio_segment(
    chapter_id: int,
    index: int,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: float = 1.0,
    db: Session = Depends(get_db),
):
    chapter = db.get(Chapter, chapter_id)
    chunks = split_chapter_text(chapter.translated_content if chapter else "")
    if not chapter or index < 0 or index >= len(chunks):
        raise HTTPException(status_code=404, detail="Không tìm thấy đoạn audio.")
    try:
        audio = await synthesize_edge_tts(chunks[index], voice=normalize_voice(voice), rate=normalize_rate(rate), chapter_id=chapter.id)
        return FileResponse(audio.path, media_type="audio/mpeg", filename=audio.path.name)
    except EdgeTTSUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/chapters/{chapter_id}/audio/tts/stream")
async def stream_chapter_audio(
    chapter_id: int,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: float = 1.0,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương.")
    chunks = split_chapter_text(chapter.translated_content or "")
    if not chunks:
        raise HTTPException(status_code=400, detail="Hãy dịch chương trước khi nghe.")
    return StreamingResponse(
        stream_edge_tts(chunks[0], voice=normalize_voice(voice), rate=normalize_rate(rate)),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/chapters/{chapter_id}/audio")
async def chapter_audio(
    chapter_id: int,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: float = 1.0,
    db: Session = Depends(get_db),
):
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương.")
    if not chapter.translated_content:
        raise HTTPException(status_code=400, detail="Hãy dịch chương trước khi sử dụng chức năng nghe.")
    try:
        playlist = await synthesize_chapter_playlist(
            chapter.translated_content,
            chapter_id=chapter.id,
            voice=normalize_voice(voice),
            rate=normalize_rate(rate),
        )
        first_segment = playlist.segments[0]
        return FileResponse(first_segment.path, media_type="audio/mpeg", filename=first_segment.path.name)
    except EdgeTTSUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/crawl-jobs/{job_id}")
def crawl_job_status(job_id: str) -> dict[str, object]:
    try:
        rows = SupabaseSync().rows(
            "remote_crawl_jobs", {"select": "status,error,result,updated_at", "id": f"eq.{job_id}", "limit": "1"}
        )
    except SyncError as exc:
        raise HTTPException(status_code=503, detail="Could not read crawl progress") from exc
    if not rows:
        raise HTTPException(status_code=404, detail="Crawl job not found")
    return rows[0]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
