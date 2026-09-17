"""Authenticated, read-only domain audit preview for the Dashboard."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import Response

from domain_audit import (
    AUDITED_BUCKET_TYPES,
    CANONICAL_DOMAINS,
    audit_bucket,
    audit_vault,
)

from . import _shared as sh


logger = sh.logger


def _supported_suggestion_domains(value: object) -> list[str]:
    """Keep at most two specific domains from an untrusted model response."""

    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        return []
    domains: list[str] = []
    for item in candidates:
        domain = str(item or "").strip()
        if domain in CANONICAL_DOMAINS and domain not in domains:
            domains.append(domain)
        if len(domains) >= 2:
            break
    return domains


def _dashboard_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return the content-free report without exposing the vault's host path."""

    public_candidates = []
    candidates = report.get("candidates", [])
    if isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, Mapping):
                continue
            public_candidates.append(
                {
                    "bucket_id": item.get("bucket_id", ""),
                    "name": item.get("name", ""),
                    "bucket_type": item.get("bucket_type", ""),
                    "current": item.get("current", {}),
                    "proposal": item.get("proposal", {}),
                    "flags": item.get("flags", []),
                    "reasons": item.get("reasons", []),
                    "confidence": item.get("confidence", "review"),
                    "requires_content_review": bool(
                        item.get("requires_content_review", True)
                    ),
                }
            )

    return {
        "schema_version": report.get("schema_version"),
        "mode": report.get("mode"),
        "summary": report.get("summary", {}),
        "candidates": public_candidates,
        "errors": report.get("errors", []),
    }


def register(mcp) -> None:
    @mcp.custom_route("/api/domain-audit", methods=["GET"])
    async def api_domain_audit(request: Request) -> Response:
        """Scan active ordinary buckets and return a read-only preview."""

        from starlette.responses import JSONResponse

        err = sh._require_auth(request)
        if err:
            return err

        try:
            # Parsing a large vault is filesystem-bound.  Keep it off the event
            # loop so MCP traffic and Dashboard heartbeats stay responsive.
            report = await asyncio.to_thread(audit_vault, dict(sh.config))
            return JSONResponse(
                _dashboard_report(report),
                headers={"Cache-Control": "no-store"},
            )
        except Exception:
            logger.exception("Dashboard domain audit failed")
            return JSONResponse(
                {"error": "domain audit failed"},
                status_code=500,
                headers={"Cache-Control": "no-store"},
            )

    @mcp.custom_route("/api/domain-audit/suggest", methods=["POST"])
    async def api_domain_audit_suggest(request: Request) -> Response:
        """Generate one content-aware domain suggestion without saving it."""

        from starlette.responses import JSONResponse

        no_store = {"Cache-Control": "no-store"}
        err = sh._require_auth(request)
        if err:
            return err

        try:
            body = await sh._read_json_object(request)
        except ValueError:
            return JSONResponse(
                {"error": "invalid JSON body"}, status_code=400, headers=no_store
            )

        bucket_id = body.get("bucket_id")
        if not isinstance(bucket_id, str) or not bucket_id.strip():
            return JSONResponse(
                {"error": "bucket_id is required"}, status_code=400, headers=no_store
            )
        bucket_id = bucket_id.strip()

        try:
            bucket = await sh.bucket_mgr.get(bucket_id)
        except Exception:
            logger.exception("Domain suggestion bucket lookup failed")
            return JSONResponse(
                {"error": "domain suggestion failed"},
                status_code=500,
                headers=no_store,
            )
        if not bucket:
            return JSONResponse(
                {"error": "bucket not found"}, status_code=404, headers=no_store
            )

        metadata = bucket.get("metadata") or {}
        bucket_type = str(metadata.get("type") or "dynamic").strip().lower()
        if bucket_type not in AUDITED_BUCKET_TYPES:
            return JSONResponse(
                {"error": "bucket type is not eligible for domain audit"},
                status_code=409,
                headers=no_store,
            )
        content = str(bucket.get("content") or "")
        candidate = audit_bucket(metadata, content)
        if not candidate or not candidate.get("requires_content_review"):
            return JSONResponse(
                {"error": "bucket does not require content review"},
                status_code=409,
                headers=no_store,
            )

        analyzer = getattr(sh, "dehydrator", None)
        if not analyzer or not getattr(analyzer, "api_available", False):
            return JSONResponse(
                {"error": "classification model is not configured"},
                status_code=503,
                headers=no_store,
            )

        name = str(metadata.get("name") or "").strip()
        analysis_input = f"标题：{name}\n正文：{content}" if name else content
        try:
            analysis = await analyzer.analyze(analysis_input)
            suggested_domains = _supported_suggestion_domains(
                analysis.get("domain") if isinstance(analysis, Mapping) else None
            )
        except Exception:
            logger.exception("Dashboard domain suggestion failed")
            return JSONResponse(
                {"error": "classification model failed"},
                status_code=502,
                headers=no_store,
            )
        if not suggested_domains:
            return JSONResponse(
                {"error": "classification model returned no supported domains"},
                status_code=502,
                headers=no_store,
            )

        return JSONResponse(
            {
                "mode": "read_only_suggestion",
                "bucket_id": bucket_id,
                "current_domains": candidate["current"]["domains"],
                "suggested_domains": suggested_domains,
                "applied": False,
            },
            headers=no_store,
        )
