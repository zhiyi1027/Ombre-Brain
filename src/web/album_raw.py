"""
/api/album、/api/raw/* —— 仪表板的「相册」和「原文」两页。

- 全部要求仪表板登录（和其他管理页同一套 cookie 会话），只读。
- 相册：列出挂了图的记忆桶；原图按桶和序号现取，不进浏览器以外的任何地方。
- 原文：按北京时间日期翻页、逐字搜原话；原文抽屉的边界见 raw_archive.py。
"""

from __future__ import annotations

import asyncio
import re

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from media_store import MediaPersistenceError
from raw_archive import get_archive
from tools.media import album_items

from . import _shared as sh

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PAGE = 60
_MIME = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


def register(mcp) -> None:
    @mcp.custom_route("/api/album", methods=["GET"])
    async def api_album(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse({"items": await album_items()})

    @mcp.custom_route("/api/album/{bucket_id}/{index}", methods=["GET"])
    async def api_album_image(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = str(request.path_params.get("bucket_id") or "")
        try:
            index = int(request.path_params.get("index"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "bad index"}, status_code=400)
        bucket = await sh.bucket_mgr.get(bucket_id)
        media = ((bucket or {}).get("metadata") or {}).get("media") or []
        if not bucket or index < 0 or index >= len(media) or not isinstance(media[index], dict):
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            data, fmt = await asyncio.to_thread(
                sh.bucket_mgr.media_store.read_image, str(media[index].get("path") or "")
            )
        except MediaPersistenceError:
            return JSONResponse({"error": "not found"}, status_code=404)
        return Response(
            data,
            media_type=_MIME.get(str(fmt).lower(), "application/octet-stream"),
            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )

    @mcp.custom_route("/api/raw/days", methods=["GET"])
    async def api_raw_days(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse({"days": await asyncio.to_thread(get_archive(sh.config).days)})

    @mcp.custom_route("/api/raw/day", methods=["GET"])
    async def api_raw_day(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        day = request.query_params.get("date", "").strip()
        if not _DAY.match(day):
            return JSONResponse({"error": "date must be YYYY-MM-DD"}, status_code=400)
        try:
            page = max(0, int(request.query_params.get("page", "0")))
        except ValueError:
            page = 0
        archive = get_archive(sh.config)
        rows, total = await asyncio.to_thread(archive.day, day, page * _PAGE, _PAGE)
        stats = await asyncio.to_thread(archive.stats)
        return JSONResponse({"date": day, "page": page, "page_size": _PAGE, "total": total,
                             "rows": rows, "stats": stats})

    @mcp.custom_route("/api/raw/search", methods=["GET"])
    async def api_raw_search(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        query = request.query_params.get("q", "").strip()[:100]
        if len(query) < 2:
            return JSONResponse({"error": "query too short"}, status_code=400)
        thinking = request.query_params.get("thinking", "") in ("1", "true")
        speaker = request.query_params.get("speaker", "")
        hits = await asyncio.to_thread(get_archive(sh.config).search, query, 50, thinking, speaker)
        return JSONResponse({"q": query, "hits": hits})
