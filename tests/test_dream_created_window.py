from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
import tools.dream as dream_mod
from tools.dream.candidates import DREAM_WINDOW_HOURS, collect_candidates


def _bucket(bucket_id: str, *, created: str = "", created_at: str = "", last_active: str = "") -> dict:
    metadata = {"type": "dynamic"}
    if created:
        metadata["created"] = created
    if created_at:
        metadata["created_at"] = created_at
    if last_active:
        metadata["last_active"] = last_active
    return {"id": bucket_id, "metadata": metadata, "content": bucket_id}


def test_dream_candidates_use_created_time_only_with_rolling_48h_window() -> None:
    now = datetime.now()
    buckets = [
        _bucket(
            "inside",
            created=(now - timedelta(hours=47, minutes=55)).isoformat(),
            last_active=(now - timedelta(days=10)).isoformat(),
        ),
        _bucket(
            "outside-but-touched",
            created=(now - timedelta(hours=48, minutes=5)).isoformat(),
            last_active=now.isoformat(),
        ),
        _bucket(
            "created-at-compatible",
            created_at=(now - timedelta(hours=2)).isoformat(),
            last_active=(now - timedelta(days=30)).isoformat(),
        ),
        _bucket(
            "future",
            created=(now + timedelta(hours=1)).isoformat(),
            last_active=now.isoformat(),
        ),
    ]

    result = collect_candidates(buckets, DREAM_WINDOW_HOURS)

    assert [bucket["id"] for bucket in result] == ["created-at-compatible", "inside"]


class _Decay:
    async def ensure_started(self):
        return None


class _Buckets:
    async def list_all(self, include_archive=False):
        assert include_archive is False
        return []


@pytest.mark.asyncio
async def test_dream_public_window_argument_is_compatibility_only(monkeypatch) -> None:
    seen = []

    def fake_collect(_all_buckets, window_hours):
        seen.append(window_hours)
        return []

    rt.init(
        config={},
        bucket_mgr=_Buckets(),
        decay_engine=_Decay(),
        logger=MagicMock(),
        fire_webhook=None,
        mark_op=None,
    )
    monkeypatch.setattr(dream_mod, "collect_candidates", fake_collect)
    monkeypatch.setattr(dream_mod, "collect_core_context", lambda _buckets: [])

    result = await dream_mod.dispatch(window_hours=1)

    assert seen == [48]
    assert result == "过去 48 小时内没有需要消化的新记忆。"
