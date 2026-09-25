import logging
import unicodedata

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cdn import CdnDataError, load_translation_memory
from app.config import settings
from app.models import TranslationMemory
from app.translator.glossary import GlossaryEntry, ProtectedText, protect, restore
from app.translator.preprocess import traditional_to_simplified

logger = logging.getLogger(__name__)

KIEM_TOI_GLOSSARY = (
    GlossaryEntry("陈平安", "Trần Bình An"),
    GlossaryEntry("宁姚", "Ninh Diêu"),
    GlossaryEntry("宋集薪", "Tống Tập Tân"),
    GlossaryEntry("齐静春", "Tề Tĩnh Xuân"),
    GlossaryEntry("刘羡阳", "Lưu Tiện Dương"),
    GlossaryEntry("顾粲", "Cố Xán"),
    GlossaryEntry("阮秀", "Nguyễn Tú"),
    GlossaryEntry("阮邛", "Nguyễn Cung"),
    GlossaryEntry("李宝瓶", "Lý Bảo Bình"),
    GlossaryEntry("李槐", "Lý Hòe"),
    GlossaryEntry("崔瀺", "Thôi Sàm"),
    GlossaryEntry("崔东山", "Thôi Đông Sơn"),
    GlossaryEntry("郑居中", "Trịnh Cư Trung"),
    GlossaryEntry("谢狗", "Tạ Cẩu"),
    GlossaryEntry("小陌", "Tiểu Mạch"),
)

# Metadata is short and prominently displayed.  Keep common genre/title terms
# in their established Hán-Việt form instead of allowing a conversational
# Vietnamese paraphrase from the MT model.
METADATA_HANVIET_GLOSSARY = (
    GlossaryEntry("开局", "Khai Cục"), GlossaryEntry("长生", "Trường Sinh"),
    GlossaryEntry("修仙", "Tu Tiên"), GlossaryEntry("重生", "Trọng Sinh"),
    GlossaryEntry("穿越", "Xuyên Việt"), GlossaryEntry("无敌", "Vô Địch"),
    GlossaryEntry("仙侠", "Tiên Hiệp"), GlossaryEntry("玄幻", "Huyền Huyễn"),
    GlossaryEntry("武侠", "Võ Hiệp"), GlossaryEntry("江湖", "Giang Hồ"),
    GlossaryEntry("大道", "Đại Đạo"), GlossaryEntry("剑道", "Kiếm Đạo"),
    GlossaryEntry("诸天", "Chư Thiên"), GlossaryEntry("神魔", "Thần Ma"),
    GlossaryEntry("大帝", "Đại Đế"), GlossaryEntry("轮回", "Luân Hồi"),
    GlossaryEntry("天命", "Thiên Mệnh"), GlossaryEntry("世家", "Thế Gia"),
    GlossaryEntry("王朝", "Vương Triều"), GlossaryEntry("皇朝", "Hoàng Triều"),
    GlossaryEntry("仙帝", "Tiên Đế"), GlossaryEntry("魔帝", "Ma Đế"),
    GlossaryEntry("师兄", "Sư Huynh"), GlossaryEntry("师姐", "Sư Tỷ"),
    GlossaryEntry("师尊", "Sư Tôn"), GlossaryEntry("宗门", "Tông Môn"),
)


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

    def translate_metadata(self, text: str, db: Session | None = None) -> str:
        """Translate book titles and author names with a formal Hán-Việt bias."""
        if not text or not text.strip():
            return text
        source = normalize_text(traditional_to_simplified(text))
        entries = [*glossary_entries(db, source), *METADATA_HANVIET_GLOSSARY]
        protected = protect(source, entries)
        # A wider beam is inexpensive for short metadata and favors literal,
        # title-like wording over a fluent paraphrase.
        translated = postprocess_v9(source, self._translate_text(protected, beam_size=4), entries)
        return restore(translated, protected)

    def translate_batch(self, texts: list[str], entries: list[GlossaryEntry] | None = None) -> list[str]:
        if not texts:
            return []
        glossary = entries or []
        protected_texts = [
            protect(normalize_text(traditional_to_simplified(text)), glossary) if glossary else ProtectedText(normalize_text(traditional_to_simplified(text)), ())
            for text in texts
        ]
        try:
            response = httpx.post(f"{settings.model_service_url.rstrip('/')}/v1/translate-batch", json={"texts": [item.text for item in protected_texts], "beam_size": settings.model_beam_size}, timeout=settings.model_service_timeout, trust_env=False)
            response.raise_for_status()
            translated = response.json()["translated_texts"]
            if not isinstance(translated, list) or len(translated) != len(texts):
                raise ValueError("Invalid batch translation response")
            return [restore(normalize_text(str(value)), protected) for value, protected in zip(translated, protected_texts)]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise TranslationUnavailableError("Không thể kết nối tới model service HachimiMT. Hãy kiểm tra service đang chạy.") from exc

    def _translate_text(self, protected: ProtectedText, beam_size: int | None = None) -> str:
        try:
            response = httpx.post(
                f"{settings.model_service_url.rstrip('/')}/v1/translate",
                json={"text": protected.text, "beam_size": beam_size or settings.model_beam_size},
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


def glossary_entries(_db: Session | None, source: str) -> list[GlossaryEntry]:
    entries: list[GlossaryEntry] = []
    if _db is not None:
        rows = [
            {"source_text": row.source_text, "replacement_text": row.replacement_text}
            for row in _db.scalars(select(TranslationMemory).order_by(TranslationMemory.source_text.asc())).all()
        ]
    else:
        try:
            rows = load_translation_memory()
        except CdnDataError:
            logger.warning("Supabase translation memory unavailable; translating without glossary")
            return entries
    for row in reversed(rows):
        keys = [traditional_to_simplified(part.strip()) for part in str(row["source_text"]).split("|") if part.strip()]
        if not keys:
            continue
        entries.append(GlossaryEntry(keys[0], str(row["replacement_text"]), tuple(keys[1:])))
    return entries


def glossary_for_source_url(source_url: str) -> list[GlossaryEntry]:
    """Combine the shared glossary with terms safe only for one novel source."""
    entries = list(glossary_entries(None, source_url))
    if "xsw.tw/37/" in source_url:
        entries.extend(KIEM_TOI_GLOSSARY)
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
