"""
/api/raw/import —— 聊天原文抽屉的导入口。

- 平时关着：只有部署时设了环境变量 OB_RAW_IMPORT_TOKEN 才存在（没设一律 404），
  导完就可以把它删掉。口令走 X-Raw-Import-Token 头，和仪表板登录、OB_TOKEN 都分开。
- 请求体是 gzip 压缩的 JSONL，每行一条原话（raw_archive_convert.py 的输出），
  解压后上限 64MB，行数上限 200k。按 msg_uuid 覆盖，重复导入不会翻倍。
- 不碰记忆桶，不触发 embedding / breath / github_sync。
"""

from __future__ import annotations

import asyncio
import gzip
import hmac
import io
import json
import os
import re

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from raw_archive import get_archive

from . import _shared as sh

MAX_BODY = 32 * 1024 * 1024
MAX_UNZIPPED = 64 * 1024 * 1024
MAX_ROWS = 200_000
LEGACY_PART = re.compile(r"^home-msg-\d+-part-\d+$")


def _token_ok(request: Request) -> bool | None:
    expected = os.environ.get("OB_RAW_IMPORT_TOKEN", "").strip()
    if not expected:
        return None
    supplied = request.headers.get("x-raw-import-token", "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _parse(body: bytes) -> list[dict]:
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
        raw = gz.read(MAX_UNZIPPED + 1)
    if len(raw) > MAX_UNZIPPED:
        raise ValueError("unzipped payload too large")
    rows = []
    for line in raw.decode("utf-8").split("\n"):
        if line.strip():
            rows.append(json.loads(line))
            if len(rows) > MAX_ROWS:
                raise ValueError("too many rows")
    return rows


def register(mcp) -> None:
    async def receive(request: Request, *, fragments: bool) -> Response:
        ok = _token_ok(request)
        if ok is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not ok:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                return JSONResponse({"error": "payload too large"}, status_code=413)
        try:
            rows = await asyncio.to_thread(_parse, body)
        except Exception as exc:
            return JSONResponse({"error": f"bad payload: {type(exc).__name__}"}, status_code=400)
        source = request.headers.get("x-raw-source", "")[:80]
        archive = get_archive(sh.config)
        if not fragments and any(
            "fragment_index" in row or LEGACY_PART.fullmatch(str(row.get("msg_uuid", "")))
            for row in rows if isinstance(row, dict)
        ):
            return JSONResponse({"error": "fragments require /api/raw/import-v2"}, status_code=409)
        try:
            importer = archive.import_rows_v2 if fragments else archive.import_rows
            result = await asyncio.to_thread(importer, rows, source)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, **result, "stats": await asyncio.to_thread(archive.stats)})

    @mcp.custom_route("/api/raw/import", methods=["POST"])
    async def api_raw_import(request: Request) -> Response:
        return await receive(request, fragments=False)

    @mcp.custom_route("/api/raw/import-v2", methods=["POST"])
    async def api_raw_import_v2(request: Request) -> Response:
        return await receive(request, fragments=True)

    @mcp.custom_route("/api/raw/delete-source", methods=["POST"])
    async def api_raw_delete_source(request: Request) -> Response:
        ok = _token_ok(request)
        if ok is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not ok:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            source = str((await request.json()).get("source", "")).strip()[:80]
        except Exception:
            source = ""
        if not source:
            return JSONResponse({"error": "source required"}, status_code=400)
        archive = get_archive(sh.config)
        deleted = await asyncio.to_thread(archive.delete_source, source)
        return JSONResponse({"ok": True, "deleted": deleted, "stats": await asyncio.to_thread(archive.stats)})

    @mcp.custom_route("/api/raw/stats", methods=["GET"])
    async def api_raw_stats(request: Request) -> Response:
        err = sh._require_auth(request)
        if err and not _token_ok(request):
            return err
        return JSONResponse(await asyncio.to_thread(get_archive(sh.config).stats))
