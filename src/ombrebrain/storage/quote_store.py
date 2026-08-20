"""Validation and rendering for deliberately preserved exact quotes.

Quotes are selected when a memory is written.  They live in frontmatter, stay
out of normal surfacing and vector indexes, and are rendered only by an
explicit search request.
"""

from __future__ import annotations

from typing import Any


MAX_QUOTES = 3
MAX_QUOTE_CHARS = 100
MAX_SPEAKER_CHARS = 40
MAX_AT_CHARS = 32


def normalize_quotes(value: Any) -> list[dict[str, str]]:
    if value in (None, "", []):
        return []
    if not isinstance(value, list):
        raise ValueError("quotes 必须是列表")
    if len(value) > MAX_QUOTES:
        raise ValueError(f"引语最多 {MAX_QUOTES} 条（给了 {len(value)} 条）")

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            raise ValueError("quotes 每项必须是字符串或对象")

        text = str(item.get("text") or "").strip()
        if not text:
            raise ValueError("quotes 每项必须有非空的 text")
        if len(text) > MAX_QUOTE_CHARS:
            raise ValueError(
                f"单条引语最多 {MAX_QUOTE_CHARS} 字（这条 {len(text)} 字）；"
                "不会截断，因为截断过的引语已经不是原话"
            )

        speaker = str(item.get("speaker") or "").strip()[:MAX_SPEAKER_CHARS]
        at = str(item.get("at") or "").strip()[:MAX_AT_CHARS]
        key = (text, speaker)
        if key in seen:
            continue
        seen.add(key)

        quote = {"text": text}
        if speaker:
            quote["speaker"] = speaker
        if at:
            quote["at"] = at
        normalized.append(quote)
    return normalized


def quotes_from_metadata(metadata: dict | None) -> list[dict[str, str]]:
    """Salvage valid stored entries without letting one bad item hide a bucket."""

    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("quotes")
    if not isinstance(raw, list):
        return []

    salvaged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        try:
            candidates = normalize_quotes([item])
        except ValueError:
            continue
        for quote in candidates:
            key = (quote["text"], quote.get("speaker", ""))
            if key in seen:
                continue
            seen.add(key)
            salvaged.append(quote)
        if len(salvaged) >= MAX_QUOTES:
            break
    return salvaged[:MAX_QUOTES]


def render_quotes(quotes: list[dict[str, str]]) -> str:
    if not quotes:
        return ""
    lines = []
    for quote in quotes:
        line = f'🗣️ 「{quote["text"]}」'
        suffix = " / ".join(
            part for part in (quote.get("speaker"), quote.get("at")) if part
        )
        if suffix:
            line += f"  —— {suffix}"
        lines.append(line)
    return "\n".join(lines)
