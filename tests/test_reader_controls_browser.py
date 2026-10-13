"""Browser smoke test for the actual Reader template and its click handlers."""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
import json
import wave

import pytest
from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]


def _page_html():
    chapter = SimpleNamespace(
        id=1, book_id="smoke", chapter_number=1, title="Chương 1",
        translated_title="Chương 1", source_url="", url="",
        original_content="Xin chào", translated_content="Xin chào. Đây là đoạn kiểm tra.",
    )
    environment = Environment(loader=FileSystemLoader(ROOT / "app" / "templates"), autoescape=True)
    return environment.get_template("reader.html").render(
        request=SimpleNamespace(state=SimpleNamespace(user=None)),
        chapter=chapter, previous=None, following=None,
        display_title=chapter.title, display_content=chapter.translated_content,
        raw_display_content=chapter.translated_content, chapter_list=[{"id": 1, "chapter_number": 1, "title": chapter.title}],
        showing_translation=True, synced_reader=True, replacement_rules=[],
        audio_manifest_url="/audio/manifest", audio_stream_url="/audio/stream",
    )


def _audio_bytes():
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 16000)
    return output.getvalue()


@pytest.mark.parametrize("width,mobile", [(1280, False), (390, True)])
@pytest.mark.parametrize("stale_bridge", [False, True])
def test_reader_player_buttons_work_in_browser(width, mobile, stale_bridge):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(channel="chrome", headless=True)
        except playwright.Error as error:
            pytest.skip(f"Chrome unavailable: {error}")
        try:
            context = browser.new_context(viewport={"width": width, "height": 844}, is_mobile=mobile, has_touch=mobile)
            page = context.new_page()
            page.set_default_timeout(3000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            html = _page_html()
            audio = _audio_bytes()

            def serve(route):
                request_url = urlparse(route.request.url)
                if request_url.hostname != "reader.test":
                    return route.continue_()
                path = request_url.path
                if path == "/chapter":
                    return route.fulfill(body=html, content_type="text/html; charset=utf-8")
                if path.startswith("/static/"):
                    script = (ROOT / "app" / path.lstrip("/")).read_bytes()
                    if path == "/static/offline.js" and stale_bridge:
                        assert b"createManager, configureMediaSession, getInitialTtsMode, getPreferredDeviceVoice, setMediaSessionState" in script
                        script = script.replace(
                            b"createManager, configureMediaSession, getInitialTtsMode, getPreferredDeviceVoice, setMediaSessionState",
                            b"createManager, getPreferredDeviceVoice",
                        )
                    return route.fulfill(body=script, content_type="application/javascript")
                if path == "/audio/manifest":
                    return route.fulfill(json={"segments": [{"url": "/audio/segment", "duration": 2}], "total_duration": 2})
                if path == "/audio/segment":
                    return route.fulfill(body=audio, content_type="audio/wav")
                if path == "/service-worker.js":
                    return route.fulfill(body="", content_type="application/javascript")
                return route.fulfill(json={"items": [], "progress": 0})

            page.route("**/*", serve)
            page.goto("http://reader.test/chapter", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(800)
            player = page.locator("[data-reader-player]")
            backdrop = page.locator("[data-sheet-backdrop]")
            assert not errors, json.dumps(errors, ensure_ascii=False)
            if mobile:
                player.locator("[data-mobile-player-toggle]").click()
            diagnostics = lambda: json.dumps({"errors": errors, "state": player.get_attribute("data-state"), "html": page.locator(".reader-player-status").inner_text()}, ensure_ascii=False)

            player.locator("[data-voice-button]").click()
            assert "is-open" in backdrop.get_attribute("class"), diagnostics()
            backdrop.locator('[data-voice-option="vi-VN-HoaiMyNeural"]').click()
            assert page.evaluate('localStorage.getItem("reader.anonymous.reader.voice")') == "vi-VN-HoaiMyNeural", diagnostics()
            player.locator("[data-sleep-button]").click()
            assert "is-open" in backdrop.get_attribute("class"), diagnostics()
            backdrop.locator('[data-sleep-option="15"]').click()
            assert page.evaluate('localStorage.getItem("reader.anonymous.reader.sleepMode")') == "timer", diagnostics()
            page.locator("[data-toc-button]").click()
            assert "is-open" in backdrop.get_attribute("class"), diagnostics()
            backdrop.locator('[data-sheet="toc"] [data-close-sheet]').click()

            player.locator("[data-play-toggle]").click()
            page.wait_for_timeout(500)
            playback = page.locator("[data-reader-audio]").evaluate("audio => ({paused:audio.paused, time:audio.currentTime})")
            assert player.get_attribute("data-state") == "playing" and not playback["paused"] and playback["time"] > 0, diagnostics() + str(playback)
            player.locator("[data-play-toggle]").click()
            assert page.locator("[data-reader-audio]").evaluate("audio => audio.paused"), diagnostics()
            assert not errors, diagnostics()
        finally:
            browser.close()


@pytest.mark.parametrize("availability", ["ready", "late", "missing"])
def test_chrome_mobile_defaults_to_available_google_online_voice(availability):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel="chrome", headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
            )
            page = context.new_page()
            voices = ([{"name": "Google Tiếng Việt 2 (Natural)", "lang": "vi-VN", "voiceURI": "google-vi-2-natural", "localService": False}]
                      if availability != "missing" else [{"name": "Giọng Việt cục bộ", "lang": "vi-VN", "voiceURI": "local-vi", "localService": True}])
            page.add_init_script(f"""
                window.__voicesReady = {str(availability != "late").lower()};
                const voiceListeners = [];
                Object.defineProperty(window, 'speechSynthesis', {{configurable:true, value:{{
                    getVoices:() => window.__voicesReady ? {json.dumps(voices, ensure_ascii=False)} : [],
                    addEventListener(type, listener){{if(type==='voiceschanged')voiceListeners.push(listener);}},
                    cancel(){{}}, pause(){{}}, resume(){{}},
                    speak(utterance){{window.__spokenVoiceURI=utterance.voice?.voiceURI; setTimeout(()=>utterance.onstart?.(),0);}}
                }}}});
                window.SpeechSynthesisUtterance = class {{constructor(text){{this.text=text;}}}};
                if ({str(availability == "late").lower()}) setTimeout(()=>{{window.__voicesReady=true;voiceListeners.forEach(listener=>listener());}},1100);
            """)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            html = _page_html()

            def serve(route):
                path = urlparse(route.request.url).path
                if path == "/chapter":
                    return route.fulfill(body=html, content_type="text/html; charset=utf-8")
                if path.startswith("/static/"):
                    return route.fulfill(body=(ROOT / "app" / path.lstrip("/")).read_bytes(), content_type="application/javascript")
                if path == "/audio/manifest":
                    return route.fulfill(json={"segments": [], "total_duration": 0})
                if path == "/service-worker.js":
                    return route.fulfill(body="", content_type="application/javascript")
                return route.fulfill(json={"items": [], "progress": 0})

            page.route("http://reader.test/**", serve)
            page.goto("http://reader.test/chapter", wait_until="domcontentloaded")
            page.wait_for_timeout(900)
            mode = page.locator("[data-tts-mode]").input_value()
            if availability != "missing":
                page.wait_for_function("document.querySelector('[data-tts-mode]').value === 'device' && document.querySelector('[data-device-voice]').value === 'google-vi-2-natural'", timeout=4000)
                assert page.locator("[data-tts-mode]").input_value() == "device", errors
                assert page.locator("[data-device-voice]").input_value() == "google-vi-2-natural", errors
                page.locator("[data-reader-player] [data-mobile-player-toggle]").click()
                page.locator("[data-reader-player] [data-play-toggle]").click()
                page.wait_for_function("window.__spokenVoiceURI === 'google-vi-2-natural'")
                assert page.locator("[data-reader-player]").get_attribute("data-state") == "playing", errors
            else:
                assert mode == "server", errors
                assert page.evaluate('localStorage.getItem("reader.anonymous.reader.ttsMode")') is None
                page.locator("[data-tts-mode]").select_option("device")
                page.wait_for_timeout(200)
                assert page.locator("[data-tts-mode]").input_value() == "device", errors
            assert not errors
        finally:
            browser.close()
