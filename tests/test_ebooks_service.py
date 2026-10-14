from __future__ import annotations

import pytest
from io import BytesIO
from zipfile import ZipFile
from fastapi.testclient import TestClient

from app import ebook_service
from app import main as main_app
from app.auth import CurrentUser
from app.ebooks import EbookChapter
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
    result = ebook_service.upload_ebook("user-id", "Chinese.txt", "第一章 开始\n这是内容。".encode(), prepare_translation=True)

    assert result.chapter_count == 1
    assert result.source_language == "zh"
    assert result.translation_inputs and 1 in result.translation_inputs
    assert len(objects) == 2
    assert all(call[1].get("check_existing") is False for call in objects if isinstance(call[0], tuple))
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


def test_epub_cover_is_stored_in_private_b2_and_metadata(monkeypatch):
    remote = FakeRemote()
    objects = setup_upload(monkeypatch, remote)
    monkeypatch.setattr(ebook_service, "parse_ebook", lambda *_args: ebook_service.ParsedEbook(title="Cover book", format="epub", cover=b"cover-bytes", cover_media_type="image/png"))

    ebook_service.upload_ebook("user-id", "cover.epub", b"epub-source")
    cover_upload = next(call for call in objects if isinstance(call[0], tuple) and "/assets/cover.png" in call[0][0])
    book_write = next(call for call in remote.calls if call[0] == "POST" and call[1].endswith("ebook_books"))
    assert cover_upload[0][1] == b"cover-bytes" and cover_upload[0][2] == "image/png"
    assert book_write[2]["json"]["cover_path"].endswith("/assets/cover.png")


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


def test_delete_ebook_removes_pending_pc_worker_job(monkeypatch):
    upload_id = "b2efc9a7-843e-4a1e-88d1-bbb1a40d87f3"
    remote = FakeRemote()
    remote.rows = lambda table, params: (
        [{"id": upload_id, "original_file_path": "ebooks/ownerhash/file.epub", "status": "processing"}]
        if table == "ebook_ingestion_queue" else []
    )
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(ebook_service.hashlib, "sha256", lambda _value: type("Hash", (), {"hexdigest": lambda self: "ownerhash"})())
    deleted = []

    class FakeS3:
        def list_object_versions(self, **kwargs):
            assert kwargs["Prefix"] == f"ebooks/ownerhash/{upload_id}/"
            return {"Versions": [], "DeleteMarkers": [], "IsTruncated": False}

        def delete_objects(self, **kwargs):
            deleted.extend(kwargs["Delete"]["Objects"])
            return {"Deleted": kwargs["Delete"]["Objects"]}

    monkeypatch.setattr("app.storage.client", lambda: FakeS3())
    monkeypatch.setenv("B2_BUCKET", "private")
    ebook_service.delete_ebook("owner", upload_id)

    delete_request = next(call for call in remote.calls if call[0] == "DELETE")
    assert delete_request[1] == "/rest/v1/ebook_ingestion_queue"
    assert delete_request[2]["params"]["owner_id"] == "eq.owner"


def test_crawled_novel_export_uses_translation_and_sanitizes_html(monkeypatch):
    user = CurrentUser("user-id", "reader@example.com", "Admin", None, "admin")
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


def test_export_preserves_plain_text_paragraphs_and_embeds_cover():
    from app.ebook_export import build_ebook_export

    content, media_type, _ = build_ebook_export(
        "Novel", "Author", "novel", [(1, "Chapter 1", "Đoạn một\nĐoạn hai")], "epub", "vi",
        cover=(b"cover-bytes", "image/png"),
    )
    with ZipFile(BytesIO(content)) as archive:
        chapter = archive.read("OEBPS/chapter-00001.xhtml").decode()
        opf = archive.read("OEBPS/content.opf").decode()
        assert "<p>Đoạn một</p><p>Đoạn hai</p>" in chapter
        assert archive.read("OEBPS/images/cover.png") == b"cover-bytes"
        assert 'properties="cover-image"' in opf
    assert media_type == "application/epub+zip"


def test_crawled_export_downloads_cover_from_novel_metadata(monkeypatch):
    user = CurrentUser("user-id", "reader@example.com", "Admin", None, "admin")
    monkeypatch.setattr(main_app, "user_from_token", lambda _token: user)
    monkeypatch.setattr(main_app, "load_synced_novel", lambda _slug: {"title": "Novel", "cover": "https://cover.example/novel.png"})
    monkeypatch.setattr(main_app, "load_chapters_index", lambda _slug: [{"chapter_number": 1, "title": "Chapter 1", "source_type": "vi"}])
    monkeypatch.setattr(main_app, "_synced_chapter_content_path", lambda *_args: "chapters/novel/001.json")
    monkeypatch.setattr(main_app, "load_cdn_json", lambda _path: {"original_content": "Đoạn một\nĐoạn hai"})

    class CoverResponse:
        content = b"cover-bytes"
        headers = {"content-type": "image/png"}
        is_success = True

    class CoverClient:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def get(self, _url): return CoverResponse()

    monkeypatch.setattr(main_app.httpx, "Client", lambda **_kwargs: CoverClient())
    client = TestClient(main_app.app)
    client.cookies.set("sb-access-token", "token")

    response = client.get("/truyen/novel/export?format=epub&language=vi")
    with ZipFile(BytesIO(response.content)) as archive:
        assert archive.read("OEBPS/images/cover.png") == b"cover-bytes"
        assert "<p>Đoạn một</p><p>Đoạn hai</p>" in archive.read("OEBPS/chapter-00001.xhtml").decode()


def test_crawled_export_is_forbidden_to_non_admin(monkeypatch):
    user = CurrentUser("user-id", "reader@example.com", "Reader", None, "user")
    monkeypatch.setattr(main_app, "user_from_token", lambda _token: user)
    monkeypatch.setattr(main_app, "load_synced_novel", lambda _slug: pytest.fail("Non-admin request must stop before reading/exporting novel data"))
    client = TestClient(main_app.app)
    client.cookies.set("sb-access-token", "token")
    response = client.get("/truyen/novel/export?format=txt")
    assert response.status_code == 403


def test_ebook_list_shows_real_translation_progress(monkeypatch):
    remote = FakeRemote()
    remote.all_rows = lambda table, _params: (
        [{"id": "ebook-1", "title": "Book", "chapter_count": 3, "created_at": "2026-01-01", "source_language": "zh"}]
        if table == "ebook_books" else [
            {"ebook_id": "ebook-1", "translation_status": "translated"},
            {"ebook_id": "ebook-1", "translation_status": "processing"},
            {"ebook_id": "ebook-1", "translation_status": "pending"},
        ] if table == "ebook_chapters" else []
    )
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)

    book = ebook_service.list_ebooks("owner")[0]
    assert (book["translated_chapters"], book["processing_chapters"], book["pending_chapters"], book["translation_percent"]) == (1, 1, 1, 33)


def test_ebook_list_shows_upload_waiting_for_pc_worker(monkeypatch):
    remote = FakeRemote()
    remote.all_rows = lambda table, _params: [{
        "id": "upload-1", "file_name": "My Book.epub", "source_format": "epub",
        "status": "queued", "created_at": "2026-01-02", "updated_at": "2026-01-02",
    }] if table == "ebook_ingestion_queue" else []
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)

    book = ebook_service.list_ebooks("owner")[0]
    assert book["pending_upload"] is True
    assert book["title"] == "My Book"
    assert book["status"] == "queued"


def test_upload_page_and_detail_page_render_the_ebook_flow(monkeypatch):
    user = CurrentUser("user-id", "reader@example.com", "Reader", None, "user")
    monkeypatch.setattr(main_app, "user_from_token", lambda _token: user)
    monkeypatch.setattr(main_app, "get_ebook_detail", lambda _owner, _book: {
        "book": {"id": "book-1", "title": "Chinese book", "author": "Author", "source_format": "epub", "source_language": "zh", "chapter_count": 2, "translated_chapters": 1, "processing_chapters": 1, "pending_chapters": 0, "failed_chapters": 0, "translation_percent": 50, "cover_url": "https://signed.example/cover.png"},
        "chapters": [{"chapter_number": 1, "title": "Chapter 1", "translation_status": "translated"}],
    })
    client = TestClient(main_app.app)
    client.cookies.set("sb-access-token", "token")

    upload = client.get("/ebooks/upload?mode=translate")
    detail = client.get("/ebooks/book-1?uploaded=1&queued=2&not_chinese=1&translate=1")
    library = client.get("/")
    assert upload.status_code == 200 and "100 MB" in upload.text and "PC worker" in upload.text
    assert 'id="ebook-upload-only"' in upload.text and 'data-translate="false"' in upload.text
    assert 'id="ebook-upload-translate"' in upload.text and 'data-translate="true"' in upload.text
    assert 'id="ebook-upload-only"' in upload.text and 'site-nav-button accent' in upload.text and "lưu trên thiết bị" in upload.text
    assert "saveLocalEbook" in upload.text
    assert 'id="ebook-upload-leave"' in upload.text and 'href="/ebooks"' in upload.text
    assert "Đang tải…" in upload.text and "Đang chờ…" in upload.text and "Upload xong" in upload.text
    assert "PC worker đang phân tích" not in upload.text and "Đang cấp quyền upload" not in upload.text
    assert "auto_translate" not in upload.text and "upload_url" in upload.text
    assert detail.status_code == 200 and "Tiến độ dịch" in detail.text and "1/2 · 50%" in detail.text
    assert "Đã xong." in detail.text and "đưa 2 chương vào hàng đợi" not in detail.text
    assert "không được nhận diện là tiếng Trung" not in detail.text
    assert "https://signed.example/cover.png" in detail.text
    assert library.status_code == 200 and 'aria-label="Ebook"' in library.text
    assert 'id="ebook-floating-menu"' not in upload.text and 'id="ebook-floating-menu"' not in detail.text


def test_non_admin_can_start_direct_ebook_upload(monkeypatch):
    user = CurrentUser("user-id", "reader@example.com", "Reader", None, "user")
    monkeypatch.setattr(main_app, "user_from_token", lambda _token: user)
    monkeypatch.setattr(main_app, "list_ebooks", lambda owner: [{"id": "ebook-1", "title": "Owned", "chapter_count": 1, "source_format": "txt", "source_language": "vi"}] if owner == user.id else [])
    requested = []
    monkeypatch.setattr(main_app, "begin_ebook_upload", lambda owner, filename, size, **options: requested.append(options) or {"upload_id": "ebook-2", "upload_url": "https://b2.test/signed", "content_type": "text/plain"})

    client = TestClient(main_app.app)
    client.cookies.set("sb-access-token", "local-test-token")
    page = client.get("/ebooks")
    response = client.post("/api/ebooks/upload", json={"file_name": "new.txt", "file_size": 9, "translate_if_chinese": True})
    upload_only = client.post("/api/ebooks/upload", json={"file_name": "new.txt", "file_size": 9, "translate_if_chinese": False})

    assert page.status_code == 200 and "Owned" in page.text
    assert response.status_code == 200 and response.json()["upload_url"] == "https://b2.test/signed"
    assert upload_only.status_code == 200
    assert requested == [{"translate_if_chinese": True}, {"translate_if_chinese": False}]


def test_begin_ebook_upload_signs_only_the_reserved_private_object(monkeypatch):
    remote = FakeRemote()
    remote.insert = lambda table, payload: remote.calls.append(("insert", table, payload)) or payload
    monkeypatch.setattr(ebook_service, "b2_configured", lambda: True)
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)
    monkeypatch.setenv("B2_BUCKET", "private-ebooks")
    signed = []

    class FakeS3:
        def generate_presigned_url(self, operation, *, Params, ExpiresIn):
            signed.append((operation, Params, ExpiresIn))
            return "https://b2.test/private-signed-put"

    monkeypatch.setattr("app.storage.client", lambda: FakeS3())
    result = ebook_service.begin_ebook_upload("owner-id", "book.epub", 80 * 1024 * 1024)

    assert result["upload_url"] == "https://b2.test/private-signed-put"
    assert signed[0][0] == "put_object" and signed[0][2] == 900
    assert signed[0][1]["Key"].endswith("/original.epub")
    assert signed[0][1]["Bucket"] == "private-ebooks"
    assert signed[0][1]["ContentLength"] == 80 * 1024 * 1024
    assert remote.calls[0][2]["expected_size"] == 80 * 1024 * 1024
    assert remote.calls[0][2]["translate_if_chinese"] is False


def test_direct_ebook_upload_enforces_100mb_limit(monkeypatch):
    monkeypatch.setattr(ebook_service, "b2_configured", lambda: True)
    with pytest.raises(ebook_service.EbookServiceError) as error:
        ebook_service.begin_ebook_upload("owner-id", "large.epub", ebook_service.MAX_UPLOAD_BYTES + 1)
    assert error.value.status_code == 413


def test_unknown_japanese_or_korean_text_is_not_classified_as_chinese():
    japanese = ebook_service.ParsedEbook(title="Japanese", chapters=(EbookChapter(1, "章", "これは日本語の文章です。"),))
    korean = ebook_service.ParsedEbook(title="Korean", chapters=(EbookChapter(1, "장", "한국어 문장입니다."),))
    assert ebook_service._language(japanese) == "unknown"
    assert ebook_service._language(korean) == "unknown"


def test_pc_worker_downloads_original_and_processes_queued_upload(monkeypatch):
    job = {"id": "upload-1", "owner_id": "owner", "original_file_path": "ebooks/hash/upload-1/original.epub", "expected_size": 5}
    remote = FakeRemote()
    remote.rows = lambda table, _params: [job] if table == "ebook_ingestion_queue" else [{"source_language": "zh"}] if table == "ebook_books" else []
    def patch_rows(table, params, payload):
        remote.calls.append(("patch_rows", table, params, payload))
        return [{**job, **payload}] if table == "ebook_ingestion_queue" and params.get("status") == "eq.queued" else []
    remote.patch_rows = patch_rows
    monkeypatch.setattr(sync_service, "SupabaseSync", lambda: remote)
    downloaded = []
    monkeypatch.setattr(sync_service, "b2_download", lambda path: downloaded.append(path) or b"bytes")
    processed = []
    monkeypatch.setattr(ebook_service, "process_ebook_ingestion_job", lambda _remote, row, content: processed.append((row, content)) or 3)

    assert sync_service.run_ebook_ingestion_worker_once() is True
    assert downloaded == [job["original_file_path"]]
    assert processed[0][0]["status"] == "processing" and processed[0][1] == b"bytes"
    assert any(call[3].get("status") == "completed" and call[3]["translation_queued"] == 3 for call in remote.calls if call[0] == "patch_rows")


@pytest.mark.parametrize(("language", "expected_queued"), [("zh", 1), ("vi", 0)])
def test_ingestion_only_enqueues_detected_chinese_and_uses_parser_memory(monkeypatch, language, expected_queued):
    remote = FakeRemote()
    monkeypatch.setattr(ebook_service, "parse_ebook", lambda *_args: ebook_service.ParsedEbook(
        title="Book", language=language, format="epub",
        chapters=(EbookChapter(1, "Chapter", "<p>Chapter content</p>"),),
    ))
    uploads = []
    monkeypatch.setattr(ebook_service, "b2_upload", lambda *args, **kwargs: uploads.append((args, kwargs)))
    enqueued = []
    monkeypatch.setattr(ebook_service, "enqueue_ebook_translation", lambda owner, book, **kwargs: enqueued.append((owner, book, kwargs)) or 1)
    job = {
        "id": "book-id", "owner_id": "owner-id", "file_name": "book.epub", "source_format": "epub",
        "original_file_path": "ebooks/hash/book-id/original.epub", "expected_size": 5, "translate_if_chinese": True,
    }
    remote.rows = lambda table, _params: [job] if table == "ebook_ingestion_queue" else []

    count = ebook_service.process_ebook_ingestion_job(remote, job, b"bytes")

    assert count == expected_queued
    assert all("original.epub" not in call[0][0] for call in uploads)
    if language == "zh":
        assert enqueued[0][2]["raw_chapters"] == {1: "<p>Chapter content</p>"}
    else:
        assert not enqueued
        chapter_request = next(call for call in remote.calls if call[0] == "POST" and call[1].endswith("ebook_chapters"))
        assert chapter_request[2]["json"][0]["translation_status"] == "not_needed"


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
    remote.download_json = lambda *_args: pytest.fail("first translation must reuse parser output, not GET B2")
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)
    monkeypatch.setattr(ebook_service, "chunk_text", lambda text: ["translated input"] if "中文正文" in text else [])

    count = ebook_service.enqueue_ebook_translation("owner", "book", raw_chapters={1: "<p>中文正文</p>"})
    assert count == 1
    writes = [call for call in remote.calls if call[0] == "insert_many_ignore"]
    assert [call[1] for call in writes] == ["ebook_translation_chunks", "ebook_translation_queue"]
    assert writes[0][2][0]["ebook_chapter_id"] == "41"


def test_failed_ebook_translation_retries_from_saved_chunks_without_b2(monkeypatch):
    remote = FakeRemote()
    remote.rows = lambda table, _params: ([{"id": "book", "source_language": "zh"}] if table == "ebook_books" else [])
    remote.all_rows = lambda table, _params: (
        [{"id": 41, "chapter_number": 1, "title": "T", "content_path": "ebooks/hash/id/chapters/0000.json", "content_index": 0, "translation_status": "failed"}]
        if table == "ebook_chapters" else
        [{"ebook_chapter_id": "41", "status": "failed", "retry_count": 1}]
        if table == "ebook_translation_queue" else
        [{"chunk_index": 0, "status": "completed"}, {"chunk_index": 1, "status": "queued"}]
    )
    remote.download_json = lambda *_args: pytest.fail("retry must reuse retained translation chunks")
    monkeypatch.setattr(ebook_service, "SupabaseSync", lambda: remote)

    assert ebook_service.enqueue_ebook_translation("owner", "book") == 1
    requeued = next(call for call in remote.calls if call[0] == "patch_rows" and call[1] == "ebook_translation_queue")
    assert requeued[3]["status"] == "queued" and requeued[3]["current_chunk"] == 1
    assert not any(call[0] == "delete_rows" and call[1] == "ebook_translation_chunks" for call in remote.calls)


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
