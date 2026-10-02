import os
import re
from functools import lru_cache
from pathlib import Path
import ctranslate2
from fastapi import FastAPI
from huggingface_hub import snapshot_download
from pydantic import BaseModel, Field
from transformers import AutoTokenizer
MODEL_NAME=os.getenv("MODEL_NAME","ngocdang83/HachimiMT-60-QT")
MODEL_DIR=Path(os.getenv("MODEL_DIR","/app/models/hachimi")); app=FastAPI(title="LeoTruyen Translator")
_EMPTY_DIALOGUE_LINE_RE = re.compile(r'(?m)^[ \t]*(?:""|“”|‘’)[ \t]*\n?')
_BROKEN_MARKER_RE = re.compile(r'(?<![\w<])P([^\n<>]*?(?:\s|[^\x00-\x7f])[^\n<>]*?)\d>')
_UNMATCHED_MARKER_CLOSE_RE = re.compile(r'(?<!<)(?<=[\wÀ-ỹ])>(?=\s+(?:["“]?[A-ZÀ-Ỵ]))')
_PUNCTUATION_ONLY_RE = re.compile(r'^[\s.。．…!！?？,，、;；:："“”\'‘’`~—–\-_=+*#()（）\[\]{}<>《》]+$')

def clean_decoded_text(text):
    text = _EMPTY_DIALOGUE_LINE_RE.sub("", text)
    text = _BROKEN_MARKER_RE.sub(lambda match: match.group(1).strip(), text)
    return _UNMATCHED_MARKER_CLOSE_RE.sub("", text).strip()


def is_punctuation_only(text):
    """Ignore stray source separator lines; translating `。` alone yields `.`."""
    return bool(text and _PUNCTUATION_ONLY_RE.fullmatch(text))
class TranslateRequest(BaseModel):
    text:str=Field(min_length=1); beam_size:int=Field(default=4,ge=1,le=4)
class TranslateBatchRequest(BaseModel):
    texts:list[str]=Field(min_length=1,max_length=4); beam_size:int=Field(default=4,ge=1,le=4)
@lru_cache(maxsize=1)
def engine():
    MODEL_DIR.mkdir(parents=True,exist_ok=True)
    path=Path(snapshot_download(MODEL_NAME,local_dir=MODEL_DIR,allow_patterns=["config.json","source.spm","target.spm","vocab.json","tokenizer_config.json","ct2-int8_float32/*"]))
    return AutoTokenizer.from_pretrained(path),ctranslate2.Translator(str(path/"ct2-int8_float32"),device="cuda",compute_type="int8_float16",inter_threads=2,intra_threads=0)
def translate_texts(texts,beam_size):
    tokenizer,translator=engine(); layouts=[[[] for _ in text.split("\n")] for text in texts]; work=[]
    for ti,text in enumerate(texts):
        for pi,paragraph in enumerate(text.split("\n")):
            if paragraph.strip() and not is_punctuation_only(paragraph):
                tokens=tokenizer.convert_ids_to_tokens(tokenizer(paragraph).input_ids)
                for start in range(0,len(tokens),480): work.append((ti,pi,tokens[start:start+480]))
    size=max(1,int(os.getenv("TRANSLATOR_BATCH_SIZE","8")))
    for start in range(0,len(work),size):
        batch=work[start:start+size]; results=translator.translate_batch([tokens for _,_,tokens in batch],beam_size=beam_size,max_decoding_length=512,end_token="</s>",disable_unk=True)
        for (ti,pi,_),result in zip(batch,results): layouts[ti][pi].append(clean_decoded_text(tokenizer.decode(tokenizer.convert_tokens_to_ids(result.hypotheses[0]),skip_special_tokens=True)))
    return ["\n".join("".join(parts) for parts in paragraphs) for paragraphs in layouts]
@app.get("/health")
def health(): return {"status":"ok","model":MODEL_NAME}
@app.post("/translate")
@app.post("/v1/translate")
def translate(payload:TranslateRequest): return {"translated_text":translate_texts([payload.text],payload.beam_size)[0]}
@app.post("/v1/translate-batch")
def translate_batch(payload:TranslateBatchRequest): return {"translated_texts":translate_texts(payload.texts,payload.beam_size)}
