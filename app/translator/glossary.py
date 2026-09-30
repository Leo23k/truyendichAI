"""Glossary protection for names and terms before model inference."""

from __future__ import annotations

from dataclasses import dataclass
import re


# The model can preserve an alias inside angle brackets while appending a
# positional digit (for example, <term0>).  Placeholder markers (<PH1>) do
# not match because this pattern requires a readable term (space/non-ASCII).
_TERM_TOKEN_ARTIFACT_RE = re.compile(r"<\s*([^<>]*?(?:\s|[^\x00-\x7f])[^<>]*?)\d+\s*>")


@dataclass(frozen=True)
class GlossaryEntry:
    source: str
    target: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProtectedText:
    text: str
    bindings: tuple[tuple[str, GlossaryEntry], ...]


def protect(text: str, entries: list[GlossaryEntry]) -> ProtectedText:
    """Replace longest non-overlapping source terms with stable placeholders."""
    ordered = sorted((entry for entry in entries if entry.source), key=lambda item: len(item.source), reverse=True)
    bindings: list[tuple[str, GlossaryEntry]] = []
    occupied: list[tuple[int, int]] = []
    pieces: list[tuple[int, int, GlossaryEntry]] = []
    for entry in ordered:
        for match in re.finditer(re.escape(entry.source), text):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            occupied.append((match.start(), match.end()))
            pieces.append((match.start(), match.end(), entry))
    pieces.sort(key=lambda item: item[0])
    if not pieces:
        return ProtectedText(text, ())
    output: list[str] = []
    cursor = 0
    for index, (start, end, entry) in enumerate(pieces, start=1):
        marker = f"<PH{index}>"
        output.append(text[cursor:start])
        output.append(marker)
        bindings.append((marker, entry))
        cursor = end
    output.append(text[cursor:])
    return ProtectedText("".join(output), tuple(bindings))


def restore(text: str, protected: ProtectedText) -> str:
    """Restore exact markers, tolerant of harmless model spacing/case changes."""
    restored = text
    for marker, entry in protected.bindings:
        variants = (marker, marker.lower(), marker.replace("<", "").replace(">", ""))
        for variant in variants:
            restored = restored.replace(variant, entry.target)
        for alias in entry.aliases:
            restored = restored.replace(alias, entry.target)
    return _TERM_TOKEN_ARTIFACT_RE.sub(lambda match: match.group(1).strip(), restored)
