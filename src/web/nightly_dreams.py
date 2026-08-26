"""Authenticated read-only Dashboard routes for nightly dream artifacts."""

from __future__ import annotations

from starlette.responses import JSONResponse

from nightly_dream import NightlyDreamError
from . import _shared as sh


def _unavailable() -> JSONResponse:
    return JSONResponse(
        {"error": "Nightly dreams are unavailable"},
        status_code=503,
        headers={"Cache-Control": "no-store"},
    )


def register(mcp) -> None:
    @mcp.custom_route("/api/nightly-dreams", methods=["GET"])
    async def list_nightly_dreams(request):
        error = sh._require_auth(request)
        if error:
            return error
        service = getattr(sh, "nightly_dreams", None)
        if service is None:
            return _unavailable()
        try:
            raw_limit = request.query_params.get("limit", "31")
            limit = max(1, min(90, int(raw_limit)))
            days = service.list_days(limit=limit)
        except (TypeError, ValueError, NightlyDreamError) as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"days": days, "total": len(days)},
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route("/api/nightly-dreams/{dream_day}", methods=["GET"])
    async def get_nightly_dream(request):
        error = sh._require_auth(request)
        if error:
            return error
        service = getattr(sh, "nightly_dreams", None)
        if service is None:
            return _unavailable()
        try:
            result = service.get_day(request.path_params.get("dream_day", ""))
        except NightlyDreamError as exc:
            status = 404 if "not found" in str(exc).lower() else 400
            return JSONResponse(
                {"error": str(exc)},
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
