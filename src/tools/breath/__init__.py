"""
========================================
tools/breath/__init__.py — breath 工具的总入口与分支调度
========================================

breath 是「我睁眼看看自己记得什么」。这个文件根据参数把请求路由到
五个分支文件之一：

- catalog.py：catalog=True → 目录模式（每桶一行元数据，0 LLM，最省 token）
- feel.py：domain="feel"（或 tags 含 feel/__feel__）→ 按主题找最多 5 条相关 feel
- importance.py：importance_min >= 1 → 跳过语义，按 importance 拉前 20
- surface.py：query 为空 → 浮现模式（无参公开调用走轻量睁眼，其余走完整浮现）
- search.py：有 query → 检索模式（关键词 + 向量双通道 + 随机漂浮）

关键行为：
- 入口 dispatch() 做参数 null-safe 兜底、token/result 上限归一化、
  tags/domain 解析，再交给具体分支函数
- 不在这里做实际取桶/调 LLM 的工作

不做什么（边界）：
- 不直接处理 embedding 调用，全部下放到检索分支
- 正文渲染统一走 _verbatim.py，不进入 dehydrator
- 不做权限校验，MCP 调用方默认是模型自身

对外暴露：dispatch(query, max_tokens, domain, valence, arousal, max_results,
                   importance_min, tags, catalog) → str
========================================
"""

from typing import Optional

from utils import parse_bool
from .. import _runtime as rt
from .._common import check_metadata_size, check_query_size
from .catalog import surface_catalog
from .feel import surface_feels
from .importance import surface_by_importance
from .surface import surface_daily_impressions, surface_default, surface_plans
from .search import surface_search
from .startup import startup_total_hard_tokens
from .trace import get_run, new_run_id, record_surface_output


async def dispatch_public(
    query: Optional[str] = "",
    max_tokens: Optional[int] = 0,
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    max_results: Optional[int] = 0,
    importance_min: Optional[int] = -1,
    tags: Optional[str] = "",
    catalog: Optional[bool] = False,
) -> str:
    """Preserve cached legacy arguments while identifying a true empty call."""

    kwargs = {
        "query": query,
        "max_tokens": max_tokens,
        "domain": domain,
        "valence": valence,
        "arousal": arousal,
        "max_results": max_results,
        "importance_min": importance_min,
        "tags": tags,
        "catalog": catalog,
    }
    is_empty_call = (
        not str(query or "").strip()
        and int(max_tokens or 0) == 0
        and not str(domain or "").strip()
        and float(valence if valence is not None else -1) == -1
        and float(arousal if arousal is not None else -1) == -1
        and int(max_results or 0) == 0
        and int(importance_min if importance_min is not None else -1) == -1
        and not str(tags or "").strip()
        and not bool(catalog)
    )
    if is_empty_call:
        kwargs["startup"] = True
    return await dispatch(**kwargs)


async def dispatch(
    query: Optional[str] = "",
    max_tokens: Optional[int] = 0,
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    max_results: Optional[int] = 0,
    importance_min: Optional[int] = -1,
    tags: Optional[str] = "",
    catalog: Optional[bool] = False,
    startup: bool = False,
    quotes: Optional[bool] = False,
) -> str:
    # --- Null-safe coercion ---
    query = "" if query is None else str(query)
    if max_tokens is None:
        max_tokens = 0
    domain = "" if domain is None else str(domain)
    if valence is None:
        valence = -1
    if arousal is None:
        arousal = -1
    if max_results is None:
        max_results = 0
    if importance_min is None:
        importance_min = -1
    tags = "" if tags is None else str(tags)
    if catalog is None:
        catalog = False
    quotes = parse_bool(quotes, default=False)

    query_err = check_query_size(query)
    if query_err:
        return query_err
    metadata_err = check_metadata_size(domain=domain, tags=tags)
    if metadata_err:
        return metadata_err

    if rt.mark_op:
        rt.mark_op("breath")
    rt.record_v3_tool_event("breath", {
        "query": query,
        "max_tokens": max_tokens,
        "domain": domain,
        "valence": valence,
        "arousal": arousal,
        "max_results": max_results,
        "importance_min": importance_min,
        "tags": tags,
        "catalog": catalog,
        "quotes": quotes,
    })
    await rt.decay_engine.ensure_started()

    # --- catalog 目录模式：最先短路，0 LLM、只读元数据、每桶一行 ---
    # 开新窗省 token 的推荐姿势：先 breath(catalog=True) 看目录，
    # 再 breath(query=...) 精准拉取正文。
    if catalog:
        domain_filter = [d.strip() for d in domain.split(",") if d.strip()]
        return await surface_catalog(domain_filter=domain_filter or None)

    surfacing_cfg = rt.config.get("surfacing", {}) or {}
    startup_surface = bool(
        startup
        and not query.strip()
        and not domain.strip()
        and importance_min < 1
        and not tags.strip()
        and not catalog
    )
    if startup_surface:
        default_results = int(surfacing_cfg.get("startup_breath_max_results") or 4)
        default_tokens = int(surfacing_cfg.get("startup_breath_max_tokens") or 5000)
    else:
        default_results = int(surfacing_cfg.get("breath_max_results") or 20)
        default_tokens = int(surfacing_cfg.get("breath_max_tokens") or 10000)
    if max_results <= 0:
        max_results = default_results
    if max_tokens <= 0:
        max_tokens = default_tokens
    if startup_surface:
        max_results = max(1, min(max_results, 4))
        max_tokens = max(500, min(max_tokens, 10000))
    else:
        max_results = min(max_results, 50)
        max_tokens = min(max_tokens, 20000)

    # --- 解析 tags 过滤；feel/__feel__ 映射到 feel 通道 ---
    tag_filter = [t.strip() for t in tags.split(",") if t.strip()]
    if any(t in ("feel", "__feel__") for t in tag_filter):
        domain = "feel"
        tag_filter = [t for t in tag_filter if t not in ("feel", "__feel__")]

    # --- Feel 通道优先：必须带主题，不再按时间全量倾倒 ---
    if domain.strip().lower() == "feel":
        return await surface_feels(
            query=query,
            max_tokens=max_tokens,
            max_results=max_results,
        )

    # --- Daily continuity is private state, never an ordinary bucket search. ---
    if domain.strip().lower() == "daily_impression" and not query.strip():
        return await surface_daily_impressions(max_tokens=max_tokens)

    # --- Plan 通道优先：active plan 不参与普通浮现，必须有独立读取入口 ---
    if domain.strip().lower() == "plan" and not query.strip():
        return await surface_plans(max_tokens=max_tokens)

    # --- importance_min 模式：跳过语义，按 importance 降序 ---
    if importance_min >= 1:
        return await surface_by_importance(
            importance_min=importance_min,
            max_tokens=max_tokens,
            tag_filter=tag_filter,
        )

    # --- 无 query：浮现模式 ---
    if not query or not query.strip():
        output = await surface_default(
            max_results=max_results,
            max_tokens=max_tokens,
            tag_filter=tag_filter,
            startup=startup_surface,
        )
        record_surface_output(
            output,
            kind="actual",
            max_results=max_results,
            max_tokens=(
                startup_total_hard_tokens(max_tokens)
                if startup_surface
                else max_tokens
            ),
            soft_tokens=(
                int(surfacing_cfg.get("startup_breath_soft_tokens") or 3000)
                if startup_surface
                else None
            ),
            mode="startup" if startup_surface else "full",
        )
        return output

    # --- 有 query：检索模式 ---
    return await surface_search(
        query=query,
        max_results=max_results,
        max_tokens=max_tokens,
        domain=domain,
        valence=valence,
        arousal=arousal,
        tag_filter=tag_filter,
        with_quotes=quotes,
    )


async def simulate_default_surface() -> dict:
    """Run the exact zero-argument startup breath without injecting its output.

    Default surfacing deliberately does not touch bucket activation state, so a
    Dashboard dry run can safely reuse the same function rather than maintaining
    a second, misleading approximation.
    """

    await rt.decay_engine.ensure_started()
    surfacing_cfg = rt.config.get("surfacing", {}) or {}
    max_results = min(
        max(int(surfacing_cfg.get("startup_breath_max_results") or 4), 1),
        4,
    )
    max_tokens = min(
        max(int(surfacing_cfg.get("startup_breath_max_tokens") or 5000), 500),
        10000,
    )
    output = await surface_default(
        max_results=max_results,
        max_tokens=max_tokens,
        tag_filter=[],
        startup=True,
    )
    run_id = new_run_id()
    record_surface_output(
        output,
        kind="simulation",
        max_results=max_results,
        max_tokens=startup_total_hard_tokens(max_tokens),
        soft_tokens=int(surfacing_cfg.get("startup_breath_soft_tokens") or 3000),
        run_id=run_id,
        mode="startup",
    )
    return get_run(run_id) or {}
