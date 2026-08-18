import json
from unittest.mock import MagicMock

import pytest

from tools import _runtime as rt
from tools.hold.feel import store_feel
import web.buckets as buckets_web


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class RepairManager:
    async def repair_digested_from_feels(self):
        return {
            "feels_scanned": 4,
            "linked_feels": 3,
            "unique_sources": 2,
            "already_digested": 1,
            "repaired": 1,
            "missing_sources": [],
            "failed_sources": [],
        }


class FailedMarkManager:
    async def create(self, **_kwargs):
        return "feel-created"

    async def update(self, _bucket_id, **_kwargs):
        return False


@pytest.mark.asyncio
async def test_repair_digested_from_feels_is_idempotent_and_deduplicates_sources(
    bucket_mgr,
):
    needs_repair = await bucket_mgr.create(
        content="source one",
        tags=[],
        importance=6,
        domain=["日常"],
        valence=0.5,
        arousal=0.3,
        name="source one",
        bucket_type="dynamic",
    )
    already_done = await bucket_mgr.create(
        content="source two",
        tags=[],
        importance=6,
        domain=["日常"],
        valence=0.5,
        arousal=0.3,
        name="source two",
        bucket_type="dynamic",
    )
    await bucket_mgr.update(already_done, digested=True)

    for body, source in (
        ("first feel", needs_repair),
        ("second feel for same source", needs_repair),
        ("already reflected", already_done),
        ("dangling feel", "missing-source"),
        ("unlinked feel", ""),
    ):
        await bucket_mgr.create(
            content=body,
            tags=["__feel__"],
            importance=5,
            domain=["feel"],
            valence=0.5,
            arousal=0.3,
            name=None,
            bucket_type="feel",
            triggered_by=source,
        )

    report = await bucket_mgr.repair_digested_from_feels()

    assert report == {
        "feels_scanned": 5,
        "linked_feels": 4,
        "unique_sources": 3,
        "already_digested": 1,
        "repaired": 1,
        "missing_sources": ["missing-source"],
        "failed_sources": [],
    }
    repaired = await bucket_mgr.get(needs_repair)
    assert repaired["metadata"]["digested"] is True

    second_report = await bucket_mgr.repair_digested_from_feels()
    assert second_report["repaired"] == 0
    assert second_report["already_digested"] == 2


@pytest.mark.asyncio
async def test_store_feel_reports_partial_success_when_source_mark_fails(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(rt, "bucket_mgr", FailedMarkManager())
    monkeypatch.setattr(rt, "logger", logger)

    result = await store_feel(
        content="I learned something",
        extra_tags=[],
        valence=0.7,
        arousal=0.4,
        source_bucket="source-id",
        why_remembered="",
    )

    assert result.startswith("🫧feel→feel-created")
    assert "feel 已保存" in result
    assert "source-id" in result
    assert "修复消化标记" in result
    logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_dashboard_repair_route_returns_report(monkeypatch):
    monkeypatch.setattr(buckets_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        buckets_web.sh,
        "bucket_mgr",
        RepairManager(),
        raising=False,
    )
    mcp = FakeMCP()
    buckets_web.register(mcp)

    response = await mcp.routes[("POST", "/api/buckets/repair-digested")](object())
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["repaired"] == 1
    assert payload["already_digested"] == 1


def test_dashboard_exposes_manual_digested_repair_control():
    with open("frontend/dashboard.html", encoding="utf-8") as handle:
        html = handle.read()

    assert 'id="repair-digested-btn"' in html
    assert "async function repairDigestedMarkers()" in html
    assert "/api/buckets/repair-digested" in html
