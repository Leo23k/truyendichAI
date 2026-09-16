from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])|(?<=……)|(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for paragraph in text.splitlines(keepends=True):
        if not paragraph.strip():
            sentences.append(paragraph)
            continue
        sentences.extend(part for part in _SENTENCE_END.split(paragraph) if part)
    return sentences or [text]


def chunk_text(text: str, max_chars: int = 500) -> list[str]:
    """Pack complete sentences and retain every final residual chunk."""
    chunks: list[str] = []
    current = ""
    for sentence in split_sentences(text):
        if not sentence.strip():
            current += sentence
            continue
        if current and len(current) + len(sentence) > max_chars:
            chunks.append(current)
            current = ""
        if len(sentence) <= max_chars:
            current += sentence
            continue
        for fragment in re.split(r"(?<=[，、,])", sentence):
            if current and len(current) + len(fragment) > max_chars:
                chunks.append(current)
                current = ""
            current += fragment
        if len(current) > max_chars:
            chunks.extend(_split_long_han_safe(current, max_chars))
            current = ""
    if current:
        chunks.append(current)
    return chunks or [text]


def _split_long_han_safe(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        end = min(cursor + max_chars, len(text))
        if end < len(text):
            end = _avoid_four_han_boundary(text, cursor, end)
        chunks.append(text[cursor:end])
        cursor = end
    return chunks


def _avoid_four_han_boundary(text: str, start: int, end: int) -> int:
    min_end = min(len(text), start + 1)
    for candidate in range(end, max(min_end, end - 4), -1):
        left = text[max(start, candidate - 2) : candidate]
        right = text[candidate : candidate + 2]
        if not (_is_han(left) and _is_han(right)):
            return candidate
    return end


def _is_han(text: str) -> bool:
    return bool(text) and all("\u3400" <= char <= "\u9fff" for char in text)
