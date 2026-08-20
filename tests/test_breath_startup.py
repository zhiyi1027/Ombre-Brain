from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
import tools.breath as breath_module
import tools.breath.startup as startup_module
from tools.breath.startup import surface_startup
from utils import count_tokens_approx
from web import config_api


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return float(meta.get("importance") or 0)


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class JsonRequest:
    headers = {}
    query_params = {}
    path_params = {}

    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def make_bucket(
    bucket_id,
    body,
    *,
    created,
    importance=5,
    bucket_type="dynamic",
    **metadata,
):
    return {
        "id": bucket_id,
        "content": body,
        "metadata": {
            "id": bucket_id,
            "name": metadata.pop("name", bucket_id),
            "created": created,
            "last_active": created,
            "importance": importance,
            "type": bucket_type,
            "domain": metadata.pop("domain", ["daily"]),
            "tags": metadata.pop("tags", []),
            **metadata,
        },
    }


@pytest.fixture(autouse=True)
def startup_runtime(monkeypatch):
    monkeypatch.setattr(rt, "decay_engine", NoopDecay())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *args, **kwargs: None)


@pytest.mark.asyncio
async def test_startup_is_deterministic_and_reconnects_recent_unfinished_and_plans():
    reference = datetime.fromisoformat("2026-08-18T12:00:00")
    buckets = [
        make_bucket(
            "core",
            "四条短核心应该逐字返回。",
            created="2026-08-01T09:00:00",
            importance=10,
            bucket_type="permanent",
            pinned=True,
        ),
        make_bucket("latest", "最新一条正文。", created="2026-08-18T11:30:00"),
        make_bucket(
            "recent-high",
            "近期高重要度正文。",
            created="2026-08-18T08:00:00",
            importance=9,
        ),
        make_bucket(
            "recent-resolved",
            "已经解决但仍属于最近一天的正文。",
            created="2026-08-18T07:00:00",
            importance=8,
            resolved=True,
        ),
        make_bucket(
            "recent-low",
            "近期低重要度正文不应挤掉更重要的候选。",
            created="2026-08-18T10:00:00",
            importance=2,
        ),
        make_bucket(
            "older-unresolved",
            "较早但仍未解决的正文。",
            created="2026-08-15T09:00:00",
            importance=10,
        ),
        make_bucket(
            "active-plan",
            "完成确定性睁眼施工。",
            created="2026-08-17T09:00:00",
            bucket_type="plan",
            status="active",
            weight=0.9,
        ),
        make_bucket(
            "test-data",
            "测试数据不能进入睁眼。",
            created="2026-08-18T11:59:00",
            importance=10,
            provenance={"kind": "test", "erasable": True},
        ),
        make_bucket(
            "digested-old",
            "已经消化过的普通记忆不能再次进入启动浮现。",
            created="2026-08-12T09:00:00",
            importance=10,
            digested=True,
        ),
    ]

    first = await surface_startup(
        buckets,
        max_results=4,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=reference,
    )
    second = await surface_startup(
        buckets,
        max_results=4,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert first == second
    assert "四条短核心应该逐字返回。" in first
    assert "[最近一条] [bucket_id:latest]" in first
    assert "近期高重要度正文。" in first
    assert "已经解决但仍属于最近一天的正文。" in first
    assert "近期低重要度正文" not in first
    assert "[未完记忆]" in first
    assert "较早但仍未解决的正文。" in first
    assert "[活动计划] [bucket_id:active-plan]" in first
    assert "测试数据不能进入睁眼" not in first
    assert "已经消化过的普通记忆" not in first
    assert "久未浮现" not in first
    assert "偶然想起" not in first
    assert count_tokens_approx(first) <= 5000


def test_older_unresolved_randomly_rotates_without_immediate_repeat(monkeypatch):
    reference = datetime.fromisoformat("2026-08-18T12:00:00")
    buckets = [
        make_bucket(
            "older-high",
            "高权重旧记忆。",
            created="2026-08-14T09:00:00",
            importance=10,
        ),
        make_bucket(
            "older-mid",
            "中权重旧记忆。",
            created="2026-08-13T09:00:00",
            importance=8,
        ),
        make_bucket(
            "older-low",
            "低权重旧记忆。",
            created="2026-08-12T09:00:00",
            importance=6,
        ),
    ]
    seen_pools = []

    def pick_first(pool):
        seen_pools.append([bucket["id"] for bucket in pool])
        return pool[0]

    monkeypatch.setattr(startup_module.random, "choice", pick_first)
    first, _ = startup_module._select_memories(
        buckets,
        max_results=1,
        reference_time=reference,
    )
    first_id = first[0][0]["id"]
    second, _ = startup_module._select_memories(
        buckets,
        max_results=1,
        reference_time=reference,
        exclude_older_id=first_id,
    )
    second_id = second[0][0]["id"]

    assert first_id == "older-high"
    assert second_id == "older-mid"
    assert first_id != second_id
    assert seen_pools == [
        ["older-high", "older-mid", "older-low"],
        ["older-mid", "older-low"],
    ]


@pytest.mark.asyncio
async def test_startup_returns_long_plan_verbatim_when_total_plan_budget_allows():
    reference = datetime.fromisoformat("2026-08-18T12:00:00")
    plan_body = "轻量睁眼应该直接带回这条完整计划正文，不能再按单条六十 token 提前折叠。" * 6
    buckets = [
        make_bucket(
            "long-active-plan",
            plan_body,
            created="2026-08-18T09:00:00",
            bucket_type="plan",
            status="active",
            weight=0.9,
        )
    ]

    output = await surface_startup(
        buckets,
        max_results=1,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert plan_body in output
    assert "[活动计划] [bucket_id:long-active-plan]" in output
    assert "[未展开]" not in output
    assert "内容较长" not in output


@pytest.mark.asyncio
async def test_startup_lists_plan_pointer_when_full_body_exceeds_plan_budget():
    reference = datetime.fromisoformat("2026-08-18T12:00:00")
    plan_body = "超出启动计划预算的正文不能被截断。" * 500
    buckets = [
        make_bucket(
            "oversized-active-plan",
            plan_body,
            created="2026-08-18T09:00:00",
            bucket_type="plan",
            status="active",
            weight=0.9,
            name="超长活动计划",
        )
    ]

    output = await surface_startup(
        buckets,
        max_results=1,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert plan_body not in output
    assert "[活动计划] [bucket_id:oversized-active-plan]" in output
    assert "↗ [未展开] 超长活动计划" in output
    assert 'breath_advanced(domain="plan")' in output
    assert count_tokens_approx(output) <= 5000


@pytest.mark.asyncio
async def test_next_whole_body_may_cross_soft_target_then_stops_selection():
    reference = datetime.fromisoformat("2026-08-18T12:00:00")
    large_body = "低优先级长正文完整性标记。" * 180
    buckets = [
        make_bucket("latest", "最新正文。", created="2026-08-18T11:30:00"),
        make_bucket(
            "large-low",
            large_body,
            created="2026-08-18T10:00:00",
            importance=5,
        ),
        make_bucket(
            "later-low",
            "跨过软目标后不应继续选择下一条正文。",
            created="2026-08-18T09:00:00",
            importance=4,
        ),
    ]

    output = await surface_startup(
        buckets,
        max_results=3,
        soft_tokens=700,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert "最新正文。" in output
    assert large_body in output
    assert count_tokens_approx(output) > 700
    assert "[未展开] [bucket_id:large-low]" not in output
    assert "soft_target" not in output
    assert "跨过软目标后不应继续选择下一条正文。" not in output
    assert "达到软目标后停止继续取桶" in output
    assert count_tokens_approx(output) <= 5000


@pytest.mark.asyncio
async def test_important_recent_body_may_cross_soft_target_but_not_hard_cap():
    reference = datetime.fromisoformat("2026-08-18T12:00:00")
    important_body = "重要长正文完整性标记。" * 110
    buckets = [
        make_bucket("latest", "最新正文。", created="2026-08-18T11:30:00"),
        make_bucket(
            "important",
            important_body,
            created="2026-08-18T10:00:00",
            importance=9,
        ),
    ]

    output = await surface_startup(
        buckets,
        max_results=3,
        soft_tokens=700,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert important_body in output
    assert count_tokens_approx(output) > 700
    assert count_tokens_approx(output) <= 5000


@pytest.mark.asyncio
async def test_oversized_latest_body_becomes_pointer_at_hard_cap():
    reference = datetime.fromisoformat("2026-08-18T12:00:00")
    body = "WHOLE-BODY-SENTINEL " * 700
    buckets = [make_bucket("too-large", body, created="2026-08-18T11:30:00")]

    output = await surface_startup(
        buckets,
        max_results=1,
        soft_tokens=500,
        hard_tokens=900,
        reference_time=reference,
    )

    assert "WHOLE-BODY-SENTINEL" not in output
    assert "[未展开] [bucket_id:too-large]" in output
    assert "[reason:hard_limit]" in output
    assert count_tokens_approx(output) <= 900


@pytest.mark.asyncio
async def test_startup_dispatch_uses_independent_hard_limit(monkeypatch):
    calls = []

    async def fake_surface_default(**kwargs):
        calls.append(kwargs)
        return "startup output"

    monkeypatch.setattr(breath_module, "surface_default", fake_surface_default)
    monkeypatch.setattr(
        rt,
        "config",
        {
            "surfacing": {
                "startup_breath_max_results": 4,
                "startup_breath_soft_tokens": 3000,
                "startup_breath_max_tokens": 5000,
                "breath_max_results": 17,
                "breath_max_tokens": 9000,
            }
        },
    )

    output = await breath_module.dispatch(startup=True)

    assert output == "startup output"
    assert calls == [
        {
            "max_results": 4,
            "max_tokens": 5000,
            "tag_filter": [],
            "startup": True,
        }
    ]


@pytest.mark.asyncio
async def test_startup_dispatch_clamps_hard_limit(monkeypatch):
    calls = []

    async def fake_surface_default(**kwargs):
        calls.append(kwargs)
        return "startup output"

    monkeypatch.setattr(breath_module, "surface_default", fake_surface_default)
    monkeypatch.setattr(
        rt,
        "config",
        {
            "surfacing": {
                "startup_breath_max_results": 99,
                "startup_breath_max_tokens": 99999,
            }
        },
    )

    await breath_module.dispatch(startup=True)

    assert calls == [
        {
            "max_results": 4,
            "max_tokens": 10000,
            "tag_filter": [],
            "startup": True,
        }
    ]


def test_public_breath_wrapper_enables_startup_without_changing_public_schema():
    server_source = Path("src/server.py").read_text(encoding="utf-8")
    breath_source = Path("src/tools/breath/__init__.py").read_text(encoding="utf-8")

    breath_block = server_source.split("async def breath(", 1)[1].split(
        "async def breath_search(", 1
    )[0]
    assert "dispatch_public(" in breath_block
    assert '"properties": {}' in breath_block
    assert 'kwargs["startup"] = True' in breath_source


def test_dashboard_exposes_soft_and_hard_startup_limits():
    dashboard = Path("frontend/dashboard.html").read_text(encoding="utf-8")
    config_api_source = Path("src/web/config_api.py").read_text(encoding="utf-8")

    assert 'id="cfg-sf-startup-results"' in dashboard
    assert 'id="cfg-sf-startup-soft-tokens"' in dashboard
    assert 'id="cfg-sf-startup-tokens"' in dashboard
    assert "startup_breath_soft_tokens" in config_api_source
    assert "startup_breath_max_tokens" in config_api_source


@pytest.mark.asyncio
async def test_dashboard_config_clamps_startup_limits_independently(monkeypatch):
    runtime = {"surfacing": {"breath_max_results": 20, "breath_max_tokens": 10000}}
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest(
            {
                "surfacing": {
                    "startup_breath_max_results": 99,
                    "startup_breath_soft_tokens": 100,
                    "startup_breath_max_tokens": 99999,
                }
            }
        )
    )

    assert response.status_code == 200
    assert runtime["surfacing"] == {
        "breath_max_results": 20,
        "breath_max_tokens": 10000,
        "startup_breath_max_results": 4,
        "startup_breath_soft_tokens": 500,
        "startup_breath_max_tokens": 10000,
    }
