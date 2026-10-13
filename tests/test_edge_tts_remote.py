import sys
import types

import pytest

from app.tts import edge_tts_service


def test_edge_tts_defaults_to_nam_minh() -> None:
    assert edge_tts_service.normalize_voice(None) == "vi-VN-NamMinhNeural"
    assert edge_tts_service.normalize_voice("unsupported") == "vi-VN-NamMinhNeural"


def test_tts_chunker_preserves_natural_sentence_boundaries() -> None:
    sentences = [f"Câu thứ {index}, có một mệnh đề ngắn để giữ nhịp đọc tự nhiên." for index in range(1, 22)]
    text = "Ông Mr. An đo được 3.14 mét, rồi hỏi: \"Ta... vẫn ổn chứ?!\" " + " ".join(sentences)

    chunks = edge_tts_service.split_chapter_text(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= edge_tts_service.MAX_CHUNK_CHARS for chunk in chunks)
    assert all(chunk[-1] in ".!?…。！？\"”'”" for chunk in chunks)
    assert " ".join(chunks).replace("\n", " ").split() == text.split()
    assert all("Mr." not in chunk or "Mr. An" in chunk for chunk in chunks)
    assert all("3.14" not in chunk or "3.14 mét" in chunk for chunk in chunks)


def test_tts_chunker_keeps_short_paragraph_and_dialogue_together() -> None:
    text = "\"Ngươi là ai?\"\n\n\"Ta... là người canh giữ nơi này.\""

    chunks = edge_tts_service.split_chapter_text(text)

    assert chunks == [text]


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


@pytest.mark.asyncio
async def test_streamed_segment_is_cached_and_reused(monkeypatch, tmp_path) -> None:
    calls = []

    async def fake_stream(text, *, voice, rate):
        calls.append((text, voice, rate))
        yield b"mp3-frame-1"
        yield b"mp3-frame-2"

    monkeypatch.setattr(edge_tts_service.settings, "audio_dir", str(tmp_path))
    monkeypatch.setattr(edge_tts_service, "stream_edge_tts", fake_stream)
    expected_path = tmp_path / edge_tts_service.audio_filename(
        text="segment text", voice="vi-VN-HoaiMyNeural", rate=1.2, chapter_id=42,
    )

    first = b"".join([
        frame async for frame in edge_tts_service.stream_cached_edge_tts(
            "segment text", chapter_id=42, voice="vi-VN-HoaiMyNeural", rate=1.2,
        )
    ])
    second = b"".join([
        frame async for frame in edge_tts_service.stream_cached_edge_tts(
            "segment text", chapter_id=42, voice="vi-VN-HoaiMyNeural", rate=1.2,
        )
    ])

    assert first == second == b"mp3-frame-1mp3-frame-2"
    assert expected_path.read_bytes() == first
    assert calls == [("segment text", "vi-VN-HoaiMyNeural", 1.2)]


@pytest.mark.asyncio
async def test_edge_tts_stream_retries_before_first_audio_frame(monkeypatch) -> None:
    calls = 0

    class Communicate:
        def __init__(self, text, voice, rate) -> None:
            pass

        async def stream(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient no-audio response")
            yield {"type": "audio", "data": b"audio-frame"}

    monkeypatch.setattr(edge_tts_service.settings, "tts_api_url", None)
    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Communicate))

    frames = [frame async for frame in edge_tts_service.stream_edge_tts("Test", voice="vi-VN-NamMinhNeural")]

    assert frames == [b"audio-frame"]
    assert calls == 2


@pytest.mark.asyncio
async def test_edge_tts_stream_does_not_retry_after_audio_started(monkeypatch) -> None:
    calls = 0

    class Communicate:
        def __init__(self, text, voice, rate) -> None:
            pass

        async def stream(self):
            nonlocal calls
            calls += 1
            yield {"type": "audio", "data": b"first-frame"}
            raise RuntimeError("connection lost after first frame")

    monkeypatch.setattr(edge_tts_service.settings, "tts_api_url", None)
    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Communicate))
    frames = []

    with pytest.raises(edge_tts_service.EdgeTTSUnavailableError):
        async for frame in edge_tts_service.stream_edge_tts("Test", voice="vi-VN-HoaiMyNeural"):
            frames.append(frame)

    assert frames == [b"first-frame"]
    assert calls == 1


@pytest.mark.asyncio
async def test_remote_html_stream_falls_back_before_emitting_audio(monkeypatch) -> None:
    class Response:
        headers = {"content-type": "text/html"}
        def raise_for_status(self):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def aiter_bytes(self):
            yield b"<html>Login</html>"

    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def stream(self, *args, **kwargs):
            return Response()

    class Communicate:
        def __init__(self, *args, **kwargs):
            pass
        async def stream(self):
            yield {"type": "audio", "data": b"real-audio"}

    monkeypatch.setattr(edge_tts_service.settings, "tts_api_url", "https://tts.example")
    monkeypatch.setattr(edge_tts_service.httpx, "AsyncClient", Client)
    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Communicate))
    frames = [frame async for frame in edge_tts_service.stream_edge_tts("Test")]
    assert frames == [b"real-audio"]


@pytest.mark.asyncio
async def test_unavailable_remote_synthesis_uses_builtin(monkeypatch, tmp_path) -> None:
    async def unavailable(*args, **kwargs):
        raise edge_tts_service.EdgeTTSUnavailableError("remote is offline")

    class Communicate:
        def __init__(self, *args, **kwargs):
            pass
        async def save(self, path):
            from pathlib import Path
            Path(path).write_bytes(b"real-audio")

    monkeypatch.setattr(edge_tts_service.settings, "tts_api_url", "https://tts.example")
    monkeypatch.setattr(edge_tts_service.settings, "audio_dir", str(tmp_path))
    monkeypatch.setattr(edge_tts_service, "synthesize_remote_tts", unavailable)
    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Communicate))
    audio = await edge_tts_service.synthesize_edge_tts("Test fallback")
    assert audio.path.read_bytes() == b"real-audio"


def test_tts_direct_response_returns_audio_without_second_request(monkeypatch, tmp_path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import main

    path = tmp_path / "test.mp3"
    path.write_bytes(b"test-audio")

    async def synthesize(*args, **kwargs):
        return edge_tts_service.EdgeAudio(path=path, url="/api/tts/audio/test.mp3")

    monkeypatch.setattr(main, "synthesize_edge_tts", synthesize)
    app = FastAPI()
    app.post("/api/tts", response_model=None)(main.api_tts)
    with TestClient(app) as client:
        direct = client.post("/api/tts?direct_audio=true", json={"text": "Test"})
        assert direct.status_code == 200
        assert direct.headers["content-type"] == "audio/mpeg"
        assert direct.content == b"test-audio"
        legacy = client.post("/api/tts", json={"text": "Test"})
        assert legacy.json() == {"audio_url": "/api/tts/audio/test.mp3"}
