import json
from datetime import datetime
from pathlib import Path

import pytest

import tools._runtime as rt
from tools.plan import core as plan_core
from plan_review import plan_review_state
from tools.breath.startup import surface_startup
from web import plans as plans_web


def _plan(
    bucket_id: str,
    *,
    created: str,
    status: str = "active",
    weight: float = 0.5,
    **metadata,
) -> dict:
    return {
        "id": bucket_id,
        "content": f"计划正文 {bucket_id}",
        "metadata": {
            "id": bucket_id,
            "name": bucket_id,
            "type": "plan",
            "status": status,
            "created": created,
            "weight": weight,
            **metadata,
        },
    }


def test_review_state_uses_legacy_created_time_and_exact_boundary():
    bucket = _plan("legacy", created="2026-07-19T12:00:00")

    before = plan_review_state(
        bucket,
        reference_time=datetime.fromisoformat("2026-08-18T11:59:59"),
        stale_after_days=30,
    )
    due = plan_review_state(
        bucket,
        reference_time=datetime.fromisoformat("2026-08-18T12:00:00"),
        stale_after_days=30,
    )

    assert before["is_stale"] is False
    assert before["days_since_confirmation"] == 29
    assert due["is_stale"] is True
    assert due["days_since_confirmation"] == 30
    assert bucket["metadata"]["status"] == "active"


def test_review_state_prefers_latest_confirmation_and_never_stales_closed_plan():
    history = [
        {"ts": "2026-06-01T00:00:00", "action": "created", "to": "active"},
        {"ts": "2026-08-20T08:00:00", "action": "confirmed"},
    ]
    active = _plan(
        "active",
        created="2026-06-01T00:00:00",
        change_log=history,
    )
    resolved = _plan(
        "resolved",
        created="2026-06-01T00:00:00",
        status="resolved",
    )
    reference = datetime.fromisoformat("2026-08-22T08:00:00")

    active_state = plan_review_state(
        active,
        reference_time=reference,
        stale_after_days=30,
    )
    resolved_state = plan_review_state(
        resolved,
        reference_time=reference,
        stale_after_days=30,
    )

    assert active_state["last_confirmed_at"] == "2026-08-20T08:00:00"
    assert active_state["days_since_confirmation"] == 2
    assert active_state["is_stale"] is False
    assert resolved_state["is_stale"] is False


@pytest.mark.asyncio
async def test_new_plan_records_initial_confirmation_without_changing_lifecycle(
    monkeypatch,
):
    class _Decay:
        async def ensure_started(self):
            return None

    class _Manager:
        def __init__(self):
            self.updated = None

        async def list_all(self, include_archive=False):
            assert include_archive is False
            return []

        async def create(self, **_kwargs):
            return "new-plan"

        async def update(self, bucket_id, **updates):
            assert bucket_id == "new-plan"
            self.updated = updates
            return True

    manager = _Manager()
    monkeypatch.setattr(plan_core.rt, "decay_engine", _Decay(), raising=False)
    monkeypatch.setattr(plan_core.rt, "bucket_mgr", manager, raising=False)
    monkeypatch.setattr(
        plan_core,
        "confirmation_timestamp",
        lambda: "2026-08-22T10:00:00",
    )

    result = await plan_core.plan_create("以后要确认的计划")

    assert result == "📋plan→new-plan [active]"
    assert manager.updated["status"] == "active"
    assert manager.updated["last_confirmed_at"] == "2026-08-22T10:00:00"
    assert manager.updated["change_log"][-1]["action"] == "created"


class _StaticBuckets:
    def __init__(self, buckets):
        self.buckets = buckets

    async def list_all(self, include_archive=False):
        assert include_archive is False
        return self.buckets


@pytest.mark.asyncio
async def test_startup_prioritizes_stale_plan_and_prompts_without_mutating(monkeypatch):
    stale = _plan(
        "stale-low-weight",
        created="2026-07-01T00:00:00",
        weight=0.1,
    )
    fresh = [
        _plan(
            f"fresh-{index}",
            created="2026-08-22T07:00:00",
            weight=1.0 - index / 10,
            last_confirmed_at="2026-08-22T07:00:00",
        )
        for index in range(5)
    ]
    monkeypatch.setattr(
        rt,
        "config",
        {"surfacing": {"plan_stale_after_days": 30}},
        raising=False,
    )

    output = await surface_startup(
        [*fresh, stale],
        max_results=1,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=datetime.fromisoformat("2026-08-22T12:00:00"),
    )

    assert "[bucket_id:stale-low-weight]" in output
    assert "[待确认:已52天]" in output
    assert "仍然有效吗" in output
    assert "系统不会自动改状态" in output
    assert stale["metadata"]["status"] == "active"


class _FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class _Request:
    headers = {}
    query_params = {}

    def __init__(self, body=None, *, bucket_id=""):
        self._body = body or {}
        self.path_params = {"bucket_id": bucket_id} if bucket_id else {}

    async def json(self):
        return self._body


class _MutableBuckets:
    def __init__(self, bucket):
        self.bucket = bucket
        self.updates = []

    async def list_all(self, include_archive=False):
        assert include_archive is False
        return [self.bucket]

    async def get(self, bucket_id):
        return self.bucket if bucket_id == self.bucket["id"] else None

    async def update(self, bucket_id, **updates):
        assert bucket_id == self.bucket["id"]
        self.updates.append(updates)
        for key, value in updates.items():
            if key == "content":
                self.bucket["content"] = value
            else:
                self.bucket["metadata"][key] = value
        return True


@pytest.mark.asyncio
async def test_dashboard_lists_stale_state_and_confirm_refreshes_timestamp(monkeypatch):
    manager = _MutableBuckets(
        _plan("old-plan", created="2020-01-01T00:00:00", weight=0.8)
    )
    monkeypatch.setattr(plans_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(plans_web.sh, "bucket_mgr", manager, raising=False)
    monkeypatch.setattr(
        plans_web.sh,
        "config",
        {"surfacing": {"plan_stale_after_days": 30}},
        raising=False,
    )
    monkeypatch.setattr(
        plans_web,
        "confirmation_timestamp",
        lambda: "2026-08-22T12:34:56",
    )
    mcp = _FakeMCP()
    plans_web.register(mcp)

    list_response = await mcp.routes[("GET", "/api/plans")](_Request())
    listed = json.loads(list_response.body.decode("utf-8"))

    assert listed["stale_active"] == 1
    assert listed["active"][0]["is_stale"] is True
    assert listed["active"][0]["days_since_confirmation"] > 30

    action_response = await mcp.routes[
        ("POST", "/api/plans/{bucket_id}/action")
    ](_Request({"action": "confirm"}, bucket_id="old-plan"))
    action = json.loads(action_response.body.decode("utf-8"))

    assert action["ok"] is True
    assert manager.updates[-1]["last_confirmed_at"] == "2026-08-22T12:34:56"
    assert manager.updates[-1]["change_log"][-1]["action"] == "confirmed"
    assert "status" not in manager.updates[-1]
    assert manager.bucket["metadata"]["status"] == "active"

    manager.bucket["metadata"]["status"] = "resolved"
    rejected_response = await mcp.routes[
        ("POST", "/api/plans/{bucket_id}/action")
    ](_Request({"action": "confirm"}, bucket_id="old-plan"))
    rejected = json.loads(rejected_response.body.decode("utf-8"))

    assert rejected_response.status_code == 400
    assert rejected["error"] == "only active plans can be confirmed"


@pytest.mark.asyncio
async def test_dashboard_confirm_persists_through_real_bucket_manager(
    monkeypatch,
    bucket_mgr,
):
    bucket_id = await bucket_mgr.create(
        content="需要继续确认的真实计划",
        bucket_type="plan",
        weight=0.8,
    )
    await bucket_mgr.update(
        bucket_id,
        status="active",
        last_confirmed_at="2026-07-01T00:00:00",
    )
    monkeypatch.setattr(plans_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(plans_web.sh, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(plans_web.sh, "config", {}, raising=False)
    monkeypatch.setattr(
        plans_web,
        "confirmation_timestamp",
        lambda: "2026-08-22T16:00:00",
    )
    mcp = _FakeMCP()
    plans_web.register(mcp)

    response = await mcp.routes[("POST", "/api/plans/{bucket_id}/action")](
        _Request({"action": "confirm"}, bucket_id=bucket_id)
    )
    stored = await bucket_mgr.get(bucket_id)

    assert response.status_code == 200
    assert stored is not None
    assert stored["metadata"]["status"] == "active"
    assert stored["metadata"]["last_confirmed_at"] == "2026-08-22T16:00:00"
    assert stored["metadata"]["change_log"][-1]["action"] == "confirmed"


def test_dashboard_contains_stale_badge_and_continue_action():
    html = Path("frontend/dashboard.html").read_text(encoding="utf-8")

    assert "待确认" in html
    assert "已确认继续" in html
    assert "planAction(this.dataset.planId,\\'confirm\\')" in html
