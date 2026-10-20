from __future__ import annotations

import json
from types import SimpleNamespace

from starlette.requests import Request

import app.main as main
from app.translator import extension_api
from app.translator.extension_queue import cancel_translation_job, claim_next_extension_job, pause_extension_job, refresh_batch_progress


def test_extension_status_probes_one_row_instead_of_scanning_queue(monkeypatch) -> None:
    class StatusRemote:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def rows(self, table: str, params: dict[str, str]) -> list[dict]:
            self.calls.append((table, params))
            return []

        def all_rows(self, *_args, **_kwargs):
            raise AssertionError("status must not scan the translation queue")

    remote = StatusRemote()
    monkeypatch.setattr(extension_api, "require_admin", lambda _request: None)
    monkeypatch.setattr(extension_api, "SupabaseSync", lambda: remote)

    result = extension_api.extension_status(None)

    assert result == {"connected": True, "provider": "gemini_web", "counts": {}}
    assert remote.calls == [("translation_queue", {"select": "id", "limit": "1"})]


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


def test_missing_raw_is_restored_from_chapter_source(monkeypatch) -> None:
    class MissingRawRemote(FakeRemote):
        def download_json(self, _bucket: str, _path: str) -> dict:
            if hasattr(self, "uploaded"):
                import json

                return json.loads(self.uploaded[2])
            raise RuntimeError("404 Not Found")

        def upload_if_changed(self, bucket, path, content, content_type, **kwargs):
            self.uploaded = (bucket, path, content, content_type)
            return True

    class Crawler:
        async def get_chapter_content(self, _url):
            return "第一段。\n\n第二段。"

    monkeypatch.setattr("app.translator.extension_queue.registry.get_for_url", lambda _url: Crawler())
    remote = MissingRawRemote()
    remote.chapter["source_url"] = "https://example.com/chapter/3"
    job = claim_next_extension_job(remote)
    assert job is not None
    assert job["content"] == "第一段。\n第二段。"
    assert remote.uploaded[0:2] == ("chapters", "novel/003.json")
    assert remote.job["status"] == "translating"


def test_cancel_active_job_sets_cancelled_status() -> None:
    remote = FakeRemote()
    remote.job["status"] = "translating"
    cancelled = cancel_translation_job(remote, "job-1")
    assert cancelled is not None
    assert remote.job["status"] == "cancelled"


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


def test_gemini_completion_is_saved_as_active_version_and_shown_in_reader(monkeypatch) -> None:
    translated = "Lời kể mở đầu bằng một buổi tối yên tĩnh.\n\nNhân vật bước ra con đường phủ ánh trăng."

    class CompletionRemote:
        def __init__(self) -> None:
            self.job = {
                "id": "job-1", "chapter_id": 7, "batch_job_id": None, "provider": "gemini_web",
                "model": "gemini-web", "prompt_id": "prompt-1", "prompt_name": "Tiên hiệp",
                "prompt_template": "Dịch tự nhiên:\n{{text}}", "source_hash": "source-hash",
                "combined_hash": "combined-hash", "status": "translating",
            }
            self.chapter = {
                "id": 7, "novel_id": 2, "chapter_number": 3, "title": "原题",
                "content_path": "chapters/novel/003.json",
            }
            self.novel = {"id": 2, "metadata_translated": True}
            self.stored = {"original_title": "原题", "original_content": "夜色笼罩着古老的山谷。" * 20}
            self.versions: list[dict] = []

        def rows(self, table: str, _params: dict[str, str]) -> list[dict]:
            if table == "translation_queue":
                return [self.job] if self.job["status"] == "translating" else []
            if table == "chapters":
                return [self.chapter]
            if table == "novels":
                return [self.novel]
            if table == "translation_versions":
                return sorted(self.versions, key=lambda item: item["version_number"], reverse=True)
            return []

        def download_json(self, _bucket: str, _path: str) -> dict:
            return self.stored

        def patch_rows(self, table: str, params: dict[str, str], payload: dict) -> list[dict]:
            if table == "translation_versions":
                for version in self.versions:
                    if params.get("is_active") == "eq.true":
                        version["is_active"] = payload["is_active"]
                return self.versions
            if table == "chapters":
                self.chapter.update(payload)
                return [self.chapter]
            if table == "translation_queue" and self.job["status"] == "translating":
                self.job.update(payload)
                return [self.job]
            return []

        def insert(self, table: str, payload: dict) -> list[dict]:
            if table == "translation_versions":
                version = {"id": "version-1", **payload}
                self.versions.append(version)
                return [version]
            return []

        def upload_if_changed(self, _bucket: str, _path: str, content: bytes, _content_type: str, **_kwargs) -> bool:
            self.stored = json.loads(content)
            return True

        def delete_rows(self, *_args, **_kwargs) -> list[dict]:
            return []

    remote = CompletionRemote()
    monkeypatch.setattr(extension_api, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(extension_api, "require_admin", lambda _request: None)
    monkeypatch.setattr(main, "load_chapters_index", lambda _slug: [{
        "id": 7, "chapter_number": 3, "title": remote.chapter["title"], "source_url": "https://example.test/chapter",
        "source_type": "zh", "translated": remote.chapter["translated"], "content_path": remote.chapter["content_path"],
    }])
    monkeypatch.setattr(main, "load_cdn_json", lambda _path: remote.stored)
    monkeypatch.setattr(main, "_replacement_context", lambda _request, _slug: ([], "novel"))

    complete_request = Request({"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/complete", "raw_path": b"/complete", "query_string": b"", "root_path": "", "headers": [], "client": ("127.0.0.1", 12345), "server": ("test", 80)})
    completion = extension_api.complete_extension_job(
        "job-1",
        extension_api.GeminiResult(translated_title="Chương 3: Ánh trăng", translated_content=translated),
        complete_request,
    )

    reader_request = Request({"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/truyen/novel/3", "raw_path": b"/truyen/novel/3", "query_string": b"", "root_path": "", "headers": [], "client": ("127.0.0.1", 12345), "server": ("test", 80)})
    reader_request.state.user = SimpleNamespace(id="reader", role="user", display_name="Reader", avatar_url=None)
    reader_response = main.read_synced_chapter("novel", 3, reader_request)

    assert completion == {"status": "completed", "version": 1}
    assert remote.job["status"] == "completed"
    assert remote.versions[0]["is_active"] is True
    assert remote.stored["translated_title"] == "Chương 3: Ánh trăng"
    assert remote.stored["translated_content"] == translated
    assert reader_response.status_code == 200
    assert reader_response.context["showing_translation"] is True
    assert reader_response.context["display_title"] == "Chương 3: Ánh trăng"
    assert reader_response.context["display_content"] == translated
