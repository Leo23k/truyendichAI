from fastapi.testclient import TestClient

from app import main
from app.auth import CurrentUser
from app import auth
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
    auth_cookies = [
        cookie
        for cookie in response.headers.get_list("set-cookie")
        if cookie.startswith(("sb-access-token=", "sb-refresh-token="))
    ]
    assert len(auth_cookies) == 2
    assert all("Max-Age=31536000" in cookie for cookie in auth_cookies)


def test_login_callback_sets_persistent_auth_cookies(monkeypatch) -> None:
    class Response:
        is_success = True

        def json(self) -> dict[str, str]:
            return {"access_token": "access", "refresh_token": "refresh"}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(main, "AUTH_URL", "https://auth.example")
    monkeypatch.setattr(main, "ANON", "anon-key")
    monkeypatch.setattr(main.httpx, "Client", lambda **_kwargs: Client())
    with TestClient(app) as client:
        client.cookies.set("sb-pkce", "verifier")
        response = client.get("/auth/callback?code=auth-code", follow_redirects=False)

    assert response.status_code == 303
    cookies = response.headers.get_list("set-cookie")
    assert any(cookie.startswith("sb-access-token=access;") and "Max-Age=31536000" in cookie for cookie in cookies)
    assert any(cookie.startswith("sb-refresh-token=refresh;") and "Max-Age=31536000" in cookie for cookie in cookies)

def test_refresh_session_reuses_a_recent_token_exchange(monkeypatch) -> None:
    auth._recent_refreshes.clear()
    calls = []
    class Response:
        is_success = True
        def json(self): return {"access_token": "new-access", "refresh_token": "new-refresh"}
    class Client:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def post(self, *_args, **_kwargs): calls.append(1); return Response()
    monkeypatch.setattr(auth.httpx, "Client", lambda **_kwargs: Client())
    assert auth.refresh_session("same-refresh") == ("new-access", "new-refresh")
    assert auth.refresh_session("same-refresh") == ("new-access", "new-refresh")
    assert len(calls) == 1
