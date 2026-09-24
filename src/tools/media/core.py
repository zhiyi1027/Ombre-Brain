"""图片目录与按需读取工具。

``media_catalog`` 只返回轻量关键词和桶定位信息；``media_read`` 仍是唯一会
读取图片本体的入口。这样可以先找图，再把明确选中的那一张放进上下文。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable

from media_store import MediaPersistenceError
from utils import parse_iso_datetime

from .. import _runtime as rt

try:  # jieba 是正常安装依赖；保留降级路径给最小化测试/旧环境。
    from jieba.analyse import extract_tags as _extract_tags
except ImportError:  # pragma: no cover - 正常 Docker 镜像会安装 jieba
    _extract_tags = None


_KEYWORD_LIMIT = 10
_KEYWORD_MAX_CHARS = 24
_FALLBACK_PREVIEW_CHARS = 48
_GENERIC_KEYWORDS = {
    "一个", "一张", "今天", "照片", "图片", "截图", "知知", "自己", "这个",
    "那个", "然后", "时候", "里面", "看见", "发来", "media", "image",
}
_MARKDOWN_NOISE = re.compile(r"[`*_>#|\[\]()]")
_DATE_PREFIX = re.compile(r"^\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[T\s][0-9:]+)?\s*")
_SPACE = re.compile(r"\s+")


def _iter_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        if value.strip():
            yield value.strip()
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, str) and item.strip():
                yield item.strip()


def _clean_text(value: object) -> str:
    text = str(value or "")
    text = _DATE_PREFIX.sub("", text)
    text = _MARKDOWN_NOISE.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def _add_keyword(target: list[str], seen: set[str], value: object) -> None:
    keyword = _clean_text(value).strip("，。！？；：、,!?;:/\\- ")
    if not keyword or len(keyword) > _KEYWORD_MAX_CHARS:
        return
    folded = keyword.casefold()
    if (
        folded in seen
        or folded in _GENERIC_KEYWORDS
        or folded.isdigit()
        or "_media/" in folded
        or keyword.startswith(("/", "\\"))
        or "\\" in keyword
    ):
        return
    seen.add(folded)
    target.append(keyword)


def _keywords_for_bucket(bucket: dict) -> list[str]:
    """Build a compact, deterministic keyword row without an LLM call."""
    meta = bucket.get("metadata") or {}
    media = meta.get("media") or []
    keywords: list[str] = []
    seen: set[str] = set()

    # Explicit human-authored labels are the strongest signals.
    for value in _iter_strings(meta.get("tags")):
        _add_keyword(keywords, seen, value)
    for entry in media:
        if not isinstance(entry, dict):
            continue
        for field in ("title", "note"):
            for value in _iter_strings(entry.get(field)):
                _add_keyword(keywords, seen, value)
    name = meta.get("name")
    if name and str(name) != str(bucket.get("id") or ""):
        _add_keyword(keywords, seen, name)

    content = _clean_text(bucket.get("content"))
    if content and _extract_tags is not None:
        try:
            for value in _extract_tags(content, topK=_KEYWORD_LIMIT * 2):
                _add_keyword(keywords, seen, value)
                if len(keywords) >= _KEYWORD_LIMIT:
                    break
        except Exception:
            pass

    # Old/minimal runtimes may not have jieba. Keep the catalog useful with a
    # short description fragment instead of hiding the bucket entirely.
    if not keywords and content:
        preview = content[:_FALLBACK_PREVIEW_CHARS].rstrip()
        if len(content) > len(preview):
            preview += "…"
        keywords.append(preview)
    return keywords[:_KEYWORD_LIMIT]


def _search_text(bucket: dict) -> str:
    meta = bucket.get("metadata") or {}
    parts = [str(bucket.get("id") or ""), str(bucket.get("content") or "")]
    for field in ("name", "tags", "domain"):
        parts.extend(_iter_strings(meta.get(field)))
    for entry in meta.get("media") or []:
        if not isinstance(entry, dict):
            continue
        parts.extend(_iter_strings(entry.get("title")))
        parts.extend(_iter_strings(entry.get("note")))
    return "\n".join(parts).casefold()


def _created_sort_key(bucket: dict) -> tuple[float, str]:
    meta = bucket.get("metadata") or {}
    value = meta.get("created_at") or meta.get("created") or ""
    try:
        stamp = parse_iso_datetime(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        stamp = 0.0
    return stamp, str(bucket.get("id") or "")


async def catalog(query: str = "") -> str:
    """List every non-deleted image bucket, optionally filtered by keywords."""
    raw_query = _SPACE.sub(" ", str(query or "").strip())
    normalized_query = raw_query.casefold()
    terms = [term for term in normalized_query.split(" ") if term]
    all_buckets = await rt.bucket_mgr.list_all(include_archive=True)

    matches: list[tuple[dict, list[dict]]] = []
    for bucket in all_buckets:
        meta = bucket.get("metadata") or {}
        if meta.get("deleted_at"):
            continue
        media = [
            entry for entry in (meta.get("media") or [])
            if isinstance(entry, dict) and entry.get("path")
        ]
        if not media:
            continue
        haystack = _search_text(bucket)
        if terms and not all(term in haystack for term in terms):
            continue
        matches.append((bucket, media))

    matches.sort(key=lambda item: _created_sort_key(item[0]), reverse=True)
    image_count = sum(len(media) for _bucket, media in matches)
    if not matches:
        if normalized_query:
            return f'没有找到包含关键词“{raw_query}”的图片记忆桶。'
        return "还没有存过图片记忆桶。"

    scope = f'关键词“{raw_query}”' if normalized_query else "全部"
    lines = [
        f"=== 图片记忆目录 · {scope}（{len(matches)} 个桶 / {image_count} 张）===",
        "这里只列关键词与定位信息，不读取原图。确定目标后调用 "
        'media_read(bucket_id="...", index=0)。',
    ]
    for bucket, media in matches:
        meta = bucket.get("metadata") or {}
        created = str(meta.get("created_at") or meta.get("created") or "未知日期")
        keywords = " / ".join(_keywords_for_bucket(bucket)) or "（无可用文字描述）"
        last_index = len(media) - 1
        index_hint = "0" if last_index == 0 else f"0-{last_index}"
        lines.append(
            f"- [bucket_id:{bucket.get('id')}] [{created}] [{len(media)}张; index:{index_hint}] "
            f"关键词: {keywords}"
        )
    return "\n".join(lines)


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
