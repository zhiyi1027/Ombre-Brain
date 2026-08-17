from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
import tools.breath as breath_module
from tools.breath.surface import surface_default
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


def install_runtime(bucket_mgr, decay_engine):
    rt.config = {"surfacing": {"sampling": {"enabled": False}}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = decay_engine
    rt.logger = MagicMock()
    rt.mark_op = None
    rt.record_v3_tool_event = lambda *args, **kwargs: None


@pytest.mark.asyncio
async def test_startup_breath_indexes_core_but_keeps_dynamic_body_verbatim(
    bucket_mgr,
    decay_eng,
    monkeypatch,
):
    core_id = await bucket_mgr.create(
        content="CORE BODY MUST STAY OUT OF LIGHTWEIGHT STARTUP.",
        name="核心关系准则",
        bucket_type="permanent",
        importance=10,
        domain=["relationship"],
    )
    dynamic_body = "动态原文必须逐字出现，不做摘要，也不截断。"
    dynamic_id = await bucket_mgr.create(
        content=dynamic_body,
        name="最近动态",
        importance=9,
        domain=["daily"],
    )
    install_runtime(bucket_mgr, decay_eng)
    monkeypatch.setattr("tools.breath.surface.random.shuffle", lambda items: None)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 0.0)

    output = await surface_default(
        max_results=1,
        max_tokens=3000,
        tag_filter=[],
        startup=True,
    )

    assert "=== 轻量睁眼 ===" in output
    assert "=== 核心索引（正文按需读取） ===" in output
    assert f"[bucket_id:{core_id}]" in output
    assert "核心关系准则" in output
    assert "CORE BODY MUST STAY OUT" not in output
    assert f"[bucket_id:{dynamic_id}]" in output
    assert dynamic_body in output
    assert "=== 久未浮现 ===" not in output
    assert "=== 偶然想起 ===" not in output
    assert count_tokens_approx(output) <= 3000


@pytest.mark.asyncio
async def test_startup_breath_hard_cap_removes_whole_oversized_body(
    bucket_mgr,
    decay_eng,
    monkeypatch,
):
    body = "WHOLE-BODY-SENTINEL " * 500
    bucket_id = await bucket_mgr.create(
        content=body,
        name="超预算动态桶",
        importance=10,
        domain=["daily"],
    )
    install_runtime(bucket_mgr, decay_eng)
    monkeypatch.setattr("tools.breath.surface.random.shuffle", lambda items: None)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    output = await surface_default(
        max_results=1,
        max_tokens=500,
        tag_filter=[],
        startup=True,
    )

    assert f"[bucket_id:{bucket_id}]" not in output
    assert "WHOLE-BODY-SENTINEL" not in output
    assert "token 预算不足" in output
    assert count_tokens_approx(output) <= 500


@pytest.mark.asyncio
async def test_startup_dispatch_uses_independent_limits(monkeypatch):
    calls = []

    async def fake_surface_default(**kwargs):
        calls.append(kwargs)
        return "startup output"

    monkeypatch.setattr(breath_module, "surface_default", fake_surface_default)
    monkeypatch.setattr(rt, "decay_engine", NoopDecay())
    monkeypatch.setattr(
        rt,
        "config",
        {
            "surfacing": {
                "startup_breath_max_results": 3,
                "startup_breath_max_tokens": 1800,
                "breath_max_results": 17,
                "breath_max_tokens": 9000,
            }
        },
    )
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *args, **kwargs: None)

    output = await breath_module.dispatch(startup=True)

    assert output == "startup output"
    assert calls == [
        {
            "max_results": 3,
            "max_tokens": 1800,
            "tag_filter": [],
            "startup": True,
        }
    ]


@pytest.mark.asyncio
async def test_startup_dispatch_clamps_config_to_lightweight_contract(monkeypatch):
    calls = []

    async def fake_surface_default(**kwargs):
        calls.append(kwargs)
        return "startup output"

    monkeypatch.setattr(breath_module, "surface_default", fake_surface_default)
    monkeypatch.setattr(rt, "decay_engine", NoopDecay())
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
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *args, **kwargs: None)

    await breath_module.dispatch(startup=True)

    assert calls == [
        {
            "max_results": 12,
            "max_tokens": 8000,
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


def test_dashboard_exposes_separate_startup_and_full_breath_limits():
    dashboard = Path("frontend/dashboard.html").read_text(encoding="utf-8")
    config_api = Path("src/web/config_api.py").read_text(encoding="utf-8")

    assert 'id="cfg-sf-startup-results"' in dashboard
    assert 'id="cfg-sf-startup-tokens"' in dashboard
    assert "startup_breath_max_results" in config_api
    assert "startup_breath_max_tokens" in config_api


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
                    "startup_breath_max_tokens": 100,
                }
            }
        )
    )

    assert response.status_code == 200
    assert runtime["surfacing"] == {
        "breath_max_results": 20,
        "breath_max_tokens": 10000,
        "startup_breath_max_results": 12,
        "startup_breath_max_tokens": 500,
    }
