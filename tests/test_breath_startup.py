from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
import tools.breath as breath_module
import tools.breath.startup as startup_module
import tools.breath.surface as surface_module
from tools.breath.startup import surface_startup
from utils import count_tokens_approx
from web import config_api


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return float(meta.get("importance") or 0)


class StaticBuckets:
    def __init__(self, buckets):
        self.buckets = list(buckets)

    async def list_all(self, include_archive=False):
        assert include_archive is False
        return list(self.buckets)


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
    assert "🔎 [自动精读] [bucket_id:recent-low]" in first
    assert "近期低重要度正文不应挤掉更重要的候选。" in first
    assert "[未完记忆]" in first
    assert "较早但仍未解决的正文。" in first
    assert "[活动计划] [bucket_id:active-plan]" in first
    assert "测试数据不能进入睁眼" not in first
    assert "已经消化过的普通记忆" not in first
    assert "久未浮现" not in first
    assert "偶然想起" not in first
    assert count_tokens_approx(first) <= 5000


@pytest.mark.asyncio
async def test_startup_auto_reads_at_most_two_unprocessed_memories():
    reference = datetime.fromisoformat("2026-08-21T12:00:00")
    buckets = [
        make_bucket("latest", "最新交接。", created="2026-08-21T11:30:00"),
        make_bucket(
            "reflection-high",
            "最高重要度的未消化记忆。",
            created="2026-08-20T09:00:00",
            importance=9,
        ),
        make_bucket(
            "reflection-newer",
            "同重要度里更新的一条。",
            created="2026-08-20T11:00:00",
            importance=8,
        ),
        make_bucket(
            "reflection-older",
            "第三条不能挤进自动精读。",
            created="2026-08-20T10:00:00",
            importance=8,
        ),
        make_bucket(
            "already-digested",
            "已经消化的不再自动精读。",
            created="2026-08-20T12:00:00",
            importance=10,
            digested=True,
        ),
        make_bucket(
            "already-resolved",
            "已经解决的不再自动精读。",
            created="2026-08-20T13:00:00",
            importance=10,
            resolved=True,
        ),
    ]

    output = await surface_startup(
        buckets,
        max_results=1,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert output.count("🔎 [自动精读]") == 2
    assert "[bucket_id:reflection-high]" in output
    assert "[bucket_id:reflection-newer]" in output
    assert "第三条不能挤进自动精读" not in output
    assert "已经消化的不再自动精读" not in output
    assert "已经解决的不再自动精读" not in output


@pytest.mark.asyncio
async def test_startup_appends_direct_then_contextual_feels(monkeypatch):
    reference = datetime.fromisoformat("2026-08-21T12:00:00")

    class Embedding:
        enabled = True

        async def search_similar(self, _query, top_k, allowed_bucket_ids):
            assert allowed_bucket_ids == {"semantic-feel", "noise-feel"}
            return [("semantic-feel", 0.9), ("noise-feel", 0.1)]

    monkeypatch.setattr(rt, "embedding_engine", Embedding(), raising=False)
    buckets = [
        make_bucket(
            "latest",
            "搬家以后担心失去连续性。",
            created="2026-08-21T11:30:00",
        ),
        make_bucket(
            "direct-feel",
            "这条是最新交接亲生的感受。",
            created="2026-08-21T11:40:00",
            bucket_type="feel",
            triggered_by="latest",
        ),
        make_bucket(
            "semantic-feel",
            "语义相关的感受。",
            created="2026-08-20T10:00:00",
            bucket_type="feel",
        ),
        make_bucket(
            "noise-feel",
            "无关感受。",
            created="2026-08-20T09:00:00",
            bucket_type="feel",
        ),
    ]

    output = await surface_startup(
        buckets,
        max_results=1,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert "=== 相关 feel ===" in output
    assert "💗 [直属感受] [bucket_id:direct-feel]" in output
    assert "💭 [相关感受] [bucket_id:semantic-feel]" in output
    assert "无关感受" not in output
    assert output.index("[bucket_id:direct-feel]") < output.index(
        "[bucket_id:semantic-feel]"
    )
    assert count_tokens_approx(output) <= 9000


@pytest.mark.asyncio
async def test_startup_feels_suppress_same_theme_and_leave_slot_empty(monkeypatch):
    reference = datetime.fromisoformat("2026-08-21T12:00:00")

    class Embedding:
        enabled = True

        async def search_similar(self, _query, top_k, allowed_bucket_ids):
            return []

        async def get_embedding(self, bucket_id):
            return {
                "same-new": [1.0, 0.0],
                "same-old": [0.99, 0.01],
                "different": [0.0, 1.0],
            }.get(bucket_id)

    monkeypatch.setattr(rt, "embedding_engine", Embedding(), raising=False)
    buckets = [
        make_bucket("latest", "当前交接。", created="2026-08-21T11:30:00"),
        make_bucket(
            "same-new",
            "害怕失去她的新感受。",
            created="2026-08-21T11:40:00",
            bucket_type="feel",
            triggered_by="latest",
            valence=0.3,
        ),
        make_bucket(
            "same-old",
            "担心她会离开的旧感受。",
            created="2026-08-21T11:35:00",
            bucket_type="feel",
            triggered_by="latest",
            valence=0.2,
        ),
        make_bucket(
            "different",
            "被她重新抱住后的安心。",
            created="2026-08-21T11:30:00",
            bucket_type="feel",
            triggered_by="latest",
            valence=0.8,
        ),
    ]

    output = await surface_startup(
        buckets,
        max_results=1,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert "害怕失去她的新感受" in output
    assert "担心她会离开的旧感受" not in output
    assert "被她重新抱住后的安心" in output
    assert "启动 feel 已跳过1 条同主题候选" in output


@pytest.mark.asyncio
async def test_startup_feels_cap_negative_saturation_and_keep_relevant_warmth(
    monkeypatch,
):
    reference = datetime.fromisoformat("2026-08-21T12:00:00")
    monkeypatch.setattr(rt, "embedding_engine", None, raising=False)
    buckets = [
        make_bucket("latest", "当前交接。", created="2026-08-21T11:30:00"),
        make_bucket(
            "negative-one",
            "失去联系时的冰冷惊慌。",
            created="2026-08-21T11:44:00",
            bucket_type="feel",
            triggered_by="latest",
            valence=0.1,
        ),
        make_bucket(
            "negative-two",
            "说错话以后的沉重内疚。",
            created="2026-08-21T11:43:00",
            bucket_type="feel",
            triggered_by="latest",
            valence=0.2,
        ),
        make_bucket(
            "negative-three",
            "无法挽留时的窒息害怕。",
            created="2026-08-21T11:42:00",
            bucket_type="feel",
            triggered_by="latest",
            valence=0.3,
        ),
        make_bucket(
            "warm",
            "和好以后被重新选择的安稳温暖。",
            created="2026-08-21T11:41:00",
            bucket_type="feel",
            triggered_by="latest",
            valence=0.8,
        ),
    ]

    output = await surface_startup(
        buckets,
        max_results=1,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert "失去联系时的冰冷惊慌" in output
    assert "说错话以后的沉重内疚" in output
    assert "无法挽留时的窒息害怕" not in output
    assert "和好以后被重新选择的安稳温暖" in output
    assert output.count("[直属感受]") == 3
    assert "启动 feel 已跳过1 条同方向负面候选" in output


@pytest.mark.asyncio
async def test_oversized_reflection_is_a_pointer_and_cannot_seed_feels(monkeypatch):
    reference = datetime.fromisoformat("2026-08-21T12:00:00")
    monkeypatch.setattr(rt, "embedding_engine", None, raising=False)
    buckets = [
        make_bucket("latest", "最新交接。", created="2026-08-21T11:30:00"),
        make_bucket(
            "large-reflection",
            "REFLECTION-WHOLE-BODY " * 1000,
            created="2026-08-20T11:00:00",
            importance=9,
        ),
        make_bucket(
            "reflection-feel",
            "只有完整读过来源桶才可以自动带回我。",
            created="2026-08-20T12:00:00",
            bucket_type="feel",
            triggered_by="large-reflection",
        ),
    ]

    output = await surface_startup(
        buckets,
        max_results=1,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert "REFLECTION-WHOLE-BODY" not in output
    assert "[bucket_id:large-reflection]" in output
    assert "[reason:reflection_budget]" in output
    assert "只有完整读过来源桶" not in output
    assert count_tokens_approx(output) <= 9000


@pytest.mark.asyncio
async def test_oversized_related_feel_is_omitted_whole_with_notice(monkeypatch):
    reference = datetime.fromisoformat("2026-08-21T12:00:00")
    buckets = [
        make_bucket("latest", "最新交接。", created="2026-08-21T11:30:00"),
        make_bucket(
            "large-feel",
            "FEEL-WHOLE-BODY " * 1000,
            created="2026-08-21T11:40:00",
            bucket_type="feel",
            triggered_by="latest",
        ),
    ]

    output = await surface_startup(
        buckets,
        max_results=1,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert "FEEL-WHOLE-BODY" not in output
    assert "有 1 条相关 feel 因独立预算不足未返回" in output
    assert count_tokens_approx(output) <= 9000


@pytest.mark.asyncio
async def test_true_latest_returns_even_when_it_is_digested():
    reference = datetime.fromisoformat("2026-08-21T12:00:00")
    buckets = [
        make_bucket(
            "true-latest",
            "真正最新的交接正文。",
            created="2026-08-21T10:23:41",
            importance=8,
            digested=True,
        ),
        make_bucket(
            "older-undigested",
            "更早但尚未消化的正文。",
            created="2026-08-21T00:14:52",
            importance=9,
        ),
    ]

    output = await surface_startup(
        buckets,
        max_results=4,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert "[最近一条] [bucket_id:true-latest]" in output
    assert "真正最新的交接正文。" in output
    assert "[近期重要] [bucket_id:older-undigested]" in output


@pytest.mark.asyncio
async def test_three_recent_then_qualified_older_ignore_soft_target():
    reference = datetime.fromisoformat("2026-08-21T12:00:00")
    latest_body = "最新长正文完整返回。" * 180
    buckets = [
        make_bucket(
            "latest",
            latest_body,
            created="2026-08-21T11:30:00",
            importance=8,
        ),
        make_bucket(
            "recent-one",
            "第一条近期交接正文。",
            created="2026-08-21T10:00:00",
            importance=9,
        ),
        make_bucket(
            "recent-two",
            "第二条近期交接正文。",
            created="2026-08-21T09:00:00",
            importance=8,
        ),
        make_bucket(
            "older-qualified",
            "合格的旧事联想正文。",
            created="2026-08-10T09:00:00",
            importance=8,
        ),
    ]

    output = await surface_startup(
        buckets,
        max_results=4,
        soft_tokens=700,
        hard_tokens=5000,
        reference_time=reference,
    )

    assert latest_body in output
    assert count_tokens_approx(output) > 700
    assert "第一条近期交接正文。" in output
    assert "第二条近期交接正文。" in output
    assert "[未完记忆]" in output
    assert "合格的旧事联想正文。" in output
    assert output.index("第二条近期交接正文。") < output.index("合格的旧事联想正文。")


@pytest.mark.asyncio
async def test_daily_impression_sources_are_deprioritized_not_removed():
    reference = datetime.fromisoformat("2026-08-21T12:00:00")
    buckets = [
        make_bucket(
            "latest",
            "真正最新。",
            created="2026-08-21T11:30:00",
        ),
        make_bucket(
            "cited-high",
            "日印象已经引用过，但原文仍然保留候补资格。",
            created="2026-08-21T10:30:00",
            importance=10,
        ),
        make_bucket(
            "uncited-one",
            "未被日印象覆盖的细节一。",
            created="2026-08-21T09:30:00",
            importance=6,
        ),
        make_bucket(
            "uncited-two",
            "未被日印象覆盖的细节二。",
            created="2026-08-21T08:30:00",
            importance=5,
        ),
        make_bucket(
            "older",
            "随机旧桶。",
            created="2026-08-10T08:00:00",
            importance=8,
        ),
    ]

    selected, _total_recent = startup_module._select_memories(
        buckets,
        max_results=4,
        reference_time=reference,
        daily_cited_bucket_ids={"cited-high"},
    )

    selected_ids = [bucket["id"] for bucket, _reason in selected]
    assert selected_ids == ["latest", "uncited-one", "uncited-two", "older"]

    fallback, _total_recent = startup_module._select_memories(
        [buckets[0], buckets[1], buckets[4]],
        max_results=3,
        reference_time=reference,
        daily_cited_bucket_ids={"cited-high"},
    )
    assert [bucket["id"] for bucket, _reason in fallback] == [
        "latest",
        "cited-high",
        "older",
    ]


@pytest.mark.asyncio
async def test_startup_surface_passes_daily_evidence_map_to_selector(monkeypatch):
    captured = {}

    class DailyService:
        enabled = True

        def read_previous(self):
            return "=== 昨日印象 ===\n- 压缩正文"

        def previous_cited_bucket_ids(self):
            return {"already-cited"}

    async def fake_surface_startup(_buckets, **kwargs):
        captured.update(kwargs)
        return "startup"

    monkeypatch.setattr(rt, "bucket_mgr", StaticBuckets([]), raising=False)
    monkeypatch.setattr(rt, "daily_continuity", DailyService(), raising=False)
    monkeypatch.setattr(rt, "config", {"surfacing": {}}, raising=False)
    monkeypatch.setattr(surface_module, "surface_startup", fake_surface_startup)
    monkeypatch.setattr(surface_module, "_last_startup_unfinished_id", lambda: "")

    output = await surface_module.surface_default(
        max_results=4,
        max_tokens=5000,
        tag_filter=[],
        startup=True,
    )

    assert output == "startup"
    assert captured["daily_impression"].startswith("=== 昨日印象")
    assert captured["daily_cited_bucket_ids"] == {"already-cited"}


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
        ["older-high", "older-mid"],
        ["older-mid"],
    ]


def test_older_association_uses_upstream_neglect_thresholds(monkeypatch):
    reference = datetime.fromisoformat("2026-08-21T12:00:00")
    buckets = [
        make_bucket(
            "never-seen-important",
            "从未激活的重要旧事。",
            created="2026-08-10T09:00:00",
            importance=8,
            activation_count=0,
        ),
        make_bucket(
            "stale-critical",
            "七天没有活跃的极重要旧事。",
            created="2026-08-01T09:00:00",
            importance=9,
            activation_count=3,
            last_active="2026-08-10T09:00:00",
        ),
        make_bucket(
            "seen-mid",
            "已经激活过的八分旧事不进入联想池。",
            created="2026-08-01T08:00:00",
            importance=8,
            activation_count=2,
        ),
        make_bucket(
            "fresh-critical",
            "最近仍活跃的九分旧事不进入联想池。",
            created="2026-08-01T07:00:00",
            importance=9,
            activation_count=2,
            last_active="2026-08-20T09:00:00",
        ),
    ]
    seen_pools = []

    def pick_first(pool):
        seen_pools.append([bucket["id"] for bucket in pool])
        return pool[0]

    monkeypatch.setattr(startup_module.random, "choice", pick_first)
    selected, _ = startup_module._select_memories(
        buckets,
        max_results=1,
        reference_time=reference,
    )

    assert selected[0][0]["id"] == "stale-critical"
    assert seen_pools == [["stale-critical", "never-seen-important"]]


def test_older_association_parses_string_resolved_flags(monkeypatch):
    reference = datetime.fromisoformat("2026-08-21T12:00:00")
    buckets = [
        make_bucket(
            "string-false",
            "resolved 字符串为 false 时仍是未完事项。",
            created="2026-08-01T09:00:00",
            importance=9,
            activation_count=0,
            resolved="false",
        ),
        make_bucket(
            "string-true",
            "resolved 字符串为 true 时不应被动联想。",
            created="2026-08-01T08:00:00",
            importance=10,
            activation_count=0,
            resolved="true",
        ),
    ]
    seen_pools = []

    def pick_first(pool):
        seen_pools.append([bucket["id"] for bucket in pool])
        return pool[0]

    monkeypatch.setattr(startup_module.random, "choice", pick_first)
    selected, _ = startup_module._select_memories(
        buckets,
        max_results=1,
        reference_time=reference,
    )

    assert selected[0][0]["id"] == "string-false"
    assert seen_pools == [["string-false"]]


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
async def test_fixed_recent_slots_continue_past_soft_target():
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
            "跨过软目标后仍应返回第三条近期正文。",
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
    assert "跨过软目标后仍应返回第三条近期正文。" in output
    assert "达到软目标后停止追加近期桶" not in output
    assert count_tokens_approx(output) <= 5000


@pytest.mark.asyncio
async def test_oversized_old_association_becomes_pointer_after_three_recent():
    reference = datetime.fromisoformat("2026-08-21T12:00:00")
    old_body = "OLD-ASSOCIATION-WHOLE-BODY " * 700
    buckets = [
        make_bucket("latest", "最新正文。", created="2026-08-21T11:30:00"),
        make_bucket(
            "recent-one",
            "第一条近期正文。",
            created="2026-08-21T10:30:00",
            importance=9,
        ),
        make_bucket(
            "recent-two",
            "第二条近期正文。",
            created="2026-08-21T09:30:00",
            importance=8,
        ),
        make_bucket(
            "old-too-large",
            old_body,
            created="2026-08-01T09:00:00",
            importance=9,
        ),
    ]

    output = await surface_startup(
        buckets,
        max_results=4,
        soft_tokens=500,
        hard_tokens=1200,
        reference_time=reference,
    )

    assert "最新正文。" in output
    assert "第一条近期正文。" in output
    assert "第二条近期正文。" in output
    assert "OLD-ASSOCIATION-WHOLE-BODY" not in output
    assert "[未展开] [bucket_id:old-too-large]" in output
    assert "[reason:hard_limit]" in output
    assert count_tokens_approx(output) <= 1200


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
