"""Admin-only HTTP bridge between the app and Gemini Web Companion."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.auth import require_admin
from app.translator.extension_queue import cancel_translation_job, claim_next_extension_job, pause_extension_job, refresh_batch_progress
from app.translator.validation import translation_issue
from sync.service import SupabaseSync


router = APIRouter()


class GeminiResult(BaseModel):
    translated_title: str = Field(min_length=1, max_length=1000)
    translated_content: str = Field(min_length=1, max_length=300000)
    translated_novel_title: str | None = Field(default=None, max_length=1000)
    translated_author: str | None = Field(default=None, max_length=1000)
    translated_description: str | None = Field(default=None, max_length=300000)


class PauseRequest(BaseModel):
    reason: str = Field(pattern="^(quota|manual|ui_changed|transient)$")
    message: str = Field(min_length=1, max_length=1000)


@router.get("/api/gemini-extension/status")
def extension_status(request: Request) -> dict:
    require_admin(request)
    remote = SupabaseSync()
    remote.rows("translation_queue", {"select": "id", "limit": "1"})
    return {"connected": True, "provider": "gemini_web", "counts": {}}


@router.get("/api/gemini-extension/jobs/next")
def next_extension_job(request: Request) -> dict:
    require_admin(request)
    try:
        return {"job": claim_next_extension_job(SupabaseSync())}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Không thể lấy job dịch từ hàng đợi.") from exc


@router.post("/api/gemini-extension/jobs/{job_id}/pause")
def pause_extension_job_route(job_id: str, payload: PauseRequest, request: Request) -> dict:
    require_admin(request)
    status = {"quota": "paused_quota", "manual": "paused_manual", "ui_changed": "paused_manual", "transient": "backoff"}[payload.reason]
    remote = SupabaseSync()
    jobs = remote.rows("translation_queue", {"select": "batch_job_id", "id": f"eq.{job_id}", "limit": "1"})
    if not pause_extension_job(remote, job_id, payload.reason, payload.message):
        raise HTTPException(status_code=409, detail="Job không còn ở trạng thái đang dịch.")
    refresh_batch_progress(remote, str(jobs[0].get("batch_job_id") or "") if jobs else None)
    return {"status": status}


@router.post("/api/gemini-extension/jobs/{job_id}/complete")
def complete_extension_job(job_id: str, payload: GeminiResult, request: Request) -> dict:
    require_admin(request)
    remote = SupabaseSync()
    jobs = remote.rows("translation_queue", {
        "select": "id,chapter_id,batch_job_id,provider,model,prompt_id,prompt_name,prompt_template,source_hash,combined_hash,status",
        "id": f"eq.{job_id}", "provider": "eq.gemini_web", "status": "eq.translating", "limit": "1",
    })
    if not jobs:
        raise HTTPException(status_code=409, detail="Job không còn ở trạng thái đang dịch.")
    job = jobs[0]
    chapters = remote.rows("chapters", {
        "select": "id,novel_id,chapter_number,title,content_path", "id": f"eq.{job['chapter_id']}", "limit": "1",
    })
    if not chapters or not str(chapters[0].get("content_path") or "").startswith("chapters/"):
        raise HTTPException(status_code=409, detail="Không tìm thấy nội dung gốc để lưu bản dịch.")
    chapter = chapters[0]
    content_path = str(chapter["content_path"])
    stored = remote.download_json("chapters", content_path.removeprefix("chapters/"))
    original_content = str(stored.get("original_content") or "")
    if not original_content:
        raise HTTPException(status_code=409, detail="Nội dung gốc của chương đang trống.")
    issue = translation_issue(original_content, payload.translated_content, str(job.get("prompt_template") or ""))
    if issue:
        raise HTTPException(status_code=422, detail=f"Bản dịch chưa đạt kiểm tra ({issue}); hãy thử lại hoặc dịch theo chunk.")
    novels = remote.rows("novels", {"select": "id,metadata_translated,original_title,original_author,original_description", "id": f"eq.{chapter['novel_id']}", "limit": "1"})
    if novels and not novels[0].get("metadata_translated", True):
        if not payload.translated_novel_title or (novels[0].get("original_author") and not payload.translated_author) or (novels[0].get("original_description") and not payload.translated_description):
            raise HTTPException(status_code=422, detail="Thiếu bản dịch metadata của truyện.")

    history = remote.rows("translation_versions", {
        "select": "version_number", "chapter_id": f"eq.{chapter['id']}", "order": "version_number.desc", "limit": "1",
    })
    version_number = int(history[0]["version_number"]) + 1 if history else 1
    remote.patch_rows("translation_versions", {"chapter_id": f"eq.{chapter['id']}", "is_active": "eq.true"}, {"is_active": False})
    remote.insert("translation_versions", {
        "chapter_id": chapter["id"], "batch_job_id": job.get("batch_job_id"), "version_number": version_number,
        "provider": "gemini_web", "model": job.get("model") or "gemini-web", "prompt_id": job.get("prompt_id"),
        "prompt_name": job.get("prompt_name") or "", "prompt_snapshot": job.get("prompt_template") or "",
        "source_hash": job.get("source_hash") or "", "combined_hash": job.get("combined_hash") or "",
        "translated_title": payload.translated_title.strip(), "translated_content": payload.translated_content.strip(), "is_active": True,
    })
    updated_json = {
        **stored, "number": chapter["chapter_number"], "title": payload.translated_title.strip(),
        "original_title": str(stored.get("original_title") or chapter.get("title") or ""),
        "translated_title": payload.translated_title.strip(), "original_content": original_content,
        "translated_content": payload.translated_content.strip(), "content": payload.translated_content.strip(), "translated": True,
    }
    object_key = content_path.removeprefix("chapters/")
    remote.upload_if_changed("chapters", object_key, json.dumps(updated_json, ensure_ascii=False).encode("utf-8"), "application/json", check_existing=False)
    now = datetime.now(timezone.utc).isoformat()
    remote.patch_rows("chapters", {"id": f"eq.{chapter['id']}"}, {
        "title": payload.translated_title.strip(), "translated": True, "content_path": content_path, "updated_at": now,
    })
    if novels and not novels[0].get("metadata_translated", True):
        remote.patch_rows("novels", {"id": f"eq.{chapter['novel_id']}"}, {
            "title": payload.translated_novel_title.strip(),
            "author": payload.translated_author.strip() if payload.translated_author else None,
            "description": payload.translated_description.strip() if payload.translated_description else None,
            "metadata_translated": True,
            "updated_at": now,
        })
    remote.patch_rows("translation_queue", {"id": f"eq.{job_id}", "status": "eq.translating"}, {
        "status": "completed", "output_chars": len(payload.translated_content.strip()), "error_code": None,
        "error_message": None, "completed_at": now, "updated_at": now,
    })
    refresh_batch_progress(remote, job.get("batch_job_id"))
    remote.delete_rows("chapter_chunks", {"chapter_id": f"eq.{chapter['id']}"})
    return {"status": "completed", "version": version_number}


@router.post("/api/gemini-extension/resume")
def resume_extension_jobs(request: Request) -> dict:
    require_admin(request)
    remote = SupabaseSync()
    rows = remote.patch_rows(
        "translation_queue", {"provider": "eq.gemini_web", "status": "in.(paused,paused_quota,paused_manual,backoff)"},
        {"status": "queued", "error_code": None, "error_message": None, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    for batch_id in {str(row["batch_job_id"]) for row in rows if row.get("batch_job_id")}:
        refresh_batch_progress(remote, batch_id)
    return {"resumed": len(rows)}


@router.post("/jobs/translations/{job_id}/cancel")
def cancel_translation_job_route(job_id: str, request: Request) -> RedirectResponse:
    require_admin(request)
    remote = SupabaseSync()
    cancelled = cancel_translation_job(remote, job_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Job dịch không còn ở trạng thái có thể hủy.")
    refresh_batch_progress(remote, str(cancelled.get("batch_job_id") or "") or None)
    return RedirectResponse(url="/jobs", status_code=303)


@router.post("/jobs/translations/batches/{batch_id}/{action}")
def control_translation_batch(batch_id: str, action: str, request: Request) -> dict:
    require_admin(request)
    if action not in {"pause", "resume", "retry"}:
        raise HTTPException(status_code=404, detail="Thao tác batch không hợp lệ.")
    remote = SupabaseSync()
    batches = remote.rows("batch_translation_jobs", {"select": "id", "id": f"eq.{batch_id}", "limit": "1"})
    if not batches:
        raise HTTPException(status_code=404, detail="Không tìm thấy batch dịch.")
    if action == "pause":
        updated = remote.patch_rows("translation_queue", {"batch_job_id": f"eq.{batch_id}", "status": "eq.queued"}, {"status": "paused", "updated_at": datetime.now(timezone.utc).isoformat()})
    elif action == "resume":
        updated = remote.patch_rows("translation_queue", {"batch_job_id": f"eq.{batch_id}", "status": "in.(paused,paused_quota,paused_manual,backoff)"}, {"status": "queued", "error_code": None, "error_message": None, "updated_at": datetime.now(timezone.utc).isoformat()})
    else:
        updated = remote.patch_rows("translation_queue", {"batch_job_id": f"eq.{batch_id}", "status": "eq.failed"}, {"status": "queued", "error_code": None, "error_message": None, "updated_at": datetime.now(timezone.utc).isoformat()})
    refresh_batch_progress(remote, batch_id)
    return {"updated": len(updated), "status": action}
