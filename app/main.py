import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import re
import threading
import uuid
from types import SimpleNamespace
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from markupsafe import Markup, escape
from starlette.templating import Jinja2Templates

from app.config import settings
from app.auth import ANON, URL as AUTH_URL, pkce, refresh_session, require_authenticated, user_from_token
from app.cdn import (
    CdnDataError,
    add_synced_chapters,
    apply_replacement_rules,
    cdn_url,
    delete_synced_novel_permanently,
    delete_synced_novel_permanently_by_id,
    delete_replacement_rule,
    delete_translation_memory as delete_supabase_translation_memory,
    get_synced_novel_for_management,
    load_cdn_json,
    load_chapters_index,
    load_chapters_page,
    load_deleted_synced_novels,
    load_novels,
    load_admin_novels,
    load_synced_novel,
    load_replacement_rules,
    load_translation_progress,
    load_translation_memory,
    publish_translated_chapter,
    save_translation_memory,
    save_replacement_rule,
    set_synced_novel_deletion,
    update_admin_novel,
    update_admin_novel_source,
)
from app.database import SessionLocal, create_tables, get_db
from app.crawlers.base import clean_chapter_text
from app.crawlers.bootstrap import registry
from app.crawlers.errors import CrawlerError
from app.models import Book
from app.models import Chapter
from app.search import normalize_search_key, search_books as run_search
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


def _chapter_pagination(current: int, total: int) -> list[int | None]:
    pages = {1, total, *range(max(1, current - 2), min(total, current + 2) + 1)}
    result: list[int | None] = []
    previous = 0
    for page in sorted(pages):
        if page > previous + 1:
            result.append(None)
        result.append(page)
        previous = page
    return result


def _chapter_list_context(slug: str, page: int, sort: str) -> dict[str, object]:
    chapters, total, page = load_chapters_page(slug, page, sort)
    total_pages = max(1, (total + 49) // 50)
    return {
        "slug": slug,
        "chapters": chapters,
        "chapter_page": page,
        "chapter_sort": "desc" if sort == "desc" else "asc",
        "chapter_total": total,
        "chapter_total_pages": total_pages,
        "chapter_pages": _chapter_pagination(page, total_pages),
    }


def _synced_chapter_entry_and_payload(slug: str, chapter_number: int) -> tuple[dict, dict]:
    entry = next(item for item in load_chapters_index(slug) if int(item["chapter_number"]) == chapter_number)
    key = f"{chapter_number:03d}"
    return entry, load_cdn_json(entry.get("content_path") or f"novels/{slug}/chapters/{key}.json")

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in {"/login", "/auth/callback", "/logout", "/health", "/manifest.webmanifest", "/service-worker.js"}:
        return await call_next(request)
    access_token = request.cookies.get("sb-access-token", "")
    request.state.user = user_from_token(access_token) if access_token else None
    refreshed_session: tuple[str, str] | None = None
    if not request.state.user:
        refreshed_session = refresh_session(request.cookies.get("sb-refresh-token", ""))
    if refreshed_session:
        request.state.user = user_from_token(refreshed_session[0])
    if not request.state.user:
        if request.headers.get("HX-Request") == "true":
            return HTMLResponse(status_code=401)
        return RedirectResponse("/login", status_code=303) if request.method == "GET" else HTMLResponse(status_code=401)
    admin = path.startswith(("/admin", "/jobs", "/retranslate", "/translation-memory", "/books/preview", "/books/search")) or (request.method != "GET" and path not in {"/api/tts"})
    if admin and request.state.user.role != "admin":
        return HTMLResponse("Chỉ quản trị viên được phép.", status_code=403)
    response = await call_next(request)
    if refreshed_session:
        cookie_options = {"httponly": True, "secure": settings.app_url.startswith("https"), "samesite": "lax"}
        response.set_cookie("sb-access-token", refreshed_session[0], **cookie_options)
        response.set_cookie("sb-refresh-token", refreshed_session[1], **cookie_options)
    return response

@app.get("/manifest.webmanifest", include_in_schema=False)
def pwa_manifest():
    return FileResponse(BASE_DIR.parent / "manifest.webmanifest", media_type="application/manifest+json")

@app.get("/service-worker.js", include_in_schema=False)
def service_worker():
    return FileResponse(BASE_DIR.parent / "service-worker.js", media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})

@app.get("/login")
def login():
    if not AUTH_URL or not ANON: raise HTTPException(503, "Supabase Auth chưa được cấu hình.")
    verifier, challenge = pkce(); callback = f"{settings.app_url.rstrip('/')}/auth/callback"
    response = RedirectResponse(f"{AUTH_URL}/auth/v1/authorize?provider=google&redirect_to={callback}&code_challenge={challenge}&code_challenge_method=s256")
    response.headers["Cache-Control"] = "no-store, private"
    response.set_cookie("sb-pkce", verifier, httponly=True, secure=settings.app_url.startswith("https"), samesite="lax", max_age=600)
    return response

@app.get("/auth/callback")
def auth_callback(request: Request, code: str):
    verifier = request.cookies.get("sb-pkce", "")
    if not verifier:
        raise HTTPException(400, "Phiên đăng nhập đã hết hạn. Hãy đăng nhập lại.")
    with httpx.Client(timeout=15, trust_env=False) as c:
        result = c.post(f"{AUTH_URL}/auth/v1/token?grant_type=pkce", headers={"apikey": ANON}, json={"auth_code": code, "code_verifier": verifier})
    if not result.is_success:
        try:
            error_payload = result.json()
            error_code = str(error_payload.get("error_code") or error_payload.get("error") or error_payload.get("code") or "unknown_error")
            error_message = str(error_payload.get("msg") or error_payload.get("message") or "").replace("\n", " ")[:160]
        except ValueError:
            error_code = "invalid_auth_response"
            error_message = ""
        logger.warning("Google OAuth PKCE exchange failed: status=%s code=%s message=%s", result.status_code, error_code, error_message)
        detail = f"Không thể hoàn tất đăng nhập Google. ({error_code})"
        raise HTTPException(401, f"{detail} {error_message}".strip())
    payload = result.json()
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise HTTPException(401, "Supabase Auth did not return a valid session.")
    response = RedirectResponse("/", status_code=303)
    cookie_options = {"httponly": True, "secure": settings.app_url.startswith("https"), "samesite": "lax"}
    response.set_cookie("sb-access-token", access_token, **cookie_options)
    response.set_cookie("sb-refresh-token", refresh_token, **cookie_options)
    response.delete_cookie("sb-pkce"); return response

@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("sb-access-token")
    if token and AUTH_URL and ANON:
        try:
            with httpx.Client(timeout=10, trust_env=False) as client:
                client.post(f"{AUTH_URL}/auth/v1/logout", headers={"apikey": ANON, "Authorization": f"Bearer {token}"})
        except httpx.HTTPError:
            pass
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("sb-access-token")
    response.delete_cookie("sb-refresh-token")
    return response


class TTSRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str = DEFAULT_EDGE_VOICE
    rate: float = Field(default=1.0, ge=0.5, le=2.0)


class ReplacementRuleRequest(BaseModel):
    source_text: str = Field(min_length=1, max_length=300)
    replacement_text: str = Field(default="", max_length=300)
    rule_id: int | None = Field(default=None, ge=1)


def _replacement_context(request: Request, book_id: str) -> tuple[list[dict], str]:
    user = getattr(request.state, "user", None)
    if not user:
        return [], book_id
    try:
        return load_replacement_rules(user.id, book_id), book_id
    except CdnDataError:
        logger.exception("Could not load replacement rules")
        return [], book_id


def _replacement_text(request: Request, book_id: str, text: str | None) -> str:
    rules, _ = _replacement_context(request, book_id)
    return apply_replacement_rules(text, rules)


class TranslateRequest(BaseModel):
    chapter_id: int | None = None
    source_url: str | None = None
    synced_slug: str | None = None
    synced_chapter_number: int | None = None
    synced_title: str | None = None


@app.post("/api/books/{book_id}/replacement-rules")
def upsert_reader_replacement_rule(book_id: str, payload: ReplacementRuleRequest, request: Request):
    user = require_authenticated(request)
    source_text = payload.source_text.strip()
    replacement_text = payload.replacement_text.strip()
    if not source_text:
        raise HTTPException(status_code=422, detail="Nội dung cần thay thế không được để trống.")
    try:
        rule = save_replacement_rule(user.id, book_id, source_text, replacement_text, payload.rule_id)
        return {"rule": rule, "rules": load_replacement_rules(user.id, book_id)}
    except CdnDataError as exc:
        raise HTTPException(status_code=503, detail="Không thể lưu quy tắc thay thế.") from exc


@app.delete("/api/books/{book_id}/replacement-rules/{rule_id}")
def remove_reader_replacement_rule(book_id: str, rule_id: int, request: Request):
    user = require_authenticated(request)
    try:
        delete_replacement_rule(user.id, book_id, rule_id)
        return {"rules": load_replacement_rules(user.id, book_id)}
    except CdnDataError as exc:
        raise HTTPException(status_code=503, detail="Không thể xóa quy tắc thay thế.") from exc


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
        synced_novels = load_novels()
    except CdnDataError:
        synced_novels = []
    return templates.TemplateResponse(
        request=request, name="library.html", context={"books": [], "deleted_books": [], "deleted_synced_novels": deleted_synced_novels, "synced_novels": synced_novels}
    )


def _source_name(source_url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(source_url).netloc.removeprefix("www.").removeprefix("m.") or source_url


def _gmt7(value: object) -> str:
    try:
        instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return instant.astimezone(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M:%S GMT+7")
    except (TypeError, ValueError):
        return "Chưa có dữ liệu"


def _crawl_job_cards() -> list[dict]:
    remote = SupabaseSync()
    # The crawler and translator are separate workers. A crawl job can therefore
    # reach `completed` while rows it owns are still waiting in translation_queue.
    # Keep those cards visible until their translation work has also finished.
    jobs = remote.rows("remote_crawl_jobs", {"select": "id,source_url,source_type,status,error,result,started_at,created_at,updated_at", "status": "in.(queued,crawling,syncing,translating,paused,completed,failed)", "order": "updated_at.desc"})
    job_ids = [str(job["id"]) for job in jobs]
    all_queues = remote.rows(
        "translation_queue",
        {
            "select": "crawl_job_id,status,chapter_id",
            "crawl_job_id": f"in.({','.join(job_ids)})",
            "status": "in.(queued,translating,paused,failed)",
            "limit": "1000",
        },
    ) if job_ids else []
    queues_by_job: dict[str, list[dict]] = {}
    for queue in all_queues:
        queues_by_job.setdefault(str(queue.get("crawl_job_id")), []).append(queue)
    visible_jobs = [job for job in jobs if job.get("status") != "completed" or queues_by_job.get(str(job["id"]))]
    book_ids = sorted({int(result["book_id"]) for job in visible_jobs if isinstance((result := job.get("result")), dict) and str(result.get("book_id") or "").isdigit()})
    novels = remote.all_rows(
        "novels",
        {"select": "id,slug,title", "id": f"in.({','.join(str(book_id) for book_id in book_ids)})"},
    ) if book_ids else []
    novel_by_id = {int(novel["id"]): novel for novel in novels}
    translating_ids = sorted({int(queue["chapter_id"]) for queue in all_queues if queue.get("status") == "translating" and str(queue.get("chapter_id") or "").isdigit()})
    translating_chapters = remote.all_rows(
        "chapters",
        {"select": "id,chapter_number", "id": f"in.({','.join(str(chapter_id) for chapter_id in translating_ids)})"},
    ) if translating_ids else []
    chapter_number_by_id = {int(chapter["id"]): chapter.get("chapter_number") for chapter in translating_chapters}

    cards: list[dict] = []
    for job in visible_jobs:
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        book_id = result.get("book_id")
        novel = novel_by_id.get(int(book_id)) if str(book_id or "").isdigit() else {}
        queues = queues_by_job.get(str(job["id"]), [])
        states = {state: sum(1 for row in queues if row.get("status") == state) for state in ("queued", "translating", "completed", "failed", "paused")}
        states["completed"] = int(result.get("translated") or 0)
        has_pending_translation = bool(states["queued"] or states["translating"])
        if job.get("status") == "completed" and not has_pending_translation:
            continue
        current = [
            chapter_number_by_id.get(int(queue["chapter_id"]))
            for queue in queues
            if queue.get("status") == "translating" and str(queue.get("chapter_id") or "").isdigit()
        ]
        display_status = "translating" if job.get("status") == "completed" and has_pending_translation else job.get("status")
        source_type = job.get("source_type") or "zh"
        job.update({"result": result, "title": result.get("title") or novel.get("title") or _source_name(str(job["source_url"])), "source_name": f"{_source_name(str(job['source_url']))} · {'Nguồn Việt' if source_type == 'vi' else 'Nguồn Trung'}", "source_type": source_type, "novel": novel, "crawl_total": result.get("total_raw"), "crawl_done": result.get("raw_loaded", 0), "queue": states, "current_chapters": [number for number in current[:3] if number is not None], "display_status": display_status, "updated_display": _gmt7(job.get("updated_at"))})
        cards.append(job)
    return cards


def _translation_queue_cards() -> list[dict]:
    """Return unfinished translation work independently of its crawl job."""
    remote = SupabaseSync()
    queue = remote.rows(
        "translation_queue",
        {
            "select": "id,chapter_id,status,current_chunk,total_chunks,updated_at",
            "status": "in.(queued,translating,paused,failed)",
            "order": "updated_at.desc",
            "limit": "100",
        },
    )
    if not queue:
        return []
    chapter_ids = ",".join(str(row["chapter_id"]) for row in queue)
    chapters = remote.all_rows(
        "chapters",
        {"select": "id,novel_id,chapter_number,title", "id": f"in.({chapter_ids})"},
    )
    chapter_by_id = {row["id"]: row for row in chapters}
    novel_ids = sorted({row["novel_id"] for row in chapters})
    novels = remote.all_rows(
        "novels",
        {"select": "id,slug,title", "id": f"in.({','.join(str(novel_id) for novel_id in novel_ids)})"},
    ) if novel_ids else []
    novel_by_id = {row["id"]: row for row in novels}
    cards: list[dict] = []
    for item in queue:
        chapter = chapter_by_id.get(item["chapter_id"], {})
        novel = novel_by_id.get(chapter.get("novel_id"), {})
        item.update({
            "chapter_number": chapter.get("chapter_number"),
            "chapter_title": chapter.get("title") or "Chương đang dịch",
            "novel_title": novel.get("title") or "Truyện không xác định",
            "novel_slug": novel.get("slug"),
            "updated_display": _gmt7(item.get("updated_at")),
        })
        cards.append(item)
    return cards


def _jobs_page_context(**extra: object) -> dict[str, object]:
    return {
        "jobs": _crawl_job_cards(),
        "translation_queue": _translation_queue_cards(),
        "workers": _worker_statuses(),
        "worker_controls_available": worker_manager.can_control_locally,
        "crawler_sources": [crawler.source_name for crawler in registry._crawlers],
        **extra,
    }


def _retranslate_page_context(**extra: object) -> dict[str, object]:
    return {
        "retranslate_books": SupabaseSync().all_rows(
            "novels", {"select": "id,title", "deleted_at": "is.null", "order": "title.asc"}
        ),
        **extra,
    }


@app.get("/admin", include_in_schema=False)
def admin_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "crawler_sources": [crawler.source_name for crawler in registry._crawlers],
            "toast": request.query_params.get("toast"),
            "toast_error": request.query_params.get("error") == "1",
        },
    )


@app.get("/admin/novels", include_in_schema=False)
def admin_novels_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="admin_novels.html",
        context={"novels": load_admin_novels(), "toast": request.query_params.get("toast"), "toast_error": request.query_params.get("error") == "1"},
    )


@app.post("/admin/novels/{novel_id}", include_in_schema=False)
def update_admin_novel_page(
    novel_id: int,
    slug: str = Form(...),
    title: str = Form(...),
    author: str = Form(""),
    description: str = Form(""),
    cover_url: str = Form(""),
    genres: str = Form(""),
    reading_status: str = Form("ongoing"),
):
    cleaned_slug = slug.strip().lower()
    cleaned_title = title.strip()
    if not cleaned_title or reading_status not in {"ongoing", "completed"} or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", cleaned_slug):
        return RedirectResponse("/admin/novels?toast=Slug%20chỉ%20gồm%20chữ%20thường,%20số%20và%20dấu%20gạch%20ngang.&error=1", status_code=303)
    try:
        update_admin_novel(
            novel_id,
            slug=cleaned_slug,
            title=cleaned_title,
            author=author.strip(),
            description=description.strip(),
            cover_url=cover_url.strip(),
            genres=sorted({item.strip() for item in genres.split(",") if item.strip()}),
            reading_status=reading_status,
        )
    except CdnDataError as exc:
        return RedirectResponse(f"/admin/novels?toast={quote(str(exc))}&error=1", status_code=303)
    return RedirectResponse("/admin/novels?toast=Đã%20lưu%20thông%20tin%20truyện.", status_code=303)


@app.post("/admin/novels/{novel_id}/source", include_in_schema=False)
def update_admin_novel_source_page(novel_id: int, source_url: str = Form(...)):
    source_url = source_url.strip()
    try:
        if not source_url:
            raise CdnDataError("URL nguồn không được để trống.")
        novel = next((item for item in load_admin_novels() if int(item["id"]) == novel_id), None)
        if not novel:
            raise CdnDataError("Không tìm thấy truyện.")
        crawler = registry.get_for_url(source_url)
        source_type = str(novel.get("source_type") or "zh")
        if getattr(crawler, "source_type", "zh") != source_type:
            raise CdnDataError("Loại nguồn không khớp với truyện này.")
        update_admin_novel_source(novel_id, source_url)
        enqueue_remote_crawl(
            source_url,
            source_type=source_type,
            result={"source_update_novel_id": novel_id, "source_update": True},
        )
    except (CdnDataError, CrawlerError, SyncError) as exc:
        return RedirectResponse(f"/admin/novels?toast={quote(str(exc))}&error=1", status_code=303)
    return RedirectResponse("/admin/novels?toast=Đã%20cập%20nhật%20nguồn%20và%20đưa%20vào%20hàng%20đợi%20crawl.", status_code=303)


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
        return templates.TemplateResponse(request=request, name="jobs.html", context=_jobs_page_context())
    except SyncError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/jobs/list")
def crawl_jobs_list(request: Request):
    response = templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context=_jobs_page_context(partial=True),
    )
    # This endpoint is polled by HTMX.  Without an explicit no-store response,
    # a CDN/browser may repeatedly serve the same job snapshot until a full reload.
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/retranslate")
def retranslate_page(request: Request):
    return templates.TemplateResponse(request=request, name="retranslate.html", context=_retranslate_page_context())


@app.get("/retranslate/chapters", include_in_schema=False)
def retranslate_chapter_picker(request: Request, novel_id: int):
    chapters = SupabaseSync().all_rows(
        "chapters",
        {"select": "id,chapter_number,title", "novel_id": f"eq.{novel_id}", "order": "chapter_number.asc"},
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/retranslate_chapters.html",
        context={"chapters": chapters},
    )


@app.post("/retranslate", include_in_schema=False)
def retranslate_novel(
    request: Request,
    novel_id: int = Form(...),
    mode: str = Form("all"),
    chapter_ids: list[int] = Form(default=[]),
):
    if mode not in {"all", "selected"}:
        raise HTTPException(status_code=400, detail="Tùy chọn dịch lại không hợp lệ.")
    if mode == "selected" and not chapter_ids:
        return templates.TemplateResponse(
            request=request, name="retranslate.html", context=_retranslate_page_context(toast="Hãy chọn ít nhất một chương để dịch lại.", toast_error=True)
        )

    selected_ids = sorted(set(chapter_ids)) if mode == "selected" else None
    try:
        result = SupabaseSync().rpc_rows(
            "reset_retranslation",
            {"p_novel_id": novel_id, "p_chapter_ids": selected_ids},
        )
    except SyncError as exc:
        logger.exception("Could not reset retranslation queue for novel %s", novel_id)
        return templates.TemplateResponse(
            request=request,
            name="retranslate.html",
            context=_retranslate_page_context(toast=f"Chưa thể đưa truyện vào hàng đợi: {exc}", toast_error=True),
        )

    state = result[0] if result else {}
    chapter_count = int(state.get("chapter_count") or 0)
    if not chapter_count:
        return templates.TemplateResponse(
            request=request, name="retranslate.html", context=_retranslate_page_context(toast="Không tìm thấy chương phù hợp của truyện này.", toast_error=True)
        )
    if state.get("blocked"):
        return templates.TemplateResponse(
            request=request,
            name="retranslate.html",
            context=_retranslate_page_context(toast="Có chương đang được translator xử lý. Hãy đợi xong hoặc dừng worker rồi thử lại để tránh ghi đè bản dịch.", toast_error=True),
        )

    message = f"Đã xóa bản dịch cũ và đưa {chapter_count} chương vào hàng đợi dịch lại từ raw gốc."
    response = templates.TemplateResponse(request=request, name="retranslate.html", context=_retranslate_page_context(toast=message))
    response.headers["HX-Trigger"] = "jobsChanged"
    return response


@app.get("/jobs/workers/status")
def worker_status() -> dict[str, dict[str, object]]:
    return _worker_statuses()


@app.post("/jobs/workers/{target}/{action}")
def control_workers(target: str, action: str, request: Request):
    if target not in {"all", "crawler", "translator"} or action not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=404, detail="Thao tác worker không hợp lệ.")
    ok, message = worker_manager.act(target, action)
    response = templates.TemplateResponse(request=request, name="jobs.html", context=_jobs_page_context(toast=message, toast_error=not ok))
    return response


@app.post("/jobs/crawler/search")
async def jobs_crawler_search(request: Request, query: str = Form(...), source_type: str = Form("zh")):
    query = " ".join(query.split())
    if source_type not in {"zh", "vi"}:
        raise HTTPException(status_code=400, detail="Loại nguồn không hợp lệ.")
    if not query:
        return templates.TemplateResponse(request=request, name="partials/message.html", context={"message": "Hãy nhập tên truyện hoặc URL nguồn để tìm.", "error": True}, status_code=400)
    try:
        if query.startswith(("http://", "https://")):
            # A URL submitted from the worker page must be persisted first.
            # Fetching the source here makes Vercel wait on a third-party site
            # and leaves the reader with only a spinner when that request stalls.
            # The PC worker owns the outbound crawl and reports its own outcome.
            crawler = registry.get_for_url(query)
            if getattr(crawler, "source_type", "zh") != source_type:
                return templates.TemplateResponse(request=request, name="partials/message.html", context={"message": "Loại nguồn đã chọn không khớp website này.", "error": True}, status_code=400)
            job = enqueue_remote_crawl(query, source_type=source_type)
            request.state.crawl_job_id = str(job["id"])
            response = templates.TemplateResponse(
                request=request,
                name="partials/message.html",
                context={
                    "message": "Đã thêm truyện vào hàng đợi crawl. PC worker sẽ bắt đầu xử lý và trạng thái sẽ hiện bên dưới.",
                    "error": False,
                },
            )
            response.headers["HX-Trigger"] = "jobsChanged"
            return response

        results = await asyncio.wait_for(registry.search(query), timeout=min(settings.crawler_timeout, 12))
        return templates.TemplateResponse(request=request, name="partials/search_results.html", context={"results": results[:20], "query": query})
    except asyncio.TimeoutError:
        return templates.TemplateResponse(request=request, name="partials/message.html", context={"message": "Tìm nguồn quá lâu (12 giây). Website nguồn có thể đang chặn hoặc chậm; hãy thử lại sau.", "error": True}, status_code=504)
    except CrawlerError as exc:
        return templates.TemplateResponse(request=request, name="partials/message.html", context={"message": str(exc), "error": True}, status_code=400)
    except SyncError as exc:
        logger.exception("Could not enqueue crawler job query=%r", query)
        return templates.TemplateResponse(request=request, name="partials/message.html", context={"message": f"Không thể tạo job crawl: {exc}", "error": True}, status_code=503)
    except Exception:
        logger.exception("Crawler search failed query=%r", query)
        return templates.TemplateResponse(request=request, name="partials/message.html", context={"message": "Không thể tìm truyện lúc này. Vui lòng thử lại sau.", "error": True}, status_code=502)


@app.post("/jobs/translations/{queue_id}/retry")
def retry_failed_translation(queue_id: str, request: Request):
    remote = SupabaseSync()
    rows = remote.rows(
        "translation_queue",
        {"select": "id,chapter_id", "id": f"eq.{queue_id}", "status": "eq.failed", "limit": "1"},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương dịch bị lỗi.")
    queue = rows[0]
    now = datetime.now(timezone.utc).isoformat()
    remote.delete_rows("chapter_chunks", {"chapter_id": f"eq.{queue['chapter_id']}"})
    remote.patch_rows(
        "translation_queue",
        {"id": f"eq.{queue_id}", "status": "eq.failed"},
        {"status": "queued", "current_chunk": 0, "total_chunks": 0, "updated_at": now},
    )
    response = templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context=_jobs_page_context(partial=True, toast="Đã đưa chương lỗi vào hàng đợi dịch lại."),
    )
    response.headers["HX-Trigger"] = "jobsChanged"
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
            delete_synced_novel_permanently_by_id(int(book_id))
        remote.delete_rows("remote_crawl_jobs", {"id": f"eq.{job_id}"})
        message = "Đã xóa tiến trình và dữ liệu truyện liên quan."
    elif action == "pause":
        remote.patch_rows("remote_crawl_jobs", {"id": f"eq.{job_id}", "status": "in.(queued,crawling,syncing,translating,completed)"}, {"status": "paused"})
        remote.patch_rows("translation_queue", {"crawl_job_id": f"eq.{job_id}", "status": "eq.queued"}, {"status": "paused"})
        message = "Đã tạm dừng tiến trình."
    else:
        target = "queued" if action in {"resume", "retry"} else "queued"
        remote.patch_rows("remote_crawl_jobs", {"id": f"eq.{job_id}"}, {"status": target, "error": None})
        remote.patch_rows("translation_queue", {"crawl_job_id": f"eq.{job_id}", "status": "in.(paused,failed)"}, {"status": "queued"})
        message = "Đã tiếp tục tiến trình." if action == "resume" else "Đã khởi động lại tiến trình."
    response = templates.TemplateResponse(request=request, name="jobs.html", context=_jobs_page_context(partial=True, toast=message))
    response.headers["HX-Trigger"] = "jobsChanged"
    return response


@app.get("/truyen/{slug}")
@app.get("/synced/{slug}", include_in_schema=False)
def synced_book(slug: str, request: Request, chapter_page: int = 1, chapter_sort: str = "asc"):
    try:
        novel = load_synced_novel(slug)
        chapter_context = _chapter_list_context(slug, chapter_page, chapter_sort)
    except CdnDataError:
        raise HTTPException(status_code=404, detail="Truyện chưa được đồng bộ.") from None
    return templates.TemplateResponse(
        request=request,
        name="synced_book_detail.html",
        context={"novel": novel, **chapter_context},
    )


@app.get("/truyen/{slug}/chapters", include_in_schema=False)
def synced_chapter_page(slug: str, request: Request, chapter_page: int = 1, chapter_sort: str = "asc"):
    try:
        context = _chapter_list_context(slug, chapter_page, chapter_sort)
    except CdnDataError:
        raise HTTPException(status_code=404, detail="Truyện chưa được đồng bộ.") from None
    response = templates.TemplateResponse(request=request, name="partials/synced_chapter_items.html", context=context)
    response.headers["HX-Push-Url"] = f"/truyen/{slug}?chapter_page={context['chapter_page']}&chapter_sort={context['chapter_sort']}"
    return response


@app.get("/truyen/{slug}/offline-chapters", include_in_schema=False)
def synced_offline_chapters(slug: str):
    chapters = load_chapters_index(slug)
    return JSONResponse([{"id": item.get("id"), "title": item.get("title"), "chapter_number": item.get("chapter_number"), "url": f"/truyen/{slug}/{item.get('chapter_number')}"} for item in chapters])


@app.get("/truyen/{slug}/{chapter_number}")
@app.get("/synced/{slug}/{chapter_number}", include_in_schema=False)
def read_synced_chapter(slug: str, chapter_number: int, request: Request):
    key = f"{chapter_number:03d}"
    try:
        entries = load_chapters_index(slug)
        position = next((index for index, item in enumerate(entries) if int(item.get("chapter_number", 0)) == chapter_number), None)
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
            replacement_rules, replacement_book_id = _replacement_context(request, slug)
            raw_display_content = chapter.translated_content if progressive else original_content
            display_content = apply_replacement_rules(raw_display_content, replacement_rules)
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
                    "display_content": display_content,
                    "raw_display_content": raw_display_content,
                    "replacement_rules": replacement_rules,
                    "replacement_book_id": replacement_book_id,
                    "tts_chunks": split_chapter_text(display_content) if progressive else [],
                    "translation_error": None if progressive else "Chương này chưa được dịch. Hãy đồng bộ hoặc dịch trước.",
                    "translation_progress": progress,
                    "original_url": current.get("source_url"),
                    "synced_reader": True,
                    "chapter_list": entries,
                    "back_url": f"/truyen/{slug}",
                    "previous_url": f"/truyen/{slug}/{entries[position - 1]['chapter_number']}" if position else "",
                    "following_url": f"/truyen/{slug}/{entries[position + 1]['chapter_number']}" if position + 1 < len(entries) else "",
                    "audio_manifest_url": f"/truyen/{slug}/{chapter_number}/audio/manifest",
                    "audio_stream_url": f"/truyen/{slug}/{chapter_number}/audio/tts/stream",
                    "queue_chapter_id": current.get("id"),
                },
            )
        payload = load_cdn_json(current.get("content_path") or f"novels/{slug}/chapters/{key}.json")
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
    replacement_rules, replacement_book_id = _replacement_context(request, slug)
    raw_display_content = translated_content if showing_translation else original_content
    display_content = apply_replacement_rules(raw_display_content, replacement_rules)
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
            "display_content": display_content,
            "raw_display_content": raw_display_content,
            "replacement_rules": replacement_rules,
            "replacement_book_id": replacement_book_id,
            "tts_chunks": split_chapter_text(display_content) if showing_translation else [],
            "translation_error": None,
            "original_url": current.get("source_url"),
            "synced_reader": True,
            "chapter_list": entries,
            "back_url": f"/truyen/{slug}",
            "previous_url": f"/truyen/{slug}/{previous['chapter_number']}" if previous else "",
            "following_url": f"/truyen/{slug}/{following['chapter_number']}" if following else "",
            "audio_manifest_url": f"/truyen/{slug}/{chapter_number}/audio/manifest",
            "audio_stream_url": f"/truyen/{slug}/{chapter_number}/audio/tts/stream",
            "queue_chapter_id": current.get("id"),
            "translation_progress": progress,
        },
    )


@app.post("/truyen/{slug}/update")
@app.post("/synced/{slug}/update", include_in_schema=False)
async def update_synced_book(slug: str):
    try:
        novel = get_synced_novel_for_management(slug)
        source_url = novel.get("source_url")
        if not source_url:
            raise CdnDataError("Truyện chưa có URL nguồn để cập nhật. Hãy đồng bộ lại từ máy local.")
        enqueue_remote_crawl(str(source_url))
        return RedirectResponse(f"/truyen/{slug}", status_code=303)
    except CrawlerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CdnDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/truyen/{slug}/delete")
@app.post("/synced/{slug}/delete", include_in_schema=False)
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


@app.post("/truyen/{slug}/restore")
@app.post("/synced/{slug}/restore", include_in_schema=False)
def restore_synced_book(slug: str):
    try:
        set_synced_novel_deletion(slug, deleted_at=None, delete_after=None)
        return RedirectResponse(f"/truyen/{slug}", status_code=303)
    except CdnDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/truyen/{slug}/delete-permanently")
@app.post("/synced/{slug}/delete-permanently", include_in_schema=False)
def delete_synced_book_permanently(slug: str):
    try:
        delete_synced_novel_permanently(slug)
        return RedirectResponse("/?restore=1", status_code=303)
    except CdnDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/truyen/{slug}/{chapter_number}/audio/manifest")
@app.get("/synced/{slug}/{chapter_number}/audio/manifest", include_in_schema=False)
async def synced_audio_manifest(
    slug: str,
    chapter_number: int,
    request: Request,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: float = 1.0,
):
    try:
        entry, chapter = _synced_chapter_entry_and_payload(slug, chapter_number)
        progress = load_translation_progress(int(entry["id"])) if entry.get("id") else None
        partial_text = "\n\n".join(str(item.get("translated_text") or "") for item in (progress or {}).get("chunks", []))
        translated_content = str(chapter.get("translated_content") or chapter.get("content") or "")
        if partial_text.strip():
            translated_content = partial_text
        chunks = split_chapter_text(_replacement_text(request, slug, translated_content))
        selected_voice, selected_rate = normalize_voice(voice), normalize_rate(rate)
        return {
            "voice": selected_voice,
            "rate": selected_rate,
            "segments": [
                {"index": index, "url": f"/truyen/{slug}/{chapter_number}/audio/segments/{index}?voice={selected_voice}&rate={selected_rate}", "duration": estimate_duration_seconds(chunk, selected_rate)}
                for index, chunk in enumerate(chunks)
            ],
        }
    except CdnDataError:
        raise HTTPException(status_code=404, detail="Chương này chưa được đồng bộ.") from None
    except (EdgeTTSUnavailableError, KeyError, StopIteration, TypeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Không thể tạo playlist audio.") from exc


@app.get("/truyen/{slug}/{chapter_number}/audio/segments/{index}")
@app.get("/synced/{slug}/{chapter_number}/audio/segments/{index}", include_in_schema=False)
async def synced_audio_segment(slug: str, chapter_number: int, index: int, request: Request, voice: str = DEFAULT_EDGE_VOICE, rate: float = 1.0):
    try:
        entry, chapter = _synced_chapter_entry_and_payload(slug, chapter_number)
        progress = load_translation_progress(int(entry["id"])) if entry.get("id") else None
        partial_text = "\n\n".join(str(item.get("translated_text") or "") for item in (progress or {}).get("chunks", []))
        translated_content = str(chapter.get("translated_content") or chapter.get("content") or "")
        if partial_text.strip():
            translated_content = partial_text
        chunks = split_chapter_text(_replacement_text(request, slug, translated_content))
    except (CdnDataError, KeyError, StopIteration, TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Không tìm thấy đoạn audio.") from None
    if index < 0 or index >= len(chunks):
        raise HTTPException(status_code=404, detail="Không tìm thấy đoạn audio.")
    try:
        audio = await synthesize_edge_tts(chunks[index], voice=normalize_voice(voice), rate=normalize_rate(rate), chapter_id=int(entry["id"]))
        return FileResponse(audio.path, media_type="audio/mpeg", filename=audio.path.name)
    except EdgeTTSUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/truyen/{slug}/{chapter_number}/audio/tts/stream")
@app.get("/synced/{slug}/{chapter_number}/audio/tts/stream", include_in_schema=False)
async def stream_synced_chapter_audio(
    slug: str,
    chapter_number: int,
    request: Request,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: float = 1.0,
) -> StreamingResponse:
    try:
        entry, chapter = _synced_chapter_entry_and_payload(slug, chapter_number)
    except CdnDataError:
        raise HTTPException(status_code=404, detail="Chương này chưa được đồng bộ.") from None
    translated_content = str(chapter.get("translated_content") or chapter.get("content") or "")
    try:
        progress = load_translation_progress(int(entry["id"])) if entry.get("id") else None
        partial_text = "\n\n".join(str(item.get("translated_text") or "") for item in (progress or {}).get("chunks", []))
        if partial_text.strip():
            translated_content = partial_text
    except (CdnDataError, KeyError, StopIteration, TypeError, ValueError):
        pass
    chunks = split_chapter_text(_replacement_text(request, slug, translated_content))
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


@app.get("/search")
def library_search(request: Request, query: str = ""):
    query = " ".join(query.split())
    if not query:
        return HTMLResponse("")
    try:
        escaped = query.replace("%", r"\%").replace("_", r"\_")
        remote = SupabaseSync()
        fields = "slug,title,author,cover_url,latest_chapter"
        rows = remote.rows("novels", {"select": fields, "or": f"(title.ilike.*{escaped}*,author.ilike.*{escaped}*)", "limit": "20"})
        # PostgREST ilike is accent-sensitive. Supplement its fast match with
        # normalized rows only when the database query found no match.
        normalized = normalize_search_key(query)
        tokens = normalized.split()
        def matches(row: dict) -> bool:
            title = normalize_search_key(str(row.get("title") or ""))
            author = normalize_search_key(str(row.get("author") or ""))
            return normalized in title or normalized in author or all(token in title for token in tokens) or all(token in author for token in tokens)

        rows = [row for row in rows if matches(row)]
        if not rows:
            for row in remote.all_rows("novels", {"select": fields}):
                if matches(row):
                    rows.append(row)
    except SyncError as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    def rank(row: dict) -> tuple[int, int, str]:
        title = normalize_search_key(str(row.get("title") or ""))
        author = normalize_search_key(str(row.get("author") or ""))
        title_has_tokens = all(token in title for token in tokens)
        if title == normalized:
            return (0, len(title), title)
        if title.startswith(normalized):
            return (1, len(title), title)
        if normalized in title and title_has_tokens:
            return (2, len(title), title)
        if title_has_tokens:
            return (3, len(title), title)
        if normalized in author or all(token in author for token in tokens):
            return (4, len(author), title)
        return (5, len(title), title)

    rows.sort(key=rank)
    for row in rows[:20]:
        title = str(row.get("title") or "")
        start = title.casefold().find(query.casefold())
        if start >= 0:
            row["title_highlight"] = Markup(f"{escape(title[:start])}<mark class=\"rounded bg-teal-100 px-0.5 text-inherit\">{escape(title[start:start + len(query)])}</mark>{escape(title[start + len(query):])}")
        else:
            row["title_highlight"] = escape(title)
    return templates.TemplateResponse(request=request, name="partials/library_search_results.html", context={"results": rows[:20], "query": query})


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
                # Validate the source before creating a job so a blocked or
                # unsupported site is reported to the reader immediately.
                crawler = registry.get_for_url(query)
                book_info = await crawler.get_book_info(query)
                chapters = await crawler.get_chapters(query)
                if not chapters:
                    raise CrawlerError("Không tìm thấy chương nào từ website nguồn.")

                job = enqueue_remote_crawl(book_info.source_url or query)
                request.state.crawl_job_id = str(job["id"])
                if request.headers.get("HX-Request"):
                    return templates.TemplateResponse(
                        request=request,
                        name="partials/message.html",
                        context={
                            "message": (
                                f"Đã kiểm tra nguồn “{book_info.title}”: tìm thấy "
                                f"{len(chapters)} chương. PC đang crawl và dịch truyện."
                            ),
                            "error": False,
                        },
                    )
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
    replacement_rules, replacement_book_id = _replacement_context(request, str(chapter.book_id))
    raw_display_content = chapter.translated_content if showing_translation else chapter.original_content
    display_content = apply_replacement_rules(raw_display_content, replacement_rules)
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
            "display_content": display_content,
            "raw_display_content": raw_display_content,
            "replacement_rules": replacement_rules,
            "replacement_book_id": replacement_book_id,
            "tts_chunks": split_chapter_text(display_content) if showing_translation else [],
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
    request: Request,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: float = 1.0,
    db: Session = Depends(get_db),
):
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="KhÃ´ng tÃ¬m tháº¥y chÆ°Æ¡ng.")
    if not chapter.translated_content:
        raise HTTPException(status_code=400, detail="HÃ£y dá»‹ch chÆ°Æ¡ng trÆ°á»›c khi sá»­ dá»¥ng chá»©c nÄƒng nghe.")
    chunks = split_chapter_text(_replacement_text(request, str(chapter.book_id), chapter.translated_content))
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
    request: Request,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: float = 1.0,
    db: Session = Depends(get_db),
):
    chapter = db.get(Chapter, chapter_id)
    chunks = split_chapter_text(_replacement_text(request, str(chapter.book_id), chapter.translated_content) if chapter else "")
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
    request: Request,
    voice: str = DEFAULT_EDGE_VOICE,
    rate: float = 1.0,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương.")
    chunks = split_chapter_text(_replacement_text(request, str(chapter.book_id), chapter.translated_content))
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
