"""
========================================
tools/hold/core.py — hold 普通存入分支（幂等去重，默认新建）
========================================

非 feel、非 pinned 时走这里：优先调 LLM 自动打标，失败则用本地中性元数据，
再检查是否已有完全相同正文；相同则幂等去重，否则默认新建。
旧版语义合并仅在 auto_merge_enabled=true 时兼容启用。

关键行为：
- analyze() 失败（API key/限流/网络不可用）时仍逐字保存正文，只降级元数据
- 她/他显式 valence/arousal 优先于 LLM 打标
- 调 _common.merge_or_create 走合并/新建
- iter 2.0：source_tool 写 ``hold``；合并到老桶时只更新 ``last_merged_by``
- embedding 失败时桶正常创建，返回追加向量化降级警告
- 写完 fire-and-forget：plan 自动闭环判断 + 新桶疑似重复扫描

不做什么（边界）：
- 不做 pinned 配额检查（那是 pinned 分支的事）
- 不做单桶字节上限校验（已在 dispatch 入口做过）

对外暴露：store_core(content, extra_tags, importance, valence, arousal,
                     why_remembered, meaning, media) → str
========================================
"""

import asyncio

from .. import _runtime as rt
from .._common import (
    WriteDisposition,
    merge_or_create,
    check_duplicate_for,
    check_plan_resolution,
)
from ombrebrain.storage.state_chain import current_state_candidates


async def store_core(
    content: str,
    extra_tags: list,
    importance: int,
    valence: float,
    arousal: float,
    why_remembered: str,
    meaning: str = "",
    media: list | str | None = None,
    test_data: bool = False,
    quotes: list[dict] | None = None,
    state_key: str = "",
) -> str:
    metadata_fallback = False
    try:
        analysis = await rt.dehydrator.analyze(content)
    except Exception as e:
        metadata_fallback = True
        rt.logger.warning(
            "hold metadata analysis failed; preserving raw content with local defaults / "
            f"hold 打标失败，使用本地默认元数据并原样保存正文: {type(e).__name__}: {e}"
        )
        default_analysis = getattr(rt.dehydrator, "_default_analysis", None)
        analysis = default_analysis() if callable(default_analysis) else {
            "domain": ["未分类"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": [],
            "suggested_name": "",
        }

    domain = analysis.get("domain") or ["未分类"]
    if not isinstance(domain, list):
        domain = ["未分类"]
    _v = analysis.get("valence", 0.5)
    _a = analysis.get("arousal", 0.3)
    final_valence = valence if 0 <= valence <= 1 else (float(_v) if _v is not None else 0.5)
    final_arousal = arousal if 0 <= arousal <= 1 else (float(_a) if _a is not None else 0.3)
    _raw_tags = analysis.get("tags") or []
    all_tags = list(dict.fromkeys((_raw_tags if isinstance(_raw_tags, list) else []) + extra_tags))
    suggested_name = analysis.get("suggested_name", "")

    result_name, disposition, embed_warn = await merge_or_create(
        content=content,
        tags=all_tags,
        importance=importance,
        domain=domain,
        valence=final_valence,
        arousal=final_arousal,
        name=suggested_name,
        raw_merge=True,
        why_remembered=why_remembered,
        source_tool="hold",
        meaning=meaning,
        media=media,
        test_data=test_data,
        quotes=quotes,
        state_key=state_key,
    )

    action = {
        WriteDisposition.CREATED: "新建→",
        WriteDisposition.DEDUPLICATED: "去重→",
        WriteDisposition.MERGED: "合并→",
    }[disposition]
    asyncio.create_task(check_plan_resolution(content, source_bucket_id=result_name))
    if disposition is WriteDisposition.CREATED:
        asyncio.create_task(check_duplicate_for(result_name, content))
    result = f"{action}{result_name} {','.join(str(d) for d in domain if d is not None)}"
    if embed_warn:
        result += f"\n⚠️ {embed_warn}"
    if metadata_fallback:
        result += "\n⚠️ 打标 API 暂不可用：正文已逐字保存，未做任何压缩；元数据暂用本地中性值。"
    if state_key:
        try:
            candidates = await current_state_candidates(
                rt.bucket_mgr,
                state_key,
                exclude_id=result_name,
            )
        except Exception as exc:
            rt.logger.warning(
                "state_key candidate lookup failed for %s: %s",
                state_key,
                type(exc).__name__,
            )
            candidates = []
        if candidates:
            candidate_ids = ", ".join(str(item.get("id") or "") for item in candidates[:5])
            result += (
                f"\nℹ️ state_key={state_key} 还有 {len(candidates)} 条未标记过时的版本："
                f"{candidate_ids}。系统没有自动取代；确认后用 "
                f'trace(bucket_id="旧桶ID", state_key="{state_key}", '
                f'superseded_by="{result_name}")。'
            )
    return result
