"""Explicit, reversible current-state relationships for ordinary memories."""

from __future__ import annotations

import asyncio
import re
import threading
import unicodedata
from contextlib import asynccontextmanager
from typing import Any

from utils import now_iso


MAX_STATE_KEY_CHARS = 120
MAX_BUCKET_POINTER_CHARS = 256
_ORDINARY_STATE_TYPES = frozenset({"dynamic", "permanent"})
_fallback_state_chain_lock = threading.Lock()


class StateChainError(ValueError):
    """A requested state relationship would be ambiguous or unsafe."""


@asynccontextmanager
async def _state_chain_turn(bucket_mgr):
    """Serialize the two-bucket transaction across loops and processes."""

    manager_turn = getattr(bucket_mgr, "state_chain_turn", None)
    if callable(manager_turn):
        async with manager_turn():
            yield
        return
    while not _fallback_state_chain_lock.acquire(blocking=False):
        await asyncio.sleep(0.01)
    try:
        yield
    finally:
        _fallback_state_chain_lock.release()


def normalize_state_key(value: Any) -> str:
    """Return a stable free-form key without imposing a category taxonomy."""

    text = str(value or "").strip().casefold()
    text = re.sub(r"\s+", " ", text)
    if not text:
        raise StateChainError("state_key 不能为空")
    if len(text) > MAX_STATE_KEY_CHARS:
        raise StateChainError(
            f"state_key 不能超过 {MAX_STATE_KEY_CHARS} 个字符"
        )
    if any(unicodedata.category(char).startswith("C") for char in text):
        raise StateChainError("state_key 不能包含控制字符或方向控制符")
    if text == r"\clear":
        raise StateChainError(r"state_key 不能使用保留值 \clear")
    return text


def is_superseded(metadata: dict | None) -> bool:
    return bool(str((metadata or {}).get("superseded_by") or "").strip())


def _bucket_type(bucket: dict) -> str:
    return str((bucket.get("metadata") or {}).get("type") or "dynamic").strip().lower()


def _require_ordinary(bucket: dict, bucket_id: str) -> None:
    bucket_type = _bucket_type(bucket)
    if bucket_type not in _ORDINARY_STATE_TYPES:
        raise StateChainError(
            f"{bucket_id} 是 {bucket_type} 桶；状态取代只适用于普通记忆，不处理 plan/feel/letter"
        )


def _stored_key(metadata: dict) -> str:
    raw = str(metadata.get("state_key") or "").strip()
    return normalize_state_key(raw) if raw else ""


async def current_state_candidates(
    bucket_mgr,
    state_key: str,
    *,
    exclude_id: str = "",
) -> list[dict]:
    """List other non-superseded ordinary buckets with the exact same key."""

    key = normalize_state_key(state_key)
    buckets = await bucket_mgr.list_all(include_archive=False)
    candidates: list[dict] = []
    for bucket in buckets:
        bucket_id = str(bucket.get("id") or "").strip()
        metadata = bucket.get("metadata") or {}
        if not bucket_id or bucket_id == exclude_id or is_superseded(metadata):
            continue
        if _bucket_type(bucket) not in _ORDINARY_STATE_TYPES:
            continue
        try:
            candidate_key = _stored_key(metadata)
        except StateChainError:
            continue
        if candidate_key == key:
            candidates.append(bucket)
    candidates.sort(
        key=lambda bucket: (
            str((bucket.get("metadata") or {}).get("created") or ""),
            str(bucket.get("id") or ""),
        ),
        reverse=True,
    )
    return candidates


async def set_supersession(
    bucket_mgr,
    *,
    old_bucket_id: str,
    new_bucket_id: str,
    state_key: str = "",
) -> dict[str, Any]:
    """Explicitly mark one ordinary bucket as replaced by another."""

    old_id = str(old_bucket_id or "").strip()
    new_id = str(new_bucket_id or "").strip()
    if not old_id or not new_id:
        raise StateChainError("旧桶和新桶的 bucket_id 都不能为空")
    if len(new_id) > MAX_BUCKET_POINTER_CHARS:
        raise StateChainError(
            f"新桶 bucket_id 不能超过 {MAX_BUCKET_POINTER_CHARS} 个字符"
        )
    if old_id == new_id:
        raise StateChainError("记忆不能取代自己")

    async with _state_chain_turn(bucket_mgr):
        old_bucket = await bucket_mgr.get(old_id)
        new_bucket = await bucket_mgr.get(new_id)
        if not old_bucket:
            raise StateChainError(f"未找到旧桶: {old_id}")
        if not new_bucket:
            raise StateChainError(f"未找到新桶: {new_id}")
        _require_ordinary(old_bucket, old_id)
        _require_ordinary(new_bucket, new_id)

        old_meta = old_bucket.get("metadata") or {}
        new_meta = new_bucket.get("metadata") or {}
        existing_target = str(old_meta.get("superseded_by") or "").strip()
        if existing_target:
            if existing_target == new_id:
                existing_key = _stored_key(old_meta)
                provided_key = (
                    normalize_state_key(state_key)
                    if str(state_key or "").strip()
                    else ""
                )
                if provided_key and provided_key != existing_key:
                    raise StateChainError(
                        "已存在关系的 state_key 与本次提供值不一致，本次未修改"
                    )
                return {
                    "ok": True,
                    "status": "unchanged",
                    "old_bucket_id": old_id,
                    "new_bucket_id": new_id,
                    "state_key": existing_key,
                }
            raise StateChainError(
                f"旧桶已经由 {existing_target} 取代；请先撤销原关系再重新标记"
            )
        if is_superseded(new_meta):
            raise StateChainError("不能把另一条历史版本设为当前版本")

        provided_key = normalize_state_key(state_key) if str(state_key or "").strip() else ""
        old_key = _stored_key(old_meta)
        new_key = _stored_key(new_meta)
        known_keys = {key for key in (provided_key, old_key, new_key) if key}
        if not known_keys:
            raise StateChainError("请提供 state_key，或先给其中一个桶设置 state_key")
        if len(known_keys) != 1:
            raise StateChainError("两个桶的 state_key 不一致，本次未修改")
        key = next(iter(known_keys))

        # Follow the target chain before writing.  A historical target is
        # rejected above, but this also protects malformed/manual chains.
        seen = {old_id}
        cursor_id = new_id
        for _ in range(100):
            if cursor_id in seen:
                raise StateChainError("状态链会形成循环，本次未修改")
            seen.add(cursor_id)
            cursor = await bucket_mgr.get(cursor_id)
            if not cursor:
                raise StateChainError(f"状态链指向不存在的桶: {cursor_id}")
            next_id = str(
                (cursor.get("metadata") or {}).get("superseded_by") or ""
            ).strip()
            if not next_id:
                break
            cursor_id = next_id
        else:
            raise StateChainError("状态链过长，本次未修改")

        target_key_was_missing = not new_key
        if target_key_was_missing:
            if not await bucket_mgr.update(new_id, state_key=key):
                raise StateChainError("无法给新桶写入 state_key，本次未修改")
        stored = await bucket_mgr.update(
            old_id,
            state_key=key,
            superseded_by=new_id,
            superseded_at=now_iso(),
        )
        if not stored:
            if target_key_was_missing:
                await bucket_mgr.update(new_id, state_key=None)
            raise StateChainError("无法保存取代关系，本次未修改")
        return {
            "ok": True,
            "status": "superseded",
            "old_bucket_id": old_id,
            "new_bucket_id": new_id,
            "state_key": key,
        }


async def clear_supersession(bucket_mgr, *, old_bucket_id: str) -> dict[str, Any]:
    """Undo a supersession link while retaining its useful state key."""

    old_id = str(old_bucket_id or "").strip()
    if not old_id:
        raise StateChainError("bucket_id 不能为空")
    async with _state_chain_turn(bucket_mgr):
        bucket = await bucket_mgr.get(old_id)
        if not bucket:
            raise StateChainError(f"未找到记忆桶: {old_id}")
        _require_ordinary(bucket, old_id)
        metadata = bucket.get("metadata") or {}
        target = str(metadata.get("superseded_by") or "").strip()
        if not target:
            return {
                "ok": True,
                "status": "unchanged",
                "old_bucket_id": old_id,
                "state_key": _stored_key(metadata),
            }
        if not await bucket_mgr.update(
            old_id,
            superseded_by=None,
            superseded_at=None,
        ):
            raise StateChainError("无法撤销取代关系")
        return {
            "ok": True,
            "status": "current",
            "old_bucket_id": old_id,
            "previous_new_bucket_id": target,
            "state_key": _stored_key(metadata),
        }
