"""Small deterministic checks for browser-provider output before publishing."""
from __future__ import annotations

import re


_TRUNCATED = re.compile(r"\[(?:response )?truncated\]|output was cut off|response was truncated|输出已截断", re.IGNORECASE)
_REFUSAL = re.compile(r"^(?:i(?:'m| am) sorry|i cannot help|i can't assist|抱歉，我无法|我不能帮助)", re.IGNORECASE)


def translation_issue(source: str, output: str, prompt_snapshot: str = "") -> str | None:
    source, output = source.strip(), output.strip()
    if not output:
        return "empty"
    if _TRUNCATED.search(output):
        return "truncated"
    if source and (output == source or (len(source) >= 100 and source[:100] in output)):
        return "source_echo"
    prompt_prefix = prompt_snapshot.split("{{text}}", 1)[0].strip()
    if len(prompt_prefix) >= 40 and output.startswith(prompt_prefix[:100]):
        return "prompt_echo"
    minimum = min(500, max(30, int(len(source) * 0.08)))
    if len(source) >= 80 and len(output) < minimum:
        return "too_short"
    if len(output) < 400 and _REFUSAL.search(output):
        return "refusal"
    paragraphs = [re.sub(r"\s+", "", part).casefold() for part in re.split(r"\n\s*\n", output) if part.strip()]
    if len(paragraphs) >= 4 and len(set(paragraphs)) <= len(paragraphs) // 2:
        return "duplicate_output"
    return None
