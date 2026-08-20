"""Bounded zero-argument Breath handoff.

The startup surface is a briefing, not an associative sample.  It returns the
small pinned core verbatim, reconnects the newest/most important memories from
the last 24 hours, randomly rotates one older unresolved item without an
immediate repeat, and lists active plans.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import random

from ombrebrain.policy.surfacing import SurfacePolicyVM

from .. import _runtime as rt
from utils import count_tokens_approx, parse_iso_datetime
from ._verbatim import render_stored_bucket


RECENT_HOURS = 24
RECENT_LIMIT = 3
PLAN_LIMIT = 5
PLAN_TOKEN_BUDGET = 350
OLDER_UNRESOLVED_POOL_LIMIT = 20
DEFAULT_SOFT_TOKENS = 3000
DEFAULT_HARD_TOKENS = 5000
_PRIVATE_TYPES = {"permanent", "feel", "plan", "letter", "self", "i"}
_SURFACE_POLICY = SurfacePolicyVM.default()


def _created_datetime(bucket: dict) -> datetime | None:
    meta = bucket.get("metadata") or {}
    for key in ("created_at", "created"):
        value = meta.get(key)
        if not value:
            continue
        try:
            return parse_iso_datetime(value)
        except (TypeError, ValueError, OSError):
            continue
    return None


def _created_timestamp(bucket: dict) -> float:
    value = _created_datetime(bucket)
    if value is None:
        return 0.0
    try:
        return value.timestamp()
    except (ValueError, OSError):
        return 0.0


def _last_active_timestamp(bucket: dict) -> float:
    meta = bucket.get("metadata") or {}
    value = meta.get("last_active") or meta.get("created_at") or meta.get("created")
    try:
        return parse_iso_datetime(value).timestamp() if value else 0.0
    except (TypeError, ValueError, OSError):
        return 0.0


def _importance(bucket: dict) -> int:
    try:
        return int((bucket.get("metadata") or {}).get("importance") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _weight(bucket: dict) -> float:
    try:
        return float((bucket.get("metadata") or {}).get("weight") or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _score(bucket: dict) -> float:
    try:
        return float(rt.decay_engine.calculate_score(bucket.get("metadata") or {}))
    except (TypeError, ValueError, OverflowError, AttributeError):
        return float(_importance(bucket))


def _is_test_data(bucket: dict) -> bool:
    provenance = (bucket.get("metadata") or {}).get("provenance") or {}
    return isinstance(provenance, dict) and provenance.get("kind") == "test"


def _is_core(bucket: dict) -> bool:
    meta = bucket.get("metadata") or {}
    return bool(
        (
            meta.get("pinned")
            or meta.get("protected")
            or meta.get("type") == "permanent"
        )
        and meta.get("type") not in ("letter", "self", "i")
        and _SURFACE_POLICY.evaluate_bucket(bucket, mode="spontaneous").allowed
    )


def _is_startup_memory(bucket: dict) -> bool:
    meta = bucket.get("metadata") or {}
    return bool(
        _SURFACE_POLICY.evaluate_bucket(bucket, mode="spontaneous").allowed
        and meta.get("type") not in _PRIVATE_TYPES
        and not meta.get("pinned")
        and not meta.get("protected")
        and not _is_test_data(bucket)
    )


def _select_memories(
    all_buckets: list[dict],
    *,
    max_results: int,
    reference_time: datetime,
    exclude_older_id: str = "",
) -> tuple[list[tuple[dict, str]], int]:
    """Select recent memories, then rotate one high-quality older unresolved."""

    eligible = [bucket for bucket in all_buckets if _is_startup_memory(bucket)]
    cutoff = reference_time - timedelta(hours=RECENT_HOURS)
    recent = []
    for bucket in eligible:
        created = _created_datetime(bucket)
        if created is not None and cutoff <= created <= reference_time:
            recent.append(bucket)

    recent.sort(key=lambda bucket: (_created_timestamp(bucket), str(bucket.get("id") or "")), reverse=True)
    selected: list[tuple[dict, str]] = []
    selected_ids: set[str] = set()
    recent_cap = min(RECENT_LIMIT, max_results)
    if recent and recent_cap > 0:
        latest = recent[0]
        selected.append((latest, "recent_latest"))
        selected_ids.add(str(latest.get("id") or ""))
        remaining = sorted(
            recent[1:],
            key=lambda bucket: (
                _importance(bucket),
                _created_timestamp(bucket),
                _score(bucket),
                str(bucket.get("id") or ""),
            ),
            reverse=True,
        )
        for bucket in remaining:
            if len(selected) >= recent_cap:
                break
            selected.append((bucket, "recent_important"))
            selected_ids.add(str(bucket.get("id") or ""))

    if len(selected) < max_results:
        older_unresolved = []
        for bucket in eligible:
            bucket_id = str(bucket.get("id") or "")
            if bucket_id in selected_ids:
                continue
            meta = bucket.get("metadata") or {}
            created = _created_datetime(bucket)
            if meta.get("resolved", False):
                continue
            if created is not None and created >= cutoff:
                continue
            older_unresolved.append(bucket)
        older_unresolved.sort(
            key=lambda bucket: (
                _score(bucket),
                _importance(bucket),
                _last_active_timestamp(bucket),
                _created_timestamp(bucket),
                str(bucket.get("id") or ""),
            ),
            reverse=True,
        )
        if older_unresolved:
            pool = older_unresolved[:OLDER_UNRESOLVED_POOL_LIMIT]
            if len(pool) > 1 and exclude_older_id:
                without_previous = [
                    bucket
                    for bucket in pool
                    if str(bucket.get("id") or "") != exclude_older_id
                ]
                if without_previous:
                    pool = without_previous
            selected.append((random.choice(pool), "older_unresolved"))

    return selected[:max_results], len(recent)


def _active_plans(all_buckets: list[dict]) -> tuple[list[dict], int]:
    plans = []
    for bucket in all_buckets:
        meta = bucket.get("metadata") or {}
        if meta.get("type") != "plan" or meta.get("status", "active") != "active":
            continue
        if meta.get("deleted_at") or meta.get("tombstone") or meta.get("dont_surface"):
            continue
        plans.append(bucket)
    plans.sort(
        key=lambda bucket: (
            _weight(bucket),
            _created_timestamp(bucket),
            str(bucket.get("id") or ""),
        ),
        reverse=True,
    )
    return plans[:PLAN_LIMIT], len(plans)


def _render_plan(bucket: dict) -> str:
    bucket_id = str(bucket.get("id") or "")
    weight = _weight(bucket)
    content = str(bucket.get("content") or "").strip() or "（计划正文为空）"
    return f"📋 [活动计划] [bucket_id:{bucket_id}] [weight:{weight:.2f}] {content}"


def _render_plan_pointer(bucket: dict) -> str:
    meta = bucket.get("metadata") or {}
    bucket_id = str(bucket.get("id") or "")
    weight = _weight(bucket)
    name = str(meta.get("name") or bucket_id)
    return (
        f"📋 [活动计划] [bucket_id:{bucket_id}] [weight:{weight:.2f}] "
        f"↗ [未展开] {name}（使用 breath_advanced(domain=\"plan\") 读取）"
    )


def _render_pointer(bucket: dict, *, estimated_tokens: int, reason: str) -> str:
    meta = bucket.get("metadata") or {}
    bucket_id = str(bucket.get("id") or "")
    name = str(meta.get("name") or bucket_id)
    return (
        f"↗ [未展开] [bucket_id:{bucket_id}] [estimated_tokens:{estimated_tokens}] "
        f"[reason:{reason}] {name}"
    )


async def surface_startup(
    all_buckets: list[dict],
    *,
    max_results: int,
    hard_tokens: int,
    soft_tokens: int,
    reference_time: datetime | None = None,
    exclude_older_id: str = "",
) -> str:
    """Render one bounded startup briefing within soft/hard budgets."""

    # The annotation is intentionally narrow, but parse_iso_datetime still
    # normalizes timezone-aware datetime test hooks to OB's naive local-time
    # convention, matching the stored created/created_at parsing above.
    reference = parse_iso_datetime(reference_time) if reference_time is not None else datetime.now()
    hard_tokens = max(500, int(hard_tokens or DEFAULT_HARD_TOKENS))
    soft_tokens = max(500, min(int(soft_tokens or DEFAULT_SOFT_TOKENS), hard_tokens))
    max_results = max(1, int(max_results or 1))

    core_results: list[str] = []
    recent_results: list[str] = []
    unfinished_results: list[str] = []
    plan_results: list[str] = []
    pointers: list[str] = []
    notices: list[str] = [
        f"软目标 {soft_tokens} token，硬上限 {hard_tokens} token；记忆正文只整桶返回，不截断。"
    ]

    def compose() -> str:
        parts = ["=== 轻量睁眼 ===\n轻量简报：核心、最近24小时、随机轮换的较早未完事项与活动计划。"]
        if core_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(core_results))
        if recent_results:
            parts.append("=== 最近24小时 ===\n" + "\n---\n".join(recent_results))
        if unfinished_results:
            parts.append("=== 较早未完事项 ===\n" + "\n---\n".join(unfinished_results))
        if plan_results:
            parts.append("=== 活动计划 ===\n" + "\n---\n".join(plan_results))
        if pointers:
            parts.append("=== 未展开（按需读取） ===\n" + "\n".join(pointers))
        if notices:
            parts.append("=== 本次预算 ===\n" + "\n".join(notices))
        return "\n\n".join(parts)

    def append_if_fits(collection: list[str], rendered: str, *, limit: int) -> bool:
        # Rebuilding the complete envelope keeps budget accounting exact and is
        # cheap under the fixed 4-memory + 5-plan startup contract.  If those
        # caps ever grow materially, replace this with incremental accounting.
        collection.append(rendered)
        if count_tokens_approx(compose()) <= limit:
            return True
        collection.pop()
        return False

    core_buckets = sorted(
        (bucket for bucket in all_buckets if _is_core(bucket)),
        key=lambda bucket: (_created_timestamp(bucket), str(bucket.get("id") or "")),
    )
    for bucket in core_buckets:
        rendered, entry_tokens = render_stored_bucket(
            bucket,
            f"📌 [核心准则] [bucket_id:{bucket['id']}]",
        )
        if not append_if_fits(core_results, rendered, limit=hard_tokens):
            pointer = _render_pointer(
                bucket,
                estimated_tokens=entry_tokens,
                reason="hard_limit",
            )
            append_if_fits(pointers, pointer, limit=hard_tokens)

    plans, total_plans = _active_plans(all_buckets)
    expanded_plans = 0
    for bucket in plans:
        rendered_plan = _render_plan(bucket)
        plan_section = "\n---\n".join([*plan_results, rendered_plan])
        if (
            count_tokens_approx(plan_section) <= PLAN_TOKEN_BUDGET
            and append_if_fits(plan_results, rendered_plan, limit=hard_tokens)
        ):
            expanded_plans += 1
            continue

        pointer = _render_plan_pointer(bucket)
        pointer_section = "\n---\n".join([*plan_results, pointer])
        if count_tokens_approx(pointer_section) > PLAN_TOKEN_BUDGET:
            break
        if not append_if_fits(plan_results, pointer, limit=hard_tokens):
            break

    selected, total_recent = _select_memories(
        all_buckets,
        max_results=max_results,
        reference_time=reference,
        exclude_older_id=exclude_older_id,
    )
    for bucket, reason in selected:
        if reason == "recent_latest":
            prefix = f"🕒 [最近一条] [bucket_id:{bucket['id']}]"
            collection = recent_results
        elif reason == "recent_important":
            prefix = f"🕒 [近期重要] [bucket_id:{bucket['id']}]"
            collection = recent_results
        else:
            prefix = f"🧭 [未完记忆] [权重:{_score(bucket):.2f}] [bucket_id:{bucket['id']}]"
            collection = unfinished_results
        rendered, entry_tokens = render_stored_bucket(bucket, prefix)
        stretch_to_hard = reason == "recent_latest" or _importance(bucket) >= 8
        limit = hard_tokens if stretch_to_hard else soft_tokens
        if append_if_fits(collection, rendered, limit=limit):
            continue
        collection.append(rendered)
        candidate_tokens = count_tokens_approx(compose())
        collection.pop()
        pointer_reason = "hard_limit" if candidate_tokens > hard_tokens else "soft_target"
        pointer = _render_pointer(
            bucket,
            estimated_tokens=entry_tokens,
            reason=pointer_reason,
        )
        append_if_fits(pointers, pointer, limit=hard_tokens)

    selected_recent = sum(1 for _, reason in selected if reason.startswith("recent_"))
    if total_recent > selected_recent:
        notice = f"最近24小时另有 {total_recent - selected_recent} 条记忆未进入本次 {max_results} 条正文名额。"
        append_if_fits(notices, notice, limit=hard_tokens)
    if total_plans > expanded_plans:
        notice = (
            f"有 {total_plans - expanded_plans} 条活动计划未展开，"
            "可用 breath_advanced(domain=\"plan\") 读取。"
        )
        append_if_fits(notices, notice, limit=hard_tokens)
    if pointers:
        notice = f"有 {len(pointers)} 条记忆只列索引；可按 bucket_id 精准读取正文。"
        append_if_fits(notices, notice, limit=hard_tokens)

    output = compose()
    if count_tokens_approx(output) > hard_tokens:  # defensive invariant
        rt.logger.error("startup breath envelope exceeded hard token cap")
        return "轻量睁眼暂时无法在配置预算内安全返回。"
    return output
