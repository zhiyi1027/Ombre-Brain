"""Relevant feel retrieval for ``breath_advanced(domain="feel")``.

Feel history grows without bound, so this channel never dumps it by recency.
The caller supplies the current topic and may include ``bucket_id:<id>`` or
``source_bucket:<id>`` markers.  Feels directly written from those source
memories come first; semantic/keyword relevance fills the remaining slots.
Returned bodies remain verbatim and are never truncated or summarized.
"""

from __future__ import annotations

import math
import re

from .. import _runtime as rt
from ..dream.feel_rank import _content_tokens, rank_feels
from ._verbatim import render_stored_bucket


MAX_RELEVANT_FEELS = 5
STARTUP_MAX_RELEVANT_FEELS = 3
STARTUP_CANDIDATE_LIMIT = 12
STARTUP_MAX_NEGATIVE_FEELS = 2
STARTUP_NEGATIVE_VALENCE = 0.4
STARTUP_THEME_VECTOR_THRESHOLD = 0.8
STARTUP_THEME_KEYWORD_THRESHOLD = 0.6
_THEME_STOPWORDS = {
    "一个",
    "一直",
    "不过",
    "不是",
    "今天",
    "什么",
    "以后",
    "但是",
    "因为",
    "宝宝",
    "已经",
    "感觉",
    "时候",
    "爸爸",
    "真的",
    "知知",
    "自己",
    "还是",
    "这次",
    "那个",
}
_SOURCE_ID_RE = re.compile(
    r"(?:bucket_id|source_bucket)\s*:\s*([A-Za-z0-9_.:-]+)",
    re.IGNORECASE,
)
_SEMANTIC_DISABLED_NOTE = "[检索降级：语义索引暂不可用，本次仅按关键词字面匹配。]"
_NEEDS_QUERY = (
    "feel 需要一个主题，不再全量返回。\n"
    "请描述此刻在想的事；若要优先找某条记忆亲生的感受，可在 query 中加入 "
    "bucket_id:<id>。\n"
    '例：breath_advanced(domain="feel", query="[bucket_id:abc123] 被误解", '
    "max_results=5, max_tokens=2000)。"
)


def _source_ids(query: str) -> set[str]:
    return {
        match.group(1).strip()
        for match in _SOURCE_ID_RE.finditer(str(query or ""))
        if match.group(1).strip()
    }


def _reference_text(query: str) -> str:
    without_ids = _SOURCE_ID_RE.sub(" ", str(query or ""))
    without_wrappers = re.sub(r"[\[\](){}]+", " ", without_ids)
    return " ".join(without_wrappers.split())


def _created(bucket: dict) -> str:
    return str((bucket.get("metadata") or {}).get("created") or "")


def _valence(bucket: dict) -> float:
    try:
        value = float((bucket.get("metadata") or {}).get("valence", 0.5))
    except (TypeError, ValueError, OverflowError):
        return 0.5
    return max(0.0, min(1.0, value))


def _theme_keyword_similarity(left: dict, right: dict) -> float:
    left_tokens = _content_tokens(str(left.get("content") or "")) - _THEME_STOPWORDS
    right_tokens = _content_tokens(str(right.get("content") or "")) - _THEME_STOPWORDS
    if not left_tokens or not right_tokens:
        return 0.0
    shared = left_tokens & right_tokens
    # One generic shared word is too weak to call two first-person feelings the
    # same theme when semantic vectors are unavailable.
    if len(shared) < 2:
        return 0.0
    return len(shared) / min(len(left_tokens), len(right_tokens))


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


async def _stored_embeddings(candidates: list[dict]) -> dict[str, list[float]]:
    engine = getattr(rt, "embedding_engine", None)
    get_embedding = getattr(engine, "get_embedding", None)
    if not engine or not getattr(engine, "enabled", False) or not callable(get_embedding):
        return {}
    embeddings: dict[str, list[float]] = {}
    for feel in candidates:
        bucket_id = str(feel.get("id") or "")
        if not bucket_id or bucket_id in embeddings:
            continue
        try:
            vector = await get_embedding(bucket_id)
            if vector:
                embeddings[bucket_id] = vector
        except Exception as exc:
            rt.logger.warning(
                "startup feel diversity could not read stored embedding for %s: %s: %s",
                bucket_id,
                type(exc).__name__,
                exc,
            )
    return embeddings


def _same_theme(
    candidate: dict,
    selected: dict,
    embeddings: dict[str, list[float]],
) -> bool:
    candidate_vector = embeddings.get(str(candidate.get("id") or ""))
    selected_vector = embeddings.get(str(selected.get("id") or ""))
    if candidate_vector and selected_vector:
        if (
            _cosine_similarity(candidate_vector, selected_vector)
            >= STARTUP_THEME_VECTOR_THRESHOLD
        ):
            return True
    return (
        _theme_keyword_similarity(candidate, selected)
        >= STARTUP_THEME_KEYWORD_THRESHOLD
    )


def _literal_matches(feels: list[dict], query: str) -> list[dict]:
    needle = str(query or "").strip().lower()
    if not needle:
        return []
    matched = []
    for feel in feels:
        meta = feel.get("metadata") or {}
        haystack = " ".join(
            [
                str(feel.get("content") or ""),
                str(meta.get("name") or ""),
                " ".join(str(tag) for tag in (meta.get("tags") or [])),
            ]
        ).lower()
        if needle in haystack:
            matched.append(feel)
    matched.sort(key=_created, reverse=True)
    return matched


async def select_relevant_feels(
    feels: list[dict],
    *,
    source_ids: set[str],
    reference_text: str,
    max_results: int = MAX_RELEVANT_FEELS,
) -> tuple[list[tuple[dict, str]], bool]:
    """Select direct-source feels first, then fill by contextual relevance."""

    limit = max(1, min(int(max_results or MAX_RELEVANT_FEELS), MAX_RELEVANT_FEELS))
    normalized_sources = {
        str(source_id or "").strip()
        for source_id in source_ids
        if str(source_id or "").strip()
    }
    direct = [
        feel
        for feel in feels
        if str((feel.get("metadata") or {}).get("triggered_by") or "")
        in normalized_sources
    ]
    direct.sort(key=_created, reverse=True)
    selected: list[tuple[dict, str]] = [
        (feel, "direct_source") for feel in direct[:limit]
    ]
    selected_ids = {str(feel.get("id") or "") for feel, _reason in selected}

    reference = str(reference_text or "").strip()
    vector_ok = True
    if len(selected) < limit and reference:
        remaining = [
            feel
            for feel in feels
            if str(feel.get("id") or "") not in selected_ids
        ]
        if remaining:
            ranked, vector_ok = await rank_feels(
                remaining,
                reference,
                max_feels=limit - len(selected),
            )
            selected.extend(
                (feel, "context_relevance") for feel, _score in ranked
            )
    return selected, vector_ok


async def select_startup_feels(
    feels: list[dict],
    *,
    source_ids: set[str],
    reference_text: str,
) -> tuple[list[tuple[dict, str]], bool, dict[str, int]]:
    """Pick a small, relevant and non-saturating feel set for one-button Breath.

    Explicit feel search keeps its five-result contract. Startup instead scans
    a wider relevant pool, returns at most three whole bodies, suppresses near-
    duplicate themes, and never lets more than two clearly negative feelings
    occupy the briefing. Missing diversity leaves slots empty; no unrelated
    positive feeling is added merely to balance the tone.
    """

    normalized_sources = {
        str(source_id or "").strip()
        for source_id in source_ids
        if str(source_id or "").strip()
    }
    direct = [
        feel
        for feel in feels
        if str((feel.get("metadata") or {}).get("triggered_by") or "")
        in normalized_sources
    ]
    direct.sort(key=_created, reverse=True)
    direct_ids = {str(feel.get("id") or "") for feel in direct}

    reference = str(reference_text or "").strip()
    ranked: list[tuple[dict, float]] = []
    vector_ok = True
    if reference:
        remaining = [
            feel
            for feel in feels
            if str(feel.get("id") or "") not in direct_ids
        ]
        if remaining:
            ranked, vector_ok = await rank_feels(
                remaining,
                reference,
                max_feels=STARTUP_CANDIDATE_LIMIT,
            )

    candidates: list[tuple[dict, str]] = [
        (feel, "direct_source")
        for feel in direct[:STARTUP_CANDIDATE_LIMIT]
    ]
    candidates.extend((feel, "context_relevance") for feel, _score in ranked)
    embeddings = await _stored_embeddings([feel for feel, _reason in candidates])

    selected: list[tuple[dict, str]] = []
    negative_count = 0
    diagnostics = {"same_theme": 0, "negative_saturation": 0}
    for feel, reason in candidates:
        if any(_same_theme(feel, kept, embeddings) for kept, _ in selected):
            diagnostics["same_theme"] += 1
            continue
        is_negative = _valence(feel) < STARTUP_NEGATIVE_VALENCE
        if is_negative and negative_count >= STARTUP_MAX_NEGATIVE_FEELS:
            diagnostics["negative_saturation"] += 1
            continue
        selected.append((feel, reason))
        if is_negative:
            negative_count += 1
        if len(selected) >= STARTUP_MAX_RELEVANT_FEELS:
            break

    return selected, vector_ok, diagnostics


async def surface_feels(
    query: str = "",
    max_tokens: int = 0,
    max_results: int = MAX_RELEVANT_FEELS,
) -> str:
    query = str(query or "").strip()
    if not query:
        return _NEEDS_QUERY

    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
        feels = [
            bucket
            for bucket in all_buckets
            if (bucket.get("metadata") or {}).get("type") == "feel"
        ]
        if not feels:
            return "还没有留下过 feel。"

        limit = max(1, min(int(max_results or MAX_RELEVANT_FEELS), MAX_RELEVANT_FEELS))
        source_ids = _source_ids(query)
        reference = _reference_text(query)
        selected_with_reasons, vector_ok = await select_relevant_feels(
            feels,
            source_ids=source_ids,
            reference_text=reference,
            max_results=limit,
        )
        selected = [feel for feel, _reason in selected_with_reasons]
        if not selected and reference and not vector_ok:
            selected = _literal_matches(feels, reference)[:limit]

        if not selected:
            head = f"没有和「{reference or query}」相关的 feel。"
            return f"{_SEMANTIC_DISABLED_NOTE}\n{head}" if reference and not vector_ok else head

        lines: list[str] = []
        used = 0
        omitted = 0
        budget = max(1, int(max_tokens or 10_000))
        for index, feel in enumerate(selected):
            created = (feel.get("metadata") or {}).get("created", "")
            entry, cost = render_stored_bucket(
                feel,
                f"[{created}] [bucket_id:{feel['id']}]",
            )
            if used + cost <= budget:
                lines.append(entry)
                used += cost
            else:
                omitted = len(selected) - index
                break

        label = reference or "指定来源"
        notice = _SEMANTIC_DISABLED_NOTE + "\n" if reference and not vector_ok else ""
        out = notice + f"=== 和「{label}」相关的 feel（最多 {limit} 条）===\n"
        out += "直属 source_bucket 优先，其余按语义与关键词相关性补充。\n"
        out += "\n---\n".join(lines)
        if omitted:
            out += f"\n\n另有 {omitted} 条相关 feel 因 token 预算不足未返回；正文未截断或摘要。"
        return out
    except Exception as exc:
        rt.logger.error("Feel retrieval failed: %s", exc)
        return "读取 feel 失败。"
