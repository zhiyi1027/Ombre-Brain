"""
========================================
tools/dream/candidates.py — 候选桶筛选 + 软上限
========================================

dream 的第一步：从全量桶里筛出「滚动 48 小时内新创建的表层动态桶」，超过
40 个时按 calculate_score 截断，避免一次涌进来太多炸上下文。

关键行为：
- 排除 permanent / feel / plan / letter / pinned / protected
- 只看 created_at（兼容现有 created 字段），不看 last_active
- 窗口固定为调用时刻往前 48 小时；window_hours 仅保留兼容性
- 默认按创建时间倒序
- 软上限 40，超了就改按 decay_engine 权重排序后截断

不做什么（边界）：
- 不做 dehydrate；返回原桶 dict 由 output.py 渲染
- 不调 LLM

对外暴露：collect_candidates(all_buckets, window_hours, reference_time) → list[dict]
========================================
"""

from datetime import datetime, timedelta

from .. import _runtime as rt
from utils import parse_iso_datetime

DREAM_MAX_CANDIDATES = 40
DREAM_WINDOW_HOURS = 48


def _created_value(meta: dict) -> str:
    return meta.get("created_at") or meta.get("created", "")


def _created_datetime(meta: dict) -> datetime | None:
    """Read created_at first, falling back to legacy created metadata."""
    for key in ("created_at", "created"):
        value = meta.get(key)
        if not value:
            continue
        try:
            return parse_iso_datetime(value)
        except (ValueError, TypeError, OSError):
            continue
    return None


def _timestamp(value: str) -> float:
    try:
        return parse_iso_datetime(value).timestamp()
    except (ValueError, TypeError, OSError):
        return 0.0


def _created_timestamp(meta: dict) -> float:
    created = _created_datetime(meta)
    if created is None:
        return 0.0
    try:
        return created.timestamp()
    except (ValueError, OSError):
        return 0.0


def collect_core_context(all_buckets: list) -> list:
    core = [
        b for b in all_buckets
        if (
            b["metadata"].get("pinned", False)
            or b["metadata"].get("protected", False)
            or b["metadata"].get("type") == "permanent"
        )
        and b["metadata"].get("type") not in ("letter", "self", "i")
        and not b["metadata"].get("dont_surface", False)
        and not str(b["metadata"].get("superseded_by") or "").strip()
    ]
    core.sort(
        key=lambda b: (
            int(b["metadata"].get("importance") or 0),
            _timestamp(
                b["metadata"].get("last_active")
                or _created_value(b["metadata"])
            ),
            b.get("id", ""),
        ),
        reverse=True,
    )
    return core[:20]


def collect_candidates(
    all_buckets: list,
    window_hours: int | None = None,
    *,
    reference_time: datetime | None = None,
) -> list:
    # window_hours stays in the Python surface for old callers, but the
    # contract deliberately fixes the window at a rolling 48 hours.
    del window_hours
    candidates = [
        b for b in all_buckets
        if b["metadata"].get("type") not in ("permanent", "feel", "plan", "letter", "self", "i")
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
        and not b["metadata"].get("dont_surface", False)
        and not str(b["metadata"].get("superseded_by") or "").strip()
    ]
    reference = (
        parse_iso_datetime(reference_time)
        if reference_time is not None
        else datetime.now()
    )
    cutoff = reference - timedelta(hours=DREAM_WINDOW_HOURS)

    recent = []
    for bucket in candidates:
        created = _created_datetime(bucket["metadata"])
        if created is not None and cutoff <= created <= reference:
            recent.append(bucket)
    recent.sort(
        key=lambda b: _created_timestamp(b["metadata"]),
        reverse=True,
    )
    if len(recent) > DREAM_MAX_CANDIDATES:
        recent.sort(
            key=lambda b: rt.decay_engine.calculate_score(b["metadata"]),
            reverse=True,
        )
        recent = recent[:DREAM_MAX_CANDIDATES]
    return recent
