"""Daily impressions, raw clerk queries and revision-bound note takeover."""

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
    @mcp.custom_route("/api/daily-continuity/notes", methods=["POST"])
    async def submit_daily_continuity_note(request):
        error = sh._require_auth(request)
        if error:
            return error
        service = getattr(sh, "daily_continuity", None)
        if service is None:
            return JSONResponse({"error": "Daily continuity unavailable"}, status_code=503)
        if not service.notes_retired:
            try:
                body = await sh._read_json_object(request)
                return JSONResponse(service.ingest_manual_note(content=body.get("content", ""), source_client=body.get("source_client", "cc")))
            except (ValueError, TypeError):
                return JSONResponse({"error": "Invalid daily note"}, status_code=400)
        # After takeover do not read the body or call a writer.
        return JSONResponse(
            {"error": "Daily note upload has been retired", "code": "daily_note_upload_retired"},
            status_code=410,
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route("/api/daily-continuity", methods=["GET"])
    async def list_daily_continuity(request):
        error = sh._require_auth(request)
        if error:
            return error
        service = getattr(sh, "daily_continuity", None)
        if service is None:
            return JSONResponse(
                {"error": "Daily continuity is unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        try:
            raw_limit = request.query_params.get("limit", "31")
            limit = max(1, min(90, int(raw_limit)))
            days = service.list_days(limit=limit)
        except (TypeError, ValueError, DailyContinuityError) as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"days": days, "total": len(days)},
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route("/api/daily-continuity/{memory_day}", methods=["GET"])
    async def get_daily_continuity_day(request):
        error = sh._require_auth(request)
        if error:
            return error
        service = getattr(sh, "daily_continuity", None)
        if service is None:
            return JSONResponse(
                {"error": "Daily continuity is unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        try:
            result = service.get_day(request.path_params.get("memory_day", ""))
        except DailyContinuityError as exc:
            status = 404 if "not found" in str(exc) else 400
            return JSONResponse(
                {"error": str(exc)},
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @mcp.custom_route(
        "/api/daily-continuity/{memory_day}/impression",
        methods=["PATCH"],
    )
    async def edit_daily_impression(request):
        error = sh._require_auth(request)
        if error:
            return error
        service = getattr(sh, "daily_continuity", None)
        if service is None:
            return JSONResponse(
                {"error": "Daily continuity is unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        try:
            body = await sh._read_json_object(request)
            content = body.get("content")
            if not isinstance(content, str):
                raise DailyContinuityError("impression content must be a string")
            result = service.edit_impression(
                request.path_params.get("memory_day", ""),
                content,
            )
        except (ValueError, DailyContinuityError) as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"ok": True, "day": result},
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route(
        "/api/daily-continuity/{memory_day}/impression",
        methods=["DELETE"],
    )
    async def clear_daily_impression_override(request):
        error = sh._require_auth(request)
        if error:
            return error
        if request.query_params.get("confirm", "").lower() not in {"1", "true", "yes"}:
            return JSONResponse(
                {"error": "confirm=true required"},
                status_code=400,
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
            result = service.clear_impression_override(
                request.path_params.get("memory_day", "")
            )
        except DailyContinuityError as exc:
            status = 404 if "not found" in str(exc) else 400
            return JSONResponse(
                {"error": str(exc)},
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"ok": True, "day": result},
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route("/internal/daily-notes", methods=["POST"])
    async def ingest_daily_note(request):
        if not _authorized(request):
            return JSONResponse(
                {"error": "Unauthorized"}, status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        service = getattr(sh, "daily_continuity", None)
        if service is None:
            return JSONResponse({"error": "Daily continuity unavailable"}, status_code=503)
        if not service.notes_retired:
            try:
                return JSONResponse(service.ingest_note(await sh._read_json_object(request)))
            except (ValueError, TypeError):
                return JSONResponse({"error": "Invalid daily note"}, status_code=400)
        # A permanent response stops legacy uploaders only after takeover.
        return JSONResponse(
            {"error": "Daily note upload has been retired", "code": "daily_note_upload_retired"},
            status_code=410,
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route("/api/night-clerk/facts", methods=["GET"])
    async def clerk_facts(request):
        error = sh._require_auth(request)
        if error:
            return error
        clerk = getattr(getattr(sh, "daily_continuity", None), "clerk", None)
        if clerk is None:
            return JSONResponse({"error": "Night clerk disabled"}, status_code=503)
        try:
            params = request.query_params
            cards = clerk.search_facts(params.get("query", ""), memory_day=params.get("day"),
                                       limit=int(params.get("limit", 5)), candidates_only=params.get("candidates") == "true")
            return JSONResponse({"cards": cards}, headers={"Cache-Control": "no-store"})
        except (ValueError, OSError):
            return JSONResponse({"error": "Facts unavailable or source changed"}, status_code=409)

    @mcp.custom_route("/api/night-clerk/run", methods=["POST"])
    async def clerk_run(request):
        error = sh._require_auth(request)
        if error:
            return error
        clerk = getattr(getattr(sh, "daily_continuity", None), "clerk", None)
        if clerk is None:
            return JSONResponse({"error": "Night clerk disabled"}, status_code=503)
        try:
            body = await sh._read_json_object(request)
            return JSONResponse(await clerk.generate_day(body.get("memory_day", "")), headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid clerk day"}, status_code=400)

    @mcp.custom_route("/api/night-clerk/takeover", methods=["POST"])
    async def clerk_takeover(request):
        error = sh._require_auth(request)
        if error:
            return error
        clerk = getattr(getattr(sh, "daily_continuity", None), "clerk", None)
        if clerk is None:
            return JSONResponse({"error": "Night clerk disabled"}, status_code=503)
        try:
            body = await sh._read_json_object(request)
            clerk.confirm_takeover(body.get("memory_day", ""), confirmed_by=body.get("confirmed_by"), expected_revision=body.get("expected_revision"))
            return JSONResponse({"ok": True, "notes_retired": True}, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError):
            return JSONResponse({"error": "Current successful clerk output, actual breath read and explicit confirmation required"}, status_code=409)

    @mcp.custom_route("/api/night-clerk/backup", methods=["GET"])
    async def clerk_backup(request):
        error = sh._require_auth(request)
        if error:
            return error
        clerk = getattr(getattr(sh, "daily_continuity", None), "clerk", None)
        if clerk is None:
            return JSONResponse({"error": "Night clerk disabled"}, status_code=503)
        from clerk_backup import build_clerk_backup
        from starlette.responses import Response
        import asyncio
        data = await asyncio.to_thread(build_clerk_backup, clerk.archive, clerk.store)
        return Response(data, media_type="application/zip", headers={"Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="raw-clerk-backup.zip"'})
