from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import TranslationMemory
from app.tts.service import content_hash
from app.translator import service as translator_service
from app.translator.service import TranslatorService, apply_memory, postprocess_v9


def test_translation_memory_replacement_and_audio_hash() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(TranslationMemory(source_text="师尊", replacement_text="Sư tôn"))
    session.commit()

    assert apply_memory("师尊 nói chuyện", session) == "Sư tôn nói chuyện"
    assert content_hash("bản dịch") != content_hash("bản dịch đã sửa")


def test_translator_delegates_full_text_to_model_service(monkeypatch) -> None:
    calls = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"translated_text": "Câu một.\r\nCâu hai."}

    def fake_post(url, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return Response()

    monkeypatch.setattr(translator_service.httpx, "post", fake_post)
    monkeypatch.setattr(translator_service.settings, "model_beam_size", 4)

    translated = TranslatorService().translate("第一句。\n第二句。")

    assert translated == "Câu một.\nCâu hai."
    assert len(calls) == 1
    assert calls[0]["json"] == {"text": "第一句。\n第二句。", "beam_size": 4}


def test_postprocess_normalizes_vietnamese_marks() -> None:
    assert postprocess_v9("有", "La\u0300n\r\nhai", []) == "Làn\nhai"


def test_user_glossary_restores_after_model_postprocess(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(TranslationMemory(source_text="陆江仙", replacement_text="Lục Giang Tiên"))
    session.commit()
    calls = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            source = calls[-1]["json"]["text"]
            assert "陆江仙" not in source
            return {"translated_text": " <PH1> đã đến.\r\n"}

    def fake_post(url, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return Response()

    monkeypatch.setattr(translator_service.httpx, "post", fake_post)

    translated = TranslatorService().translate("陆江仙到了。", session)

    assert translated == "Lục Giang Tiên đã đến."


def test_short_titles_and_names_go_through_model_service(monkeypatch) -> None:
    calls = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"translated_text": "Huyền Giám Tiên Tộc"}

    def fake_post(url, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return Response()

    monkeypatch.setattr(translator_service.httpx, "post", fake_post)

    assert TranslatorService().translate("陸江仙") == "Huyền Giám Tiên Tộc"
    assert calls[0]["json"]["text"] == "陆江仙"
