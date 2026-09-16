import asyncio
import logging
from pathlib import Path
import threading
import uuid
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.templating import Jinja2Templates

from app.config import settings
from app.cdn import CdnDataError, cdn_url, load_cdn_json, load_chapters_index, publish_translated_chapter
from app.database import SessionLocal, create_tables, get_db
from app.crawlers.base import clean_chapter_text
from app.crawlers.bootstrap import registry
from app.crawlers.errors import CrawlerError
from app.models import Book
from app.models import Chapter, TranslationMemory
from app.search import search_books as run_search
from app.services.book_service import BookService
from app.translator.service import TranslationUnavailableError, translator
from app.tts.edge_tts_service import (
    DEFAULT_EDGE_VOICE,
    EdgeTTSUnavailableError,
    normalize_rate,
    normalize_voice,
    synthesize_chapter_playlist,
    synthesize_edge_tts,
)
from sync.service import SyncError, sync_once


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


translation_jobs: dict[str, dict[str, object]] = {}
translation_jobs_lock = threading.Lock()


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
def library(request: Request, db: Session = Depends(get_db)):
    books = db.scalars(select(Book).order_by(Book.updated_at.desc())).all()
    synced_novels = []
    if not books:
        try:
            metadata = load_cdn_json("metadata.json")
            synced_novels = metadata.get("novels", [])
        except CdnDataError:
            pass
    return templates.TemplateResponse(
        request=request, name="library.html", context={"books": books, "synced_novels": synced_novels}
    )


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
        if not current.get("translated"):
            payload = {}
            if current.get("content_path"):
                try:
                    payload = load_cdn_json(f"novels/{slug}/chapters/{key}.json")
                except CdnDataError:
                    payload = {}
            original_content = payload.get("original_content") or payload.get("content") or ""
            chapter = SimpleNamespace(
                id=f"cdn-{slug}-{chapter_number}",
                book_id=slug,
                chapter_number=chapter_number,
                title=payload.get("original_title") or current.get("title"),
                translated_title=current.get("title"),
                source_url=current.get("source_url"),
                original_content=original_content,
                translated_content="",
            )
            return templates.TemplateResponse(
                request=request,
                name="reader.html",
                context={
                    "chapter": chapter,
                    "previous": entries[position - 1] if position else None,
                    "following": entries[position + 1] if position + 1 < len(entries) else None,
                    "mode": "translated",
                    "showing_translation": False,
                    "display_title": current.get("title"),
                    "display_content": original_content,
                    "translation_error": "Chương này chưa được dịch. Hãy đồng bộ hoặc dịch trước.",
                    "original_url": current.get("source_url"),
                    "synced_reader": True,
                    "chapter_list": entries,
                    "back_url": f"/synced/{slug}",
                    "previous_url": f"/synced/{slug}/{entries[position - 1]['chapter_number']}" if position else "",
                    "following_url": f"/synced/{slug}/{entries[position + 1]['chapter_number']}" if position + 1 < len(entries) else "",
                    "audio_manifest_url": "",
                },
            )
        payload = load_cdn_json(f"novels/{slug}/chapters/{key}.json")
    except CdnDataError:
        return HTMLResponse("Chương này chưa được đồng bộ.", status_code=404)
    previous = entries[position - 1] if position else None
    following = entries[position + 1] if position + 1 < len(entries) else None
    chapter = SimpleNamespace(
        id=f"cdn-{slug}-{chapter_number}",
        book_id=slug,
        chapter_number=chapter_number,
        title=payload.get("original_title") or payload.get("title"),
        translated_title=payload.get("title"),
        source_url=current.get("source_url"),
        original_content=payload.get("original_content") or payload.get("content"),
        translated_content=payload.get("translated_content") or payload.get("content"),
    )
    return templates.TemplateResponse(
        request=request,
        name="reader.html",
        context={
            "chapter": chapter,
            "previous": previous,
            "following": following,
            "mode": "translated",
            "showing_translation": True,
            "display_title": payload.get("title"),
            "display_content": payload.get("translated_content") or payload.get("content"),
            "translation_error": None,
            "original_url": current.get("source_url"),
            "synced_reader": True,
            "chapter_list": entries,
            "back_url": f"/synced/{slug}",
            "previous_url": f"/synced/{slug}/{previous['chapter_number']}" if previous else "",
            "following_url": f"/synced/{slug}/{following['chapter_number']}" if following else "",
            "audio_manifest_url": f"/synced/{slug}/{chapter_number}/audio/manifest",
        },
    )


@app.get("/synced/{slug}/{chapter_number}/audio/manifest")
def synced_audio_manifest(slug: str, chapter_number: int):
    key = f"{chapter_number:03d}"
    try:
        chapter = load_cdn_json(f"novels/{slug}/chapters/{key}.json")
        audio_path = chapter.get("audio") or chapter.get("audio_url")
        if not audio_path:
            raise CdnDataError("No synced audio")
        return {"segments": [{"index": 0, "url": cdn_url(f"novels/{slug}/{audio_path}"), "duration": 0}]}
    except CdnDataError:
        raise HTTPException(status_code=404, detail="Chương này chưa được đồng bộ.") from None


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
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy truyện.")
    return templates.TemplateResponse(request=request, name="book_detail.html", context={"book": book})


@app.post("/books/import")
async def import_book(request: Request, url: str = Form(...), db: Session = Depends(get_db)):
    try:
        crawler = registry.get_for_url(url)
        book, chapters_added = await BookService(db, crawler).import_book(url.strip())
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
            crawler = registry.get_for_url(query)
            book, _ = await BookService(db, crawler).import_book(query)
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
    db.delete(book)
    db.commit()
    return RedirectResponse("/", status_code=303)


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
def translation_memory(request: Request, db: Session = Depends(get_db)):
    entries = db.scalars(select(TranslationMemory).order_by(TranslationMemory.source_text)).all()
    return templates.TemplateResponse(request=request, name="translation_memory.html", context={"entries": entries})


@app.post("/translation-memory")
def add_translation_memory(source_text: str = Form(...), replacement_text: str = Form(...), db: Session = Depends(get_db)):
    db.add(TranslationMemory(source_text=source_text.strip(), replacement_text=replacement_text.strip()))
    db.commit()
    return RedirectResponse("/translation-memory", status_code=303)


@app.post("/translation-memory/{entry_id}/delete")
def delete_translation_memory(entry_id: int, db: Session = Depends(get_db)):
    entry = db.get(TranslationMemory, entry_id)
    if entry:
        db.delete(entry)
        db.commit()
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
    try:
        playlist = await synthesize_chapter_playlist(
            chapter.translated_content,
            chapter_id=chapter.id,
            voice=normalize_voice(voice),
            rate=normalize_rate(rate),
        )
        return {
            "chapter_id": playlist.chapter_id,
            "voice": playlist.voice,
            "rate": playlist.rate,
            "cache_hit": playlist.cache_hit,
            "total_duration": playlist.total_duration_seconds,
            "segments": [
                {
                    "index": segment.index,
                    "url": segment.url,
                    "duration": segment.duration_seconds,
                    "cache_hit": segment.cache_hit,
                }
                for segment in playlist.segments
            ],
        }
    except EdgeTTSUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
