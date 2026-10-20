from __future__ import annotations

import re
import shutil
import subprocess
import threading
from pathlib import Path

import httpx

from app.config import settings


PROVIDER_NAMES = {
    "hachimi": "HachimiMT",
    "gemini_api": "Gemini API",
    "gemini_web": "Gemini Web",
}
DEFAULT_PROMPT = (
    "Bạn là dịch giả tiểu thuyết Trung-Việt. Dịch sát nghĩa nhưng tự nhiên, "
    "giữ nguyên tên riêng và các token dạng __GLOSSARY_0__. "
    "Chỉ trả về bản dịch, không giải thích.\n\n{{text}}"
)


class ProviderUnavailableError(RuntimeError):
    pass


class ProviderPausedError(ProviderUnavailableError):
    pass


def render_prompt(template: str, text: str) -> str:
    if "{{text}}" not in template:
        raise ValueError("Prompt dịch phải chứa {{text}}.")
    return template.replace("{{text}}", text)


class HachimiProvider:
    def translate_batch(self, texts: list[str], *, beam_size: int | None = None, **_: object) -> list[str]:
        try:
            response = httpx.post(
                f"{settings.model_service_url.rstrip('/')}/v1/translate-batch",
                json={"texts": texts, "beam_size": beam_size or settings.model_beam_size},
                timeout=settings.model_service_timeout,
                trust_env=False,
            )
            response.raise_for_status()
            translated = response.json()["translated_texts"]
            if not isinstance(translated, list) or len(translated) != len(texts):
                raise ValueError("Invalid batch translation response")
            return [str(value) for value in translated]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ProviderUnavailableError(
                "Không thể kết nối tới model service HachimiMT. Hãy kiểm tra service đang chạy."
            ) from exc

    def translate_text(self, text: str, *, beam_size: int | None = None) -> str:
        try:
            response = httpx.post(
                f"{settings.model_service_url.rstrip('/')}/v1/translate",
                json={"text": text, "beam_size": beam_size or settings.model_beam_size},
                timeout=settings.model_service_timeout,
                trust_env=False,
            )
            response.raise_for_status()
            return str(response.json()["translated_text"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ProviderUnavailableError(
                "Không thể kết nối tới model service HachimiMT. Hãy kiểm tra service đang chạy."
            ) from exc


class GeminiApiProvider:
    def translate_batch(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        prompt_template: str = DEFAULT_PROMPT,
        **_: object,
    ) -> list[str]:
        api_key = settings.gemini_api_key
        if not api_key:
            raise ProviderUnavailableError("Chưa cấu hình GEMINI_API_KEY cho Gemini API.")
        model_name = model or settings.gemini_model
        results: list[str] = []
        for text in texts:
            try:
                response = httpx.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                    headers={"x-goog-api-key": api_key},
                    json={
                        "contents": [{"parts": [{"text": render_prompt(prompt_template, text)}]}],
                        "generationConfig": {"temperature": 0.2},
                    },
                    timeout=settings.model_service_timeout,
                    trust_env=False,
                )
                response.raise_for_status()
                parts = response.json()["candidates"][0]["content"]["parts"]
                translated = "".join(str(part.get("text") or "") for part in parts).strip()
                if not translated:
                    raise ValueError("Gemini returned an empty response")
                results.append(translated)
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                raise ProviderUnavailableError(f"Gemini API không thể dịch bằng model {model_name}.") from exc
        return results


class GeminiWebProvider:
    _lock = threading.Lock()
    _profile_pattern = re.compile(r"[^a-zA-Z0-9_-]+")
    _browser_labels = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}

    @classmethod
    def browser_executable(cls, browser: str) -> Path:
        if browser not in cls._browser_labels:
            raise ValueError("Trình duyệt Gemini Web không được hỗ trợ.")
        names = {"chrome": ("chrome.exe", "chrome"), "edge": ("msedge.exe", "msedge")}[browser]
        candidates = [Path(found) for name in names if (found := shutil.which(name))]
        if browser == "chrome":
            candidates.extend([
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
            ])
        else:
            candidates.extend([
                Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
                Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                Path.home() / "AppData/Local/Microsoft/Edge/Application/msedge.exe",
            ])
        executable = next((candidate for candidate in candidates if candidate.is_file()), None)
        if executable is None:
            raise ProviderUnavailableError(f"Không tìm thấy {cls._browser_labels[browser]} trên máy này.")
        return executable

    @classmethod
    def available_browsers(cls) -> list[dict[str, str]]:
        available = []
        for browser, label in cls._browser_labels.items():
            try:
                cls.browser_executable(browser)
            except ProviderUnavailableError:
                continue
            available.append({"id": browser, "name": label})
        return available

    @classmethod
    def default_browser(cls) -> str:
        available = [browser["id"] for browser in cls.available_browsers()]
        preferred = None
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                prog_id = winreg.QueryValueEx(key, "ProgId")[0]
            preferred = "edge" if "MSEdge" in prog_id else "chrome" if "ChromeHTML" in prog_id else None
        except (ImportError, OSError):
            pass
        if preferred in available:
            return preferred
        return "edge" if "edge" in available else (available[0] if available else "chrome")

    @classmethod
    def profile_identity(cls, name: str, browser: str | None = None) -> tuple[str, str]:
        if browser is None and ":" in name:
            browser, name = name.split(":", 1)
        browser = browser or "chrome"
        if browser not in cls._browser_labels:
            raise ValueError("Trình duyệt Gemini Web không được hỗ trợ.")
        return browser, cls.normalize_profile(name)

    @classmethod
    def playwright_channel(cls, browser: str) -> str:
        if browser not in cls._browser_labels:
            raise ValueError("Trình duyệt Gemini Web không được hỗ trợ.")
        return "msedge" if browser == "edge" else "chrome"

    @classmethod
    def normalize_profile(cls, name: str) -> str:
        normalized = cls._profile_pattern.sub("-", name.strip()).strip("-")
        if not normalized:
            raise ValueError("Tên tài khoản Gemini Web không hợp lệ.")
        return normalized[:64]

    @classmethod
    def profile_dir(cls, name: str, browser: str | None = None) -> Path:
        browser, profile_name = cls.profile_identity(name, browser)
        root = settings.gemini_profile_path
        return root / profile_name if browser == "chrome" else root / browser / profile_name

    @classmethod
    def list_profiles(cls) -> list[dict[str, object]]:
        root = settings.gemini_profile_path
        if not root.exists():
            return []
        return [
            {
                "name": item.name,
                "ready": (item / "Default" / "Cookies").exists()
                or (item / "Default" / "Network" / "Cookies").exists(),
            }
            for item in sorted(root.iterdir())
            if item.is_dir()
        ]

    @classmethod
    def open_profile(cls, name: str, browser: str = "chrome") -> str:
        browser, profile_name = cls.profile_identity(name, browser)
        profile = cls.profile_dir(profile_name, browser)
        profile.mkdir(parents=True, exist_ok=True)
        executable = cls.browser_executable(browser)
        subprocess.Popen([str(executable), f"--user-data-dir={profile}", "https://gemini.google.com/app"])
        return f"{browser}:{profile_name}"

    @classmethod
    def logout(cls, name: str, browser: str = "chrome") -> None:
        browser, profile_name = cls.profile_identity(name, browser)
        profile = cls.profile_dir(profile_name, browser)
        profile.mkdir(parents=True, exist_ok=True)
        executable = cls.browser_executable(browser)
        subprocess.Popen([
            str(executable), f"--user-data-dir={profile}",
            "https://accounts.google.com/Logout?continue=https%3A%2F%2Fgemini.google.com%2Fapp",
        ])

    @classmethod
    def launch_context(cls, playwright, web_profile: str):
        browser, _ = cls.profile_identity(web_profile)
        return playwright.chromium.launch_persistent_context(
            str(cls.profile_dir(web_profile)), channel=cls.playwright_channel(browser), headless=False,
        )

    def translate_batch(
        self,
        texts: list[str],
        *,
        prompt_template: str = DEFAULT_PROMPT,
        web_profile: str | None = None,
        **_: object,
    ) -> list[str]:
        if not web_profile:
            raise ProviderPausedError("Hãy chọn tài khoản Gemini Web và đăng nhập trước khi chạy.")
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ProviderUnavailableError("Chưa cài Playwright cho Gemini Web.") from exc

        with self._lock:
            try:
                with sync_playwright() as playwright:
                    context = self.launch_context(playwright, web_profile)
                    try:
                        page = context.pages[0] if context.pages else context.new_page()
                        results = []
                        for index, text in enumerate(texts):
                            results.append(self._translate_one(page, render_prompt(prompt_template, text)))
                            if index < len(texts) - 1 and settings.gemini_web_cooldown_seconds > 0:
                                page.wait_for_timeout(int(settings.gemini_web_cooldown_seconds * 1000))
                    finally:
                        context.close()
                return results
            except PlaywrightTimeoutError as exc:
                raise ProviderPausedError(
                    "Gemini Web không phản hồi đúng hạn. Job đã tạm dừng để bạn kiểm tra trình duyệt."
                ) from exc
            except ProviderUnavailableError:
                raise
            except Exception as exc:
                raise ProviderUnavailableError("Không thể điều khiển phiên Gemini Web.") from exc

    def _translate_one(self, page, prompt: str) -> str:
        page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1_000)
        body_text = page.locator("body").inner_text().lower()
        if any(marker in body_text for marker in ("sign in", "đăng nhập", "verify it's you", "xác minh")):
            raise ProviderPausedError("Phiên Gemini Web cần đăng nhập hoặc xác minh thủ công.")
        if any(marker in body_text for marker in ("usage limit", "đã đạt giới hạn", "try again later", "thử lại sau")):
            raise ProviderPausedError("Gemini Web đang giới hạn lượt dùng. Job đã được tạm dừng.")

        box = page.locator("textarea, [contenteditable='true'][role='textbox'], [contenteditable='true']").last
        box.wait_for(state="visible", timeout=30_000)
        responses = page.locator("message-content, [data-message-author-role='model']")
        before = responses.count()
        box.fill(prompt)
        box.press("Enter")
        page.wait_for_function(
            "([selector, count]) => document.querySelectorAll(selector).length > count",
            arg=["message-content, [data-message-author-role='model']", before],
            timeout=120_000,
        )
        response = responses.last
        previous = ""
        stable = 0
        for _ in range(180):
            page.wait_for_timeout(1_000)
            current = response.inner_text().strip()
            if current and current == previous:
                stable += 1
                if stable >= 3:
                    return current
            else:
                stable = 0
                previous = current
        raise ProviderPausedError("Gemini Web chưa hoàn tất phản hồi. Job đã được tạm dừng.")


def provider_for(name: str):
    providers = {
        "hachimi": HachimiProvider,
        "gemini_api": GeminiApiProvider,
        "gemini_web": GeminiWebProvider,
    }
    try:
        return providers[name]()
    except KeyError as exc:
        raise ValueError("Provider dịch không hợp lệ.") from exc
