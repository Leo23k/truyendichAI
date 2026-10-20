"""Admin-only HTTP bridge between the app and Gemini Web Companion."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import require_admin
from app.translator.extension_queue import claim_next_extension_job, pause_extension_job
from sync.service import SupabaseSync


router = APIRouter()


class GeminiResult(BaseModel):
    translated_title: str = Field(min_length=1, max_length=1000)
    translated_content: str = Field(min_length=1, max_length=300000)


class PauseRequest(BaseModel):
    reason: str = Field(pattern="^(quota|manual|ui_changed|transient)$")
    message: str = Field(min_length=1, max_length=1000)


@router.get("/api/gemini-extension/status")
def extension_status(request: Request) -> dict:
    require_admin(request)
    remote = SupabaseSync()
    rows = remote.all_rows("translation_queue", {
        "select": "status", "provider": "eq.gemini_web", "status": "in.(queued,translating,backoff,paused,paused_quota,paused_manual,review_required,completed,failed,cancelled)",
    })
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {"connected": True, "provider": "gemini_web", "counts": counts}


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
    if not pause_extension_job(SupabaseSync(), job_id, payload.reason, payload.message):
        raise HTTPException(status_code=409, detail="Job không còn ở trạng thái đang dịch.")
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
    remote.patch_rows("translation_queue", {"id": f"eq.{job_id}", "status": "eq.translating"}, {
        "status": "completed", "output_chars": len(payload.translated_content.strip()), "error_code": None,
        "error_message": None, "completed_at": now, "updated_at": now,
    })
    remote.delete_rows("chapter_chunks", {"chapter_id": f"eq.{chapter['id']}"})
    return {"status": "completed", "version": version_number}


@router.post("/api/gemini-extension/resume")
def resume_extension_jobs(request: Request) -> dict:
    require_admin(request)
    rows = SupabaseSync().patch_rows(
        "translation_queue", {"provider": "eq.gemini_web", "status": "in.(paused,paused_quota,paused_manual,backoff)"},
        {"status": "queued", "error_code": None, "error_message": None, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return {"resumed": len(rows)}
