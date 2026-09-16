import pytest

from app.tts import edge_tts_service


@pytest.mark.asyncio
async def test_edge_tts_uses_remote_service_when_configured(monkeypatch, tmp_path) -> None:
    calls = []

    class Response:
        def __init__(self, payload=None, content=b"audio") -> None:
            self.payload = payload or {}
            self.content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return self.payload

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url, json):
            calls.append(("post", url, json))
            return Response({"audio_url": "/audio/test.mp3"})

        async def get(self, url):
            calls.append(("get", url, None))
            return Response(content=b"\xff\xfb\x90\x64" + b"\0" * 200)

    monkeypatch.setattr(edge_tts_service.httpx, "AsyncClient", Client)
    monkeypatch.setattr(edge_tts_service.settings, "tts_api_url", "https://tts.example/tts")
    monkeypatch.setattr(edge_tts_service.settings, "audio_dir", str(tmp_path))

    audio = await edge_tts_service.synthesize_edge_tts("Xin chao", voice="vi-VN-HoaiMyNeural", rate=1.2)

    assert audio.path.exists()
    assert audio.url.startswith("/api/tts/audio/")
    assert calls[0] == (
        "post",
        "https://tts.example/tts",
        {"text": "Xin chao", "voice": "vi-VN-HoaiMyNeural", "rate": 1.2},
    )
    assert calls[1] == ("get", "https://tts.example/audio/test.mp3", None)
