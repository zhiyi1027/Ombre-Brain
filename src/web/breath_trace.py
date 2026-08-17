"""Authenticated Dashboard routes for exact breath run visibility."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

from tools.breath import simulate_default_surface
from tools.breath.trace import get_run, list_runs
from . import _shared as sh


async def _with_bucket_names(record: dict | None) -> dict | None:
    if record is None:
        return None
    try:
        buckets = await sh.bucket_mgr.list_all(include_archive=False)
        names = {
            bucket.get("id", ""): (bucket.get("metadata") or {}).get("name")
            or bucket.get("id", "")
            for bucket in buckets
        }
    except Exception:
        names = {}
    for entry in record.get("entries") or []:
        bucket_id = entry.get("bucket_id", "")
        entry["name"] = names.get(bucket_id, bucket_id)
    return record


def _no_store_json(payload: object, *, status_code: int = 200):
    from starlette.responses import JSONResponse

    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def register(mcp) -> None:
    @mcp.custom_route("/api/breath-runs", methods=["GET"])
    async def api_breath_runs(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        run_id = str(request.query_params.get("run_id", "") or "").strip()
        if run_id:
            row = await _with_bucket_names(get_run(run_id))
            if row is None:
                return _no_store_json({"error": "breath run not found"}, status_code=404)
            return _no_store_json(row)

        try:
            limit = max(1, min(int(request.query_params.get("limit", "20")), 50))
        except (TypeError, ValueError, OverflowError):
            return _no_store_json({"error": "limit must be an integer in [1,50]"}, status_code=400)
        kind = str(request.query_params.get("kind", "") or "").strip()
        rows = list_runs(limit=limit, kind=kind)
        enriched = []
        for row in rows:
            enriched.append(await _with_bucket_names(row))
        return _no_store_json({"runs": enriched})

    @mcp.custom_route("/api/breath-simulate", methods=["POST"])
    async def api_breath_simulate(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        try:
            row = await _with_bucket_names(await simulate_default_surface())
            return _no_store_json(row or {})
        except Exception as exc:
            sh.logger.exception("Exact breath simulation failed")
            return _no_store_json({"error": str(exc)}, status_code=500)
