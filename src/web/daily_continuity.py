"""Authenticated HTTP ingestion for private daily handoff notes."""

from __future__ import annotations

import hmac
import os

from starlette.responses import JSONResponse

from daily_continuity import DailyContinuityError
from . import _shared as sh


def _header(request, name: str) -> str:
    try:
        return str(request.headers.get(name, "") or "").strip()
    except Exception:
        return ""


def _authorized(request) -> bool:
    cfg = (getattr(sh, "config", {}) or {}).get("daily_continuity") or {}
    secret = (
        os.environ.get("OMBRE_DAILY_NOTE_TOKEN", "").strip()
        or str(cfg.get("token") or "").strip()
        or os.environ.get("OMBRE_HOOK_TOKEN", "").strip()
    )
    if secret:
        auth = _header(request, "authorization")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        supplied = (_header(request, "x-ombre-daily-note-token"), bearer)
        if any(value and hmac.compare_digest(value, secret) for value in supplied):
            return True
    try:
        return bool(sh._is_authenticated(request))
    except Exception:
        return False


def register(mcp) -> None:
    @mcp.custom_route("/internal/daily-notes", methods=["POST"])
    async def ingest_daily_note(request):
        if not _authorized(request):
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        service = getattr(sh, "daily_continuity", None)
        if service is None:
            return JSONResponse(
                {"error": "Daily continuity is unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        try:
            body = await sh._read_json_object(request)
            result = service.ingest_note(body)
        except (ValueError, DailyContinuityError) as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        except Exception:
            sh.logger.exception("daily note ingestion failed")
            return JSONResponse(
                {"error": "Daily note could not be stored"},
                status_code=500,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            result,
            headers={"Cache-Control": "no-store"},
        )
