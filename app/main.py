from pathlib import Path
from types import SimpleNamespace

import logging

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.templating import Jinja2Templates

from app.config import settings
from app.cdn import CdnDataError, cdn_url, load_cdn_json
from app.database import create_tables, get_db
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
            synced_novels = [item for item in metadata.get("novels", []) if item.get("latest_chapter")]
        except CdnDataError:
            pass
    return templates.TemplateResponse(
        request=request, name="library.html", context={"books": books, "synced_novels": synced_novels}
    )


@app.get("/synced/{slug}/{chapter_number}")
def read_synced_chapter(slug: str, chapter_number: int, request: Request):
    key = f"{chapter_number:03d}"
    try:
        manifest = load_cdn_json(f"novels/{slug}/manifest.json")
        entries = manifest.get("chapters", [])
        position = next((index for index, item in enumerate(entries) if item.get("number") == chapter_number), None)
        if position is None or not entries[position].get("synced"):
            return HTMLResponse("Chương này chưa được đồng bộ.", status_code=404)
        payload = load_cdn_json(f"novels/{slug}/chapters/{key}.json")
    except CdnDataError:
        return HTMLResponse("Chương này chưa được đồng bộ.", status_code=404)
    previous = next((item for item in reversed(entries[:position]) if item.get("synced")), None)
    following = next((item for item in entries[position + 1 :] if item.get("synced")), None)
    chapter = SimpleNamespace(
        id=f"cdn-{slug}-{chapter_number}",
        book_id=slug,
        title=payload.get("original_title") or payload.get("title"),
        translated_title=payload.get("title"),
        original_content=payload.get("content"),
        translated_content=payload.get("content"),
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
            "display_content": payload.get("content"),
            "translation_error": None,
            "synced_reader": True,
            "back_url": "/",
            "previous_url": f"/synced/{slug}/{previous['number']}" if previous else "",
            "following_url": f"/synced/{slug}/{following['number']}" if following else "",
            "audio_manifest_url": f"/synced/{slug}/{chapter_number}/audio/manifest",
        },
    )


@app.get("/synced/{slug}/{chapter_number}/audio/manifest")
def synced_audio_manifest(slug: str, chapter_number: int):
    key = f"{chapter_number:03d}"
    try:
        chapter = load_cdn_json(f"novels/{slug}/chapters/{key}.json")
        audio_path = chapter.get("audio")
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
    elif chapter.original_content:
        chapter.original_content = clean_chapter_text(chapter.original_content)
    translation_error = None
    if not chapter.translated_content and chapter.original_content:
        try:
            chapter.translated_content = translator.translate(chapter.original_content, db)
        except TranslationUnavailableError as exc:
            translation_error = str(exc)
        except Exception:
            logger.exception("Automatic chapter translation failed for %s", chapter_id)
            translation_error = "Không thể dịch chương lúc này."
    if not chapter.translated_title:
        try:
            chapter.translated_title = translator.translate(chapter.title, db)
        except TranslationUnavailableError as exc:
            translation_error = translation_error or str(exc)
        except Exception:
            logger.exception("Automatic chapter title translation failed for %s", chapter_id)
            translation_error = translation_error or "Không thể dịch tiêu đề chương lúc này."
    db.commit()
    previous = db.scalar(select(Chapter).where(Chapter.book_id == chapter.book_id, Chapter.chapter_number < chapter.chapter_number).order_by(Chapter.chapter_number.desc()))
    following = db.scalar(select(Chapter).where(Chapter.book_id == chapter.book_id, Chapter.chapter_number > chapter.chapter_number).order_by(Chapter.chapter_number))
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
        return RedirectResponse(f"/chapters/{chapter.id}", status_code=303)
    except TranslationUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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
