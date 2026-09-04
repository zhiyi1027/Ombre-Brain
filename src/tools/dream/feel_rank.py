"""Select feelings related to the memories in the current dream."""

from __future__ import annotations

from bm25_index import _tokenize

from .. import _runtime as rt


VECTOR_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3
RELEVANCE_THRESHOLD = 0.5
MAX_FEELS = 5

# Query boilerplate should not count as evidence that a feel is relevant.
# These words are common in natural-language requests (and in many feel
# bodies), so keeping them in the denominator/overlap can make unrelated
# entries look like matches merely because they mention "知知" or "最近".
_QUERY_STOPWORDS = {
    "一个",
    "一些",
    "一下",
    "一直",
    "不过",
    "不是",
    "为什么",
    "今天",
    "什么",
    "以后",
    "但是",
    "因为",
    "如何",
    "宝宝",
    "已经",
    "感觉",
    "感受",
    "怎么样",
    "怎么",
    "想起",
    "时候",
    "是不是",
    "最近",
    "爸爸",
    "真的",
    "知知",
    "自己",
    "还是",
    "这个",
    "那个",
    "相关",
    "记得",
    "关于",
}


def _content_tokens(text: str) -> set[str]:
    # Single-character Chinese function words create misleading overlap.
    return {token for token in _tokenize(text or "") if len(token) > 1}


def _query_tokens(text: str) -> set[str]:
    """Return content-bearing query tokens, excluding retrieval boilerplate."""

    return _content_tokens(text) - _QUERY_STOPWORDS


def keyword_overlap(feel_text: str, reference_tokens: set[str]) -> float:
    if not reference_tokens:
        return 0.0
    feel_tokens = _content_tokens(feel_text)
    if not feel_tokens:
        return 0.0
    # Recall against the *query*, not the candidate: a long, multi-sentence
    # feel body naturally has many more tokens than a short query, so
    # dividing by the feel's own token count made the ratio nearly
    # unreachable for realistic feel bodies regardless of true relevance.
    return len(feel_tokens & reference_tokens) / len(reference_tokens)


async def _vector_scores(
    reference_text: str, feel_ids: set[str]
) -> tuple[dict[str, float], bool]:
    engine = getattr(rt, "embedding_engine", None)
    if not engine or not getattr(engine, "enabled", False) or not feel_ids:
        return {}, False
    try:
        strict_search = getattr(engine, "search_similar_strict", None)
        search = strict_search if callable(strict_search) else engine.search_similar
        pairs = await search(
            reference_text,
            top_k=max(len(feel_ids), 1),
            allowed_bucket_ids=feel_ids,
        )
    except Exception as exc:
        rt.logger.warning(
            "dream feel vector ranking failed; using keywords: %s: %s",
            type(exc).__name__,
            exc,
        )
        return {}, False
    if not pairs:
        # An enabled provider with no indexed feel rows is not useful for this
        # ranking pass; let keywords carry the full score instead of silently
        # multiplying them by 0.3 and filtering everything out.
        return {}, False
    return {str(bucket_id): float(score) for bucket_id, score in pairs}, True


async def rank_feels(
    feels: list[dict],
    reference_text: str,
    *,
    max_feels: int = MAX_FEELS,
    threshold: float = RELEVANCE_THRESHOLD,
) -> tuple[list[tuple[dict, float]], bool]:
    if not feels or not str(reference_text or "").strip():
        return [], False

    feel_ids = {str(feel.get("id") or "") for feel in feels if feel.get("id")}
    vector_scores, vector_ok = await _vector_scores(reference_text, feel_ids)
    reference_tokens = _query_tokens(reference_text)
    ranked: list[tuple[dict, float]] = []
    for feel in feels:
        bucket_id = str(feel.get("id") or "")
        keyword = keyword_overlap(str(feel.get("content") or ""), reference_tokens)
        if vector_ok and bucket_id in vector_scores:
            score = VECTOR_WEIGHT * vector_scores[bucket_id] + KEYWORD_WEIGHT * keyword
        else:
            # The index may be only partially caught up.  A successful vector
            # search for other feels must not make an unindexed feel incapable
            # of crossing the relevance threshold.
            score = keyword
        if score >= threshold:
            ranked.append((feel, score))

    ranked.sort(
        key=lambda item: (
            item[1],
            str((item[0].get("metadata") or {}).get("created") or ""),
        ),
        reverse=True,
    )
    return ranked[:max_feels], vector_ok
