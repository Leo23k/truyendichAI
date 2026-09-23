import logging
import unicodedata

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import TranslationMemory
from app.translator.glossary import GlossaryEntry, ProtectedText, protect, restore
from app.translator.preprocess import traditional_to_simplified

logger = logging.getLogger(__name__)


class TranslationUnavailableError(RuntimeError):
    pass


class TranslatorService:
    """Client for the separately hosted local HachimiMT model service."""

    def translate(self, text: str, db: Session | None = None) -> str:
        if not text or not text.strip():
            return text
        source = normalize_text(traditional_to_simplified(text))
        entries = glossary_entries(db, source)
        protected = protect(source, entries) if entries else ProtectedText(source, ())
        translated = postprocess_v9(source, self._translate_text(protected), entries)
        return restore(translated, protected)

    def translate_batch(self, texts: list[str]) -> list[str]:
        return [self.translate(text) for text in texts]

    def _translate_text(self, protected: ProtectedText) -> str:
        try:
            response = httpx.post(
                f"{settings.model_service_url.rstrip('/')}/v1/translate",
                json={"text": protected.text, "beam_size": settings.model_beam_size},
                timeout=settings.model_service_timeout,
                trust_env=False,
            )
            response.raise_for_status()
            translated = response.json()["translated_text"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("Model service translation failed: %s", exc)
            raise TranslationUnavailableError(
                "Không thể kết nối tới model service HachimiMT. Hãy kiểm tra service đang chạy."
            ) from exc
        return normalize_text(translated)


def glossary_entries(db: Session | None, source: str) -> list[GlossaryEntry]:
    if db is None:
        return []
    entries: list[GlossaryEntry] = []
    for row in db.scalars(select(TranslationMemory).order_by(TranslationMemory.source_text.desc())).all():
        keys = [traditional_to_simplified(part.strip()) for part in row.source_text.split("|") if part.strip()]
        if not keys:
            continue
        entries.append(GlossaryEntry(keys[0], row.replacement_text, tuple(keys[1:])))
    return entries


def all_glossary_entries(db: Session | None, source: str) -> list[GlossaryEntry]:
    return glossary_entries(db, source)


def apply_memory(text: str, db: Session) -> str:
    """Backward-compatible helper for existing callers/tests."""
    for entry in glossary_entries(db, text):
        text = text.replace(entry.source, entry.target)
        for alias in entry.aliases:
            text = text.replace(alias, entry.target)
    return text


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def postprocess_v9(source: str, translated: str, entries: list[GlossaryEntry]) -> str:
    """Conservative V9 boundary: glossary is authoritative; text stays intact."""
    # Keep paragraph boundaries and never invent content during postprocess.
    translated = normalize_text(translated).replace("\r\n", "\n").replace("\r", "\n")
    return translated.strip() if source.strip() else translated


translator = TranslatorService()
