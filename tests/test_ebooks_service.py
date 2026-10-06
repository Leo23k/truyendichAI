from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import ebook_service
from app import main as main_app
from app.auth import CurrentUser
from sync import service as sync_service


class FakeRemote:
    def __init__(self, *, duplicate=False, fail_chapters=False):
        self.duplicate = duplicate
        self.fail_chapters = fail_chapters
        self.calls = []

    def rows(self, table, params):
        self.calls.append(("rows", table, params))
        return [{"id": "existing"}] if self.duplicate else []

    def all_rows(self, table, params):
        self.calls.append(("all_rows", table, params))
        return []

    def insert_many_ignore(self, table, payloads, conflict):
        self.calls.append(("insert_many_ignore", table, payloads, conflict))

    def delete_rows(self, table, params):
        self.calls.append(("delete_rows", table, params))

    def patch_rows(self, table, params, payload):
        self.calls.append(("patch_rows", table, params, payload))

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.fail_chapters and path.endswith("ebook_chapters"):
            raise RuntimeError("insert failure")

        class Response:
            def json(self):
                return [{"id": kwargs.get("json", {}).get("id", "ebook-id")}]

        return Response()


def setup_upload(monkeypatch, remote):
    objects = []
    monkeypatch.setattr(ebook_service, "b2_configured", lambda: True)
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(ebook_service, "b2_upload", lambda *args, **kwargs: objects.append((args, kwargs)))
    monkeypatch.setattr(ebook_service, "_rollback_objects", lambda paths: objects.append(("rollback", list(paths))))
    return objects


def test_upload_persists_private_paths_and_owner_scoped_metadata(monkeypatch):
    remote = FakeRemote()
    objects = setup_upload(monkeypatch, remote)
    result = ebook_service.upload_ebook("user-id", "Chinese.txt", "第一章 开始\n这是内容。".encode())

    assert result.chapter_count == 1
    assert result.source_language == "zh"
    assert len(objects) == 2
    assert all("user-id" not in call[0][0] for call in objects if isinstance(call[0], tuple))
    book_write = next(call for call in remote.calls if call[0] == "POST" and call[1].endswith("ebook_books"))
    assert book_write[2]["json"]["owner_id"] == "user-id"
    chapter_write = next(call for call in remote.calls if call[0] == "POST" and call[1].endswith("ebook_chapters"))
    assert chapter_write[2]["json"][0]["translation_status"] == "pending"


def test_upload_rejects_duplicate_before_storage_write(monkeypatch):
    remote = FakeRemote(duplicate=True)
    objects = setup_upload(monkeypatch, remote)
    with pytest.raises(ebook_service.EbookServiceError) as error:
        ebook_service.upload_ebook("owner", "book.txt", b"some text")
    assert error.value.status_code == 409
    assert objects == []


def test_upload_rejects_oversized_file_without_storage(monkeypatch):
    remote = FakeRemote()
    objects = setup_upload(monkeypatch, remote)
    with pytest.raises(ebook_service.EbookServiceError) as error:
        ebook_service.upload_ebook("owner", "book.txt", b"x" * (ebook_service.MAX_UPLOAD_BYTES + 1))
    assert error.value.status_code == 413
    assert objects == []
    assert not remote.calls


def test_original_download_query_is_scoped_to_owner(monkeypatch):
    remote = FakeRemote()
    def rows(table, params):
        remote.calls.append(("rows", table, params))
        return [{"original_file_path": "ebooks/hash/ebook/original.epub", "file_name": "private.epub"}]

    remote.rows = rows
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(ebook_service, "presigned_download_url", lambda path, **_kwargs: "signed:" + path)

    url, filename = ebook_service.get_original_download("owner-a", "ebook-id")
    assert url.endswith("original.epub") and filename == "private.epub"
    query = remote.calls[0][2]
    assert query["owner_id"] == "eq.owner-a"


def test_non_admin_can_access_ebook_upload_without_admin_mutation(monkeypatch):
    user = CurrentUser("user-id", "reader@example.com", "Reader", None, "user")
    monkeypatch.setattr(main_app, "user_from_token", lambda _token: user)
    monkeypatch.setattr(main_app, "list_ebooks", lambda owner: [{"id": "ebook-1", "title": "Owned", "chapter_count": 1, "source_format": "txt", "source_language": "vi"}] if owner == user.id else [])
    monkeypatch.setattr(main_app, "upload_ebook", lambda owner, filename, content: ebook_service.UploadedEbook("ebook-2", "New", 1, "vi"))

    client = TestClient(main_app.app)
    client.cookies.set("sb-access-token", "local-test-token")
    page = client.get("/ebooks")
    response = client.post("/api/ebooks/upload", files={"file": ("new.txt", b"some text", "text/plain")}, follow_redirects=False)

    assert page.status_code == 200 and "Owned" in page.text
    assert response.status_code == 303 and response.headers["location"] == "/ebooks?uploaded=1"


def test_ebook_reader_rejects_content_path_outside_owner_prefix(monkeypatch):
    remote = FakeRemote()
    calls = iter([
        [{"id": "book-id", "title": "Book", "chapter_count": 1}],
        [{"chapter_number": 1, "title": "Chapter", "content_path": "chapters/public.json", "content_index": 0}],
    ])
    remote.rows = lambda _table, _params: next(calls)
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)
    with pytest.raises(ebook_service.EbookServiceError, match="không hợp lệ") as error:
        ebook_service.get_ebook_chapter("owner-id", "book-id", 1)
    assert error.value.status_code == 503


def test_translation_enqueue_is_owner_scoped_and_reuses_chunker(monkeypatch):
    remote = FakeRemote()
    remote.rows = lambda table, params: [{"id": "book", "source_language": "zh"}] if table == "ebook_books" else []
    remote.all_rows = lambda table, params: ([{"id": 41, "chapter_number": 1, "title": "T", "content_path": "ebooks/hash/id/chapters/0000.json", "content_index": 0, "translation_status": "pending"}] if table == "ebook_chapters" else [])
    remote.download_json = lambda _bucket, _path: {"chapters": [{"original_content": "<p>中文正文</p>"}]}
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(ebook_service, "chunk_text", lambda text: ["translated input"] if "中文正文" in text else [])

    count = ebook_service.enqueue_ebook_translation("owner", "book")
    assert count == 1
    writes = [call for call in remote.calls if call[0] == "insert_many_ignore"]
    assert [call[1] for call in writes] == ["ebook_translation_chunks", "ebook_translation_queue"]
    assert writes[0][2][0]["ebook_chapter_id"] == "41"


def test_reader_can_load_owner_scoped_translated_chapter(monkeypatch):
    remote = FakeRemote()
    calls = iter([
        [{"id": "book-id", "title": "Book", "chapter_count": 1}],
        [{"chapter_number": 1, "title": "Chapter", "content_path": "ebooks/hash/book-id/chapters/0000.json", "content_index": 0, "translation_status": "translated", "translated_content_path": "ebooks/hash/book-id/translations/000001.json"}],
        [], [],
    ])
    remote.rows = lambda _table, _params: next(calls)
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(ebook_service.hashlib, "sha256", lambda _value: type("Hash", (), {"hexdigest": lambda self: "hash"})())
    monkeypatch.setattr(ebook_service, "b2_download", lambda path: (
        b'{"chapters":[{"chapter_number":1,"original_content":"<p>Chinese</p>"}]}'
        if path.endswith("0000.json") else b'{"chapter_number":1,"translated_content":"<p>Vietnamese</p>"}'
    ))

    result = ebook_service.get_ebook_chapter("owner-id", "book-id", 1, mode="translated")
    assert result["content"] == "<p>Vietnamese</p>"
    assert result["translated"] is True


def test_ebook_translation_worker_publishes_separate_translation(monkeypatch):
    remote = FakeRemote()
    job = {"id": "job-1", "ebook_chapter_id": "chapter-1", "total_chunks": 1, "current_chunk": 0, "retry_count": 0}
    remote.rows = lambda table, _params: {
        "ebook_translation_queue": [job],
        "ebook_chapters": [{"id": "chapter-1", "ebook_id": "book-1", "chapter_number": 1, "title": "Title"}],
        "ebook_books": [{"id": "book-1", "owner_id": "owner-1", "source_language": "zh"}],
        "ebook_translation_chunks": [{"id": "chunk-1", "original_text": "Chinese"}],
    }.get(table, [])
    remote.patch_rows = lambda table, _params, payload: ([{**job, **payload}] if table == "ebook_translation_queue" else [])
    remote.all_rows = lambda _table, _params: [{"chunk_index": 0, "translated_text": "Vietnamese", "status": "completed"}]
    monkeypatch.setattr(sync_service, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(sync_service, "reclaim_stale_ebook_translation_jobs", lambda _remote: 0)
    monkeypatch.setattr(sync_service.translator, "translate_batch", lambda texts: ["Vietnamese" for _ in texts])
    uploaded = []
    monkeypatch.setattr(sync_service, "b2_upload", lambda *args, **kwargs: uploaded.append((args, kwargs)))

    assert sync_service.run_ebook_translation_worker_once(remote) is True
    assert uploaded and "translations/000001.json" in uploaded[0][0][0]
    assert any(call[0] == "delete_rows" and call[1] == "ebook_translation_chunks" for call in remote.calls)
