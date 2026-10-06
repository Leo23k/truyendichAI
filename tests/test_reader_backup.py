from types import SimpleNamespace

import pytest
from starlette.requests import Request

import app.main as main


def test_synced_chapter_uses_saved_object_path_even_when_chapter_numbers_are_compacted() -> None:
    entry = {"chapter_number": 4, "content_path": "chapters/quy-bi-chi-chu/005.json"}

    assert main._synced_chapter_content_path("quy-bi-chi-chu", entry, 4) == "chapters/quy-bi-chi-chu/005.json"


def test_untranslated_chapter_loads_saved_path_when_b2_upload_precedes_metadata_patch(monkeypatch) -> None:
    chapter = {
        "id": 640700, "chapter_number": 1, "title": "Chương 1", "source_url": "https://example.test/chapter",
        "source_type": "vi", "translated": False, "content_path": "chapters/quy-bi-chi-chu/001.json",
    }
    requested_paths = []
    monkeypatch.setattr(main, "load_chapters_index", lambda slug: [chapter])
    progress_lookups = []
    monkeypatch.setattr(main, "load_translation_progress", lambda chapter_id: progress_lookups.append(chapter_id))
    monkeypatch.setattr(main, "_replacement_context", lambda request, slug: ([], slug))
    monkeypatch.setattr(main, "load_cdn_json", lambda path: requested_paths.append(path) or {"original_content": "nội dung gốc"})
    request = Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/truyen/quy-bi-chi-chu/1", "raw_path": b"/truyen/quy-bi-chi-chu/1",
        "query_string": b"", "root_path": "", "headers": [], "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    })
    request.state.user = SimpleNamespace(id="reader", role="user", display_name="Reader", avatar_url=None)

    response = main.read_synced_chapter("quy-bi-chi-chu", 1, request)

    assert response.status_code == 200
    assert requested_paths == ["chapters/quy-bi-chi-chu/001.json"]
    assert progress_lookups == [640700]
    assert response.context["display_content"] == "nội dung gốc"


def test_reader_uses_original_when_backup_has_no_translation(monkeypatch) -> None:
    chapter = {
        "id": 640696,
        "chapter_number": 1295,
        "title": "Chương 73 Giấc mộng đẹp thành sự thật",
        "source_url": "https://example.test/chapter",
        "source_type": "zh",
        "translated": True,
        "content_path": "chapters/kiem-lai/1295.json",
    }
    monkeypatch.setattr(main, "load_chapters_index", lambda slug: [chapter])
    progress_lookups = []
    monkeypatch.setattr(main, "load_translation_progress", lambda chapter_id: progress_lookups.append(chapter_id))
    monkeypatch.setattr(main, "_replacement_context", lambda request, slug: ([], slug))
    monkeypatch.setattr(main, "load_cdn_json", lambda path: {
        "original_title": "第73章 好梦成真",
        "original_content": "测试正文内容",
        "content": "测试正文内容",
        "translated_content": "",
        "translated": False,
    })
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": "/truyen/kiem-lai/1295",
        "raw_path": b"/truyen/kiem-lai/1295", "query_string": b"",
        "root_path": "", "headers": [], "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    request = Request(scope)
    request.state.user = SimpleNamespace(id="reader", role="user", display_name="Reader", avatar_url=None)

    response = main.read_synced_chapter("kiem-lai", 1295, request)

    assert response.status_code == 200
    assert response.context["display_content"] == "测试正文内容"
    assert response.context["showing_translation"] is False
    assert response.context["mode"] == "original"
    assert response.context["translation_error"]
    assert response.context["tts_chunks"] == []
    assert progress_lookups == []


@pytest.mark.asyncio
async def test_synced_tts_stream_starts_at_segment_for_text_offset(monkeypatch) -> None:
    entry = {"id": 88}
    monkeypatch.setattr(main, "_synced_chapter_entry_and_payload", lambda slug, number: (entry, {"translated_content": "all text"}))
    monkeypatch.setattr(main, "load_translation_progress", lambda chapter_id: None)
    monkeypatch.setattr(main, "_replacement_text", lambda request, slug, text: text)
    monkeypatch.setattr(main, "split_chapter_text", lambda text: ["first", "second"])
    streamed = []

    async def fake_stream(text, **kwargs):
        streamed.append(text)
        yield b"audio-frame"

    monkeypatch.setattr(main, "stream_cached_edge_tts", fake_stream)
    request = Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/truyen/sample/1/audio/tts/stream", "raw_path": b"/truyen/sample/1/audio/tts/stream",
        "query_string": b"", "root_path": "", "headers": [],
        "client": ("127.0.0.1", 12345), "server": ("test", 80),
    })
    request.state.user = None

    response = await main.stream_synced_chapter_audio("sample", 1, request, offset=7)
    body = b"".join([frame async for frame in response.body_iterator])

    assert streamed == ["second"]
    assert body == b"audio-frame"
