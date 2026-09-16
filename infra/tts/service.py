import hashlib
import os
from pathlib import Path

import edge_tts
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


VOICES = {"vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"}
CACHE_DIR = Path(os.getenv("TTS_CACHE_DIR", "/app/data/tts_cache"))
app = FastAPI(title="LeoTruyen TTS")


class TTSRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str = "vi-VN-HoaiMyNeural"
    rate: float = Field(default=1.0, ge=0.5, le=2.0)


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
        sign = "+" if payload.rate >= 1 else ""
        await edge_tts.Communicate(payload.text, payload.voice, rate=f"{sign}{round((payload.rate - 1) * 100)}%").save(str(target))
    return {"audio_url": f"/audio/{target.name}"}


@app.get("/audio/{filename}")
def audio(filename: str) -> FileResponse:
    path = (CACHE_DIR / filename).resolve()
    if CACHE_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(path, media_type="audio/mpeg", filename=path.name)
