import asyncio

import pytest
from botocore.exceptions import ClientError

from app import cdn
from app.crawlers.base import BookInfo, ChapterInfo
from sync import service
from sync.service import enqueue_untranslated_chapters, restore_stored_chapters


def test_crawl_job_returns_while_its_translation_is_queued(monkeypatch) -> None:
    class FakeCrawler:
        source_name = "example"

        async def get_book_info(self, url: str) -> BookInfo:
            return BookInfo(url, self.source_name, "book-1", "Novel", None, None, None)

        async def get_chapters(self, url: str) -> list[ChapterInfo]:
            return [ChapterInfo(1, "chapter-1", "Chapter 1", f"{url}/chapter-1")]

        async def get_chapter_content(self, _url: str) -> str:
            return "A paragraph of original text. " * 200

    class CrawlRemote:
        def __init__(self) -> None:
            self.chapters: list[dict] = []
            self.queues: list[dict] = []

        def rows(self, table: str, _params: dict[str, str]) -> list[dict]:
            if table == "novels":
                return [{"id": 7, "slug": "novel", "title": "Novel", "author": None}]
            if table == "translation_queue":
                return []
            if table == "remote_crawl_jobs":
                return [{"id": "crawl-1"}]
            raise AssertionError(f"Unexpected table: {table}")

        def all_rows(self, table: str, _params: dict[str, str]) -> list[dict]:
            if table == "chapters":
                return self.chapters
            if table == "translation_queue":
                return self.queues
            raise AssertionError(f"Unexpected table: {table}")

        def patch_rows(self, table: str, params: dict[str, str], payload: dict) -> list[dict]:
            if table == "chapters":
                chapter_id = int(params["id"].removeprefix("eq."))
                chapter = next(row for row in self.chapters if row["id"] == chapter_id)
                chapter.update(payload)
            return []

        def insert_many_ignore(self, table: str, payloads: list[dict], _conflict: str) -> None:
            if table == "chapters":
                self.chapters.extend(
                    {"id": 11 + index, **payload, "content_path": None}
                    for index, payload in enumerate(payloads)
                )
            elif table == "translation_queue":
                self.queues.extend(payloads)

        def upload_if_changed(self, *_args) -> None:
            return None

    remote = CrawlRemote()
    statuses: list[dict] = []
    monkeypatch.setattr(service, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(service.registry, "get_for_url", lambda _url: FakeCrawler())

    asyncio.run(
        service._crawl_and_translate_url(
            "https://example.com/novel",
            lambda **values: statuses.append(values),
            "crawl-1",
        )
    )

    assert len(remote.queues) == 1
    assert remote.queues[0]["crawl_job_id"] == "crawl-1"
    assert statuses[-1]["status"] == "translating"
    assert not any(status.get("status") == "completed" for status in statuses)


class FakeRemote:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict], str]] = []

    def rows(self, _table: str, _params: dict[str, str]) -> list[dict]:
        return [{"chapter_id": 2}]

    def insert_many_ignore(self, table: str, payloads: list[dict], conflict: str) -> None:
        self.inserted.append((table, payloads, conflict))


def test_unique_slug_only_adds_a_suffix_for_duplicates() -> None:
    assert service.unique_slug("Xích Tâm Tuần Thiên", set()) == "xich-tam-tuan-thien"
    assert service.unique_slug("Xích Tâm Tuần Thiên", {"xich-tam-tuan-thien"}) == "xich-tam-tuan-thien-1"
    assert service.unique_slug("Xích Tâm Tuần Thiên", {"xich-tam-tuan-thien", "xich-tam-tuan-thien-1"}) == "xich-tam-tuan-thien-2"


def test_existing_novel_refresh_preserves_cover_and_description() -> None:
    info = BookInfo(
        "https://source.example/novel", "example", "novel", "Title",
        "Author", "New source synopsis", "https://source.example/new-cover.jpg",
        genres=["Fantasy"],
    )

    patch = service._existing_novel_refresh_patch(info, "zh")

    assert patch["genres"] == ["Fantasy"]
    assert "cover_url" not in patch
    assert "description" not in patch


def test_store_novel_cover_uploads_image_and_returns_database_path(monkeypatch) -> None:
    uploaded: list[tuple[str, str, bytes, str]] = []

    class CoverRemote:
        def upload_if_changed(self, bucket: str, path: str, content: bytes, content_type: str) -> bool:
            uploaded.append((bucket, path, content, content_type))
            return True

    monkeypatch.setattr(service, "download_cover", lambda _url: (b"image-bytes", "image/webp", ".webp"))

    path = service.store_novel_cover(CoverRemote(), "a novel/slug", "https://source.test/cover.webp")

    assert path == "covers/a-novel-slug/cover.webp"
    assert uploaded == [("covers", "a-novel-slug/cover.webp", b"image-bytes", "image/webp")]


def test_cover_storage_failure_does_not_break_the_crawl(monkeypatch) -> None:
    class BrokenCoverRemote:
        def upload_if_changed(self, *_args) -> bool:
            raise RuntimeError("temporary storage failure")

    monkeypatch.setattr(service, "download_cover", lambda _url: (b"image-bytes", "image/jpeg", ".jpg"))

    assert service.store_novel_cover(BrokenCoverRemote(), "novel", "https://source.test/cover.jpg") is None


def test_cover_upload_stays_in_supabase_when_b2_is_configured(monkeypatch) -> None:
    class ExistingObject:
        status_code = 404

    class StorageClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return ExistingObject()

    remote = object.__new__(service.SupabaseSync)
    remote.headers = {}
    uploads: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(service, "b2_configured", lambda: True)
    monkeypatch.setattr(service, "b2_upload", lambda *_args: (_ for _ in ()).throw(AssertionError("cover must not use B2")))
    monkeypatch.setattr(service.httpx, "Client", lambda **_kwargs: StorageClient())
    monkeypatch.setattr(remote, "request", lambda method, path, **kwargs: uploads.append((method, path, kwargs)))

    assert remote.upload_if_changed("covers", "novel/cover.jpg", b"image", "image/jpeg") is True
    assert uploads[0][0:2] == ("POST", "/storage/v1/object/covers/novel/cover.jpg")


def test_cover_urls_use_supabase_while_chapter_urls_keep_using_b2(monkeypatch) -> None:
    monkeypatch.setattr(cdn, "b2_configured", lambda: True)
    monkeypatch.setattr(cdn, "SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(cdn, "presigned_download_url", lambda path: f"b2://{path}")

    assert cdn.storage_url("covers", "novel/cover.jpg") == (
        "https://project.supabase.co/storage/v1/object/public/covers/novel/cover.jpg"
    )
    assert cdn.storage_url("chapters", "novel/001.json") == "b2://chapters/novel/001.json"


def test_admin_cover_upload_accepts_valid_png_and_returns_public_url(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            pass

    class StorageClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(cdn, "SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(cdn, "_service_headers", lambda: {"apikey": "test"})
    monkeypatch.setattr(cdn.httpx, "Client", lambda **_kwargs: StorageClient())

    url = cdn.upload_admin_novel_cover("test-novel", b"\x89PNG\r\n\x1a\nimage", "image/png")

    assert url.startswith("https://project.supabase.co/storage/v1/object/public/covers/test-novel/uploaded-")
    assert url.endswith(".png")


def test_migrate_remote_cover_urls_updates_only_after_success(monkeypatch) -> None:
    class CoverRemote:
        def __init__(self) -> None:
            self.updated: list[tuple[dict, dict]] = []

        def all_rows(self, table: str, _params: dict) -> list[dict]:
            assert table == "novels"
            return [
                {"id": 1, "slug": "novel-one", "cover_url": "https://source.test/one.jpg"},
                {"id": 2, "slug": "novel-two", "cover_url": "covers/novel-two/cover.webp", "source_url": "https://source.test/novel-two"},
                {"id": 3, "slug": "novel-three", "cover_url": "https://source.test/missing.jpg"},
            ]

        def patch_rows(self, table: str, params: dict, payload: dict) -> list[dict]:
            assert table == "novels"
            self.updated.append((params, payload))
            return []

    remote = CoverRemote()
    monkeypatch.setattr(service, "source_cover_url", lambda url: "https://images.example/novel-two.jpg" if url else None)

    result = service.migrate_remote_cover_urls(remote)

    assert result == {"scanned": 3, "restored": 1, "skipped": 2, "unavailable": 0}
    assert remote.updated == [({"id": "eq.2"}, {"cover_url": "https://images.example/novel-two.jpg"})]


def test_source_cover_url_can_recover_missing_legacy_cover(monkeypatch) -> None:
    class CoverCrawler:
        async def get_book_info(self, _url: str):
            return type("BookInfoResult", (), {"cover_url": "https://images.example/cover.jpg"})()

    monkeypatch.setattr(service.registry, "get_for_url", lambda _url: CoverCrawler())

    assert service.source_cover_url("https://source.example/novel") == "https://images.example/cover.jpg"


def test_translation_worker_concurrency_defaults_to_two_and_is_bounded(monkeypatch) -> None:
    monkeypatch.delenv("TRANSLATION_WORKER_CONCURRENCY", raising=False)
    assert service.translation_worker_concurrency() == 2

    monkeypatch.setenv("TRANSLATION_WORKER_CONCURRENCY", "12")
    assert service.translation_worker_concurrency() == 4

    monkeypatch.setenv("TRANSLATION_WORKER_CONCURRENCY", "0")
    assert service.translation_worker_concurrency() == 1


def test_paused_crawl_stops_crawler_but_allows_translation(monkeypatch) -> None:
    class StatusRemote:
        def rows(self, _table: str, params: dict[str, str]) -> list[dict]:
            status_filter = params["status"]
            return [{"id": "crawl-1"}] if status_filter.endswith("paused)") or status_filter == "eq.paused" else []

    remote = StatusRemote()
    assert service.remote_crawl_is_running(remote, "crawl-1") is False
    assert service.remote_job_is_running(remote, "crawl-1") is True
    assert service.remote_crawl_is_paused(remote, "crawl-1") is True


def test_all_rows_reads_past_postgrest_default_page_size() -> None:
    class PaginatedRemote:
        all_rows = service.SupabaseSync.all_rows

        def __init__(self) -> None:
            self.offsets: list[int] = []

        def rows(self, _table: str, params: dict[str, str]) -> list[dict]:
            offset = int(params["offset"])
            self.offsets.append(offset)
            if offset == 0:
                return [{"id": index} for index in range(1000)]
            return [{"id": 1000}]

    remote = PaginatedRemote()

    assert len(remote.all_rows("chapters", {"select": "id", "order": "id.asc"})) == 1001
    assert remote.offsets == [0, 1000]


def test_enqueue_creates_chunks_only_for_new_untranslated_chapters() -> None:
    remote = FakeRemote()
    count = enqueue_untranslated_chapters(remote, [
        {"id": 1, "original_content": "Nội dung." * 250, "translated_content": None},
        {"id": 2, "original_content": "Đã có queue.", "translated_content": None},
        {"id": 3, "original_content": "Đã dịch.", "translated_content": "Done"},
    ])

    assert count == 1
    chunks, queue = remote.inserted
    assert chunks[0] == "chapter_chunks"
    assert {row["chapter_id"] for row in chunks[1]} == {1}
    assert queue[0] == "translation_queue"
    assert queue[1][0]["status"] == "queued"
    assert queue[1][0]["total_chunks"] == len(chunks[1])


def test_restore_stored_chapters_recreates_missing_translation_queue() -> None:
    class RestoreRemote(FakeRemote):
        def rows(self, _table: str, _params: dict[str, str]) -> list[dict]:
            return []

        def download_json(self, bucket: str, path: str) -> dict:
            assert (bucket, path) == ("chapters", "novel/001.json")
            return {"original_content": "Ná»™i dung Ä‘Ã£ crawl." * 250}

    remote = RestoreRemote()
    restored = restore_stored_chapters(remote, [
        {"id": 11, "chapter_number": 1, "content_path": "chapters/novel/001.json"},
    ])

    assert restored == {11}
    assert [table for table, *_ in remote.inserted] == ["chapter_chunks", "translation_queue"]


def test_worker_translates_chunks_one_at_a_time(monkeypatch) -> None:
    class WorkerRemote:
        def __init__(self) -> None:
            self.job = {"id": "job", "chapter_id": 7, "current_chunk": 0, "total_chunks": 2}
            self.chunks = [{"id": "a", "original_text": "Một."}, {"id": "b", "original_text": "Hai."}]
            self.patches: list[tuple[str, dict, dict]] = []

        def rows(self, table: str, _params: dict[str, str]) -> list[dict]:
            if table == "translation_queue":
                return [self.job]
            if table == "chapters":
                return [{"id": 7, "source_url": "https://example.test", "content_path": "chapters/novel/007.json", "title": "Raw title"}]
            return [self.chunks[self.job["current_chunk"]]]

        def download_json(self, *_args) -> dict:
            raise AssertionError("Existing translation chunks should not require a B2 download")

        def patch_rows(self, table: str, _params: dict[str, str], payload: dict) -> list[dict]:
            self.patches.append((table, _params, payload))
            if table == "translation_queue" and payload.get("status") == "translating":
                return [self.job]
            return []

    remote = WorkerRemote()
    monkeypatch.setattr(service.translator, "translate_batch", lambda texts, _glossary: [f"Dịch {text}" for text in texts])

    published: list[int] = []
    monkeypatch.setattr(service, "publish_completed_translation", lambda _remote, chapter_id: published.append(chapter_id))

    assert service.run_translation_worker_once(remote) is True
    assert [patch[2]["translated_text"] for patch in remote.patches if patch[0] == "chapter_chunks"] == ["Dịch Một.", "Dịch Hai."]
    updates = [index for index, patch in enumerate(remote.patches) if patch[0] == "translation_queue" and patch[2].get("status") in {"translating", "completed"}]
    assert [index for index, patch in enumerate(remote.patches) if patch[0] == "chapter_chunks"][0] < updates[1]
    assert remote.patches[-1][2]["status"] == "completed"
    assert published == [7]


def test_worker_rebuilds_chunks_from_stored_raw_when_retranslation_has_none(monkeypatch) -> None:
    class WorkerRemote:
        def __init__(self) -> None:
            self.job = {"id": "job", "chapter_id": 7, "current_chunk": 0, "total_chunks": 0}
            self.chunks: list[dict] = []
            self.inserted: list[tuple[str, list[dict], str]] = []
            self.patches: list[tuple[str, dict, dict]] = []

        def rows(self, table: str, _params: dict[str, str]) -> list[dict]:
            if table == "translation_queue":
                return [self.job]
            if table == "chapters":
                return [{"id": 7, "title": "Tiêu đề dịch", "content_path": "chapters/novel/007.json"}]
            return [self.chunks[self.job["current_chunk"]]]

        def download_json(self, bucket: str, path: str) -> dict:
            assert (bucket, path) == ("chapters", "novel/007.json")
            return {"original_title": "Tiêu đề gốc", "original_content": "Nội dung gốc."}

        def insert_many_ignore(self, table: str, payloads: list[dict], conflict: str) -> None:
            self.inserted.append((table, payloads, conflict))
            self.chunks = [{"id": str(row["chunk_index"]), "original_text": row["original_text"]} for row in payloads]

        def patch_rows(self, table: str, _params: dict, payload: dict) -> list[dict]:
            self.patches.append((table, _params, payload))
            if table == "translation_queue" and payload.get("status") == "translating":
                self.job.update(payload)
                return [self.job]
            return []

    remote = WorkerRemote()
    monkeypatch.setattr(service, "claim_translation_job", lambda _remote: remote.job)
    monkeypatch.setattr(service.translator, "translate_batch", lambda texts, _glossary: [f"Dịch {text}" for text in texts])
    monkeypatch.setattr(service, "publish_completed_translation", lambda *_args: None)

    assert service.run_translation_worker_once(remote) is True
    assert remote.inserted[0][0] == "chapter_chunks"
    assert remote.inserted[0][1][0]["original_text"] == "Nội dung gốc."
    assert remote.job["total_chunks"] == 1
    assert any(table == "chapters" and payload.get("title") == "Tiêu đề gốc" for table, _, payload in remote.patches)


def test_download_json_converts_b2_cap_errors_to_sync_error(monkeypatch) -> None:
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "download bandwidth or transaction cap exceeded"}},
        "GetObject",
    )
    monkeypatch.setattr(service, "b2_configured", lambda: True)
    monkeypatch.setattr(service, "b2_download", lambda _path: (_ for _ in ()).throw(error))
    remote = object.__new__(service.SupabaseSync)

    with pytest.raises(service.SyncError, match="cap exceeded"):
        remote.download_json("chapters", "novel/001.json")


def test_worker_marks_b2_blocked_retranslation_failed_without_raising(monkeypatch) -> None:
    class BlockedStorageRemote:
        def __init__(self) -> None:
            self.job = {"id": "job", "chapter_id": 7, "current_chunk": 0, "total_chunks": 0}
            self.patches: list[tuple[str, dict, dict]] = []

        def rows(self, table: str, _params: dict[str, str]) -> list[dict]:
            if table == "translation_queue":
                return [self.job]
            if table == "chapters":
                return [{"id": 7, "source_url": "https://example.test", "novel_id": 3,
                         "content_path": "chapters/novel/007.json", "title": "Title"}]
            return []

        def patch_rows(self, table: str, _params: dict[str, str], payload: dict) -> list[dict]:
            self.patches.append((table, _params, payload))
            if table == "translation_queue" and payload.get("status") == "translating":
                self.job.update(payload)
                return [self.job]
            return []

        def download_json(self, _bucket: str, _path: str) -> dict:
            raise service.SyncError("B2 download cap exceeded")

    remote = BlockedStorageRemote()
    monkeypatch.setattr(service, "glossary_for_source_url", lambda _url: [])
    monkeypatch.setattr(service, "_auto_glossary_entries", lambda *_args: [])

    assert service.run_translation_worker_once(remote) is False
    assert any(table == "translation_queue" and payload.get("status") == "failed" for table, _, payload in remote.patches)


def test_stale_translation_jobs_are_requeued_before_claiming() -> None:
    class QueueRemote:
        def __init__(self) -> None:
            self.patches: list[tuple[str, dict, dict]] = []

        def patch_rows(self, table: str, params: dict, payload: dict) -> list[dict]:
            self.patches.append((table, params, payload))
            return [{"id": "stale"}]

    remote = QueueRemote()

    assert service.reclaim_stale_translation_jobs(remote) == 1
    table, params, payload = remote.patches[0]
    assert table == "translation_queue"
    assert params["status"] == "eq.translating"
    assert params["updated_at"].startswith("lt.")
    assert payload["status"] == "queued"


def test_crawl_job_completes_when_its_translation_queue_is_empty() -> None:
    class QueueRemote:
        def __init__(self, active: list[dict]) -> None:
            self.active = active
            self.patches: list[tuple[str, dict, dict]] = []

        def rows(self, table: str, params: dict) -> list[dict]:
            assert table == "translation_queue"
            assert params["status"] == "in.(queued,translating,paused,failed)"
            return self.active

        def patch_rows(self, table: str, params: dict, payload: dict) -> list[dict]:
            self.patches.append((table, params, payload))
            return [{"id": "crawl"}]

    remote = QueueRemote([])
    assert service.complete_crawl_job_when_translation_finishes(remote, "crawl") is True
    assert remote.patches[0][0] == "remote_crawl_jobs"
    assert remote.patches[0][1]["status"] == "eq.translating"
    assert remote.patches[0][2]["status"] == "completed"

    active_remote = QueueRemote([{"id": "pending"}])
    assert service.complete_crawl_job_when_translation_finishes(active_remote, "crawl") is False
    assert active_remote.patches == []


def test_remote_crawl_job_is_claimed_only_while_queued() -> None:
    class CrawlRemote:
        def __init__(self) -> None:
            self.patches: list[tuple[str, dict, dict]] = []

        def rows(self, table: str, _params: dict[str, str]) -> list[dict]:
            assert table == "remote_crawl_jobs"
            return [{"id": "crawl-1", "source_url": "https://example.com/novel", "status": "queued"}]

        def patch_rows(self, table: str, params: dict[str, str], payload: dict) -> list[dict]:
            self.patches.append((table, params, payload))
            return [{"id": "crawl-1", "status": "crawling"}]

    remote = CrawlRemote()

    assert service.claim_remote_crawl_job(remote) == {"id": "crawl-1", "status": "crawling"}
    assert remote.patches[0][1] == {"id": "eq.crawl-1", "status": "eq.queued"}
    assert remote.patches[0][2]["status"] == "crawling"


def test_completed_crawl_job_keeps_its_translation_worker_running() -> None:
    class CrawlRemote:
        def rows(self, table: str, params: dict[str, str]) -> list[dict]:
            assert table == "remote_crawl_jobs"
            assert params["status"] == "in.(crawling,syncing,translating,completed,paused)"
            return [{"id": "crawl-1"}]

    assert service.remote_job_is_running(CrawlRemote(), "crawl-1") is True


def test_stale_crawl_job_is_reclaimed_after_worker_restart() -> None:
    class CrawlRemote:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []
            self.patches: list[tuple[str, dict, dict]] = []

        def rows(self, _table: str, params: dict[str, str]) -> list[dict]:
            self.calls.append(params)
            if params["status"] == "eq.queued":
                return []
            return [{"id": "stale-crawl", "status": "crawling"}]

        def patch_rows(self, table: str, params: dict, payload: dict) -> list[dict]:
            self.patches.append((table, params, payload))
            return [{"id": "stale-crawl", "status": "crawling"}]

    remote = CrawlRemote()

    assert service.claim_remote_crawl_job(remote) == {"id": "stale-crawl", "status": "crawling"}
    assert remote.calls[1]["status"] == "eq.crawling"
    assert "updated_at" in remote.patches[0][1]


def test_enqueue_remote_crawl_reuses_an_active_job(monkeypatch) -> None:
    class CrawlRemote:
        def rows(self, table: str, _params: dict[str, str]) -> list[dict]:
            assert table == "remote_crawl_jobs"
            return [{"id": "crawl-1", "status": "crawling"}]

        def insert(self, _table: str, _payload: dict) -> dict:
            raise AssertionError("active crawl must not be duplicated")

    monkeypatch.setattr(service, "SupabaseSync", CrawlRemote)

    assert service.enqueue_remote_crawl("https://example.com/novel") == {"id": "crawl-1", "status": "crawling"}


def test_enqueue_remote_crawl_recovers_from_concurrent_insert(monkeypatch) -> None:
    class CrawlRemote:
        def __init__(self) -> None:
            self.calls = 0

        def rows(self, _table: str, _params: dict[str, str]) -> list[dict]:
            self.calls += 1
            return [] if self.calls == 1 else [{"id": "crawl-2", "status": "queued"}]

        def insert(self, _table: str, _payload: dict) -> dict:
            raise service.SyncError("unique index conflict")

    monkeypatch.setattr(service, "SupabaseSync", CrawlRemote)

    assert service.enqueue_remote_crawl("https://example.com/novel") == {"id": "crawl-2", "status": "queued"}
