from fastapi.testclient import TestClient

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
