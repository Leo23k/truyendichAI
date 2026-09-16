import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AudioCache


class TTSUnavailableError(RuntimeError):
    pass


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def synthesize(content: str, chapter_id: int, voice: str, db: Session) -> AudioCache:
    if not content.strip():
        raise TTSUnavailableError("Chương chưa có bản dịch để tạo audio.")
    digest = content_hash(content)
    cached = db.scalar(select(AudioCache).where(AudioCache.chapter_id == chapter_id, AudioCache.content_hash == digest, AudioCache.voice == voice))
    if cached and Path(cached.audio_path).exists():
        return cached
    try:
        import pyttsx3
    except ImportError as exc:
        raise TTSUnavailableError("Chưa cài dịch vụ TTS.") from exc
    output_path = settings.audio_path / f"chapter-{chapter_id}-{digest[:16]}-{voice}.wav"
    engine = pyttsx3.init()
    engine.save_to_file(content, str(output_path))
    engine.runAndWait()
    if not output_path.exists():
        raise TTSUnavailableError("Không thể tạo audio cho chương.")
    cache = AudioCache(chapter_id=chapter_id, content_hash=digest, voice=voice, audio_path=str(output_path))
    db.add(cache)
    db.commit()
    db.refresh(cache)
    return cache
