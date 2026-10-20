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
