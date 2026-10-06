import json
from types import SimpleNamespace

from starlette.requests import Request

import app.main as main
import app.cdn as cdn
import app.auth as auth


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

    response = main.get_library_novel_metadata()

    assert json.loads(response.body)["items"] == [{
        key: rows[0].get(key)
        for key in (
            "id", "slug", "title", "author", "reading_status", "cover", "cover_fallback",
            "latest_chapter", "updated_at", "total_chapters",
        )
    }]
    assert response.headers["cache-control"] == "public, max-age=60, s-maxage=300, stale-while-revalidate=600"


def test_vercel_chapter_read_does_not_start_bulk_supabase_sync(monkeypatch) -> None:
    monkeypatch.setenv("VERCEL", "1")

    def unexpected_thread(*args, **kwargs):
        raise AssertionError("Vercel chapter reads must not start a bulk sync thread")

    monkeypatch.setattr(main.threading, "Thread", unexpected_thread)
    main._queue_supabase_sync()


def test_profile_upsert_is_throttled_but_auth_is_still_checked(monkeypatch) -> None:
    monkeypatch.setattr(auth, "URL", "https://supabase.example")
    monkeypatch.setattr(auth, "ANON", "anon-key")
    monkeypatch.setattr(auth, "SERVICE", "service-key")
    monkeypatch.setattr(auth, "_recent_profile_syncs", {})
    monkeypatch.setattr(auth, "_profile_sync_locks", {})
    counts = {"auth": 0, "profile": 0}

    class Response:
        is_success = True

        def json(self):
            return {"id": "user-1", "email": "reader@example.test", "user_metadata": {}}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            counts["auth"] += 1
            return Response()

        def post(self, url, **kwargs):
            counts["profile"] += 1
            return Response()

    monkeypatch.setattr(auth.httpx, "Client", Client)

    assert auth.user_from_token("valid-token") is not None
    assert auth.user_from_token("valid-token") is not None
    assert counts == {"auth": 2, "profile": 1}


def test_novel_counts_use_one_embedded_query_and_keep_response_fields(monkeypatch) -> None:
    monkeypatch.setattr(cdn, "_novels_cache", None)
    monkeypatch.setattr(cdn, "_configured", lambda: True)
    calls = []

    def rows(table, params):
        calls.append((table, params))
        assert table == "novels"
        return [
            {"id": 1, "slug": "many", "cover_url": None, "source_url": None,
             "chapter_rows": [{"count": 1201}], "translated_chapter_rows": [{"count": 1199}]},
            {"id": 2, "slug": "new", "cover_url": None, "source_url": None,
             "chapter_rows": [{"count": 0}], "translated_chapter_rows": [{"count": 0}]},
        ]

    monkeypatch.setattr(cdn, "_all_rows", rows)
    monkeypatch.setattr(cdn, "_fallback_json", lambda _: {"novels": []})

    result = cdn.load_novels()

    assert len(calls) == 1
    assert calls[0][1]["translated_chapter_rows.translated"] == "eq.true"
    assert [(row["total_chapters"], row["translated_chapters"]) for row in result] == [(1201, 1199), (0, 0)]
    assert "chapter_rows" not in result[0]
    assert "translated_chapter_rows" not in result[0]


def test_novel_cache_ttl_starts_after_fetch_completes(monkeypatch) -> None:
    monkeypatch.setattr(cdn, "_novels_cache", None)
    monkeypatch.setattr(cdn, "_configured", lambda: True)
    clock = [100.0]
    calls = []

    def rows(table, params):
        calls.append(table)
        clock[0] += 20.0
        return [{"id": 1, "slug": "book", "cover_url": None, "source_url": None,
                 "chapter_rows": [{"count": 3}], "translated_chapter_rows": [{"count": 2}]}]

    monkeypatch.setattr(cdn.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cdn, "_all_rows", rows)
    monkeypatch.setattr(cdn, "_fallback_json", lambda _: {"novels": []})

    assert cdn.load_novels()[0]["total_chapters"] == 3
    assert cdn.load_novels()[0]["total_chapters"] == 3
    assert calls == ["novels"]
    clock[0] += cdn._NOVELS_CACHE_TTL_SECONDS
    cdn.load_novels()
    assert calls == ["novels", "novels"]
