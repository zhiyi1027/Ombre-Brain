from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.dream.feel_rank import rank_feels
from tools.dream.output import format_dream_output


def _feel(bucket_id: str, content: str, created: str = "2026-08-20") -> dict:
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "type": "feel",
            "created": created,
            "valence": 0.5,
            "domain": ["feel"],
        },
    }


class _DisabledEmbedding:
    enabled = False


@pytest.mark.asyncio
async def test_keyword_fallback_keeps_only_relevant_feelings(monkeypatch):
    monkeypatch.setattr(rt, "embedding_engine", _DisabledEmbedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    relevant = _feel("relevant", "tennis training progress")
    unrelated = _feel("unrelated", "database migration failure")

    ranked, vector_ok = await rank_feels(
        [unrelated, relevant], "today tennis training progress"
    )

    assert vector_ok is False
    assert [feel["id"] for feel, _score in ranked] == ["relevant"]


@pytest.mark.asyncio
async def test_vector_ranking_is_restricted_to_feel_ids(monkeypatch):
    observed_allowed = None

    class _Embedding:
        enabled = True

        async def search_similar(self, _query, top_k, allowed_bucket_ids):
            nonlocal observed_allowed
            observed_allowed = allowed_bucket_ids
            return [("feel-high", 0.9), ("feel-low", 0.6)]

    monkeypatch.setattr(rt, "embedding_engine", _Embedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    feels = [_feel("feel-low", "alpha"), _feel("feel-high", "beta")]

    ranked, vector_ok = await rank_feels(feels, "unrelated reference")

    assert vector_ok is True
    assert observed_allowed == {"feel-low", "feel-high"}
    assert [feel["id"] for feel, _score in ranked] == ["feel-high"]


def test_dream_renders_only_preselected_related_feelings(monkeypatch):
    monkeypatch.setattr(
        rt,
        "config",
        {"surfacing": {"dream_max_tokens": 10_000, "feel_max_tokens": 5_000}},
    )
    related = _feel("related", "和这次事件有关的感受")
    unrelated = _feel("unrelated", "另一件事的感受")

    output = format_dream_output(
        recent=[],
        all_buckets=[related, unrelated],
        window_hours=48,
        connection_hint="",
        crystal_hint="",
        related_feels=[related],
        feel_vector_ok=False,
    )

    assert "和这次回顾相关的 feel" in output
    assert "仅按关键词重合度" in output
    assert "和这次事件有关的感受" in output
    assert "另一件事的感受" not in output
