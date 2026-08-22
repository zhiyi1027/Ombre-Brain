from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath import dispatch


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None


class BucketManager:
    def __init__(self, buckets):
        self.buckets = list(buckets)

    async def list_all(self, include_archive=False):
        assert include_archive is False
        return list(self.buckets)


def plan_bucket(bucket_id, content, *, status="active", weight=0.5, **metadata):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "id": bucket_id,
            "name": metadata.pop("name", bucket_id),
            "created": metadata.pop("created", "2026-08-19T08:00:00"),
            "type": "plan",
            "status": status,
            "weight": weight,
            **metadata,
        },
    }


@pytest.fixture(autouse=True)
def plan_runtime(monkeypatch):
    monkeypatch.setattr(rt, "config", {"surfacing": {}})
    monkeypatch.setattr(rt, "decay_engine", NoopDecay())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *args, **kwargs: None)


@pytest.mark.asyncio
async def test_plan_domain_returns_only_active_plans_verbatim(monkeypatch):
    active_body = "这是必须逐字返回的 active plan 正文。"
    buckets = [
        plan_bucket("active-high", active_body, weight=0.9),
        plan_bucket("resolved", "已经完成的计划不能返回。", status="resolved"),
        {
            "id": "core",
            "content": "核心准则不能混入 plan 通道。",
            "metadata": {
                "type": "permanent",
                "pinned": True,
                "importance": 10,
            },
        },
    ]
    monkeypatch.setattr(rt, "bucket_mgr", BucketManager(buckets))

    output = await dispatch(domain="plan", max_tokens=10000)

    assert "=== 你的 active plans" in output
    assert active_body in output
    assert "[bucket_id:active-high]" in output
    assert "已经完成的计划不能返回" not in output
    assert "核心准则不能混入 plan 通道" not in output


@pytest.mark.asyncio
async def test_plan_domain_reports_empty_state(monkeypatch):
    monkeypatch.setattr(
        rt,
        "bucket_mgr",
        BucketManager([plan_bucket("resolved", "完成。", status="resolved")]),
    )

    assert await dispatch(domain="plan", max_tokens=10000) == "没有计划。"


@pytest.mark.asyncio
async def test_plan_domain_omits_whole_body_at_token_limit(monkeypatch):
    body = "计划正文必须整条返回，预算不足时不能截断。" * 100
    monkeypatch.setattr(
        rt,
        "bucket_mgr",
        BucketManager([plan_bucket("too-long", body, weight=0.9)]),
    )

    output = await dispatch(domain="plan", max_tokens=500)

    assert body not in output
    assert "另有 1 条 plan 因 token 预算不足未返回" in output
    assert "[bucket_id:too-long]" not in output


@pytest.mark.asyncio
async def test_plan_domain_marks_stale_plan_without_changing_status(monkeypatch):
    stale = plan_bucket(
        "stale-plan",
        "仍需人工决定是否继续的旧计划。",
        created="2020-01-01T00:00:00",
        weight=0.3,
    )
    monkeypatch.setattr(rt, "bucket_mgr", BucketManager([stale]))
    monkeypatch.setattr(
        rt,
        "config",
        {"surfacing": {"plan_stale_after_days": 30}},
    )

    output = await dispatch(domain="plan", max_tokens=10000)

    assert "[待确认:已" in output
    assert "仍然有效吗" in output
    assert "系统不会自动改状态" in output
    assert stale["metadata"]["status"] == "active"
