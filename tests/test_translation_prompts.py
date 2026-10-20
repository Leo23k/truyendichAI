from app.translator.prompt_service import (
    create_prompt,
    delete_prompt,
    provider_settings,
    resolve_prompt,
    set_default_prompt,
    update_prompt,
    validate_prompt,
)


def test_prompt_requires_named_template_with_text_slot() -> None:
    assert validate_prompt("  Dịch truyện  ", "Giữ nguyên văn phong.\n{{text}}") == (
        "Dịch truyện", "Giữ nguyên văn phong.\n{{text}}",
    )
    for name, template in (("", "{{text}}"), ("dịch", "Không có slot"), ("x" * 121, "{{text}}")):
        try:
            validate_prompt(name, template)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid prompt should be rejected")


def test_provider_options_validate_gemini_requirements(monkeypatch) -> None:
    monkeypatch.setattr("app.translator.prompt_service.settings.gemini_api_key", "")
    assert provider_settings("hachimi") == {
        "provider": "hachimi", "model": "hachimi-local", "web_profile": None,
    }
    try:
        provider_settings("gemini_api")
    except ValueError as error:
        assert "GEMINI_API_KEY" in str(error)
    else:
        raise AssertionError("Gemini API should require a server-side key")
    try:
        provider_settings("gemini_web")
    except ValueError as error:
        assert "tài khoản" in str(error)
    else:
        raise AssertionError("Gemini Web should require a selected profile")


def test_prompt_crud_and_default_snapshot() -> None:
    class Remote:
        def __init__(self) -> None:
            self.rows_by_id: dict[str, dict] = {}
            self.patches: list[tuple[str, dict, dict]] = []
            self.deleted: list[tuple[str, dict]] = []

        def insert(self, table: str, payload: dict) -> dict:
            row = {"id": "c7e6a0b6-291a-41e0-a1ea-2b139783cce1", "is_default": False, **payload}
            self.rows_by_id[row["id"]] = row
            return row

        def rows(self, table: str, params: dict[str, str]) -> list[dict]:
            if params.get("id"):
                target_id = params["id"].removeprefix("eq.")
                row = self.rows_by_id.get(target_id)
                return [row] if row else []
            if params.get("is_default") == "eq.true":
                return [row for row in self.rows_by_id.values() if row.get("is_default")]
            return list(self.rows_by_id.values())[:1]

        def patch_rows(self, table: str, params: dict[str, str], payload: dict) -> list[dict]:
            self.patches.append((table, params, payload))
            target_id = params.get("id", "").removeprefix("eq.")
            rows = [self.rows_by_id[target_id]] if target_id in self.rows_by_id else list(self.rows_by_id.values())
            for row in rows:
                row.update(payload)
            return rows

        def delete_rows(self, table: str, params: dict[str, str]) -> None:
            self.deleted.append((table, params))
            self.rows_by_id.pop(params["id"].removeprefix("eq."), None)

    remote = Remote()
    created = create_prompt(remote, "Tiên hiệp", "Dịch tự nhiên.\n{{text}}")
    snapshot = resolve_prompt(remote, created["id"])
    assert snapshot["prompt_name"] == "Tiên hiệp"
    assert snapshot["prompt_template"] == "Dịch tự nhiên.\n{{text}}"

    update_prompt(remote, created["id"], "Cổ phong", "Giữ Hán Việt.\n{{text}}")
    assert resolve_prompt(remote, created["id"])["prompt_name"] == "Cổ phong"
    set_default_prompt(remote, created["id"])
    assert remote.rows_by_id[created["id"]]["is_default"] is True
    delete_prompt(remote, created["id"])
    assert remote.deleted
