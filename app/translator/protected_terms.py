from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from app.translator.glossary import GlossaryEntry


TERMS_PATH = Path(__file__).with_name("protected_terms.json")

HAN_VIET = {
    "玄": "Huyền",
    "鑒": "Giám",
    "鉴": "Giám",
    "仙": "Tiên",
    "族": "Tộc",
    "季": "Quý",
    "越": "Việt",
    "人": "Nhân",
    "太": "Thái",
    "陰": "Âm",
    "阴": "Âm",
    "吐": "Thổ",
    "納": "Nạp",
    "纳": "Nạp",
    "練": "Luyện",
    "练": "Luyện",
    "氣": "Khí",
    "气": "Khí",
    "訣": "Quyết",
    "诀": "Quyết",
    "月": "Nguyệt",
    "華": "Hoa",
    "华": "Hoa",
    "紀": "Kỷ",
    "纪": "Kỷ",
    "要": "Yếu",
    "秘": "Bí",
    "旨": "Chỉ",
    "陸": "Lục",
    "陆": "Lục",
    "江": "Giang",
    "李": "Lý",
    "木": "Mộc",
    "田": "Điền",
    "長": "Trường",
    "长": "Trường",
    "湖": "Hồ",
    "通": "Thông",
    "崖": "Nhai",
    "項": "Hạng",
    "项": "Hạng",
    "平": "Bình",
    "尺": "Xích",
    "涇": "Kinh",
    "泾": "Kinh",
    "葉": "Diệp",
    "叶": "Diệp",
    "盛": "Thịnh",
    "初": "Mới",
    "入": "Vào",
    "第": "Chương",
    "章": "",
}

CHINESE_RE = re.compile(r"[\u3400-\u9fff]")
TITLE_WRAPPER_RE = re.compile(r"《([^》]+)》")
CHAPTER_TITLE_RE = re.compile(r"^第\s*([0-9零〇一二三四五六七八九十百千万兩两]+)\s*章\s*(.*)$")


@lru_cache(maxsize=1)
def protected_terms() -> dict[str, dict[str, str]]:
    with TERMS_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def protected_entries() -> list[GlossaryEntry]:
    entries: list[GlossaryEntry] = []
    terms = protected_terms()
    for section in ("names", "titles", "authors", "phrases"):
        for source, target in terms.get(section, {}).items():
            entries.append(GlossaryEntry(source, target))
    return sorted(entries, key=lambda entry: len(entry.source), reverse=True)


def lookup_protected_term(text: str, *sections: str) -> str | None:
    stripped = unwrap_title(text.strip())
    terms = protected_terms()
    for section in sections:
        target = terms.get(section, {}).get(stripped)
        if target:
            return target
    return None


def unwrap_title(text: str) -> str:
    if text.startswith("《") and text.endswith("》"):
        return text[1:-1].strip()
    return text


def replace_wrapped_titles(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        return lookup_protected_term(inner, "titles", "names", "authors") or han_viet(inner)

    return TITLE_WRAPPER_RE.sub(replace, text)


def should_transliterate_directly(text: str) -> bool:
    stripped = unwrap_title(text.strip())
    if not stripped or not CHINESE_RE.search(stripped):
        return False
    if re.search(r"[。！？；，、,.!?;:“”\"'\n]", stripped):
        return False
    if CHAPTER_TITLE_RE.match(stripped):
        return True
    return len(stripped) <= 12


def transliterate_directly(text: str) -> str:
    stripped = unwrap_title(text.strip())
    protected = lookup_protected_term(stripped, "titles", "authors", "names", "phrases")
    if protected:
        return protected

    match = CHAPTER_TITLE_RE.match(stripped)
    if match:
        number, rest = match.groups()
        translated_rest = lookup_protected_term(rest, "phrases", "titles") or han_viet(rest)
        return f"Chương {number} {translated_rest}".strip()

    return han_viet(stripped)


def han_viet(text: str) -> str:
    words: list[str] = []
    for char in text:
        if char.isspace():
            continue
        words.append(HAN_VIET.get(char, char))
    return " ".join(word for word in words if word).strip()
