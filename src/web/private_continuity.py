"""Authenticated reads and mutations for private continuity state."""

from __future__ import annotations

import hmac
import os

from starlette.responses import JSONResponse

from private_continuity import (
    PrivateContinuityConflictError,
    PrivateContinuityError,
)
from . import _shared as sh


_NO_STORE = {"Cache-Control": "no-store"}


def _header(request, name: str) -> str:
    try:
        return str(request.headers.get(name, "") or "").strip()
    except Exception:
        return ""


def _internal_authorized(request) -> bool:
    cfg = (getattr(sh, "config", {}) or {}).get("private_continuity") or {}
    secret = (
        os.environ.get("OMBRE_PRIVATE_CONTINUITY_TOKEN", "").strip()
        or str(cfg.get("token") or "").strip()
        or os.environ.get("OMBRE_DAILY_NOTE_TOKEN", "").strip()
        or os.environ.get("OMBRE_HOOK_TOKEN", "").strip()
    )
    if secret:
        auth = _header(request, "authorization")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        supplied = (_header(request, "x-ombre-private-token"), bearer)
        if any(
            value and hmac.compare_digest(value, secret)
            for value in supplied
        ):
            return True
    try:
        return bool(sh._is_authenticated(request))
    except Exception:
        return False


def _service():
    return getattr(sh, "private_continuity", None)


def _unavailable():
    return JSONResponse(
        {"error": "Private continuity is unavailable"},
        status_code=503,
        headers=_NO_STORE,
    )


def _error_response(exc: Exception):
    status = 409 if isinstance(exc, PrivateContinuityConflictError) else 400
    return JSONResponse(
        {"error": str(exc)},
        status_code=status,
        headers=_NO_STORE,
    )


async def _upsert(request, *, internal: bool):
    service = _service()
    if service is None:
        return _unavailable()
    try:
        body = await sh._read_json_object(request)
        result = service.upsert(
            content=body.get("content"),
            source_client=body.get("source_client", "cc" if internal else "dashboard"),
            expected_revision=body.get("expected_revision"),
        )
    except (ValueError, PrivateContinuityError) as exc:
        return _error_response(exc)
    except Exception:
        sh.logger.exception("private continuity upsert failed")
        return JSONResponse(
            {"error": "Private continuity could not be stored"},
            status_code=500,
            headers=_NO_STORE,
        )
    # Never echo the private body from a mutation response.  The authenticated
    # Dashboard can explicitly GET it after a successful save.
    return JSONResponse(result, headers=_NO_STORE)


async def _resolve(request, *, internal: bool):
    if request.query_params.get("confirm", "").lower() not in {"1", "true", "yes"}:
        return JSONResponse(
            {"error": "confirm=true required"},
            status_code=400,
            headers=_NO_STORE,
        )
    service = _service()
    if service is None:
        return _unavailable()
    try:
        body = await sh._read_json_object(request)
        result = service.resolve(
            source_client=body.get("source_client", "cc" if internal else "dashboard"),
            expected_revision=body.get("expected_revision"),
        )
    except (ValueError, PrivateContinuityError) as exc:
        return _error_response(exc)
    except Exception:
        sh.logger.exception("private continuity resolve failed")
        return JSONResponse(
            {"error": "Private continuity could not be resolved"},
            status_code=500,
            headers=_NO_STORE,
        )
    return JSONResponse(result, headers=_NO_STORE)


def register(mcp) -> None:
    @mcp.custom_route("/api/private-continuity/conflict", methods=["GET"])
    async def get_private_conflict(request):
        error = sh._require_auth(request)
        if error:
            return error
        service = _service()
        if service is None:
            return _unavailable()
        return JSONResponse(
            service.get_state(include_content=True),
            headers=_NO_STORE,
        )

    @mcp.custom_route("/api/private-continuity/conflict", methods=["PUT"])
    async def put_private_conflict(request):
        error = sh._require_auth(request)
        if error:
            return error
        return await _upsert(request, internal=False)

    @mcp.custom_route("/api/private-continuity/conflict", methods=["DELETE"])
    async def delete_private_conflict(request):
        error = sh._require_auth(request)
        if error:
            return error
        return await _resolve(request, internal=False)

    @mcp.custom_route(
        "/api/private-continuity/conflict/restore",
        methods=["POST"],
    )
    async def restore_private_conflict(request):
        error = sh._require_auth(request)
        if error:
            return error
        if request.query_params.get("confirm", "").lower() not in {"1", "true", "yes"}:
            return JSONResponse(
                {"error": "confirm=true required"},
                status_code=400,
                headers=_NO_STORE,
            )
        service = _service()
        if service is None:
            return _unavailable()
        try:
            body = await sh._read_json_object(request)
            result = service.restore(
                source_client=body.get("source_client", "dashboard")
            )
        except (ValueError, PrivateContinuityError) as exc:
            return _error_response(exc)
        except Exception:
            sh.logger.exception("private continuity restore failed")
            return JSONResponse(
                {"error": "Private continuity could not be restored"},
                status_code=500,
                headers=_NO_STORE,
            )
        return JSONResponse(result, headers=_NO_STORE)

    @mcp.custom_route(
        "/internal/private-continuity/conflict",
        methods=["GET"],
    )
    async def inspect_private_conflict(request):
        if not _internal_authorized(request):
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers=_NO_STORE,
            )
        service = _service()
        if service is None:
            return _unavailable()
        # Sync clients only need the revision guard.  Never expose the body on
        # the token-authenticated transport endpoint.
        return JSONResponse(
            service.get_state(include_content=False),
            headers=_NO_STORE,
        )

    @mcp.custom_route(
        "/internal/private-continuity/conflict",
        methods=["PUT"],
    )
    async def ingest_private_conflict(request):
        if not _internal_authorized(request):
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers=_NO_STORE,
            )
        return await _upsert(request, internal=True)

    @mcp.custom_route(
        "/internal/private-continuity/conflict",
        methods=["DELETE"],
    )
    async def resolve_private_conflict(request):
        if not _internal_authorized(request):
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers=_NO_STORE,
            )
        return await _resolve(request, internal=True)
