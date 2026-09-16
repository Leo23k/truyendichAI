import os
from functools import lru_cache
from pathlib import Path

import ctranslate2
from fastapi import FastAPI
from huggingface_hub import snapshot_download
from pydantic import BaseModel, Field
from transformers import AutoTokenizer


MODEL_NAME = os.getenv("MODEL_NAME", "ngocdang83/HachimiMT-60-QT")
MODEL_DIR = Path(os.getenv("MODEL_DIR", "/app/models/hachimi"))
app = FastAPI(title="LeoTruyen Translator")


class TranslateRequest(BaseModel):
    text: str = Field(min_length=1)
    beam_size: int = Field(default=1, ge=1, le=4)


@lru_cache(maxsize=1)
def engine() -> tuple[object, ctranslate2.Translator]:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(snapshot_download(
        MODEL_NAME,
        local_dir=MODEL_DIR,
        allow_patterns=[
            "config.json", "source.spm", "target.spm", "vocab.json",
            "tokenizer_config.json", "ct2-int8_float32/*",
        ],
    ))
    tokenizer = AutoTokenizer.from_pretrained(path)
    translator = ctranslate2.Translator(
        str(path / "ct2-int8_float32"), device="cpu", compute_type="int8_float32"
    )
    return tokenizer, translator


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL_NAME}


@app.post("/translate")
@app.post("/v1/translate")
def translate(payload: TranslateRequest) -> dict[str, str]:
    tokenizer, translator = engine()
    tokens = tokenizer.convert_ids_to_tokens(tokenizer(payload.text).input_ids)
    result = translator.translate_batch(
        [tokens], beam_size=payload.beam_size, max_decoding_length=512
    )[0]
    translated = tokenizer.decode(
        tokenizer.convert_tokens_to_ids(result.hypotheses[0]), skip_special_tokens=True
    )
    return {"translated_text": translated}
