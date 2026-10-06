from __future__ import annotations

import pytest
from io import BytesIO
from zipfile import ZipFile
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


def test_pdf_upload_keeps_original_without_creating_fake_chapters(monkeypatch):
    remote = FakeRemote()
    objects = setup_upload(monkeypatch, remote)
    result = ebook_service.upload_ebook("user-id", "manual.pdf", b"%PDF-1.7\nminimal fixture")
    assert result.chapter_count == 0
    assert objects[0][0][2] == "application/pdf"
    book_write = next(call for call in remote.calls if call[0] == "POST" and call[1].endswith("ebook_books"))
    assert book_write[2]["json"]["source_format"] == "pdf"


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


def test_pdf_reader_uses_short_lived_private_url_and_owner_filter(monkeypatch):
    remote = FakeRemote()
    remote.rows = lambda table, params: (remote.calls.append(("rows", table, params)) or ([{"source_format": "pdf", "original_file_path": "ebooks/hash/pdf-id/original.pdf"}] if table == "ebook_books" else []))
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(ebook_service.hashlib, "sha256", lambda _value: type("Hash", (), {"hexdigest": lambda self: "hash"})())
    monkeypatch.setattr(ebook_service, "presigned_download_url", lambda path: "signed:" + path)

    assert ebook_service.get_pdf_view_url("owner", "pdf-id") == "signed:ebooks/hash/pdf-id/original.pdf"
    assert remote.calls[0][2]["owner_id"] == "eq.owner"


def test_delete_ebook_is_owner_scoped_and_purges_all_b2_versions(monkeypatch):
    ebook_id = "b2efc9a7-843e-4a1e-88d1-bbb1a40d87f3"
    remote = FakeRemote()
    remote.rows = lambda table, params: (remote.calls.append(("rows", table, params)) or ([{"id": ebook_id}] if table == "ebook_books" and params["owner_id"] == "eq.owner" else []))
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(ebook_service.hashlib, "sha256", lambda _value: type("Hash", (), {"hexdigest": lambda self: "ownerhash"})())
    deleted = []

    class FakeS3:
        def list_object_versions(self, **kwargs):
            assert kwargs["Prefix"] == f"ebooks/ownerhash/{ebook_id}/"
            return {"Versions": [{"Key": "ebooks/ownerhash/file", "VersionId": "version-1"}], "DeleteMarkers": [{"Key": "ebooks/ownerhash/file", "VersionId": "marker-1"}], "IsTruncated": False}

        def delete_objects(self, **kwargs):
            deleted.extend(kwargs["Delete"]["Objects"])
            return {"Deleted": kwargs["Delete"]["Objects"]}

    monkeypatch.setattr("app.storage.client", lambda: FakeS3())
    monkeypatch.setenv("B2_BUCKET", "private")
    ebook_service.delete_ebook("owner", ebook_id)

    assert {item["VersionId"] for item in deleted} == {"version-1", "marker-1"}
    delete_request = next(call for call in remote.calls if call[0] == "DELETE")
    assert delete_request[2]["params"]["owner_id"] == "eq.owner"


def test_crawled_novel_export_uses_translation_and_sanitizes_html(monkeypatch):
    user = CurrentUser("user-id", "reader@example.com", "Reader", None, "user")
    monkeypatch.setattr(main_app, "user_from_token", lambda _token: user)
    monkeypatch.setattr(main_app, "load_synced_novel", lambda slug: {"slug": slug, "title": "Novel", "author": "Author"})
    monkeypatch.setattr(main_app, "load_chapters_index", lambda _slug: [{"chapter_number": 1, "title": "Chapter 1", "source_type": "zh"}])
    monkeypatch.setattr(main_app, "_synced_chapter_content_path", lambda *_args: "chapters/novel/001.json")
    monkeypatch.setattr(main_app, "load_cdn_json", lambda _path: {
        "original_content": "<p>原文</p>", "translated_content": "<p>Vietnamese</p><script>alert(1)</script>",
    })
    client = TestClient(main_app.app)
    client.cookies.set("sb-access-token", "token")

    response = client.get("/truyen/novel/export?format=html&language=vi")
    assert response.status_code == 200
    assert "Vietnamese" in response.text and "<script>" not in response.text


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
    remote.patch_rows = lambda table, params, payload: (remote.calls.append(("patch_rows", table, params, payload)) or ([{**job, **payload}] if table == "ebook_translation_queue" and params.get("status") == "eq.queued" else []))
    remote.all_rows = lambda _table, _params: [{"chunk_index": 0, "translated_text": "Vietnamese", "status": "completed"}]
    monkeypatch.setattr(sync_service, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(sync_service, "reclaim_stale_ebook_translation_jobs", lambda _remote: 0)
    monkeypatch.setattr(sync_service.translator, "translate_batch", lambda texts: ["Vietnamese" for _ in texts])
    uploaded = []
    monkeypatch.setattr(sync_service, "b2_upload", lambda *args, **kwargs: uploaded.append((args, kwargs)))

    assert sync_service.run_ebook_translation_worker_once(remote) is True
    assert uploaded and "translations/000001.json" in uploaded[0][0][0]
    assert any(call[0] == "delete_rows" and call[1] == "ebook_translation_chunks" for call in remote.calls)
    assert any(call[0] == "patch_rows" and call[1] == "ebook_chapters" and call[3]["translation_status"] == "processing" for call in remote.calls)


def test_export_epub_preserves_chapter_order_and_translated_content(monkeypatch):
    remote = FakeRemote()
    remote.rows = lambda _table, _params: [{"id": "book-id", "title": "Sample", "author": "Writer", "source_language": "zh"}]
    remote.all_rows = lambda _table, _params: [
        {"chapter_number": 2, "title": "Second", "content_path": "ebooks/hash/book-id/chapters/0000.json", "content_index": 1, "translation_status": "translated", "translated_content_path": "ebooks/hash/book-id/translations/000002.json"},
        {"chapter_number": 1, "title": "First", "content_path": "ebooks/hash/book-id/chapters/0000.json", "content_index": 0, "translation_status": "translated", "translated_content_path": "ebooks/hash/book-id/translations/000001.json"},
    ]
    remote.download_json = lambda *_args: {"chapters": [
        {"original_content": "<p>原文 1</p>"}, {"original_content": "<p>原文 2</p>"},
    ]}
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(ebook_service.hashlib, "sha256", lambda _value: type("Hash", (), {"hexdigest": lambda self: "hash"})())
    monkeypatch.setattr(ebook_service, "b2_download", lambda path: (
        b'{"translated_content":"<p>Ban dich 1</p>"}' if path.endswith("000001.json")
        else b'{"translated_content":"<p>Ban dich 2</p>"}'
    ))

    content, media_type, filename = ebook_service.export_ebook("owner-id", "book-id", "epub", "vi")
    with ZipFile(BytesIO(content)) as archive:
        chapter_files = [name for name in archive.namelist() if name.endswith(".xhtml") and "chapter-" in name]
        first = archive.read(chapter_files[0]).decode()
        second = archive.read(chapter_files[1]).decode()
    assert media_type == "application/epub+zip" and filename == "Sample.epub"
    assert "Ban dich 1" in first and "Ban dich 2" in second
