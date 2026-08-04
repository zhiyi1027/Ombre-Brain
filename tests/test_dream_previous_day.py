from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.dream import dispatch
from tools.dream.candidates import collect_candidates, previous_day


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


def test_dream_only_collects_buckets_created_on_previous_calendar_day():
    reference = datetime(2026, 8, 4, 9, 30)
    buckets = [
        _bucket("yesterday-early", "2026-08-03T00:00:00"),
        _bucket("yesterday-late", "2026-08-03T23:59:59"),
        _bucket("today", "2026-08-04T00:00:00"),
        _bucket("older-but-touched", "2026-07-01T12:00:00"),
    ]

    recent = collect_candidates(buckets, reference_time=reference)

    assert previous_day(reference) == "2026-08-03"
    assert [bucket["id"] for bucket in recent] == [
        "yesterday-late",
        "yesterday-early",
    ]


def test_dream_accepts_created_at_field_name():
    reference = datetime(2026, 8, 4, 9, 30)
    bucket = _bucket("created-at", "2026-07-01T12:00:00")
    bucket["metadata"]["created_at"] = "2026-08-03T12:00:00"

    recent = collect_candidates([bucket], reference_time=reference)

    assert [item["id"] for item in recent] == ["created-at"]


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
async def test_dream_catalog_is_explicit_and_omits_body(monkeypatch):
    yesterday = datetime.now() - timedelta(days=1)
    buckets = [_bucket("yesterday", yesterday.isoformat())]
    buckets[0]["metadata"].update(
        name="昨天的桶",
        domain=["恋爱"],
        importance=8,
    )
    monkeypatch.setattr(rt, "bucket_mgr", _StaticBucketManager(buckets))
    monkeypatch.setattr(rt, "decay_engine", _DummyDecay())
    monkeypatch.setattr(rt, "embedding_engine", _NoEmbedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)

    output = await dispatch(catalog=True)

    assert "yesterday | 昨天的桶 | 恋爱 | 8 |" in output
    assert "yesterday 的完整正文" not in output
