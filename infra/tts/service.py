import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import edge_tts
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field


VOICES = {"vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"}
CACHE_DIR = Path(os.getenv("TTS_CACHE_DIR", "/app/data/tts_cache"))
app = FastAPI(title="LeoTruyen TTS")


class TTSRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str = "vi-VN-HoaiMyNeural"
    rate: float = Field(default=1.0, ge=0.5, le=2.0)


def edge_rate(rate: float) -> str:
    return f"{round((rate - 1) * 100):+d}%"


def trim_silence(path: Path) -> None:
    """Remove leading/trailing silence when ffmpeg is available."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return
    trimmed = path.with_suffix(".trimmed.mp3")
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(path), "-af",
             "silenceremove=start_periods=1:start_duration=0.08:start_threshold=-45dB:stop_periods=1:stop_duration=0.12:stop_threshold=-45dB",
             "-codec:a", "libmp3lame", "-q:a", "4", str(trimmed)],
            check=True,
            capture_output=True,
        )
        if trimmed.exists() and trimmed.stat().st_size > 0:
            trimmed.replace(path)
    except (OSError, subprocess.CalledProcessError):
        pass
    finally:
        trimmed.unlink(missing_ok=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tts")
async def synthesize(payload: TTSRequest) -> dict[str, str]:
    if payload.voice not in VOICES:
        raise HTTPException(status_code=400, detail="Unsupported Vietnamese voice")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(f"{payload.voice}:{payload.rate}:{payload.text}".encode()).hexdigest()
    target = CACHE_DIR / f"{cache_key}.mp3"
    if not target.exists():
        await edge_tts.Communicate(payload.text, payload.voice, rate=edge_rate(payload.rate)).save(str(target))
        trim_silence(target)
    return {"audio_url": f"/audio/{target.name}"}


@app.post("/tts/stream")
async def stream_synthesize(payload: TTSRequest) -> StreamingResponse:
    """Yield Edge audio frames immediately instead of waiting for a complete MP3."""
    if payload.voice not in VOICES:
        raise HTTPException(status_code=400, detail="Unsupported Vietnamese voice")

    async def audio_frames():
        communicate = edge_tts.Communicate(payload.text, payload.voice, rate=edge_rate(payload.rate))
        async for event in communicate.stream():
            if event["type"] == "audio":
                yield event["data"]

    return StreamingResponse(audio_frames(), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.get("/audio/{filename}")
def audio(filename: str) -> FileResponse:
    path = (CACHE_DIR / filename).resolve()
    if CACHE_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(path, media_type="audio/mpeg", filename=path.name)
