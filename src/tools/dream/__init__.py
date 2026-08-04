"""
========================================
tools/dream/__init__.py — dream 工具入口
========================================

dream 是「我做一次梦——默认读前一天新创建的桶全文，自己沉进去想一遍」。
catalog=True 是显式可选的目录模式，不改变默认全文链路。这里把整个流程拆成三步：
1. candidates.py：筛选前一天新建的桶 + 软上限
2. hints.py：连接提示 + 结晶提示
3. output.py：拼最终文本（包含 active plan 段、feel 历史段）

dispatch() 只负责把这三步串起来。

对外暴露：dispatch(window_hours, catalog) → str
========================================
"""

from typing import Optional

from .. import _runtime as rt
from .candidates import collect_candidates, collect_core_context, previous_day
from .hints import build_connection_hint, build_crystal_hint
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

    recent = collect_candidates(all_buckets, window_hours)
    target_date = previous_day()
    if catalog:
        final_text = format_dream_catalog(recent, target_date)
        if rt.fire_webhook:
            await rt.fire_webhook(
                "dream",
                {"recent": len(recent), "chars": len(final_text), "catalog": True},
            )
        return final_text

    core_context = collect_core_context(all_buckets)
    if not recent and not core_context:
        return f"{target_date} 没有新创建的记忆桶。"

    connection_hint = await build_connection_hint(recent)
    crystal_hint = await build_crystal_hint(all_buckets)

    final_text = format_dream_output(
        recent=recent,
        all_buckets=all_buckets,
        window_hours=window_hours,
        target_date=target_date,
        connection_hint=connection_hint,
        crystal_hint=crystal_hint,
        core_context=core_context,
    )

    if rt.fire_webhook:
        await rt.fire_webhook("dream", {"recent": len(recent), "chars": len(final_text)})
    return final_text
