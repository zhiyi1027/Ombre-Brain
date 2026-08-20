"""
========================================
tools/dream/__init__.py — dream 工具入口
========================================

dream 是「我做一次梦——默认读滚动 48 小时内新创建的桶全文，自己沉进去想一遍」。
catalog=True 是显式可选的目录模式，不改变默认全文链路。这里把整个流程拆成三步：
1. candidates.py：筛选滚动 48 小时内新建的桶 + 软上限
2. hints.py：连接提示 + 结晶提示
3. feel_rank.py：挑与本轮候选相关的 feel，向量不可用时退回关键词
4. output.py：拼最终文本（包含 active plan 段、相关 feel 段）

dispatch() 只负责把这三步串起来。

对外暴露：dispatch(window_hours, catalog) → str
========================================
"""

from typing import Optional

from .. import _runtime as rt
from .candidates import DREAM_WINDOW_HOURS, collect_candidates, collect_core_context
from .hints import build_connection_hint, build_crystal_hint
from .feel_rank import rank_feels
from .output import format_dream_catalog, format_dream_output


async def dispatch(
    window_hours: Optional[int] = 48,
    catalog: Optional[bool] = False,
) -> str:
    await rt.decay_engine.ensure_started()

    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        rt.logger.error(f"Dream failed to list buckets: {e}")
        return "记忆系统暂时无法访问。"

    # Keep accepting window_hours for old clients, but do not let a stale or
    # narrow client override the fixed reflection window.
    recent = collect_candidates(all_buckets, DREAM_WINDOW_HOURS)
    window_label = f"过去 {DREAM_WINDOW_HOURS} 小时内"
    if catalog:
        final_text = format_dream_catalog(recent, window_label)
        if rt.fire_webhook:
            await rt.fire_webhook(
                "dream",
                {"recent": len(recent), "chars": len(final_text), "catalog": True},
            )
        return final_text

    core_context = collect_core_context(all_buckets)
    if not recent and not core_context:
        return f"{window_label}没有需要消化的新记忆。"

    connection_hint = await build_connection_hint(recent)
    crystal_hint = await build_crystal_hint(all_buckets)
    feels = [
        bucket
        for bucket in all_buckets
        if (bucket.get("metadata") or {}).get("type") == "feel"
    ]
    reference_text = "\n".join(
        str(bucket.get("content") or "") for bucket in recent
    )[:20_000]
    ranked_feels, feel_vector_ok = await rank_feels(feels, reference_text)

    final_text = format_dream_output(
        recent=recent,
        all_buckets=all_buckets,
        window_hours=DREAM_WINDOW_HOURS,
        target_date=window_label,
        connection_hint=connection_hint,
        crystal_hint=crystal_hint,
        core_context=core_context,
        related_feels=[feel for feel, _score in ranked_feels],
        feel_vector_ok=feel_vector_ok,
    )

    if rt.fire_webhook:
        await rt.fire_webhook("dream", {"recent": len(recent), "chars": len(final_text)})
    return final_text
