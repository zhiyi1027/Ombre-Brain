from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from starlette.responses import JSONResponse

from ombrebrain.policy.surfacing import SurfacePolicyVM
from ombrebrain.storage.state_chain import (
    StateChainError,
    clear_supersession,
    set_supersession,
)
from tools import _runtime as rt
from tools.breath.search import surface_search
from tools.hold import dispatch as hold_dispatch
from tools.trace.core import trace_core
from web import _shared as web_shared
from web import buckets as web_buckets


class DisabledEmbedding:
    enabled = False


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, metadata):
        return float(metadata.get("importance") or 5)


class FakeDehydrator:
    async def analyze(self, _content):
        return {
            "domain": ["项目"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": ["状态"],
            "suggested_name": "项目状态",
        }

    def invalidate_cache(self, _content):
        return None


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
    method = "POST"

    def __init__(self, body=None, *, path_params=None, query_params=None):
        self._body = body
        self.path_params = path_params or {}
        self.query_params = query_params or {}

    async def json(self):
        return self._body


def install_runtime(monkeypatch, bucket_mgr, test_config):
    monkeypatch.setattr(rt, "config", test_config, raising=False)
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "embedding_engine", DisabledEmbedding(), raising=False)
    monkeypatch.setattr(rt, "decay_engine", NoopDecay(), raising=False)
    monkeypatch.setattr(rt, "dehydrator", FakeDehydrator(), raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    monkeypatch.setattr(rt, "fire_webhook", None, raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    monkeypatch.setattr(rt, "v3_runtime", None, raising=False)


@pytest.mark.asyncio
async def test_explicit_supersession_is_reversible_and_keeps_both_bodies(
    bucket_mgr,
):
    old_id = await bucket_mgr.create(content="kiwi-mem 正在使用")
    new_id = await bucket_mgr.create(content="kiwi-mem 已经作废")

    result = await set_supersession(
        bucket_mgr,
        old_bucket_id=old_id,
        new_bucket_id=new_id,
        state_key=" Project:KIWI-MEM ",
    )
    old_bucket = await bucket_mgr.get(old_id)
    new_bucket = await bucket_mgr.get(new_id)

    assert result["status"] == "superseded"
    assert result["state_key"] == "project:kiwi-mem"
    assert old_bucket["content"] == "kiwi-mem 正在使用"
    assert new_bucket["content"] == "kiwi-mem 已经作废"
    assert old_bucket["metadata"]["state_key"] == "project:kiwi-mem"
    assert old_bucket["metadata"]["superseded_by"] == new_id
    assert old_bucket["metadata"]["superseded_at"]
    assert new_bucket["metadata"]["state_key"] == "project:kiwi-mem"

    cleared = await clear_supersession(bucket_mgr, old_bucket_id=old_id)
    restored = await bucket_mgr.get(old_id)

    assert cleared["status"] == "current"
    assert restored["metadata"]["state_key"] == "project:kiwi-mem"
    assert "superseded_by" not in restored["metadata"]
    assert "superseded_at" not in restored["metadata"]


@pytest.mark.asyncio
async def test_supersession_rejects_self_plan_mismatch_and_historical_target(
    bucket_mgr,
):
    first = await bucket_mgr.create(content="第一版", state_key="project:one")
    second = await bucket_mgr.create(content="第二版", state_key="project:two")
    plan = await bucket_mgr.create(content="计划", bucket_type="plan")

    with pytest.raises(StateChainError, match="不能取代自己"):
        await set_supersession(
            bucket_mgr,
            old_bucket_id=first,
            new_bucket_id=first,
            state_key="project:one",
        )
    with pytest.raises(StateChainError, match="plan"):
        await set_supersession(
            bucket_mgr,
            old_bucket_id=plan,
            new_bucket_id=second,
            state_key="project:two",
        )
    with pytest.raises(StateChainError, match="不一致"):
        await set_supersession(
            bucket_mgr,
            old_bucket_id=first,
            new_bucket_id=second,
        )

    third = await bucket_mgr.create(content="第三版", state_key="project:two")
    await set_supersession(
        bucket_mgr,
        old_bucket_id=second,
        new_bucket_id=third,
    )
    with pytest.raises(StateChainError, match="历史版本"):
        await set_supersession(
            bucket_mgr,
            old_bucket_id=first,
            new_bucket_id=second,
            state_key="project:one",
        )


@pytest.mark.asyncio
async def test_concurrent_confirmations_cannot_replace_one_old_state_twice(
    bucket_mgr,
):
    old_id = await bucket_mgr.create(
        content="旧状态",
        state_key="project:race",
    )
    first_new = await bucket_mgr.create(
        content="新状态一",
        state_key="project:race",
    )
    second_new = await bucket_mgr.create(
        content="新状态二",
        state_key="project:race",
    )

    results = await asyncio.gather(
        set_supersession(
            bucket_mgr,
            old_bucket_id=old_id,
            new_bucket_id=first_new,
        ),
        set_supersession(
            bucket_mgr,
            old_bucket_id=old_id,
            new_bucket_id=second_new,
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, StateChainError) for result in results) == 1
    stored_target = (await bucket_mgr.get(old_id))["metadata"]["superseded_by"]
    assert stored_target in {first_new, second_new}


@pytest.mark.asyncio
async def test_hold_with_state_key_only_suggests_and_never_auto_supersedes(
    bucket_mgr,
    test_config,
    monkeypatch,
):
    install_runtime(monkeypatch, bucket_mgr, test_config)
    old_id = await bucket_mgr.create(
        content="kiwi-mem 正在使用",
        state_key="project:kiwi-mem",
    )

    output = await hold_dispatch(
        content="kiwi-mem 已经作废",
        importance=7,
        state_key=" Project:KIWI-MEM ",
    )
    await asyncio.sleep(0)
    old_bucket = await bucket_mgr.get(old_id)

    assert "系统没有自动取代" in output
    assert old_id in output
    assert "superseded_by" not in old_bucket["metadata"]


@pytest.mark.asyncio
async def test_superseded_memory_leaves_passive_surfaces_but_search_labels_it(
    bucket_mgr,
    test_config,
    monkeypatch,
):
    install_runtime(monkeypatch, bucket_mgr, test_config)
    old_id = await bucket_mgr.create(
        content="猕猴桃项目目前正在使用",
        importance=10,
        pinned=True,
    )
    new_id = await bucket_mgr.create(
        content="猕猴桃项目目前已经作废",
        importance=7,
    )
    await set_supersession(
        bucket_mgr,
        old_bucket_id=old_id,
        new_bucket_id=new_id,
        state_key="project:kiwi-mem",
    )

    old_bucket = await bucket_mgr.get(old_id)
    policy = SurfacePolicyVM.default()
    assert not policy.evaluate_bucket(old_bucket, mode="spontaneous").allowed
    assert "superseded" in policy.evaluate_bucket(
        old_bucket, mode="spontaneous"
    ).reasons
    assert policy.evaluate_bucket(old_bucket, mode="search").allowed

    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)
    output = await surface_search(
        query="猕猴桃项目",
        max_results=10,
        max_tokens=10_000,
        domain="",
        valence=-1,
        arousal=-1,
        tag_filter=[],
    )

    assert output.index(f"[bucket_id:{new_id}]") < output.index(
        f"[bucket_id:{old_id}]"
    )
    assert "[historical_state:true]" in output
    assert f"[superseded_by:{new_id}]" in output
    assert f"[历史状态] [bucket_id:{old_id}]" in output
    assert f"📌 [核心准则] [bucket_id:{old_id}]" not in output


@pytest.mark.asyncio
async def test_trace_and_dashboard_require_a_separate_confirmed_state_operation(
    bucket_mgr,
    test_config,
    monkeypatch,
):
    install_runtime(monkeypatch, bucket_mgr, test_config)
    old_id = await bucket_mgr.create(content="旧状态")
    new_id = await bucket_mgr.create(content="新状态")

    rejected = await trace_core(
        old_id,
        importance=8,
        state_key="project:test",
        superseded_by=new_id,
    )
    assert "必须单独确认" in rejected
    assert "superseded_by" not in (await bucket_mgr.get(old_id))["metadata"]

    traced = await trace_core(
        old_id,
        state_key="project:test",
        superseded_by=new_id,
    )
    assert "已把记忆桶" in traced

    ignored_clear_key = await trace_core(
        old_id,
        state_key="project:other",
        superseded_by=r"\clear",
    )
    assert "不要同时传 state_key" in ignored_clear_key
    assert (await bucket_mgr.get(old_id))["metadata"]["superseded_by"] == new_id

    monkeypatch.setattr(web_shared, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(web_shared, "_require_auth", lambda _request: None)
    mcp = FakeMCP()
    web_buckets.register(mcp)
    route = mcp.routes[("DELETE", "/api/bucket/{bucket_id}/supersession")]

    denied = await route(
        JsonRequest(path_params={"bucket_id": old_id})
    )
    accepted = await route(
        JsonRequest(
            path_params={"bucket_id": old_id},
            query_params={"confirm": "true"},
        )
    )

    assert denied.status_code == 400
    assert accepted.status_code == 200
    assert json.loads(accepted.body)["status"] == "current"

    post_route = mcp.routes[("POST", "/api/bucket/{bucket_id}/supersession")]
    post_denied = await post_route(
        JsonRequest(
            {"state_key": "project:test", "superseded_by": new_id},
            path_params={"bucket_id": old_id},
        )
    )
    post_accepted = await post_route(
        JsonRequest(
            {
                "state_key": "project:test",
                "superseded_by": new_id,
                "confirm": True,
            },
            path_params={"bucket_id": old_id},
        )
    )

    assert post_denied.status_code == 400
    assert post_accepted.status_code == 200
    post_payload = json.loads(post_accepted.body)
    assert post_payload["status"] == "superseded"
    assert "旧状态" not in post_payload.values()


@pytest.mark.asyncio
async def test_trace_rejects_state_key_on_plan_and_state_delete_mix(
    bucket_mgr,
    test_config,
    monkeypatch,
):
    install_runtime(monkeypatch, bucket_mgr, test_config)
    plan_id = await bucket_mgr.create(content="旧计划", bucket_type="plan")
    old_id = await bucket_mgr.create(content="旧状态")
    new_id = await bucket_mgr.create(content="新状态")

    plan_result = await trace_core(plan_id, state_key="project:plan")
    mixed_result = await trace_core(
        old_id,
        delete=True,
        state_key="project:test",
        superseded_by=new_id,
    )

    assert "仅用于普通 dynamic/permanent" in plan_result
    assert "不能与 delete/hard_delete" in mixed_result
    assert await bucket_mgr.get(old_id) is not None


@pytest.mark.asyncio
async def test_trace_does_not_rekey_an_existing_state_chain(
    bucket_mgr,
    test_config,
    monkeypatch,
):
    install_runtime(monkeypatch, bucket_mgr, test_config)
    bucket_id = await bucket_mgr.create(
        content="当前事实",
        state_key="project:one",
    )

    result = await trace_core(bucket_id, state_key="project:two")

    assert "不能直接改成" in result
    bucket = await bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["state_key"] == "project:one"


def test_dashboard_exposes_state_chain_without_raw_html_interpolation():
    dashboard = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "frontend"
        / "dashboard.html"
    ).read_text(encoding="utf-8")

    assert "标记为历史版本" in dashboard
    assert "bucketSupersede" in dashboard
    assert "bucketClearSupersession" in dashboard
    assert "/supersession" in dashboard
    assert "esc(meta.state_key)" in dashboard
    assert "escAttr(meta.superseded_by)" in dashboard
