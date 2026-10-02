import asyncio

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
            return [self.chunks[self.job["current_chunk"]]]

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
                return [{"id": 7, "content_path": "chapters/novel/007.json"}]
            return [self.chunks[self.job["current_chunk"]]]

        def download_json(self, bucket: str, path: str) -> dict:
            assert (bucket, path) == ("chapters", "novel/007.json")
            return {"original_content": "Nội dung gốc."}

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
