"""Authenticated, read-only domain audit preview for the Dashboard."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import Response

from domain_audit import audit_vault

from . import _shared as sh


logger = sh.logger


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
