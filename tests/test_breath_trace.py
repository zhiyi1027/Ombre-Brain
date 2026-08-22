import json

import pytest

import tools._runtime as rt
import tools.breath as breath_module
from tools.breath._envelope import DAILY_IMPRESSION_SENTINEL
from tools.breath.trace import (
    clear_runs_for_tests,
    get_run,
    list_runs,
    record_surface_output,
)
from web import breath_trace


SAMPLE_OUTPUT = """=== 核心准则 ===
📌 [核心准则] [bucket_id:core-one]
核心一正文里即使提到 [bucket_id:not-a-returned-bucket] 也不是新桶
---
📌 [核心准则] [bucket_id:core-two]
核心二正文

=== 浮现记忆 ===
💭 [权重:0.812] [bucket_id:dynamic-one]
动态正文

=== 久未浮现 ===
🌙 [久未浮现] [bucket_id:passive-one]
被动正文

token 预算不足：有 3 条主要浮现记忆因放不下剩余预算而未返回；已返回正文均保持完整，未截断或摘要。当前约使用 987/1000 token，如需被省略的整桶请提高 max_tokens 后重试。"""

STARTUP_OUTPUT = """=== 轻量睁眼 ===
轻量简报：核心、最近24小时、随机轮换的较早未完事项与活动计划。

=== 核心准则 ===
📌 [核心准则] [bucket_id:core-one]
核心正文

=== 最近24小时 ===
🕒 [最近一条] [bucket_id:recent-one]
最近正文

=== 较早未完事项 ===
🧭 [未完记忆] [权重:0.80] [bucket_id:unfinished-one]
未完正文

=== 活动计划 ===
📋 [活动计划] [bucket_id:plan-one] [weight:0.90] 完成施工

=== 未展开（按需读取） ===
↗ [未展开] [bucket_id:large-one] [estimated_tokens:4000] [reason:hard_limit] 大桶

=== 本次预算 ===
软目标 3000 token，硬上限 5000 token；记忆正文只整桶返回，不截断。"""

ONE_BUTTON_OUTPUT = f"""=== 一键睁眼 ===
一次返回完整启动上下文。

{DAILY_IMPRESSION_SENTINEL}
=== 昨日印象 · 2026-08-20 ===
发生了什么：
- 昨日正文

=== 最近24小时 ===
🕒 [最近一条] [bucket_id:recent-one]
最近正文

=== 自动精读 ===
🔎 [自动精读] [bucket_id:reflection-one]
精读正文
---
🔎 [自动精读] ↗ [未展开] [bucket_id:reflection-large] [reason:reflection_budget] 大桶

=== 相关 feel ===
💗 [直属感受] [bucket_id:feel-direct] [source_bucket:recent-one]
直属正文
---
💭 [相关感受] [bucket_id:feel-related]
相关正文
"""

DAILY_HEADING_INSIDE_BUCKET_OUTPUT = """=== 核心准则 ===
📌 [核心准则] [bucket_id:core-with-heading]
这是记忆正文。
=== 昨日印象 · 2026-08-19 ===
这一行也只是记忆正文，不是启动分区。

=== 最近24小时 ===
🕒 [最近一条] [bucket_id:recent-after-heading]
最近正文
"""


class NoopDecay:
    async def ensure_started(self):
        return None


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(fn):
            for method in methods:
                self.routes[(method, path)] = fn
            return fn

        return decorator


class FakeBucketManager:
    def __init__(self):
        self.list_calls = 0

    async def list_all(self, include_archive=False):
        self.list_calls += 1
        return [
            {"id": "core-one", "metadata": {"name": "核心一"}},
            {"id": "dynamic-one", "metadata": {"name": "动态一"}},
        ]


class FakeRequest:
    query_params = {"kind": "actual", "limit": "10"}


@pytest.fixture(autouse=True)
def isolated_trace():
    clear_runs_for_tests()
    yield
    clear_runs_for_tests()


def test_trace_preserves_order_across_sections_and_exact_output():
    row = record_surface_output(
        SAMPLE_OUTPUT,
        kind="actual",
        max_results=20,
        max_tokens=1000,
        run_id="known-run",
    )

    assert [entry["bucket_id"] for entry in row["entries"]] == [
        "core-one",
        "core-two",
        "dynamic-one",
        "passive-one",
    ]
    assert [entry["section"] for entry in row["entries"]] == [
        "core",
        "core",
        "dynamic",
        "passive",
    ]
    assert row["counts"] == {"returned": 4, "omitted_budget": 3, "pointers": 0}
    assert row["budgeted_entry_tokens"] == 987
    assert row["limits"]["max_tokens"] == 1000
    assert row["output"] == SAMPLE_OUTPUT
    assert get_run("known-run")["output"] == SAMPLE_OUTPUT
    assert "output" not in list_runs(limit=1)[0]


def test_trace_understands_deterministic_startup_sections_and_pointer():
    row = record_surface_output(
        STARTUP_OUTPUT,
        kind="actual",
        mode="startup",
        max_results=4,
        max_tokens=5000,
        run_id="startup-run",
    )

    assert row["mode"] == "startup"
    assert [entry["section"] for entry in row["entries"]] == [
        "core",
        "recent",
        "unfinished",
        "plan",
        "deferred",
    ]
    assert [entry["reason"] for entry in row["entries"]] == [
        "core_always_surface",
        "recent_latest",
        "older_unresolved",
        "active_plan",
        "budget_pointer",
    ]
    assert row["entries"][-1]["status"] == "pointer"
    assert row["counts"] == {"returned": 4, "omitted_budget": 1, "pointers": 1}
    assert row["output"] == STARTUP_OUTPUT


def test_trace_understands_one_button_reflection_and_feel_sections():
    row = record_surface_output(
        ONE_BUTTON_OUTPUT,
        kind="simulation",
        mode="startup",
        max_results=4,
        max_tokens=9000,
        run_id="one-button-run",
    )

    assert [entry["section"] for entry in row["entries"]] == [
        "daily",
        "recent",
        "reflection",
        "reflection",
        "feel",
        "feel",
    ]
    assert [entry["reason"] for entry in row["entries"]] == [
        "daily_continuity",
        "recent_latest",
        "automatic_reflection",
        "automatic_reflection",
        "direct_source_feel",
        "context_relevance",
    ]
    assert row["entries"][3]["status"] == "pointer"
    assert row["counts"] == {"returned": 5, "omitted_budget": 1, "pointers": 1}
    enriched = breath_trace._with_bucket_names(row, {})
    assert enriched["entries"][0]["name"] == "昨日印象 · 2026-08-20"


def test_trace_does_not_treat_daily_heading_inside_bucket_body_as_section():
    row = record_surface_output(
        DAILY_HEADING_INSIDE_BUCKET_OUTPUT,
        kind="actual",
        mode="startup",
        max_results=4,
        max_tokens=9000,
    )

    assert [entry["bucket_id"] for entry in row["entries"]] == [
        "core-with-heading",
        "recent-after-heading",
    ]
    assert all(entry["section"] != "daily" for entry in row["entries"])


@pytest.mark.asyncio
async def test_dispatch_records_the_exact_default_breath_without_changing_it(monkeypatch):
    async def fake_surface_default(**kwargs):
        assert kwargs == {
            "max_results": 12,
            "max_tokens": 1000,
            "tag_filter": [],
            "startup": False,
        }
        return SAMPLE_OUTPUT

    monkeypatch.setattr(breath_module, "surface_default", fake_surface_default)
    monkeypatch.setattr(rt, "decay_engine", NoopDecay())
    monkeypatch.setattr(
        rt,
        "config",
        {"surfacing": {"breath_max_results": 12, "breath_max_tokens": 1000}},
    )
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *args, **kwargs: None)

    output = await breath_module.dispatch()

    assert output == SAMPLE_OUTPUT
    runs = list_runs(limit=10, kind="actual")
    assert len(runs) == 1
    assert runs[0]["counts"]["returned"] == 4
    assert get_run(runs[0]["run_id"])["output"] == SAMPLE_OUTPUT


@pytest.mark.asyncio
async def test_exact_simulation_reuses_default_surface_and_is_labeled(monkeypatch):
    calls = []

    async def fake_surface_default(**kwargs):
        calls.append(kwargs)
        return ONE_BUTTON_OUTPUT

    monkeypatch.setattr(breath_module, "surface_default", fake_surface_default)
    monkeypatch.setattr(rt, "decay_engine", NoopDecay())
    monkeypatch.setattr(
        rt,
        "config",
        {
            "surfacing": {
                "startup_breath_max_results": 3,
                "startup_breath_max_tokens": 1500,
                "breath_max_results": 9,
                "breath_max_tokens": 1000,
            }
        },
    )

    row = await breath_module.simulate_default_surface()

    assert calls == [
        {
            "max_results": 3,
            "max_tokens": 1500,
            "tag_filter": [],
            "startup": True,
        }
    ]
    assert row["kind"] == "simulation"
    assert row["mode"] == "startup"
    assert row["limits"]["max_tokens"] == 7500
    assert row["output"] == ONE_BUTTON_OUTPUT


@pytest.mark.asyncio
async def test_authenticated_web_list_enriches_bucket_names(monkeypatch):
    record_surface_output(
        SAMPLE_OUTPUT,
        kind="actual",
        max_results=20,
        max_tokens=1000,
        run_id="route-run",
    )
    record_surface_output(
        SAMPLE_OUTPUT,
        kind="actual",
        max_results=20,
        max_tokens=1000,
        run_id="route-run-two",
    )
    manager = FakeBucketManager()
    monkeypatch.setattr(breath_trace.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(breath_trace.sh, "bucket_mgr", manager)
    mcp = FakeMCP()
    breath_trace.register(mcp)

    response = await mcp.routes[("GET", "/api/breath-runs")](FakeRequest())
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert len(payload["runs"]) == 2
    assert payload["runs"][0]["entries"][0]["name"] == "核心一"
    assert "output" not in payload["runs"][0]
    assert manager.list_calls == 1


def test_dashboard_contract_separates_actual_trace_from_score_debug():
    dashboard_module = open("src/web/dashboard.py", encoding="utf-8").read()
    web_init = open("src/web/__init__.py", encoding="utf-8").read()
    script = open("frontend/breath-trace.js", encoding="utf-8").read()

    assert 'breath-trace.js?v={sh.version}' in dashboard_module
    assert '"breath-trace.js": "text/javascript"' in dashboard_module
    assert '("web.breath_trace", breath_trace.register)' in web_init
    assert "/api/breath-runs?kind=actual" in script
    assert "/api/breath-simulate" in script
    assert "最近一次真实 Breath" in script
    assert "自动精读" in script
    assert "相关 feel" in script
    assert "四维评分调试" in script
    assert "不代表真实无参 breath 返回顺序" in script
