from fastapi.testclient import TestClient

from app import main
from app.auth import CurrentUser
from app.main import app


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_library_page() -> None:
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Nhập truyện" in response.text


def test_import_error_is_rendered_for_htmx() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/books/import",
            data={"url": "ftp://unsupported.example/book"},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 400
    assert "URL không hợp lệ" in response.text


def test_expired_access_token_is_refreshed(monkeypatch) -> None:
    user = CurrentUser("user-1", "reader@example.com", "Reader", None, "user")
    monkeypatch.setattr(main, "user_from_token", lambda token: user if token == "fresh-access" else None)
    monkeypatch.setattr(main, "refresh_session", lambda token: ("fresh-access", "fresh-refresh") if token == "old-refresh" else None)
    with TestClient(app) as client:
        client.cookies.set("sb-access-token", "expired-access")
        client.cookies.set("sb-refresh-token", "old-refresh")
        response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "sb-access-token=fresh-access" in response.headers["set-cookie"]
    assert "sb-refresh-token=fresh-refresh" in response.headers["set-cookie"]
