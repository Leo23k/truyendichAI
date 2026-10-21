"""Queue operations shared by the Gemini browser companion API."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.crawlers.base import clean_chapter_text
from app.crawlers.bootstrap import registry

EDIT_TRANSLATED_PROMPT_NAME = "Biên tập bản dịch hiện có"
EDIT_TRANSLATED_PROMPT = (
    "Bạn là biên tập viên tiểu thuyết tiếng Việt. Hãy biên tập bản dịch sau cho tự nhiên, mạch lạc và dễ đọc; "
    "giữ nguyên đầy đủ nội dung, tình tiết, lời thoại, tên riêng và cách xuống đoạn. Không thêm lời dẫn, nhận xét hoặc tóm tắt. "
    "Chỉ trả về bản đã biên tập.\n\n{{text}}"
)


def _restore_missing_source(remote, chapter: dict, path: str) -> dict:
    source_url = str(chapter.get("source_url") or "")
    crawler = registry.get_for_url(source_url)
    content = clean_chapter_text(asyncio.run(crawler.get_chapter_content(source_url)))
    if not content:
        raise ValueError("Website nguồn không trả nội dung chương.")
    payload = {
        "number": chapter["chapter_number"], "title": chapter["title"],
        "original_title": chapter["title"], "original_content": content,
        "translated_content": None, "content": None, "translated": False,
    }
    remote.upload_if_changed(
        "chapters", path.removeprefix("chapters/"),
        json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json",
        check_existing=False,
    )
    return remote.download_json("chapters", path.removeprefix("chapters/"))


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
    chapters = remote.rows(
        "chapters", {"select": "id,novel_id,chapter_number,title,source_url,content_path", "id": f"eq.{job['chapter_id']}", "limit": "1"},
    )
    if not chapters:
        remote.patch_rows("translation_queue", {"id": f"eq.{job['id']}", "status": "eq.queued"}, {"status": "failed", "error_code": "chapter_missing", "error_message": "Không tìm thấy chương gốc."})
        return None
    chapter = chapters[0]
    novels = remote.rows("novels", {
        "select": "id,slug,title,author,description,original_title,original_author,original_description,metadata_translated",
        "id": f"eq.{chapter['novel_id']}", "limit": "1",
    })
    path = str(chapter.get("content_path") or "")
    if not path.startswith("chapters/"):
        slug = str(novels[0].get("slug") or "") if novels else ""
        number = int(chapter.get("chapter_number") or 0)
        if not slug or number < 1:
            remote.patch_rows("translation_queue", {"id": f"eq.{job['id']}", "status": "eq.queued"}, {"status": "failed", "error_code": "source_missing", "error_message": "Chương chưa có nội dung gốc trong kho lưu trữ."})
            return None
        path = f"chapters/{slug}/{number:03d}.json"
        remote.patch_rows("chapters", {"id": f"eq.{chapter['id']}"}, {"content_path": path})
    try:
        input_mode = str(job.get("input_mode") or "original")
        source = remote.download_json("chapters", path.removeprefix("chapters/"))
    except Exception as exc:
        try:
            if input_mode == "translated":
                raise ValueError("Không thể đọc bản dịch hiện có từ kho lưu trữ; dữ liệu cũ được giữ nguyên.") from exc
            if "404 Not Found" not in str(exc):
                raise exc
            source = _restore_missing_source(remote, chapter, path)
        except Exception as recovery_exc:
            remote.patch_rows(
                "translation_queue", {"id": f"eq.{job['id']}", "status": "eq.queued"},
                {"status": "backoff", "error_code": "source_unavailable", "error_message": f"Không thể đọc raw chương từ kho lưu trữ: {str(recovery_exc)[:800]}", "updated_at": datetime.now(timezone.utc).isoformat()},
            )
            return None
    if input_mode not in {"original", "translated"}:
        remote.patch_rows("translation_queue", {"id": f"eq.{job['id']}", "status": "eq.queued"}, {"status": "failed", "error_code": "invalid_input_mode", "error_message": "Chế độ đầu vào của job không hợp lệ."})
        return None
    raw_content = source.get("translated_content") if input_mode == "translated" else source.get("original_content")
    content = str(raw_content or "").strip()
    title = str((source.get("translated_title") or source.get("title")) if input_mode == "translated" else (source.get("original_title") or chapter.get("title") or "")).strip()
    prompt = str(job.get("prompt_template") or "")
    if not content or "{{text}}" not in prompt:
        remote.patch_rows("translation_queue", {"id": f"eq.{job['id']}", "status": "eq.queued"}, {"status": "failed", "error_code": "invalid_job", "error_message": "Thiếu nội dung chương hoặc prompt snapshot."})
        return None
    source_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    combined_hash = hashlib.sha256(json.dumps([input_mode, content, prompt], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    claimed = remote.patch_rows(
        "translation_queue", {"id": f"eq.{job['id']}", "provider": "eq.gemini_web", "status": "eq.queued"},
        {"status": "translating", "attempts": int(job.get("attempts") or 0) + 1, "started_at": now, "updated_at": now,
         "error_code": None, "error_message": None, "source_hash": source_hash, "combined_hash": combined_hash, "input_chars": len(content)},
    )
    if not claimed:
        return None
    job = claimed[0]
    return {
        "id": str(job["id"]), "chapter_id": int(chapter["id"]), "chapter_number": int(chapter.get("chapter_number") or 0),
        "title": title, "content": content, "input_mode": input_mode,
        "prompt_name": str(job.get("prompt_name") or ""), "prompt_snapshot": prompt,
        "model": str(job.get("model") or "gemini-web"), "attempts": int(job.get("attempts") or 0),
        "source_hash": source_hash, "combined_hash": combined_hash,
        "metadata": ({
            "title": novels[0].get("original_title") or novels[0].get("title"),
            "author": novels[0].get("original_author") or novels[0].get("author"),
            "description": novels[0].get("original_description") or novels[0].get("description"),
        } if input_mode == "original" and novels and not novels[0].get("metadata_translated", True) else None),
    }


def pause_extension_job(remote, job_id: str, reason: str, message: str) -> bool:
    status = "paused_quota" if reason == "quota" else "backoff" if reason == "transient" else "paused_manual"
    rows = remote.patch_rows(
        "translation_queue", {"id": f"eq.{job_id}", "provider": "eq.gemini_web", "status": "eq.translating"},
        {"status": status, "error_code": reason, "error_message": message[:1000], "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return bool(rows)


def cancel_translation_job(remote, job_id: str) -> dict | None:
    rows = remote.patch_rows(
        "translation_queue",
        {"id": f"eq.{job_id}", "status": "in.(queued,translating,backoff,paused,paused_quota,paused_manual,review_required,failed)"},
        {"status": "cancelled", "error_code": None, "error_message": None, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return rows[0] if rows else None


def refresh_batch_progress(remote, batch_id: str | None) -> dict | None:
    if not batch_id:
        return None
    batches = remote.rows("batch_translation_jobs", {"select": "id,total", "id": f"eq.{batch_id}", "limit": "1"})
    if not batches:
        return None
    jobs = remote.all_rows("translation_queue", {"select": "status", "batch_job_id": f"eq.{batch_id}"})
    completed = sum(row.get("status") == "completed" for row in jobs)
    failed = sum(row.get("status") == "failed" for row in jobs)
    active = sum(row.get("status") in {"queued", "translating", "backoff"} for row in jobs)
    paused = sum(row.get("status") in {"paused", "paused_quota", "paused_manual", "review_required", "cancelled"} for row in jobs)
    total = int(batches[0].get("total") or 0)
    status = "completed" if total and completed + failed >= total and not failed else "failed" if failed else "running" if active else "paused" if paused else "queued"
    updates = remote.patch_rows("batch_translation_jobs", {"id": f"eq.{batch_id}"}, {
        "completed": completed, "failed": failed, "status": status, "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    return updates[0] if updates else None
