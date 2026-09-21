"""media_read 工具实现：只在明确指定桶和序号时返回图片本体。"""

from __future__ import annotations

import asyncio

from media_store import MediaPersistenceError

from .. import _runtime as rt


async def read(bucket_id: str, index: int = 0) -> tuple[bytes, str] | str:
    bucket_id = str(bucket_id or "").strip()
    if not bucket_id:
        return "bucket_id 不能为空。"
    if not isinstance(index, int) or isinstance(index, bool):
        return "index 必须是从 0 开始的整数。"
    if index < 0:
        return "index 必须是从 0 开始的整数。"

    bucket = await rt.bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶：{bucket_id}"
    media = (bucket.get("metadata") or {}).get("media") or []
    if not isinstance(media, list) or not media:
        return f"记忆桶 {bucket_id} 没有媒体附件。"
    if index >= len(media):
        return f"媒体序号越界：该桶共有 {len(media)} 项，index 从 0 开始。"
    entry = media[index]
    if not isinstance(entry, dict) or not entry.get("path"):
        return "该媒体附件缺少可读取的持久路径。"
    try:
        return await asyncio.to_thread(
            rt.bucket_mgr.media_store.read_image,
            str(entry["path"]),
        )
    except MediaPersistenceError as exc:
        return f"图片读取失败：{exc}"
