from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx

from app.config import settings


DEFAULT_EDGE_VOICE = "vi-VN-HoaiMyNeural"
SUPPORTED_EDGE_VOICES = {"vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"}
MIN_CHUNK_CHARS = 1000
MAX_CHUNK_CHARS = 2000
EDGE_TTS_TIMEOUT_SECONDS = 120
logger = logging.getLogger(__name__)


class EdgeTTSUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class EdgeAudio:
    path: Path
    url: str


@dataclass(frozen=True)
class EdgeAudioSegment:
    index: int
    path: Path
    url: str
    duration_seconds: float
    cache_hit: bool


@dataclass(frozen=True)
class EdgeChapterPlaylist:
    chapter_id: int
    voice: str
    rate: float
    segments: list[EdgeAudioSegment]
    cache_hit: bool

    @property
    def total_duration_seconds(self) -> float:
        return sum(segment.duration_seconds for segment in self.segments)


def content_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_voice(voice: str | None) -> str:
    return voice if voice in SUPPORTED_EDGE_VOICES else DEFAULT_EDGE_VOICE


def normalize_rate(rate: float | int | str | None) -> float:
    try:
        value = float(rate)
    except (TypeError, ValueError):
        value = 1.0
    return max(0.5, min(2.0, value))


def edge_rate(rate: float) -> str:
    percent = round((rate - 1.0) * 100)
    return f"{percent:+d}%"


def safe_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")


def rate_key(rate: float) -> str:
    return f"{rate:.2f}".replace(".", "_")


def audio_filename(*, text: str, voice: str, rate: float, chapter_id: int | None = None) -> str:
    digest = content_key(text)[:16]
    chapter = f"chapter-{chapter_id}" if chapter_id is not None else "tts"
    return f"{chapter}-{safe_part(voice)}-{rate_key(rate)}-{digest}.mp3"


def chapter_cache_stem(*, chapter_id: int, voice: str, rate: float) -> str:
    return f"chapter-{chapter_id}-{safe_part(voice)}-{rate_key(rate)}"


def chapter_segment_filename(*, chapter_id: int, voice: str, rate: float, index: int) -> str:
    return f"{chapter_cache_stem(chapter_id=chapter_id, voice=voice, rate=rate)}-part-{index:03d}.mp3"


def chapter_manifest_path(*, chapter_id: int, voice: str, rate: float) -> Path:
    return settings.audio_path / f"{chapter_cache_stem(chapter_id=chapter_id, voice=voice, rate=rate)}.json"


async def synthesize_remote_tts(text: str, *, voice: str, rate: float, path: Path) -> None:
    if not settings.tts_api_url:
        raise EdgeTTSUnavailableError("TTS service is not configured.")
    try:
        async with httpx.AsyncClient(timeout=EDGE_TTS_TIMEOUT_SECONDS, trust_env=False) as client:
            response = await client.post(
                settings.tts_api_url,
                json={"text": text, "voice": voice, "rate": rate},
            )
            response.raise_for_status()
            audio_url = response.json()["audio_url"]
            audio_response = await client.get(urljoin(settings.tts_api_url, audio_url))
            audio_response.raise_for_status()
        path.write_bytes(audio_response.content)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise EdgeTTSUnavailableError("Khong the tao audio bang TTS service.") from exc


def split_long_paragraph(paragraph: str) -> list[str]:
    pieces: list[str] = []
    remaining = paragraph.strip()
    while len(remaining) > MAX_CHUNK_CHARS:
        window = remaining[:MAX_CHUNK_CHARS]
        split_at = max(window.rfind(mark) for mark in ("。", "！", "？", ".", "!", "?", "\n", " "))
        if split_at < MIN_CHUNK_CHARS:
            split_at = MAX_CHUNK_CHARS
        pieces.append(remaining[: split_at + 1].strip())
        remaining = remaining[split_at + 1 :].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def split_chapter_text(text: str) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n|\r\n\s*\r\n", text or "") if part.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n\n".join(current).strip())
            current = []
            current_len = 0

    for paragraph in paragraphs:
        for piece in split_long_paragraph(paragraph):
            piece_len = len(piece)
            separator_len = 2 if current else 0
            if current and current_len + separator_len + piece_len > MAX_CHUNK_CHARS:
                flush()
            current.append(piece)
            current_len += separator_len + piece_len
            if current_len >= MIN_CHUNK_CHARS:
                flush()
    flush()
    return chunks


def mp3_duration_seconds(path: Path) -> float:
    bitrates = {
        "1": {
            1: [None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
            2: [None, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
            3: [None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
        },
        "2": {
            1: [None, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
            2: [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
            3: [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
        },
    }
    sample_rates = {
        3: [44100, 48000, 32000],
        2: [22050, 24000, 16000],
        0: [11025, 12000, 8000],
    }
    data = path.read_bytes()
    offset = 0
    if data.startswith(b"ID3") and len(data) > 10:
        size = 0
        for byte in data[6:10]:
            size = (size << 7) | (byte & 0x7F)
        offset = 10 + size

    duration = 0.0
    i = offset
    while i + 4 <= len(data):
        if data[i] != 0xFF or data[i + 1] & 0xE0 != 0xE0:
            i += 1
            continue

        version_bits = (data[i + 1] >> 3) & 0x03
        layer_bits = (data[i + 1] >> 1) & 0x03
        bitrate_index = (data[i + 2] >> 4) & 0x0F
        sample_rate_index = (data[i + 2] >> 2) & 0x03
        padding = (data[i + 2] >> 1) & 0x01
        if version_bits == 1 or layer_bits == 0 or bitrate_index in (0, 15) or sample_rate_index == 3:
            i += 1
            continue

        version_key = "1" if version_bits == 3 else "2"
        layer = 4 - layer_bits
        bitrate = bitrates[version_key][layer][bitrate_index]
        sample_rate = sample_rates[version_bits][sample_rate_index]
        if not bitrate or not sample_rate:
            i += 1
            continue

        samples_per_frame = 384 if layer == 1 else (1152 if version_key == "1" or layer == 2 else 576)
        if layer == 1:
            frame_size = int((12 * bitrate * 1000 / sample_rate + padding) * 4)
        else:
            coefficient = 144 if version_key == "1" or layer == 2 else 72
            frame_size = int(coefficient * bitrate * 1000 / sample_rate + padding)
        if frame_size <= 0:
            i += 1
            continue

        duration += samples_per_frame / sample_rate
        i += frame_size

    if duration > 0:
        return round(duration, 3)
    return round(path.stat().st_size * 8 / 48000, 3)


async def synthesize_edge_tts(
    text: str,
    *,
    voice: str | None = None,
    rate: float | int | str | None = 1.0,
    chapter_id: int | None = None,
) -> EdgeAudio:
    text = (text or "").strip()
    if not text:
        raise EdgeTTSUnavailableError("Khong co van ban de tao audio.")

    selected_voice = normalize_voice(voice)
    selected_rate = normalize_rate(rate)
    settings.ensure_directories()
    path = settings.audio_path / audio_filename(
        text=text,
        voice=selected_voice,
        rate=selected_rate,
        chapter_id=chapter_id,
    )
    if path.exists() and path.stat().st_size > 0:
        return EdgeAudio(path=path, url=f"/api/tts/audio/{path.name}")

    if settings.tts_api_url:
        await synthesize_remote_tts(text, voice=selected_voice, rate=selected_rate, path=path)
        if path.exists() and path.stat().st_size > 0:
            return EdgeAudio(path=path, url=f"/api/tts/audio/{path.name}")
        raise EdgeTTSUnavailableError("TTS service did not return audio.")

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            import edge_tts

            communicate = edge_tts.Communicate(text, selected_voice, rate=edge_rate(selected_rate))
            await asyncio.wait_for(communicate.save(str(path)), timeout=EDGE_TTS_TIMEOUT_SECONDS)
            if path.exists() and path.stat().st_size > 0:
                return EdgeAudio(path=path, url=f"/api/tts/audio/{path.name}")
        except Exception as exc:  # pragma: no cover - network/service dependent
            last_error = exc
            logger.warning(
                "Edge TTS attempt failed voice=%s rate=%s chapter_id=%s chars=%s attempt=%s/2",
                selected_voice,
                selected_rate,
                chapter_id,
                len(text),
                attempt + 1,
                exc_info=True,
            )
            if path.exists():
                path.unlink(missing_ok=True)
            await asyncio.sleep(0.4)

    raise EdgeTTSUnavailableError("Khong the tao audio bang Microsoft Edge TTS.") from last_error


async def synthesize_chapter_playlist(
    text: str,
    *,
    chapter_id: int,
    voice: str | None = None,
    rate: float | int | str | None = 1.0,
) -> EdgeChapterPlaylist:
    text = (text or "").strip()
    if not text:
        raise EdgeTTSUnavailableError("Khong co van ban de tao audio.")

    selected_voice = normalize_voice(voice)
    selected_rate = normalize_rate(rate)
    settings.ensure_directories()
    chunks = split_chapter_text(text)
    if not chunks:
        raise EdgeTTSUnavailableError("Khong co van ban de tao audio.")

    text_hash = content_key(text)
    manifest = chapter_manifest_path(chapter_id=chapter_id, voice=selected_voice, rate=selected_rate)
    if manifest.exists():
        try:
            cached = json.loads(manifest.read_text(encoding="utf-8"))
            segments = [
                EdgeAudioSegment(
                    index=item["index"],
                    path=settings.audio_path / item["filename"],
                    url=f"/api/tts/audio/{item['filename']}",
                    duration_seconds=float(item.get("duration_seconds") or 0),
                    cache_hit=True,
                )
                for item in cached.get("segments", [])
            ]
            if cached.get("text_hash") == text_hash and len(segments) == len(chunks) and all(segment.path.exists() and segment.path.stat().st_size > 0 for segment in segments):
                logger.info(
                    "Edge TTS cache hit chapter_id=%s voice=%s segments=%s durations=%s",
                    chapter_id,
                    selected_voice,
                    len(segments),
                    [segment.duration_seconds for segment in segments],
                )
                return EdgeChapterPlaylist(chapter_id=chapter_id, voice=selected_voice, rate=selected_rate, segments=segments, cache_hit=True)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("Edge TTS manifest unreadable chapter_id=%s voice=%s", chapter_id, selected_voice)

    logger.info(
        "Edge TTS cache miss chapter_id=%s voice=%s segments=%s",
        chapter_id,
        selected_voice,
        len(chunks),
    )
    segments: list[EdgeAudioSegment] = []
    for index, chunk in enumerate(chunks):
        filename = chapter_segment_filename(chapter_id=chapter_id, voice=selected_voice, rate=selected_rate, index=index)
        path = settings.audio_path / filename
        if path.exists():
            path.unlink(missing_ok=True)
        audio = await synthesize_edge_tts(chunk, voice=selected_voice, rate=selected_rate)
        audio.path.replace(path)
        duration = mp3_duration_seconds(path)
        segments.append(
            EdgeAudioSegment(
                index=index,
                path=path,
                url=f"/api/tts/audio/{filename}",
                duration_seconds=duration,
                cache_hit=False,
            )
        )
        logger.info(
            "Edge TTS segment generated chapter_id=%s voice=%s segment=%s/%s duration=%s",
            chapter_id,
            selected_voice,
            index + 1,
            len(chunks),
            duration,
        )

    manifest.write_text(
        json.dumps(
            {
                "chapter_id": chapter_id,
                "voice": selected_voice,
                "rate": selected_rate,
                "text_hash": text_hash,
                "segments": [
                    {
                        "index": segment.index,
                        "filename": segment.path.name,
                        "duration_seconds": segment.duration_seconds,
                    }
                    for segment in segments
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(
        "Edge TTS playlist ready chapter_id=%s voice=%s segments=%s durations=%s",
        chapter_id,
        selected_voice,
        len(segments),
        [segment.duration_seconds for segment in segments],
    )
    return EdgeChapterPlaylist(chapter_id=chapter_id, voice=selected_voice, rate=selected_rate, segments=segments, cache_hit=False)
