import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import edge_tts
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

VOICES = {"vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"}

CACHE_DIR = Path(os.getenv("TTS_CACHE_DIR", "/app/data/tts_cache"))

app = FastAPI(title="LeoTruyen TTS")


class TTSRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str = "vi-VN-HoaiMyNeural"
    rate: float = Field(default=1.0, ge=0.5, le=2.0)


def edge_rate(rate: float) -> str:
    return f"{round((rate - 1) * 100):+d}%"


def split_text(text: str, limit: int = 1200) -> list[str]:
    """Chia văn bản dài thành các đoạn khoảng 1200 ký tự."""
    parts = []
    current = ""

    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()

        if not paragraph:
            continue

        if len(current) + len(paragraph) + 1 <= limit:
            current += ("\n" if current else "") + paragraph
        else:
            if current:
                parts.append(current)
            current = paragraph

    if current:
        parts.append(current)

    return parts


def trim_silence(path: Path) -> None:
    """Cắt khoảng lặng đầu/cuối nếu có ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")

    if not ffmpeg:
        return

    trimmed = path.with_suffix(".trimmed.mp3")

    try:
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(path),
                "-af",
                (
                    "silenceremove="
                    "start_periods=1:"
                    "start_duration=0.08:"
                    "start_threshold=-45dB:"
                    "stop_periods=1:"
                    "stop_duration=0.12:"
                    "stop_threshold=-45dB"
                ),
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "4",
                str(trimmed),
            ],
            check=True,
            capture_output=True,
        )

        if trimmed.exists() and trimmed.stat().st_size > 0:
            trimmed.replace(path)

    except (OSError, subprocess.CalledProcessError):
        logger.warning("Trim silence failed.")

    finally:
        trimmed.unlink(missing_ok=True)


async def generate_mp3(text: str, voice: str, rate: float, target: Path):
    """Sinh MP3, tự chia đoạn và ghép lại."""

    parts = split_text(text)

    logger.info("Generating TTS: %d part(s)", len(parts))

    with tempfile.TemporaryDirectory() as tmpdir:

        tmpdir = Path(tmpdir)
        files = []

        for i, part in enumerate(parts):
            file = tmpdir / f"{i}.mp3"

            await edge_tts.Communicate(
                part,
                voice,
                rate=edge_rate(rate),
            ).save(str(file))

            files.append(file)

        ffmpeg = shutil.which("ffmpeg")

        if ffmpeg and len(files) > 1:

            concat = tmpdir / "list.txt"

            concat.write_text(
                "".join(f"file '{f.name}'\n" for f in files),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat),
                    "-c",
                    "copy",
                    str(target),
                ],
                cwd=tmpdir,
                check=True,
                capture_output=True,
            )

        else:
            shutil.copy(files[0], target)

    await asyncio.to_thread(trim_silence, target)

    logger.info("TTS completed: %s", target.name)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/tts")
async def synthesize(payload: TTSRequest):

    if payload.voice not in VOICES:
        raise HTTPException(400, "Unsupported Vietnamese voice")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cache_key = hashlib.sha256(
        f"{payload.voice}:{payload.rate}:{payload.text}".encode()
    ).hexdigest()

    target = CACHE_DIR / f"{cache_key}.mp3"

    if target.exists():

        logger.info("Cache hit: %s", target.name)

    else:

        logger.info("Cache miss: creating %s", target.name)

        await generate_mp3(
            payload.text,
            payload.voice,
            payload.rate,
            target,
        )

    return {"audio_url": f"/audio/{target.name}"}


@app.post("/tts/stream")
async def stream_synthesize(payload: TTSRequest):

    if payload.voice not in VOICES:
        raise HTTPException(400, "Unsupported Vietnamese voice")

    async def generator():

        communicate = edge_tts.Communicate(
            payload.text,
            payload.voice,
            rate=edge_rate(payload.rate),
        )

        async for event in communicate.stream():

            if event["type"] == "audio":
                yield event["data"]

    return StreamingResponse(
        generator(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "Accept-Ranges": "bytes",
        },
    )


@app.get("/audio/{filename}")
def audio(filename: str):

    path = (CACHE_DIR / filename).resolve()

    if CACHE_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(404, "Audio not found")

    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=path.name,
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=31536000",
        },
    )