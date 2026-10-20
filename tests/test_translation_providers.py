import pytest

from app.config import Settings
from app.translator import providers
from app.translator.service import TranslatorService


def test_render_prompt_requires_text_placeholder() -> None:
    assert providers.render_prompt("Dịch:\n{{text}}", "你好") == "Dịch:\n你好"
    with pytest.raises(ValueError, match="text"):
        providers.render_prompt("Dịch đoạn này", "你好")


def test_gemini_flash_lite_is_the_default_provider_and_model() -> None:
    defaults = Settings(_env_file=None)

    assert defaults.translation_provider == "hachimi"
    assert defaults.gemini_model == "gemini-3.5-flash-lite"


def test_gemini_api_uses_default_model_and_prompt(monkeypatch) -> None:
    request: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"candidates": [{"content": {"parts": [{"text": "Xin chào"}]}}]}

    def post(url: str, **kwargs):
        request.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(providers.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(providers.httpx, "post", post)

    translated = providers.GeminiApiProvider().translate_batch(
        ["你好"], prompt_template="Chỉ dịch:\n{{text}}",
    )

    assert translated == ["Xin chào"]
    assert "gemini-3.5-flash-lite" in str(request["url"])
    assert request["headers"] == {"x-goog-api-key": "test-key"}
    assert request["json"]["contents"][0]["parts"][0]["text"] == "Chỉ dịch:\n你好"


def test_translator_dispatches_selected_provider(monkeypatch) -> None:
    called: dict[str, object] = {}

    class Provider:
        def translate_batch(self, texts: list[str], **kwargs) -> list[str]:
            called.update(texts=texts, **kwargs)
            return ["Bản dịch"]

    monkeypatch.setattr("app.translator.service.provider_for", lambda name: called.update(provider=name) or Provider())

    result = TranslatorService().translate_batch(
        ["原文"], provider="gemini_web", web_profile="tai-khoan-1",
        prompt_template="Dịch:\n{{text}}",
    )

    assert result == ["Bản dịch"]
    assert called["provider"] == "gemini_web"
    assert called["web_profile"] == "tai-khoan-1"


def test_gemini_web_profile_name_is_filesystem_safe() -> None:
    assert providers.GeminiWebProvider.normalize_profile(" Tài khoản 01 ") == "T-i-kho-n-01"
    with pytest.raises(ValueError):
        providers.GeminiWebProvider.normalize_profile("***")


def test_gemini_web_edge_profile_launches_with_edge_channel(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Chromium:
        def launch_persistent_context(self, user_data_dir, **options):
            captured.update(user_data_dir=user_data_dir, **options)
            return "edge-context"

    monkeypatch.setattr(
        providers.GeminiWebProvider, "profile_dir",
        classmethod(lambda _cls, _name, _browser=None: tmp_path / "edge" / "default"),
    )

    context = providers.GeminiWebProvider.launch_context(type("Playwright", (), {"chromium": Chromium()})(), "edge:default")

    assert context == "edge-context"
    assert captured["channel"] == "msedge"
    assert captured["user_data_dir"] == str(tmp_path / "edge" / "default")


def test_gemini_web_login_opens_selected_edge(tmp_path, monkeypatch) -> None:
    launched: dict[str, object] = {}
    monkeypatch.setattr(
        providers.GeminiWebProvider, "profile_dir",
        classmethod(lambda _cls, name, browser=None: tmp_path / browser / name),
    )
    monkeypatch.setattr(
        providers.GeminiWebProvider, "browser_executable",
        classmethod(lambda _cls, browser: tmp_path / f"{browser}.exe"),
    )
    monkeypatch.setattr(providers.subprocess, "Popen", lambda args: launched.update(args=args))

    result = providers.GeminiWebProvider.open_profile("default", "edge")

    assert result == "edge:default"
    assert launched["args"][0] == str(tmp_path / "edge.exe")
    assert launched["args"][1] == f"--user-data-dir={tmp_path / 'edge' / 'default'}"


def test_hachimi_does_not_require_a_saved_prompt() -> None:
    from app import main

    class Remote:
        def rows(self, *_args, **_kwargs):
            raise AssertionError("Hachimi should not look up a saved prompt")

    choice = main._translation_prompt_choice(Remote(), "hachimi", "")

    assert choice["prompt_id"] is None
    assert choice["prompt_name"] == "Hachimi mặc định"
    assert choice["prompt_template"] == providers.DEFAULT_PROMPT


def test_update_synced_book_accepts_hachimi_without_prompt(monkeypatch) -> None:
    import asyncio

    from app import main

    captured: dict[str, object] = {}
    monkeypatch.setattr(main, "get_synced_novel_for_management", lambda _slug: {"source_url": "https://example.com/novel"})
    monkeypatch.setattr(main, "SupabaseSync", lambda: object())
    monkeypatch.setattr(main, "enqueue_remote_crawl", lambda url, **options: captured.update(url=url, **options))

    response = asyncio.run(main.update_synced_book(
        "novel", provider="hachimi", model="hachimi-local", web_profile="", prompt_id="", gemini_api_key="",
    ))

    assert response.status_code == 303
    assert captured["prompt_id"] is None
    assert captured["prompt_template"] == providers.DEFAULT_PROMPT


def test_gemini_web_login_route_opens_profile_and_returns_feedback(monkeypatch) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from app import main

    opened: list[str] = []
    monkeypatch.setattr(main, "user_from_token", lambda token: SimpleNamespace(role="admin") if token else None)
    monkeypatch.setattr(main.GeminiWebProvider, "default_browser", classmethod(lambda _cls: "edge"))
    monkeypatch.setattr(main.GeminiWebProvider, "open_profile", classmethod(lambda _cls, name, browser: opened.append((name, browser)) or name))

    response = TestClient(main.app).post(
        "/translation/providers/gemini-web/open", data={"name": "default"},
        cookies={"sb-access-token": "test"}, headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "Đã mở Gemini Web trong trình duyệt mặc định" in response.text
    assert "đăng nhập tài khoản Google" in response.text
    assert opened == [("default", "edge")]


def test_gemini_web_login_route_returns_actionable_error(monkeypatch) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(main, "user_from_token", lambda token: SimpleNamespace(role="admin") if token else None)
    monkeypatch.setattr(main.GeminiWebProvider, "default_browser", classmethod(lambda _cls: "edge"))
    monkeypatch.setattr(main.GeminiWebProvider, "open_profile", classmethod(lambda _cls, _name, _browser: (_ for _ in ()).throw(RuntimeError("Không tìm thấy Google Chrome."))))

    response = TestClient(main.app).post(
        "/translation/providers/gemini-web/open", data={"name": "default"},
        cookies={"sb-access-token": "test"},
    )

    assert response.status_code == 400
    assert "Không tìm thấy Google Chrome." in response.text


def test_gemini_web_login_ui_has_direct_link_fallback() -> None:
    from types import SimpleNamespace

    from app import main

    html = main.templates.get_template("partials/translation_options.html").render(
        default_provider="hachimi",
        default_model="hachimi-local",
        translation_providers=SimpleNamespace(
            gemini_api_configured=False,
            gemini_web_default_browser="edge",
            gemini_web_available=True,
        ),
        translation_prompts=[],
        option_id="test-provider",
    )

    assert 'id="test-provider-direct-fallback"' in html
    assert 'href="https://gemini.google.com/app"' in html
    assert "fallback.hidden=response.ok" in html


def test_gemini_web_logout_route_opens_google_logout(monkeypatch) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from app import main

    logged_out: list[str] = []
    monkeypatch.setattr(main, "user_from_token", lambda token: SimpleNamespace(role="admin") if token else None)
    monkeypatch.setattr(main.GeminiWebProvider, "logout", classmethod(lambda _cls, name, browser: logged_out.append((name, browser))))

    response = TestClient(main.app).post(
        "/translation/providers/gemini-web/logout", data={"name": "default", "browser": "edge"},
        cookies={"sb-access-token": "test"},
    )

    assert response.status_code == 200
    assert "đăng xuất khỏi Gemini Web" in response.text
    assert logged_out == [("default", "edge")]


@pytest.mark.parametrize(("title", "expected"), [
    ("Chương 12", 12),
    ("Thứ 13 chương", 13),
    ("**Chương 14**", 14),
    ("第15章", 15),
    ("第十六章", 16),
    ("正文 第17章", 17),
])
def test_chapter_title_patterns(title: str, expected: int) -> None:
    from app.crawlers.plugins.xsw import chapter_number_from_title

    assert chapter_number_from_title(title) == expected


def test_gemini_api_key_is_saved_to_local_config_not_translation_jobs(tmp_path, monkeypatch) -> None:
    from app import main

    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text("APP_URL=http://localhost\nGOOGLE_API_KEY=old-key\n", encoding="utf-8")
    monkeypatch.setattr(main, "BASE_DIR", project_root / "app")
    monkeypatch.setattr(main.settings, "gemini_api_key", None)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    main._save_gemini_api_key("  new-secret-key  ")

    saved = (project_root / ".env").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=new-secret-key" in saved
    assert "GOOGLE_API_KEY=old-key" not in saved
    assert main.settings.gemini_api_key == "new-secret-key"
    assert main.os.environ["GEMINI_API_KEY"] == "new-secret-key"
