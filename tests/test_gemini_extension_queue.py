from __future__ import annotations

from app.translator.extension_queue import claim_next_extension_job, pause_extension_job, refresh_batch_progress


class FakeRemote:
    def __init__(self) -> None:
        self.job = {
            "id": "job-1", "chapter_id": 7, "provider": "gemini_web", "status": "queued", "attempts": 0,
            "prompt_name": "Tiên hiệp", "prompt_template": "Dịch tự nhiên:\n{{text}}", "model": "gemini-web",
        }
        self.chapter = {"id": 7, "novel_id": 2, "chapter_number": 3, "title": "原题", "content_path": None}
        self.downloaded_path = None
        self.patches: list[tuple[str, dict, dict]] = []

    def rows(self, table: str, params: dict[str, str]) -> list[dict]:
        if table == "translation_queue":
            return [self.job] if self.job["status"] == "queued" else []
        if table == "chapters":
            return [self.chapter]
        if table == "novels":
            return [{"id": 2, "slug": "novel", "title": "原題", "author": "作者", "description": "简介", "original_title": "原題", "original_author": "作者", "original_description": "简介", "metadata_translated": False}]
        return []

    def download_json(self, _bucket: str, path: str) -> dict:
        self.downloaded_path = path
        return {"original_title": "原题", "original_content": "第一段。\n\n第二段。"}

    def patch_rows(self, table: str, params: dict[str, str], payload: dict) -> list[dict]:
        self.patches.append((table, params, payload))
        if params.get("updated_at", "").startswith("lt."):
            return []
        if table == "chapters":
            self.chapter.update(payload)
            return [self.chapter]
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
    assert job["metadata"] == {"title": "原題", "author": "作者", "description": "简介"}
    assert remote.job["status"] == "translating"
    assert remote.downloaded_path == "novel/003.json"
    assert remote.chapter["content_path"] == "chapters/novel/003.json"


def test_raw_download_failure_does_not_leave_job_translating() -> None:
    class BrokenRawRemote(FakeRemote):
        def download_json(self, _bucket: str, _path: str) -> dict:
            raise RuntimeError("B2 unavailable")

    remote = BrokenRawRemote()
    assert claim_next_extension_job(remote) is None
    assert remote.job["status"] == "backoff"
    assert remote.job["error_code"] == "source_unavailable"
    assert "B2 unavailable" in remote.job["error_message"]
    assert remote.job["attempts"] == 0


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


def test_pause_classifies_transient_errors_for_backoff() -> None:
    remote = FakeRemote()
    remote.job["status"] = "translating"
    assert pause_extension_job(remote, "job-1", "transient", "temporary network error")
    assert remote.job["status"] == "backoff"


def test_batch_progress_tracks_completion_and_manual_pause() -> None:
    class BatchRemote(FakeRemote):
        def __init__(self) -> None:
            super().__init__()
            self.batch = {"id": "batch-1", "total": 2}
            self.jobs = [{"status": "completed"}, {"status": "paused_manual"}]

        def rows(self, table: str, params: dict[str, str]) -> list[dict]:
            if table == "batch_translation_jobs":
                return [self.batch]
            return super().rows(table, params)

        def all_rows(self, table: str, params: dict[str, str]) -> list[dict]:
            return self.jobs

        def patch_rows(self, table: str, params: dict[str, str], payload: dict) -> list[dict]:
            if table == "batch_translation_jobs":
                self.batch.update(payload)
                return [self.batch]
            return super().patch_rows(table, params, payload)

    remote = BatchRemote()
    result = refresh_batch_progress(remote, "batch-1")
    assert result["completed"] == 1
    assert result["failed"] == 0
    assert result["status"] == "paused"
