from __future__ import annotations

from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from starlette.responses import JSONResponse


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from private_continuity import (  # noqa: E402
    PrivateContinuityConflictError,
    PrivateContinuityError,
    PrivateContinuityService,
)
from tools.breath.startup import surface_startup  # noqa: E402
from tools.breath.trace import record_surface_output  # noqa: E402
from web import _shared as web_shared  # noqa: E402
from web import private_continuity as private_web  # noqa: E402


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
    cookies = {}

    def __init__(self, body=None, headers=None, *, query_params=None):
        self._body = body
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.path_params = {}

    async def json(self):
        return self._body


class NoopDecay:
    def calculate_score(self, metadata):
        return float(metadata.get("importance") or 0)


def make_service(tmp_path, **private_config):
    return PrivateContinuityService(
        {
            "buckets_dir": str(tmp_path),
            "private_continuity": {
                "enabled": True,
                **private_config,
            },
        }
    )


@pytest.fixture(autouse=True)
def startup_runtime(monkeypatch):
    import tools._runtime as rt

    monkeypatch.setattr(rt, "decay_engine", NoopDecay())
    monkeypatch.setattr(rt, "logger", MagicMock())


def test_private_conflict_lifecycle_is_revisioned_and_recoverable(tmp_path):
    service = make_service(tmp_path)
    first = "## 发生了什么\n第一次没有说开。"
    second = "## 发生了什么\n第二版写清楚了，但仍未解决。"

    created = service.upsert(
        content=first,
        source_client="CC",
        expected_revision=0,
    )
    unchanged = service.upsert(
        content=first,
        source_client="cc",
        expected_revision=1,
    )
    updated = service.upsert(
        content=second,
        source_client="codex",
        expected_revision=1,
    )

    assert created["status"] == "created" and created["revision"] == 1
    assert unchanged["status"] == "unchanged" and unchanged["revision"] == 1
    assert updated["status"] == "updated" and updated["revision"] == 2
    assert "content" not in updated
    assert service.get_state()["content"] == second
    assert "第一次没有说开" in service.recovery_path.read_text(encoding="utf-8")
    assert os.stat(service.active_path).st_mode & 0o777 == 0o600

    with pytest.raises(PrivateContinuityConflictError, match="revision changed"):
        service.upsert(
            content="过期窗口试图覆盖",
            source_client="cc",
            expected_revision=1,
        )

    resolved = service.resolve(source_client="dashboard", expected_revision=2)
    assert resolved["status"] == "resolved"
    assert service.get_state()["open"] is False
    assert not service.active_path.exists()
    assert "第二版写清楚" in service.recovery_path.read_text(encoding="utf-8")

    restored = service.restore(source_client="dashboard")
    assert restored["status"] == "restored"
    assert restored["revision"] == 3
    assert service.get_state()["content"] == second


def test_private_conflict_rejects_oversized_or_invalid_content(tmp_path):
    service = make_service(
        tmp_path,
        max_content_chars=500,
        max_breath_tokens=256,
    )
    with pytest.raises(PrivateContinuityError, match="cannot be empty"):
        service.upsert(content=" ", source_client="cc")
    with pytest.raises(PrivateContinuityError, match="characters"):
        service.upsert(content="a" * 501, source_client="cc")
    with pytest.raises(PrivateContinuityError, match="source_client"):
        service.upsert(content="正文", source_client="bad client")


def test_manually_oversized_private_document_is_rejected_before_read(tmp_path):
    service = make_service(tmp_path)
    service.active_path.write_bytes(b"x" * (1024 * 1024 + 1))

    with pytest.raises(PrivateContinuityError, match="1 MiB"):
        service.get_state()


def test_two_stale_windows_cannot_both_overwrite_same_revision(tmp_path):
    service = make_service(tmp_path)
    service.upsert(content="初始正文", source_client="cc", expected_revision=0)

    def write(content):
        try:
            return service.upsert(
                content=content,
                source_client="cc",
                expected_revision=1,
            )["status"]
        except PrivateContinuityConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, ("窗口甲的正文", "窗口乙的正文")))

    assert sorted(results) == ["conflict", "updated"]
    assert service.get_state()["revision"] == 2


@pytest.mark.asyncio
async def test_startup_places_private_conflict_after_core_before_daily():
    buckets = [
        {
            "id": "core",
            "content": "核心正文",
            "metadata": {
                "id": "core",
                "name": "核心",
                "created": "2026-08-01T00:00:00",
                "last_active": "2026-08-01T00:00:00",
                "importance": 10,
                "type": "permanent",
                "pinned": True,
                "domain": ["关系"],
                "tags": [],
            },
        }
    ]
    output = await surface_startup(
        buckets,
        max_results=4,
        soft_tokens=3000,
        hard_tokens=5000,
        reference_time=datetime.fromisoformat("2026-08-22T12:00:00"),
        private_continuity="⚠ [私有连续状态] [revision:2]\n尚未说开的正文",
        daily_impression="=== 昨日印象 · 2026-08-21 ===\n昨日正文",
        reflection_tokens=0,
        feel_tokens=0,
    )

    assert output.index("=== 核心准则 ===") < output.index("=== 尚未解决的冲突 ===")
    assert output.index("=== 尚未解决的冲突 ===") < output.index("=== 昨日印象")
    assert "尚未说开的正文" in output
    assert "私有连续状态另有 2000 token" in output


def test_trace_records_private_state_without_treating_it_as_bucket():
    output = """=== 一键睁眼 ===
说明

=== 核心准则 ===
📌 [核心准则] [bucket_id:core]
核心正文

=== 尚未解决的冲突 ===
⚠ [私有连续状态] [revision:1]
冲突正文

=== 本次预算 ===
总硬上限 11000 token。
"""
    row = record_surface_output(
        output,
        kind="simulation",
        mode="startup",
        max_results=4,
        max_tokens=5000,
    )
    private = next(
        entry for entry in row["entries"]
        if entry["bucket_id"] == "private_continuity:conflict"
    )
    assert private["section"] == "private_continuity"
    assert private["reason"] == "unresolved_private_continuity"
    assert private["status"] == "returned"


@pytest.mark.asyncio
async def test_internal_route_uses_token_revision_and_never_echoes_body(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)
    monkeypatch.setattr(web_shared, "private_continuity", service, raising=False)
    monkeypatch.setattr(web_shared, "config", {"private_continuity": {}}, raising=False)
    monkeypatch.setattr(web_shared, "_is_authenticated", lambda _request: False)
    monkeypatch.setenv("OMBRE_PRIVATE_CONTINUITY_TOKEN", "secret")
    mcp = FakeMCP()
    private_web.register(mcp)

    get_handler = mcp.routes[("GET", "/internal/private-continuity/conflict")]
    put_handler = mcp.routes[("PUT", "/internal/private-continuity/conflict")]
    denied = await get_handler(JsonRequest())
    state = await get_handler(
        JsonRequest(headers={"authorization": "Bearer secret"})
    )
    accepted = await put_handler(
        JsonRequest(
            {
                "content": "不能在写入响应里回显的冲突正文",
                "source_client": "cc",
                "expected_revision": 0,
            },
            headers={"authorization": "Bearer secret"},
        )
    )

    assert denied.status_code == 401
    assert json.loads(state.body)["revision"] == 0
    assert accepted.status_code == 200
    assert json.loads(accepted.body)["revision"] == 1
    assert "不能在" not in accepted.body.decode("utf-8")


@pytest.mark.asyncio
async def test_dashboard_routes_edit_resolve_restore_and_require_auth(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)
    monkeypatch.setattr(web_shared, "private_continuity", service, raising=False)
    monkeypatch.setattr(web_shared, "_require_auth", lambda _request: None)
    mcp = FakeMCP()
    private_web.register(mcp)

    put_response = await mcp.routes[("PUT", "/api/private-continuity/conflict")](
        JsonRequest(
            {
                "content": "Dashboard 写下的未解决正文",
                "source_client": "dashboard",
                "expected_revision": 0,
            }
        )
    )
    detail = await mcp.routes[("GET", "/api/private-continuity/conflict")](
        JsonRequest()
    )
    resolved = await mcp.routes[("DELETE", "/api/private-continuity/conflict")](
        JsonRequest(
            {"source_client": "dashboard", "expected_revision": 1},
            query_params={"confirm": "true"},
        )
    )
    restored = await mcp.routes[
        ("POST", "/api/private-continuity/conflict/restore")
    ](
        JsonRequest(
            {"source_client": "dashboard"},
            query_params={"confirm": "true"},
        )
    )

    assert put_response.status_code == 200
    assert "Dashboard 写下" in json.loads(detail.body)["content"]
    assert json.loads(resolved.body)["status"] == "resolved"
    assert json.loads(restored.body)["status"] == "restored"

    monkeypatch.setattr(
        web_shared,
        "_require_auth",
        lambda _request: JSONResponse({"error": "Unauthorized"}, status_code=401),
    )
    denied = await mcp.routes[("PUT", "/api/private-continuity/conflict")](
        JsonRequest({"content": "不能覆盖"})
    )
    assert denied.status_code == 401
    assert service.get_state()["content"] == "Dashboard 写下的未解决正文"


def test_dashboard_contains_private_continuity_surface():
    dashboard = (ROOT / "frontend" / "dashboard.html").read_text(encoding="utf-8")
    assert 'data-tab="continuity"' in dashboard
    assert 'id="continuity-view"' in dashboard
    assert "/api/private-continuity/conflict" in dashboard
    assert "双方确认已解决" in dashboard
    assert "expected_revision" in dashboard
    assert "恢复上一版本" in dashboard


def _load_sync_module():
    path = ROOT / "scripts" / "sync-private-continuity.py"
    spec = importlib.util.spec_from_file_location("sync_private_continuity", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sync_requires_https_except_loopback_or_explicit_override():
    module = _load_sync_module()
    assert module._validate_url(
        "https://ob.example/internal/private-continuity/conflict",
        allow_insecure_http=False,
    ).startswith("https://")
    assert module._validate_url(
        "http://127.0.0.1:8282/internal/private-continuity/conflict",
        allow_insecure_http=False,
    ).startswith("http://127.0.0.1")
    with pytest.raises(module.SyncError, match="cleartext HTTP"):
        module._validate_url(
            "http://ob.example/internal/private-continuity/conflict",
            allow_insecure_http=False,
        )
    assert module._validate_url(
        "http://ob.example/internal/private-continuity/conflict",
        allow_insecure_http=True,
    ).startswith("http://")
    with pytest.raises(module.SyncError, match="must not contain credentials"):
        module._validate_url(
            "https://token@ob.example/internal/private-continuity/conflict",
            allow_insecure_http=False,
        )
