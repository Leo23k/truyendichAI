"""Sentence-aware chapter chunking for incremental translation."""

from __future__ import annotations

import re


MIN_CHUNK_SIZE = 1200
MAX_CHUNK_SIZE = 1800
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])")


def chunk_text(text: str, *, min_size: int = MIN_CHUNK_SIZE, max_size: int = MAX_CHUNK_SIZE) -> list[str]:
    """Split text at paragraph and sentence boundaries without retaining all chunks in callers."""
    if min_size <= 0 or max_size < min_size:
        raise ValueError("Invalid chunk size range")
    units = _sentence_units(text)
    chunks: list[str] = []
    current = ""
    for unit in units:
        for piece in _split_long_unit(unit, max_size):
            candidate = _join(current, piece)
            if current and len(candidate) > max_size:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        if chunks and len(current) < min_size and len(_join(chunks[-1], current)) <= max_size:
            chunks[-1] = _join(chunks[-1], current)
        else:
            chunks.append(current)
    return chunks


def _sentence_units(text: str) -> list[str]:
    units: list[str] = []
    for paragraph in (part.strip() for part in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")):
        if not paragraph:
            continue
        units.extend(part.strip() for part in _SENTENCE_END.split(paragraph) if part.strip())
    return units


def _split_long_unit(unit: str, max_size: int) -> list[str]:
    parts: list[str] = []
    remaining = unit
    while len(remaining) > max_size:
        boundary = max(remaining.rfind(mark, 0, max_size + 1) for mark in "，、,:： ")
        cut = boundary + 1 if boundary > max_size // 2 else max_size
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def _join(left: str, right: str) -> str:
    return f"{left}\n{right}" if left else right
