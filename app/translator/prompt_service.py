from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from app.config import settings
from app.translator.providers import DEFAULT_PROMPT


PROVIDERS = {"hachimi", "gemini_api", "gemini_web"}
DEFAULT_PROMPT_NAME = "Tiên hiệp tự nhiên"
DEFAULT_MODELS = {
    "hachimi": "hachimi-local",
    "gemini_api": "gemini-3.5-flash-lite",
    "gemini_web": "gemini-web",
}
GEMINI_API_MODELS = (
    ("gemini-4-argon", "Gemini 4 Argon"),
    ("gemini-3.8-flash", "Gemini 3.8 Flash"),
    ("gemini-3.6-flash", "Gemini 3.6 Flash"),
    ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite"),
    ("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite"),
    ("gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview"),
    ("gemini-3-flash-preview", "Gemini 3 Flash Preview"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite"),
    ("gemini-2.5-pro", "Gemini 2.5 Pro"),
)
MAX_PROMPT_LENGTH = 12000


def validate_prompt(name: str, template: str) -> tuple[str, str]:
    name = " ".join(name.split())
    template = template.strip()
    if not name or len(name) > 120:
        raise ValueError("Tên prompt phải có từ 1 đến 120 ký tự.")
    if not template or len(template) > MAX_PROMPT_LENGTH or "{{text}}" not in template:
        raise ValueError(f"Prompt phải có {{text}} và không quá {MAX_PROMPT_LENGTH} ký tự.")
    return name, template


def provider_settings(provider: str, model: str = "", web_profile: str = "") -> dict[str, str | None]:
    if provider not in PROVIDERS:
        raise ValueError("Provider dịch không hợp lệ.")
    model = (model or DEFAULT_MODELS[provider]).strip()
    if provider == "gemini_api" and model not in {model_id for model_id, _ in GEMINI_API_MODELS}:
        raise ValueError("Hãy chọn một model Gemini API trong danh sách.")
    if provider == "gemini_api" and not settings.gemini_api_key:
        raise ValueError("Chưa cấu hình GEMINI_API_KEY trên máy chạy dịch.")
    # Gemini Web authentication belongs to the user's browser extension, not a
    # server-side Playwright profile. Keep web_profile optional for old queue
    # rows while new extension jobs do not need it.
    return {"provider": provider, "model": model, "web_profile": web_profile.strip() or None}


def resolve_prompt(remote, prompt_id: str) -> dict[str, str | bool]:
    try:
        parsed_id = str(UUID(prompt_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Hãy chọn prompt dịch.") from exc
    rows = remote.rows("translation_prompts", {"select": "id,name,template,is_default", "id": f"eq.{parsed_id}", "limit": "1"})
    if not rows:
        raise ValueError("Prompt đã chọn không còn tồn tại.")
    row = rows[0]
    name, template = validate_prompt(str(row.get("name") or ""), str(row.get("template") or ""))
    return {
        "prompt_id": parsed_id, "prompt_name": name, "prompt_template": template,
        "is_default": bool(row.get("is_default")),
    }


def load_prompts(remote) -> list[dict]:
    return remote.rows(
        "translation_prompts",
        {"select": "id,name,template,is_default,updated_at", "order": "is_default.desc,name.asc"},
    )


def create_prompt(remote, name: str, template: str) -> dict:
    name, template = validate_prompt(name, template)
    return remote.insert("translation_prompts", {"name": name, "template": template})


def update_prompt(remote, prompt_id: str, name: str, template: str) -> dict:
    name, template = validate_prompt(name, template)
    try:
        parsed_id = str(UUID(prompt_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Prompt không hợp lệ.") from exc
    rows = remote.patch_rows(
        "translation_prompts", {"id": f"eq.{parsed_id}"},
        {"name": name, "template": template, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    if not rows:
        raise ValueError("Không tìm thấy prompt để cập nhật.")
    return rows[0]


def set_default_prompt(remote, prompt_id: str) -> None:
    prompt = resolve_prompt(remote, prompt_id)
    remote.patch_rows("translation_prompts", {"is_default": "eq.true"}, {"is_default": False})
    rows = remote.patch_rows(
        "translation_prompts", {"id": f"eq.{prompt['prompt_id']}"}, {"is_default": True},
    )
    if not rows:
        raise ValueError("Không tìm thấy prompt để đặt mặc định.")


def delete_prompt(remote, prompt_id: str) -> None:
    prompt = resolve_prompt(remote, prompt_id)
    remote.delete_rows("translation_prompts", {"id": f"eq.{prompt['prompt_id']}"})
    if prompt["is_default"]:
        remaining = remote.rows("translation_prompts", {"select": "id", "order": "created_at.asc", "limit": "1"})
        if remaining:
            remote.patch_rows("translation_prompts", {"id": f"eq.{remaining[0]['id']}"}, {"is_default": True})
