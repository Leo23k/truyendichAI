"""Queue operations shared by the Gemini browser companion API."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone


def reclaim_stale_extension_jobs(remote, stale_seconds: int = 900) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(60, stale_seconds))).isoformat()
    rows = remote.patch_rows(
        "translation_queue",
        {"provider": "eq.gemini_web", "status": "eq.translating", "updated_at": f"lt.{cutoff}"},
        {"status": "queued", "error_code": "worker_lost", "error_message": "Extension ngắt kết nối; job sẽ được nhận lại.", "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return len(rows)


def claim_next_extension_job(remote) -> dict | None:
    reclaim_stale_extension_jobs(remote)
    jobs = remote.rows(
        "translation_queue",
        {"select": "*", "provider": "eq.gemini_web", "status": "eq.queued", "order": "priority.desc,created_at.asc", "limit": "1"},
    )
    if not jobs:
        return None
    job = jobs[0]
    now = datetime.now(timezone.utc).isoformat()
    claimed = remote.patch_rows(
        "translation_queue",
        {"id": f"eq.{job['id']}", "provider": "eq.gemini_web", "status": "eq.queued"},
        {"status": "translating", "attempts": int(job.get("attempts") or 0) + 1, "started_at": now, "updated_at": now, "error_code": None, "error_message": None},
    )
    if not claimed:
        return None
    job = claimed[0]
    chapters = remote.rows(
        "chapters", {"select": "id,chapter_number,title,content_path", "id": f"eq.{job['chapter_id']}", "limit": "1"},
    )
    if not chapters:
        remote.patch_rows("translation_queue", {"id": f"eq.{job['id']}", "status": "eq.translating"}, {"status": "failed", "error_code": "chapter_missing", "error_message": "Không tìm thấy chương gốc."})
        return None
    chapter = chapters[0]
    path = str(chapter.get("content_path") or "")
    if not path.startswith("chapters/"):
        remote.patch_rows("translation_queue", {"id": f"eq.{job['id']}", "status": "eq.translating"}, {"status": "failed", "error_code": "source_missing", "error_message": "Chương chưa có nội dung gốc trong kho lưu trữ."})
        return None
    source = remote.download_json("chapters", path.removeprefix("chapters/"))
    content = str(source.get("original_content") or "").strip()
    title = str(source.get("original_title") or chapter.get("title") or "").strip()
    prompt = str(job.get("prompt_template") or "")
    if not content or "{{text}}" not in prompt:
        remote.patch_rows("translation_queue", {"id": f"eq.{job['id']}", "status": "eq.translating"}, {"status": "failed", "error_code": "invalid_job", "error_message": "Thiếu nội dung chương hoặc prompt snapshot."})
        return None
    source_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    combined_hash = hashlib.sha256(json.dumps([content, prompt], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    remote.patch_rows(
        "translation_queue", {"id": f"eq.{job['id']}", "status": "eq.translating"},
        {"source_hash": source_hash, "combined_hash": combined_hash, "input_chars": len(content), "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return {
        "id": str(job["id"]), "chapter_id": int(chapter["id"]), "chapter_number": int(chapter.get("chapter_number") or 0),
        "title": title, "content": content, "prompt_name": str(job.get("prompt_name") or ""), "prompt_snapshot": prompt,
        "model": str(job.get("model") or "gemini-web"), "attempts": int(job.get("attempts") or 0),
        "source_hash": source_hash, "combined_hash": combined_hash,
    }


def pause_extension_job(remote, job_id: str, reason: str, message: str) -> bool:
    status = "paused_quota" if reason == "quota" else "paused_manual"
    rows = remote.patch_rows(
        "translation_queue", {"id": f"eq.{job_id}", "provider": "eq.gemini_web", "status": "eq.translating"},
        {"status": status, "error_code": reason, "error_message": message[:1000], "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return bool(rows)
