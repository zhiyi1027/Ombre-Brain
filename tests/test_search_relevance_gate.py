"""Regression tests for the two-stage explicit-search pipeline.

Admission is based only on lexical/BM25/semantic relevance.  Memory priors such
as freshness, importance, affect, and touch frequency may order admitted
candidates, but must never make an unrelated bucket searchable by themselves.
"""

from __future__ import annotations

import pytest


def _bucket(bucket_id: str, *, importance: int = 5, content: str = "memory") -> dict:
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": bucket_id,
            "domain": [],
            "tags": [],
            "importance": importance,
            "activation_count": 0,
        },
    }


def _isolate_search(monkeypatch, bucket_mgr, buckets: list[dict]) -> None:
    async def _list_all(*, include_archive=False):
        assert include_archive is False
        return buckets

    monkeypatch.setattr(bucket_mgr, "list_all", _list_all)
    monkeypatch.setattr(bucket_mgr, "_bm25", None)


@pytest.mark.asyncio
async def test_fresh_important_but_irrelevant_bucket_cannot_enter(
    bucket_mgr, monkeypatch
):
    irrelevant = _bucket("fresh-important", importance=10)
    relevant = _bucket("relevant", importance=1)
    _isolate_search(monkeypatch, bucket_mgr, [irrelevant, relevant])

    monkeypatch.setattr(
        bucket_mgr,
        "_calc_topic_score",
        lambda _query, bucket: 0.10 if bucket["id"] == "fresh-important" else 0.70,
    )
    monkeypatch.setattr(bucket_mgr, "_calc_emotion_score", lambda *_args: 1.0)
    monkeypatch.setattr(bucket_mgr, "_calc_time_score", lambda meta: 1.0)
    monkeypatch.setattr(bucket_mgr, "_calc_touch_score", lambda meta: 1.0)

    # Make non-relevance priors overwhelmingly strong.  The old single-score
    # threshold admitted fresh-important here despite its 0.10 topic score.
    bucket_mgr.w_topic = 1.0
    bucket_mgr.w_emotion = 10.0
    bucket_mgr.w_time = 10.0
    bucket_mgr.w_importance = 10.0
    bucket_mgr.w_touch = 10.0
    bucket_mgr.fuzzy_threshold = 50

    results = await bucket_mgr.search("target subject", vector_scores={})

    assert [item["id"] for item in results] == ["relevant"]


@pytest.mark.asyncio
async def test_memory_priors_still_rank_candidates_after_relevance_gate(
    bucket_mgr, monkeypatch
):
    older = _bucket("older", importance=2)
    current = _bucket("current", importance=9)
    _isolate_search(monkeypatch, bucket_mgr, [older, current])

    monkeypatch.setattr(bucket_mgr, "_calc_topic_score", lambda *_args: 0.80)
    monkeypatch.setattr(bucket_mgr, "_calc_emotion_score", lambda *_args: 0.5)
    monkeypatch.setattr(
        bucket_mgr,
        "_calc_time_score",
        lambda meta: 1.0 if meta["importance"] == 9 else 0.1,
    )
    monkeypatch.setattr(bucket_mgr, "_calc_touch_score", lambda _meta: 0.0)
    bucket_mgr.fuzzy_threshold = 50

    results = await bucket_mgr.search("shared topic", vector_scores={})

    assert [item["id"] for item in results] == ["current", "older"]


@pytest.mark.asyncio
async def test_strong_bm25_match_is_an_independent_admission_signal(
    bucket_mgr, monkeypatch
):
    candidate = _bucket("bm25-hit")
    _isolate_search(monkeypatch, bucket_mgr, [candidate])
    monkeypatch.setattr(bucket_mgr, "_calc_topic_score", lambda *_args: 0.05)

    class _Index:
        def score(self, _query):
            return {"bm25-hit": 0.90}

    bucket_mgr._bm25 = _Index()
    bucket_mgr._bm25_dirty = False
    bucket_mgr.fuzzy_threshold = 50

    results = await bucket_mgr.search("tokenized phrase", vector_scores={})

    assert [item["id"] for item in results] == ["bm25-hit"]


@pytest.mark.asyncio
async def test_semantic_threshold_remains_an_independent_admission_signal(
    bucket_mgr, monkeypatch
):
    candidate = _bucket("semantic-hit")
    _isolate_search(monkeypatch, bucket_mgr, [candidate])
    monkeypatch.setattr(bucket_mgr, "_calc_topic_score", lambda *_args: 0.05)

    results = await bucket_mgr.search(
        "different words same meaning",
        vector_scores={"semantic-hit": bucket_mgr.vector_recall_threshold + 0.01},
    )

    assert [item["id"] for item in results] == ["semantic-hit"]


@pytest.mark.asyncio
async def test_literal_match_still_bypasses_numeric_threshold(bucket_mgr, monkeypatch):
    candidate = _bucket("literal-hit", content="the exact needle appears here")
    _isolate_search(monkeypatch, bucket_mgr, [candidate])
    monkeypatch.setattr(bucket_mgr, "_calc_topic_score", lambda *_args: 0.0)
    bucket_mgr.fuzzy_threshold = 100

    results = await bucket_mgr.search("exact needle", vector_scores={})

    assert [item["id"] for item in results] == ["literal-hit"]
