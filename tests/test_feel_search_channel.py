from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
import tools.breath as breath_module
from tools.breath.feel import surface_feels


class StaticBuckets:
    def __init__(self, buckets):
        self.buckets = list(buckets)

    async def list_all(self, include_archive=False):
        assert include_archive is False
        return list(self.buckets)


class VectorEngine:
    enabled = True

    def __init__(self, scores):
        self.scores = dict(scores)
        self.allowed = None

    async def search_similar(self, query, top_k=10, allowed_bucket_ids=None):
        self.allowed = set(allowed_bucket_ids or set())
        pairs = [
            (bucket_id, score)
            for bucket_id, score in self.scores.items()
            if bucket_id in self.allowed
        ]
        return sorted(pairs, key=lambda item: item[1], reverse=True)[:top_k]


class DisabledEngine:
    enabled = False


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None


def feel(bucket_id, content, *, created="2026-08-21T10:00:00", source=""):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "id": bucket_id,
            "name": bucket_id,
            "created": created,
            "type": "feel",
            "domain": ["feel"],
            "tags": ["__feel__"],
            "triggered_by": source,
        },
    }


@pytest.fixture(autouse=True)
def feel_runtime(monkeypatch):
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "decay_engine", NoopDecay())
    monkeypatch.setattr(rt, "config", {"surfacing": {}})
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *args, **kwargs: None)


@pytest.mark.asyncio
async def test_missing_query_never_dumps_all_feels(monkeypatch):
    monkeypatch.setattr(
        rt,
        "bucket_mgr",
        StaticBuckets([feel("private-feel", "不该被无差别倒出来。")]),
    )
    monkeypatch.setattr(rt, "embedding_engine", DisabledEngine(), raising=False)

    output = await surface_feels(query="", max_tokens=10_000)

    assert "需要一个主题" in output
    assert "不该被无差别倒出来" not in output


@pytest.mark.asyncio
async def test_direct_source_feel_precedes_semantic_fill(monkeypatch):
    buckets = [
        feel("direct", "这条是源记忆亲生的感受。", source="memory-1"),
        feel("related", "被误解时我很想把话说清楚。"),
        feel("unrelated", "窗外的雨停了。"),
    ]
    engine = VectorEngine({"direct": 0.1, "related": 0.9, "unrelated": 0.1})
    monkeypatch.setattr(rt, "bucket_mgr", StaticBuckets(buckets))
    monkeypatch.setattr(rt, "embedding_engine", engine, raising=False)

    output = await surface_feels(
        query="[bucket_id:memory-1] 被误解",
        max_tokens=2_000,
        max_results=5,
    )

    assert "源记忆亲生的感受" in output
    assert "被误解时" in output
    assert "窗外的雨" not in output
    assert output.index("源记忆亲生的感受") < output.index("被误解时")
    assert engine.allowed == {"related", "unrelated"}


@pytest.mark.asyncio
async def test_keyword_fallback_is_explicit_and_does_not_add_noise(monkeypatch):
    buckets = [
        feel("related", "删掉自己写的东西时真的很舍不得。"),
        feel("noise", "今天的风很大。"),
    ]
    monkeypatch.setattr(rt, "bucket_mgr", StaticBuckets(buckets))
    monkeypatch.setattr(rt, "embedding_engine", DisabledEngine(), raising=False)

    output = await surface_feels(
        query="舍不得",
        max_tokens=2_000,
        max_results=5,
    )

    assert "检索降级" in output
    assert "真的很舍不得" in output
    assert "今天的风很大" not in output


@pytest.mark.asyncio
async def test_result_count_is_capped_at_five_and_bodies_stay_whole(monkeypatch):
    buckets = [
        feel(
            f"feel-{index}",
            f"完整正文-{index}",
            created=f"2026-08-21T{index:02d}:00:00",
            source="memory-1",
        )
        for index in range(7)
    ]
    monkeypatch.setattr(rt, "bucket_mgr", StaticBuckets(buckets))
    monkeypatch.setattr(rt, "embedding_engine", DisabledEngine(), raising=False)

    output = await surface_feels(
        query="bucket_id:memory-1",
        max_tokens=10_000,
        max_results=20,
    )

    assert output.count("完整正文-") == 5
    assert "完整正文-6" in output
    assert "完整正文-2" in output
    assert "完整正文-1" not in output


@pytest.mark.asyncio
async def test_dispatch_passes_topic_budget_and_result_limit(monkeypatch):
    captured = {}

    async def fake_surface_feels(**kwargs):
        captured.update(kwargs)
        return "related feels"

    monkeypatch.setattr(breath_module, "surface_feels", fake_surface_feels)

    output = await breath_module.dispatch(
        query="[bucket_id:memory-1] 被误解",
        domain="feel",
        max_tokens=2_000,
        max_results=5,
    )

    assert output == "related feels"
    assert captured == {
        "query": "[bucket_id:memory-1] 被误解",
        "max_tokens": 2_000,
        "max_results": 5,
    }
