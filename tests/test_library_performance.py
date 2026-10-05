from types import SimpleNamespace

from starlette.requests import Request

import app.main as main


def _request(path: str = "/") -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    request = Request(scope)
    request.state.user = SimpleNamespace(
        id="audit-user", role="user", display_name="Audit", avatar_url=None, email=""
    )
    return request


def test_library_shell_does_not_wait_for_novel_metadata(monkeypatch) -> None:
    def unexpected_metadata_load():
        raise AssertionError("Library shell must not synchronously load novel metadata")

    monkeypatch.setattr(main, "load_novels", unexpected_metadata_load)

    response = main.library(_request())

    assert response.status_code == 200
    assert b'id="library-count"' in response.body


def test_library_metadata_endpoint_still_returns_lightweight_fields(monkeypatch) -> None:
    rows = [{
        "id": 7,
        "slug": "test",
        "title": "Test",
        "author": "Author",
        "reading_status": "ongoing",
        "cover": "https://example.test/cover.webp",
        "cover_fallback": "https://cdn.example.test/cover.webp",
        "latest_chapter": 4,
        "updated_at": "2026-10-05T00:00:00Z",
        "total_chapters": 4,
        "translated_chapters": 3,
        "description": "not used by Library",
    }]
    monkeypatch.setattr(main, "load_novels", lambda: rows)

    response = main.get_library_novel_metadata(_request("/api/novels/metadata"))

    assert response["items"] == [{
        key: rows[0].get(key)
        for key in (
            "id", "slug", "title", "author", "reading_status", "cover", "cover_fallback",
            "latest_chapter", "updated_at", "total_chapters",
        )
    }]
