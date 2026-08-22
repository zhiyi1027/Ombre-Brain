"""
========================================
tools/breath/surface.py — 无 query 浮现模式
========================================

走 breath()（不传 query）时进入这里。无参公开调用转到 startup.py 生成
轻量睁眼简报；其中较早未完事项从高权重候选池随机轮换，手动完整浮现
仍使用本文件原有的权重采样与被动联想。

关键行为：
- 排除 anchor 桶（anchor 是坐标系，不主动出现）
- pinned/protected 桶始终作为「核心准则」置顶（letter 桶即使 importance=10 也不置顶）
- 未解决桶按 calculate_score 排序；冷启动桶（从未访问且 importance>=8）插队前 2
- 配置开关 surfacing.sampling.enabled 启用后做加权无放回采样，否则
  保留 top1 + top20 内随机洗牌
- 末尾 1~2 条「久未浮现」passive association（imp>=8 且未访问 / imp>=9 且 7 天未活跃）

不做什么（边界）：
- 不调用 touch()：浮现不能重置衰减计时器
- 不返回 feel / plan / letter / archived（专用通道有自己的入口）
- 不做关键词检索（那是 search.py 的事）

对外暴露：surface_default(max_results, max_tokens, tag_filter, startup=False) → str
========================================
"""

import random
import time
from datetime import datetime, timedelta

from ombrebrain.policy.surfacing import SurfacePolicyVM
from plan_review import plan_review_state, plan_stale_after_days
from .. import _runtime as rt
from utils import count_tokens_approx, parse_bool, parse_iso_datetime
from ._verbatim import render_stored_bucket
from .startup import DEFAULT_SOFT_TOKENS, surface_startup
from .trace import list_runs

# U-07 fix: throttle the sampling-fallback INFO log to once per 5 minutes.
# 库小且 sampling=ON 时此分支每次 breath 都触发，原本会刷屏；改为 ≥300s
# 才打一次，并附带本窗口被压制的次数（首次为 0）。
_FALLBACK_LOG_INTERVAL_SEC = 300
_fallback_log_state = {"last_ts": 0.0, "suppressed": 0}
_SURFACE_POLICY = SurfacePolicyVM.default()
_BUDGET_NOTICE = (
    "token 预算不足：有 {omitted} 条主要浮现记忆因放不下剩余预算而未返回；"
    "已返回正文均保持完整，未截断或摘要。"
    "当前约使用 {used}/{limit} token，如需被省略的整桶请提高 max_tokens 后重试。"
)


def _bucket_has_tags(meta: dict, tag_filter: list) -> bool:
    if not tag_filter:
        return True
    bucket_tags = set(meta.get("tags", []) or [])
    return all(t in bucket_tags for t in tag_filter)


def _can_surface(bucket: dict) -> bool:
    return _SURFACE_POLICY.evaluate_bucket(bucket, mode="spontaneous").allowed


def _budget_notice(*, omitted: int, used: int, limit: int) -> str:
    return _BUDGET_NOTICE.format(omitted=omitted, used=used, limit=limit)


def _last_startup_unfinished_id() -> str:
    """Return the last actually surfaced startup unfinished bucket, if any."""

    for run in list_runs(limit=20, kind="actual"):
        if run.get("mode") != "startup":
            continue
        for entry in run.get("entries") or []:
            if (
                entry.get("section") == "unfinished"
                and entry.get("status") == "returned"
            ):
                return str(entry.get("bucket_id") or "")
    return ""


async def surface_plans(max_tokens: int) -> str:
    """Return active plans verbatim without entering ordinary surfacing."""

    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as exc:
        rt.logger.error(f"Plan retrieval failed / 计划读取失败: {exc}")
        return "读取 plan 失败。"

    plans = []
    reference_time = datetime.now()
    stale_after_days = plan_stale_after_days(getattr(rt, "config", {}))
    for bucket in all_buckets:
        meta = bucket.get("metadata") or {}
        if meta.get("type") != "plan" or meta.get("status", "active") != "active":
            continue
        if meta.get("deleted_at") or meta.get("tombstone") or meta.get("dont_surface"):
            continue
        plans.append(bucket)
    plans.sort(
        key=lambda bucket: (
            plan_review_state(
                bucket,
                reference_time=reference_time,
                stale_after_days=stale_after_days,
            )["is_stale"],
            float((bucket.get("metadata") or {}).get("weight") or 0.0),
            str((bucket.get("metadata") or {}).get("created") or ""),
            str(bucket.get("id") or ""),
        ),
        reverse=True,
    )
    if not plans:
        return "没有计划。"

    header = (
        "=== 你的 active plans（权重高→低）===\n"
        "完成了用 trace(bucket_id, status=\"resolved\")，"
        "放弃了用 trace(bucket_id, status=\"abandoned\")。"
    )
    used = count_tokens_approx(header)
    rendered_plans: list[str] = []
    omitted = 0
    for index, plan in enumerate(plans):
        meta = plan.get("metadata") or {}
        created = str(meta.get("created") or "")
        review = plan_review_state(
            plan,
            reference_time=reference_time,
            stale_after_days=stale_after_days,
        )
        review_label = ""
        review_notice = ""
        if review["is_stale"]:
            days = int(review["days_since_confirmation"] or 0)
            review_label = f" [待确认:已{days}天]"
            review_notice = (
                f"\n⚠ 这条计划已 {days} 天未确认，仍然有效吗？"
                "可在 Dashboard 选择继续、完成或放弃；系统不会自动改状态。"
            )
        rendered, entry_tokens = render_stored_bucket(
            plan,
            (
                f"📋 [活动计划] [bucket_id:{plan['id']}] "
                f"[weight:{float(meta.get('weight') or 0.0):.2f}]"
                f"{review_label} [{created}]"
            ),
        )
        if review_notice:
            rendered += review_notice
            entry_tokens = count_tokens_approx(rendered)
        separator_tokens = count_tokens_approx("\n---\n") if rendered_plans else 0
        if used + separator_tokens + entry_tokens > max_tokens:
            omitted = len(plans) - index
            break
        rendered_plans.append(rendered)
        used += separator_tokens + entry_tokens

    output = header
    if rendered_plans:
        output += "\n\n" + "\n---\n".join(rendered_plans)
    if omitted:
        output += (
            f"\n\n另有 {omitted} 条 plan 因 token 预算不足未返回；"
            "正文未截断或摘要。"
        )
    return output


async def surface_daily_impressions(max_tokens: int) -> str:
    """Read generated daily continuity artifacts outside ordinary buckets."""

    service = getattr(rt, "daily_continuity", None)
    if service is None or not getattr(service, "enabled", False):
        return "日印象未启用。"
    try:
        return service.read_recent(max_tokens=max_tokens)
    except Exception as exc:
        rt.logger.error("Daily impression retrieval failed: %s", exc)
        return "读取日印象失败。"


async def surface_default(
    max_results: int,
    max_tokens: int,
    tag_filter: list,
    *,
    startup: bool = False,
) -> str:
    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        rt.logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
        return "记忆系统暂时无法访问。"

    surfacing_cfg = rt.config.get("surfacing", {}) or {}
    if startup:
        daily_impression = ""
        daily_cited_bucket_ids: set[str] = set()
        service = getattr(rt, "daily_continuity", None)
        if service is not None and getattr(service, "enabled", False):
            try:
                daily_impression = service.read_previous()
            except Exception as exc:
                rt.logger.warning("Daily startup impression unavailable: %s", exc)
            try:
                cited_reader = getattr(service, "previous_cited_bucket_ids", None)
                if callable(cited_reader):
                    daily_cited_bucket_ids = set(cited_reader())
            except Exception as exc:
                rt.logger.warning(
                    "Daily startup evidence map unavailable: %s",
                    exc,
                )
        return await surface_startup(
            all_buckets,
            max_results=max_results,
            hard_tokens=max_tokens,
            soft_tokens=int(
                surfacing_cfg.get("startup_breath_soft_tokens")
                or DEFAULT_SOFT_TOKENS
            ),
            exclude_older_id=_last_startup_unfinished_id(),
            daily_impression=daily_impression,
            daily_cited_bucket_ids=daily_cited_bucket_ids,
        )

    # --- pinned/protected 桶置顶（排除 letter 桶：letter 的 importance=10 不代表核心准则）---
    # 注意：pinned 提取在 anchor 过滤 *之前*，保证 anchor+pinned 桶也能出现在核心准则段。
    # pinned 优先级高于 anchor（她/他钉选的原则永远可见）。
    pinned_buckets = [
        b for b in all_buckets
        if (
            b["metadata"].get("pinned")
            or b["metadata"].get("protected")
            or b["metadata"].get("type") == "permanent"
        )
        and _can_surface(b)
        and b["metadata"].get("type") != "letter"
        and not b["metadata"].get("anchor", False)  # 防御：anchor 是坐标系，永不主动浮现，即使 pinned
    ]
    pinned_ids = {b["id"] for b in pinned_buckets}
    pinned_results = []
    token_budget = max_tokens
    primary_omitted = 0
    for b in pinned_buckets:
        try:
            rendered, entry_tokens = render_stored_bucket(
                b,
                f"📌 [核心准则] [bucket_id:{b['id']}]",
            )
            if entry_tokens > token_budget:
                primary_omitted += 1
                continue
            pinned_results.append(rendered)
            token_budget -= entry_tokens
        except Exception as e:
            rt.logger.warning(f"Failed to render pinned bucket / 钉选桶渲染失败: {e}")

    # --- iter 2.0: anchor 桶在默认浮现模式的 *未解决池* 不出现（anchor 是坐标系不是浮现对象）---
    # anchor 过滤仅作用于 unresolved 候选，不影响 pinned 提取（上方已完成）。
    all_buckets_non_anchor = [b for b in all_buckets if not b["metadata"].get("anchor", False)]

    # --- 未解决桶 ---
    unresolved = [
        b for b in all_buckets_non_anchor
        if _can_surface(b)
        and not b["metadata"].get("resolved", False)
        and b["metadata"].get("type") not in ("permanent", "feel", "plan", "letter", "self", "i")
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
        and not b["metadata"].get("dont_surface", False)
        and _bucket_has_tags(b["metadata"], tag_filter)
    ]

    rt.logger.info(
        f"Breath surfacing: {len(all_buckets)} total, "
        f"{len(pinned_buckets)} pinned, {len(unresolved)} unresolved"
    )


    def _sort_key(b: dict):
        """F-05: 二级排序 key，消除同分时浮现随机抖动。
        主键：decay_score（降序）
        次键：last_active 时间戳（越新越高）
        三键：arousal × valence（情感强度，越高越先浮现）
        四键：importance
        """
        meta = b["metadata"]
        score = rt.decay_engine.calculate_score(meta)
        try:
            last_ts = parse_iso_datetime(
                meta.get("last_active") or meta.get("created", "")
            ).timestamp()
        except (ValueError, TypeError):
            last_ts = 0.0
        # `or` 会把合法的 0.0（比如效价/唤醒度恰好为极端值的记忆）当成缺失值
        # 吞掉，静默换成默认值——用 .get(key, default) 才能保留 0.0 本身。
        try:
            av = float(meta.get("arousal", 0.3)) * float(meta.get("valence", 0.5))
        except (TypeError, ValueError):
            av = 0.3 * 0.5
        imp = int(meta.get("importance") or 5)
        return (score, last_ts, av, imp)

    scored = sorted(unresolved, key=_sort_key, reverse=True)

    if scored:
        top_scores = [(b["metadata"].get("name", b["id"]), rt.decay_engine.calculate_score(b["metadata"])) for b in scored[:5]]
        rt.logger.info(f"Top unresolved scores: {top_scores}")

    # --- 冷启动检测 ---
    cold_start = [
        b for b in unresolved
        if int(b["metadata"].get("activation_count") or 0) == 0
        and int(b["metadata"].get("importance") or 0) >= 8
    ][:2]
    cold_start_ids = {b["id"] for b in cold_start}
    _ = pinned_ids  # suppress unused-var warning; used implicitly for logging only
    scored_deduped = [b for b in scored if b["id"] not in cold_start_ids]
    scored_with_cold = cold_start + scored_deduped

    # --- 按 token 预算浮现，加权采样 / 随机洗牌 + 硬上限 ---
    candidates = list(scored_with_cold)
    sampling_cfg = surfacing_cfg.get("sampling", {}) or {}
    sampling_enabled = parse_bool(sampling_cfg.get("enabled", False), default=False)
    if sampling_enabled and len(candidates) > len(cold_start) + 1:
        n_cold = len(cold_start)
        non_cold = candidates[n_cold:]
        top_k = int(sampling_cfg.get("top_k") or 5)
        sample_k = int(sampling_cfg.get("sample_k") or 2)
        temperature = max(0.1, float(sampling_cfg.get("temperature") or 0.7))
        pool = non_cold[:max(top_k, sample_k)]
        try:
            weights = [
                max(0.0001, rt.decay_engine.calculate_score(b["metadata"])) ** (1.0 / temperature)
                for b in pool
            ]
            picked = []
            pool_copy = list(pool)
            weights_copy = list(weights)
            for _ in range(min(sample_k, len(pool_copy))):
                idx = random.choices(range(len(pool_copy)), weights=weights_copy, k=1)[0]
                picked.append(pool_copy.pop(idx))
                weights_copy.pop(idx)
            rest = pool_copy + non_cold[len(pool):]
            non_cold = picked + rest
            candidates = cold_start + non_cold
        except Exception as e:
            rt.logger.warning(f"Weighted sampling failed, fallback to original / 加权采样失败: {e}")
    elif len(candidates) > 1:
        if sampling_enabled:
            now_ts = time.monotonic()
            if now_ts - _fallback_log_state["last_ts"] >= _FALLBACK_LOG_INTERVAL_SEC:
                suppressed = _fallback_log_state["suppressed"]
                rt.logger.info(
                    f"weighted sampling fallback: candidates={len(candidates)}, "
                    f"cold_start={len(cold_start)}, sample_k={sampling_cfg.get('sample_k', 2)}, "
                    f"reason=pool_too_small, suppressed_in_window={suppressed}"
                )
                _fallback_log_state["last_ts"] = now_ts
                _fallback_log_state["suppressed"] = 0
            else:
                _fallback_log_state["suppressed"] += 1
        n_cold = len(cold_start)
        non_cold = candidates[n_cold:]
        if len(non_cold) > 1:
            top1 = [non_cold[0]]
            pool = non_cold[1:min(20, len(non_cold))]
            random.shuffle(pool)
            non_cold = top1 + pool + non_cold[min(20, len(non_cold)):]
        candidates = cold_start + non_cold
    candidates = candidates[:max_results]

    dynamic_results = []
    for b in candidates:
        try:
            score = rt.decay_engine.calculate_score(b["metadata"])
            rendered, entry_tokens = render_stored_bucket(
                b,
                f"[权重:{score:.2f}] [bucket_id:{b['id']}]",
            )
            if entry_tokens > token_budget:
                primary_omitted += 1
                continue
            dynamic_results.append(rendered)
            token_budget -= entry_tokens
        except Exception as e:
            rt.logger.warning(f"Failed to render surfaced bucket / 浮现渲染失败: {e}")
            continue

    if not pinned_results and not dynamic_results:
        if primary_omitted:
            return _budget_notice(
                omitted=primary_omitted,
                used=max_tokens - token_budget,
                limit=max_tokens,
            )
        if rt.mark_op:
            rt.mark_op("breath_empty")
        stats = await rt.bucket_mgr.get_stats()
        total = stats.get("permanent_count", 0) + stats.get("dynamic_count", 0)
        if total == 0:
            return (
                "我的记忆池现在是空的。\n"
                "想给我留点种子？用 hold(content=\"...\", importance=你判断的1到10) 写下第一条；\n"
                "或者 grow(content=\"...\") 把一段长对话/日记一次性灌给我。"
            )
        return (
            "权重池暂时平静——我手上没什么需要主动浮现的东西。\n"
            "可以试试 breath_search(query=\"想找的关键词\") 走检索，\n"
            "或者 dream() 让我自己挑几段最近的记忆嚼一嚼。"
        )

    # --- iter 1.6 §7: passive association ---
    passive_results: list[str] = []
    try:
        now = datetime.now()
        seven_days_ago = now - timedelta(days=7)
        already = {b["id"] for b in candidates}
        passive_pool = []
        for b in unresolved:
            if b["id"] in already:
                continue
            meta = b["metadata"]
            ac = int(meta.get("activation_count") or 0)
            imp = int(meta.get("importance") or 0)
            cond_a = ac == 0 and imp >= 8
            cond_b = False
            if imp >= 9:
                last = meta.get("last_active") or meta.get("created", "")
                try:
                    last_dt = parse_iso_datetime(last) if last else None
                    if last_dt and last_dt < seven_days_ago:
                        cond_b = True
                except Exception:
                    cond_b = False
            if cond_a or cond_b:
                passive_pool.append(b)
        if passive_pool and not primary_omitted:
            random.shuffle(passive_pool)
            for b in passive_pool[:2]:
                try:
                    rendered, entry_tokens = render_stored_bucket(
                        b,
                        f"💤 [久未浮现] [bucket_id:{b['id']}]",
                    )
                    if entry_tokens > token_budget:
                        continue
                    passive_results.append(rendered)
                    token_budget -= entry_tokens
                except Exception as e:
                    rt.logger.warning(f"passive association render failed: {e}")
    except Exception as e:
        rt.logger.warning(f"passive association block failed: {e}")

    # --- 3% 偶遇：从 resolved 池随机浮现 1~3 条沉底记忆 (iter 2.1) ---
    # 设计意图：让已解决的记忆有小概率重新出现，制造"忽然想起"的温度。
    # 与无结果兜底逻辑并存；不替换主流程。
    dream_results: list[str] = []
    if not primary_omitted and random.random() < 0.03:
        try:
            shown_ids = {b["id"] for b in candidates}
            resolved_pool = [
                b for b in all_buckets
                if _can_surface(b)
                and b["metadata"].get("resolved", False)
                and b["id"] not in shown_ids
                and b["metadata"].get("type") not in ("feel", "plan", "letter")
                and not b["metadata"].get("pinned")
            ]
            if resolved_pool:
                random.shuffle(resolved_pool)
                for b in resolved_pool[:3]:
                    try:
                        rendered, entry_tokens = render_stored_bucket(
                            b,
                            f"✨ [偶遇] [bucket_id:{b['id']}]",
                        )
                        if entry_tokens > token_budget:
                            continue
                        dream_results.append(rendered)
                        token_budget -= entry_tokens
                        rt.logger.info(f"Dream surface triggered / 偶遇机制触发: {b['id']}")
                    except Exception as e:
                        rt.logger.warning(f"Dream surface render failed / 偶遇渲染失败: {e}")
        except Exception as e:
            rt.logger.warning(f"Dream surface block failed / 偶遇模块异常: {e}")

    def compose_output() -> str:
        parts = []
        if pinned_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
        if dynamic_results:
            parts.append("=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results))
        if passive_results:
            parts.append("=== 久未浮现 ===\n" + "\n---\n".join(passive_results))
        if dream_results:
            parts.append("=== 偶然想起 ===\n" + "\n---\n".join(dream_results))
        if primary_omitted:
            parts.append(
                _budget_notice(
                    omitted=primary_omitted,
                    used=max_tokens - token_budget,
                    limit=max_tokens,
                )
            )
        return "\n\n".join(parts)

    return compose_output()
