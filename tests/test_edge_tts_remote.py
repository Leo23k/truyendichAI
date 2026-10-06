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
