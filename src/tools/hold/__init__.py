"""
========================================
tools/hold/__init__.py — hold 工具入口
========================================

hold 是「我把这件事/这个感受存进我的记忆」。这个文件按入参把请求
路由到三种分支：feel（写第一人称感受）、pinned（钉为永久核心准则）、
core（普通存入 + 自动合并）。

关键行为：
- null-safe 兜底；先做 content / 字节上限校验，再分支
- feel=True / pinned=True 是互斥分支，否则走 core
- core 写完后 fire-and-forget 触发 plan 自动闭环 + 疑似重复扫描

不做什么（边界）：
- 不在这里做 LLM 打标，分支模块负责
- 不返回结构化数据，统一返回供模型阅读的中文短句

对外暴露：dispatch(content, tags, importance, pinned, feel, source_bucket,
                   valence, arousal, why_remembered, meaning, media, quotes) → str
========================================
"""

from typing import Optional

from ombrebrain.storage.quote_store import normalize_quotes
from ombrebrain.storage.state_chain import StateChainError, normalize_state_key
from utils import parse_bool

from .. import _runtime as rt
from .._common import (
    check_content_size,
    check_metadata_size,
    enforce_pinned_quota,
)
from .feel import store_feel
from .pinned import store_pinned
from .core import store_core


async def dispatch(
    content: str,
    tags: Optional[str] = "",
    importance: Optional[int] = None,
    pinned: Optional[bool] = False,
    feel: Optional[bool] = False,
    source_bucket: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    why_remembered: Optional[str] = "",
    meaning: Optional[str] = "",
    media: Optional[list | str] = None,
    test_data: Optional[bool] = False,
    quotes: Optional[list] = None,
    state_key: Optional[str] = "",
) -> str:
    content = "" if content is None else str(content)
    if tags is None:
        tags = ""
    if pinned is None:
        pinned = False
    if feel is None:
        feel = False
    if source_bucket is None:
        source_bucket = ""
    if valence is None:
        valence = -1
    if arousal is None:
        arousal = -1
    if why_remembered is None:
        why_remembered = ""
    why_remembered = str(why_remembered).strip()[:500]
    if meaning is None:
        meaning = ""
    meaning = str(meaning).strip()
    test_data = parse_bool(test_data, default=False)
    if state_key is None:
        state_key = ""
    try:
        state_key = normalize_state_key(state_key) if str(state_key).strip() else ""
    except StateChainError as exc:
        return f"state_key 无效，未创建任何桶：{exc}"
    if state_key and feel:
        return "state_key 只用于普通事实记忆；feel 本次未创建。"
    if test_data and (pinned or feel):
        return "测试数据不能创建为 pinned 或 feel；请使用普通测试桶。"
    if feel:
        importance = 5
    elif pinned:
        importance = 10
    else:
        if importance is None:
            return "普通 hold 的 importance 必填；请先选择 1-10 的整数。"
        if not isinstance(importance, int) or isinstance(importance, bool):
            return "普通 hold 的 importance 必须是 1-10 的整数。"
        if not 1 <= importance <= 10:
            return "普通 hold 的 importance 必须在 1-10 之间。"
    try:
        valence = float(valence)
    except (TypeError, ValueError, OverflowError):
        valence = -1
    try:
        arousal = float(arousal)
    except (TypeError, ValueError, OverflowError):
        arousal = -1

    metadata_err = check_metadata_size(
        tags=tags,
        source_bucket=source_bucket,
        why_remembered=why_remembered,
        meaning=meaning,
    )
    if metadata_err:
        return metadata_err

    try:
        normalized_quotes = normalize_quotes(quotes)
    except ValueError as exc:
        return f"引语无效，未创建任何桶：{exc}"
    if rt.mark_op:
        rt.mark_op("hold")
    rt.record_v3_tool_event("hold", {
        "content_length": len(content or ""),
        "tags": tags,
        "importance": importance,
        "pinned": pinned,
        "feel": feel,
        "source_bucket": source_bucket,
        "valence": valence,
        "arousal": arousal,
        "why_remembered_length": len(why_remembered or ""),
        "quotes_count": len(normalized_quotes),
    })
    await rt.decay_engine.ensure_started()

    if not content or not content.strip():
        return "内容为空，无法存储。"

    err = check_content_size(content)
    if err:
        return err

    # pinned 配额检查（OB-W004 软警告 / OB-I002 自动退出）
    if pinned and not feel:
        pinned = await enforce_pinned_quota(True)

    # 普通桶的 importance 配额在 merge_or_create 的最终 merge/create
    # 事务内检查；这里预检查会在“合并到已占位桶”时产生假降级提示。

    # valence/arousal 越界回退到自动打标（OB-W002 由 bucket_manager 在 clamp 时 push；
    # 这里的 -1 咨兵语义是"她/他未传"，越界则忽略，让 LLM analyze 决定）
    if valence != -1 and not (0 <= valence <= 1):
        try:
            try:
                from errors import push_warning  # type: ignore
            except ImportError:
                from ..errors import push_warning  # type: ignore
            push_warning("OB-W002", f"hold 入参 valence={valence} 越界，已忽略，回退到自动打标")
        except Exception:
            pass
        valence = -1
    if arousal != -1 and not (0 <= arousal <= 1):
        try:
            try:
                from errors import push_warning  # type: ignore
            except ImportError:
                from ..errors import push_warning  # type: ignore
            push_warning("OB-W002", f"hold 入参 arousal={arousal} 越界，已忽略，回退到自动打标")
        except Exception:
            pass
        arousal = -1

    if isinstance(tags, list):
        extra_tags = [str(t).strip() for t in tags if t]
    else:
        extra_tags = [t.strip() for t in str(tags).split(",") if t.strip()]

    # 所有越界/配额提醒走统一 warnings channel；server.py _with_notice 末尾自动追加。
    # 这里返回值只承载业务正文。

    if feel:
        if not source_bucket or not source_bucket.strip():
            return "feel 必须指向一条原始记忆（source_bucket 不能为空）。请先用 breath_search(query=...) 找到那条桶的 bucket_id，再传入 source_bucket=id。"
        result = await store_feel(
            content=content,
            extra_tags=extra_tags,
            valence=valence,
            arousal=arousal,
            source_bucket=source_bucket,
            why_remembered=why_remembered,
            meaning=meaning,
            media=media,
            quotes=normalized_quotes or None,
        )
        return result

    if pinned:
        result = await store_pinned(
            content=content,
            extra_tags=extra_tags,
            valence=valence,
            arousal=arousal,
            why_remembered=why_remembered,
            meaning=meaning,
            media=media,
            quotes=normalized_quotes or None,
        )
        return result

    result = await store_core(
        content=content,
        extra_tags=extra_tags,
        importance=importance,
        valence=valence,
        arousal=arousal,
        why_remembered=why_remembered,
        meaning=meaning,
        media=media,
        test_data=test_data,
        quotes=normalized_quotes or None,
        state_key=state_key,
    )
    return result
