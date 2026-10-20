from __future__ import annotations

from app.translator.extension_queue import claim_next_extension_job, pause_extension_job


class FakeRemote:
    def __init__(self) -> None:
        self.job = {
            "id": "job-1", "chapter_id": 7, "provider": "gemini_web", "status": "queued", "attempts": 0,
            "prompt_name": "Tiên hiệp", "prompt_template": "Dịch tự nhiên:\n{{text}}", "model": "gemini-web",
        }
        self.patches: list[tuple[str, dict, dict]] = []

    def rows(self, table: str, params: dict[str, str]) -> list[dict]:
        if table == "translation_queue":
            return [self.job] if self.job["status"] == "queued" else []
        if table == "chapters":
            return [{"id": 7, "chapter_number": 3, "title": "原题", "content_path": "chapters/novel/003.json"}]
        return []

    def download_json(self, _bucket: str, _path: str) -> dict:
        return {"original_title": "原题", "original_content": "第一段。\n\n第二段。"}

    def patch_rows(self, table: str, params: dict[str, str], payload: dict) -> list[dict]:
        self.patches.append((table, params, payload))
        if params.get("updated_at", "").startswith("lt."):
            return []
        if table == "translation_queue" and params.get("status") == "eq.queued" and self.job["status"] != "queued":
            return []
        self.job.update(payload)
        return [self.job]


def test_claim_extension_job_returns_whole_chapter_and_prompt_snapshot() -> None:
    remote = FakeRemote()
    job = claim_next_extension_job(remote)
    assert job is not None
    assert job["content"] == "第一段。\n\n第二段。"
    assert job["prompt_snapshot"] == "Dịch tự nhiên:\n{{text}}"
    assert job["attempts"] == 1
    assert job["source_hash"] and job["combined_hash"]
    assert remote.job["status"] == "translating"


def test_claim_is_conditional_and_loses_race_safely() -> None:
    remote = FakeRemote()
    remote.job["status"] = "translating"
    assert claim_next_extension_job(remote) is None


def test_pause_classifies_quota_and_manual_action() -> None:
    remote = FakeRemote()
    remote.job["status"] = "translating"
    assert pause_extension_job(remote, "job-1", "quota", "usage limit")
    assert remote.job["status"] == "paused_quota"
    assert remote.job["error_code"] == "quota"
