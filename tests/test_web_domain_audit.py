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

    def __init__(self, body=None):
        self._body = body

    async def json(self):
        return self._body


class FakeBucketManager:
    def __init__(self, bucket):
        self.bucket = bucket
        self.lookups = []

    async def get(self, bucket_id):
        self.lookups.append(bucket_id)
        return self.bucket


class FakeAnalyzer:
    api_available = True

    def __init__(self, result):
        self.result = result
        self.inputs = []

    async def analyze(self, content):
        self.inputs.append(content)
        return self.result


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


def test_supported_suggestion_domains_accepts_scalar_and_bounds_lists():
    assert domain_audit_web._supported_suggestion_domains("编程") == ["编程"]
    assert domain_audit_web._supported_suggestion_domains(
        ["编程", "编程", "伴侣", "亲密"]
    ) == ["编程", "伴侣"]
    assert domain_audit_web._supported_suggestion_domains("恋爱") == []


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


@pytest.mark.asyncio
async def test_domain_suggestion_route_authenticates_before_bucket_lookup(monkeypatch):
    bucket_mgr = FakeBucketManager(None)
    monkeypatch.setattr(domain_audit_web.sh, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(
        domain_audit_web.sh,
        "_require_auth",
        lambda _request: JSONResponse({"error": "Unauthorized"}, status_code=401),
    )
    mcp = FakeMCP()
    domain_audit_web.register(mcp)

    response = await mcp.routes[("POST", "/api/domain-audit/suggest")](
        FakeRequest({"bucket_id": "private-memory"})
    )

    assert response.status_code == 401
    assert bucket_mgr.lookups == []


@pytest.mark.asyncio
async def test_domain_suggestion_is_content_aware_and_never_applies(monkeypatch):
    private_body = "PRIVATE BODY SENT TO THE CONFIGURED MODEL ONLY"
    bucket = {
        "id": "legacy-romance",
        "metadata": {
            "id": "legacy-romance",
            "name": "一起修复分类",
            "type": "dynamic",
            "domain": ["恋爱"],
            "tags": ["沟通"],
        },
        "content": private_body,
        "path": "/vault/dynamic/恋爱/memory.md",
    }
    before = json.loads(json.dumps(bucket, ensure_ascii=False))
    bucket_mgr = FakeBucketManager(bucket)
    analyzer = FakeAnalyzer(
        {"domain": ["编程", "伴侣", "亲密"], "tags": ["分类"]}
    )
    monkeypatch.setattr(domain_audit_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(domain_audit_web.sh, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(domain_audit_web.sh, "dehydrator", analyzer)
    mcp = FakeMCP()
    domain_audit_web.register(mcp)

    response = await mcp.routes[("POST", "/api/domain-audit/suggest")](
        FakeRequest({"bucket_id": "legacy-romance"})
    )
    payload = _payload(response)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert payload == {
        "mode": "read_only_suggestion",
        "bucket_id": "legacy-romance",
        "current_domains": ["恋爱"],
        "suggested_domains": ["编程", "伴侣"],
        "applied": False,
    }
    assert analyzer.inputs == [f"标题：一起修复分类\n正文：{private_body}"]
    assert private_body not in response.body.decode("utf-8")
    assert bucket == before


@pytest.mark.asyncio
async def test_domain_suggestion_rejects_bucket_without_content_review(monkeypatch):
    bucket = {
        "id": "clean-memory",
        "metadata": {
            "id": "clean-memory",
            "name": "clean",
            "type": "dynamic",
            "domain": ["编程"],
            "tags": [],
        },
        "content": "clean body",
    }
    analyzer = FakeAnalyzer({"domain": ["编程"]})
    monkeypatch.setattr(domain_audit_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(domain_audit_web.sh, "bucket_mgr", FakeBucketManager(bucket))
    monkeypatch.setattr(domain_audit_web.sh, "dehydrator", analyzer)
    mcp = FakeMCP()
    domain_audit_web.register(mcp)

    response = await mcp.routes[("POST", "/api/domain-audit/suggest")](
        FakeRequest({"bucket_id": "clean-memory"})
    )

    assert response.status_code == 409
    assert _payload(response) == {"error": "bucket does not require content review"}
    assert analyzer.inputs == []


@pytest.mark.asyncio
async def test_domain_suggestion_hides_model_output_when_no_domain_is_supported(
    monkeypatch,
):
    private_body = "PRIVATE BODY MUST NOT BE RETURNED"
    bucket = {
        "id": "unknown-memory",
        "metadata": {
            "id": "unknown-memory",
            "name": "unknown",
            "type": "permanent",
            "domain": ["未分类"],
            "tags": [],
        },
        "content": private_body,
    }
    analyzer = FakeAnalyzer({"domain": ["PRIVATE MODEL OUTPUT", "恋爱"]})
    monkeypatch.setattr(domain_audit_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(domain_audit_web.sh, "bucket_mgr", FakeBucketManager(bucket))
    monkeypatch.setattr(domain_audit_web.sh, "dehydrator", analyzer)
    mcp = FakeMCP()
    domain_audit_web.register(mcp)

    response = await mcp.routes[("POST", "/api/domain-audit/suggest")](
        FakeRequest({"bucket_id": "unknown-memory"})
    )

    assert response.status_code == 502
    assert _payload(response) == {
        "error": "classification model returned no supported domains"
    }
    assert private_body not in response.body.decode("utf-8")
    assert "PRIVATE MODEL OUTPUT" not in response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_domain_suggestion_requires_configured_model(monkeypatch):
    bucket = {
        "id": "unknown-memory",
        "metadata": {
            "id": "unknown-memory",
            "type": "dynamic",
            "domain": ["未分类"],
            "tags": [],
        },
        "content": "body",
    }
    monkeypatch.setattr(domain_audit_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(domain_audit_web.sh, "bucket_mgr", FakeBucketManager(bucket))
    monkeypatch.setattr(domain_audit_web.sh, "dehydrator", None)
    mcp = FakeMCP()
    domain_audit_web.register(mcp)

    response = await mcp.routes[("POST", "/api/domain-audit/suggest")](
        FakeRequest({"bucket_id": "unknown-memory"})
    )

    assert response.status_code == 503
    assert _payload(response) == {"error": "classification model is not configured"}


def test_dashboard_domain_audit_is_explicitly_read_only_and_on_demand():
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index("function openDomainAudit()")
    end = html.index("function renderDomainAuditSummary()", start)
    source = html[start:end]

    assert 'id="domain-audit-btn"' in html
    assert "只读预览，不会修改记忆、标签、索引或文件" in html
    assert "authFetch('/api/domain-audit')" in source
    assert "openDomainAudit()" in source
    assert "runDomainAudit();" in source
    assert "method: 'POST'" not in source
    assert "apply" not in source.lower()
    assert "DOMAIN_AUDIT_PAGE_SIZE = 40" in html
    assert "fetch('/api/domain-audit/suggest'" in html
    assert "result.applied !== false" in html
    assert "标题和正文发送给当前配置的打标模型" in html
