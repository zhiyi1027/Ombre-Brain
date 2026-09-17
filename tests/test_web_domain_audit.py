import json
from pathlib import Path

import frontmatter
import pytest
from starlette.responses import JSONResponse

import web.domain_audit as domain_audit_web


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "frontend" / "dashboard.html"


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class FakeRequest:
    headers = {}
    cookies = {}
    query_params = {}
    path_params = {}


def _payload(response):
    return json.loads(response.body.decode("utf-8"))


def _write_bucket(root: Path) -> Path:
    path = root / "dynamic" / "技术" / "memory.md"
    path.parent.mkdir(parents=True)
    post = frontmatter.Post(
        "PRIVATE BODY MUST NOT ENTER THE AUDIT RESPONSE",
        id="audit-memory",
        name="需要归一的技术记忆",
        type="dynamic",
        domain=["技术"],
        tags=[],
    )
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_domain_audit_route_is_authenticated_and_never_starts_scan(
    monkeypatch,
):
    called = False

    def fail_if_called(_config):
        nonlocal called
        called = True
        raise AssertionError("audit must not run before Dashboard auth")

    monkeypatch.setattr(domain_audit_web, "audit_vault", fail_if_called)
    monkeypatch.setattr(
        domain_audit_web.sh,
        "_require_auth",
        lambda _request: JSONResponse({"error": "Unauthorized"}, status_code=401),
    )
    mcp = FakeMCP()
    domain_audit_web.register(mcp)

    response = await mcp.routes[("GET", "/api/domain-audit")](FakeRequest())

    assert response.status_code == 401
    assert called is False
    assert ("POST", "/api/domain-audit") not in mcp.routes


@pytest.mark.asyncio
async def test_domain_audit_route_returns_content_free_read_only_preview(
    monkeypatch, tmp_path
):
    root = tmp_path / "buckets"
    bucket_path = _write_bucket(root)
    before = bucket_path.read_bytes()
    monkeypatch.setattr(domain_audit_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        domain_audit_web.sh,
        "config",
        {"buckets_dir": str(root)},
    )
    mcp = FakeMCP()
    domain_audit_web.register(mcp)

    response = await mcp.routes[("GET", "/api/domain-audit")](FakeRequest())
    payload = _payload(response)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert payload["mode"] == "read_only_preview"
    assert payload["summary"]["candidate_buckets"] == 1
    assert payload["candidates"][0]["bucket_id"] == "audit-memory"
    assert payload["candidates"][0]["proposal"]["domains"] == ["编程"]
    assert "root" not in payload
    assert "source_path" not in payload["candidates"][0]
    assert "content_sha256" not in payload["candidates"][0]
    assert "PRIVATE BODY" not in response.body.decode("utf-8")
    assert bucket_path.read_bytes() == before


@pytest.mark.asyncio
async def test_domain_audit_route_hides_internal_exception_details(monkeypatch):
    monkeypatch.setattr(domain_audit_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(domain_audit_web.sh, "config", {"buckets_dir": "/vault"})

    def fail(_config):
        raise RuntimeError("PRIVATE MEMORY BODY")

    monkeypatch.setattr(domain_audit_web, "audit_vault", fail)
    mcp = FakeMCP()
    domain_audit_web.register(mcp)

    response = await mcp.routes[("GET", "/api/domain-audit")](FakeRequest())

    assert response.status_code == 500
    assert _payload(response) == {"error": "domain audit failed"}
    assert "PRIVATE MEMORY BODY" not in response.body.decode("utf-8")


def test_dashboard_domain_audit_is_explicitly_read_only_and_on_demand():
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index("function openDomainAudit()")
    end = html.index("// ========================================", start)
    source = html[start:end]

    assert 'id="domain-audit-btn"' in html
    assert "只读预览，不会修改记忆、标签、索引或文件" in html
    assert "authFetch('/api/domain-audit')" in source
    assert "openDomainAudit()" in source
    assert "runDomainAudit();" in source
    assert "method: 'POST'" not in source
    assert "apply" not in source.lower()
    assert "DOMAIN_AUDIT_PAGE_SIZE = 40" in html
