from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.dream import dispatch
from tools.dream.candidates import DREAM_WINDOW_HOURS, collect_candidates


def _bucket(bucket_id, created, *, last_active="2026-08-04T12:00:00"):
    return {
        "id": bucket_id,
        "metadata": {
            "type": "dynamic",
            "created": created,
            "last_active": last_active,
            "importance": 5,
        },
        "content": f"{bucket_id} 的完整正文",
    }


def test_dream_collects_buckets_created_in_rolling_48_hour_window():
    reference = datetime(2026, 8, 4, 9, 30)
    buckets = [
        _bucket("inside-old-edge", "2026-08-02T09:31:00"),
        _bucket("yesterday", "2026-08-03T23:59:59"),
        _bucket("today", "2026-08-04T08:00:00"),
        _bucket("outside-but-touched", "2026-08-02T09:29:59"),
        _bucket("future", "2026-08-04T09:31:00"),
    ]

    recent = collect_candidates(buckets, reference_time=reference)

    assert [bucket["id"] for bucket in recent] == [
        "today",
        "yesterday",
        "inside-old-edge",
    ]


def test_dream_accepts_created_at_field_name():
    reference = datetime(2026, 8, 4, 9, 30)
    bucket = _bucket("created-at", "2026-07-01T12:00:00")
    bucket["metadata"]["created_at"] = "2026-08-03T12:00:00"

    recent = collect_candidates([bucket], reference_time=reference)

    assert [item["id"] for item in recent] == ["created-at"]


def test_dream_falls_back_to_created_when_created_at_is_invalid():
    reference = datetime(2026, 8, 4, 9, 30)
    bucket = _bucket("legacy-created", "2026-08-04T08:00:00")
    bucket["metadata"]["created_at"] = "not-a-date"

    recent = collect_candidates([bucket], reference_time=reference)

    assert [item["id"] for item in recent] == ["legacy-created"]


class _StaticBucketManager:
    def __init__(self, buckets):
        self.buckets = buckets

    async def list_all(self, include_archive=False):
        return self.buckets


class _DummyDecay:
    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return float(meta.get("importance") or 0)


class _NoEmbedding:
    enabled = False


@pytest.mark.asyncio
async def test_dream_returns_full_body_and_ignores_old_touched_bucket(monkeypatch):
    yesterday = datetime.now() - timedelta(days=1)
    old = datetime.now() - timedelta(days=30)
    buckets = [
        _bucket("yesterday", yesterday.isoformat()),
        _bucket("old-touched", old.isoformat(), last_active=datetime.now().isoformat()),
    ]
    monkeypatch.setattr(rt, "bucket_mgr", _StaticBucketManager(buckets))
    monkeypatch.setattr(rt, "decay_engine", _DummyDecay())
    monkeypatch.setattr(rt, "embedding_engine", _NoEmbedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "config", {})

    output = await dispatch()

    assert "yesterday 的完整正文" in output
    assert "old-touched 的完整正文" not in output


@pytest.mark.asyncio
async def test_dream_window_argument_does_not_narrow_fixed_window(monkeypatch):
    within_48h = datetime.now() - timedelta(hours=30)
    buckets = [_bucket("within-48h", within_48h.isoformat())]
    monkeypatch.setattr(rt, "bucket_mgr", _StaticBucketManager(buckets))
    monkeypatch.setattr(rt, "decay_engine", _DummyDecay())
    monkeypatch.setattr(rt, "embedding_engine", _NoEmbedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "config", {})

    output = await dispatch(window_hours=1)

    assert "within-48h 的完整正文" in output
    assert f"过去 {DREAM_WINDOW_HOURS} 小时内新建记忆" in output


@pytest.mark.asyncio
async def test_dream_catalog_is_explicit_and_omits_body(monkeypatch):
    yesterday = datetime.now() - timedelta(days=1)
    buckets = [_bucket("yesterday", yesterday.isoformat())]
    buckets[0]["metadata"].update(
        name="昨天的桶",
        domain=["恋爱"],
        importance=8,
        resolved=True,
        digested=False,
    )
    monkeypatch.setattr(rt, "bucket_mgr", _StaticBucketManager(buckets))
    monkeypatch.setattr(rt, "decay_engine", _DummyDecay())
    monkeypatch.setattr(rt, "embedding_engine", _NoEmbedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)

    output = await dispatch(catalog=True)

    assert (
        "bucket_id | 名称 | 域 | 重要度 | last_active | resolved | digested | created"
        in output
    )
    assert "yesterday | 昨天的桶 | 恋爱 | 8 | 2026-08-04T12:00:00 | true | false |" in output
    assert "yesterday 的完整正文" not in output


@pytest.mark.asyncio
async def test_dream_catalog_parses_legacy_string_boolean_metadata(monkeypatch):
    yesterday = datetime.now() - timedelta(days=1)
    buckets = [_bucket("legacy-flags", yesterday.isoformat())]
    buckets[0]["metadata"].update(
        resolved="false",
        digested="true",
    )
    monkeypatch.setattr(rt, "bucket_mgr", _StaticBucketManager(buckets))
    monkeypatch.setattr(rt, "decay_engine", _DummyDecay())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)

    output = await dispatch(catalog=True)

    assert (
        "legacy-flags | legacy-flags | 未分类 | 5 | 2026-08-04T12:00:00 | "
        "false | true |"
    ) in output
