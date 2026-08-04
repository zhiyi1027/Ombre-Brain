"""
========================================
tools/dream/candidates.py — 候选桶筛选 + 软上限
========================================

dream 的第一步：从全量桶里筛出「前一天新创建的表层动态桶」，超过
40 个时按 calculate_score 截断，避免一次涌进来太多炸上下文。

关键行为：
- 排除 permanent / feel / plan / letter / pinned / protected
- 只看 created_at（兼容现有 created 字段），不看 last_active
- “前一天”指服务本地日历的昨天，不是滚动 24/48 小时
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


def _created_value(meta: dict) -> str:
    return meta.get("created_at") or meta.get("created", "")


def _timestamp(value: str) -> float:
    try:
        return parse_iso_datetime(value).timestamp()
    except (ValueError, TypeError, OSError):
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


def previous_day(reference_time: datetime | None = None) -> str:
    """Return yesterday in the service's local calendar."""
    reference = (
        parse_iso_datetime(reference_time)
        if reference_time is not None
        else datetime.now()
    )
    return (reference.date() - timedelta(days=1)).isoformat()


def collect_candidates(
    all_buckets: list,
    window_hours: int | None = None,
    *,
    reference_time: datetime | None = None,
) -> list:
    # window_hours stays in the Python surface for old callers, but the new
    # contract deliberately uses the previous local calendar day only.
    del window_hours
    candidates = [
        b for b in all_buckets
        if b["metadata"].get("type") not in ("permanent", "feel", "plan", "letter", "self", "i")
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
        and not b["metadata"].get("dont_surface", False)
    ]
    target_date = previous_day(reference_time)

    def _created_on_target_date(meta: dict) -> bool:
        try:
            return parse_iso_datetime(_created_value(meta)).date().isoformat() == target_date
        except (ValueError, TypeError):
            return False

    recent = [b for b in candidates if _created_on_target_date(b["metadata"])]
    recent.sort(
        key=lambda b: _timestamp(_created_value(b["metadata"])),
        reverse=True,
    )
    if len(recent) > DREAM_MAX_CANDIDATES:
        recent.sort(
            key=lambda b: rt.decay_engine.calculate_score(b["metadata"]),
            reverse=True,
        )
        recent = recent[:DREAM_MAX_CANDIDATES]
    return recent
