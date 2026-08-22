"""Bounded one-button Breath handoff.

The startup surface returns the stable briefing, automatically reads up to two
remaining 48-hour reflection candidates, then appends feelings related to the
ordinary memories whose full bodies actually fit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import random

from ombrebrain.policy.surfacing import SurfacePolicyVM

from .. import _runtime as rt
from utils import count_tokens_approx, parse_bool, parse_iso_datetime
from ._envelope import DAILY_IMPRESSION_SENTINEL
from ._verbatim import render_stored_bucket
from .feel import select_startup_feels


RECENT_HOURS = 24
RECENT_LIMIT = 3
REFLECTION_HOURS = 48
REFLECTION_LIMIT = 2
REFLECTION_TOKEN_BUDGET = 2000
STARTUP_FEEL_TOKEN_BUDGET = 2000
PLAN_LIMIT = 5
PLAN_TOKEN_BUDGET = 350
OLDER_UNRESOLVED_POOL_LIMIT = 20
OLDER_INACTIVE_DAYS = 7
DEFAULT_SOFT_TOKENS = 3000
DEFAULT_HARD_TOKENS = 5000
_PRIVATE_TYPES = {"permanent", "feel", "plan", "letter", "self", "i"}
_SURFACE_POLICY = SurfacePolicyVM.default()


def startup_total_hard_tokens(base_hard_tokens: int) -> int:
    """Return the complete one-button envelope cap shown by Breath Trace."""

    base = max(500, int(base_hard_tokens or DEFAULT_HARD_TOKENS))
    return base + REFLECTION_TOKEN_BUDGET + STARTUP_FEEL_TOKEN_BUDGET


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


def _is_startup_memory(bucket: dict, *, allow_digested: bool = False) -> bool:
    meta = bucket.get("metadata") or {}
    decision = _SURFACE_POLICY.evaluate_bucket(bucket, mode="spontaneous")
    allowed = decision.allowed or (
        allow_digested and set(decision.reasons) == {"digested"}
    )
    return bool(
        allowed
        and meta.get("type") not in _PRIVATE_TYPES
        and not meta.get("pinned")
        and not meta.get("protected")
        and not _is_test_data(bucket)
    )


def _is_neglected_older_memory(bucket: dict, *, reference_time: datetime) -> bool:
    """Match upstream OB's narrow passive-association eligibility rule."""

    meta = bucket.get("metadata") or {}
    importance = _importance(bucket)
    try:
        activation_count = int(meta.get("activation_count") or 0)
    except (TypeError, ValueError, OverflowError):
        activation_count = 0
    if activation_count == 0 and importance >= 8:
        return True
    if importance < 9:
        return False

    value = meta.get("last_active") or meta.get("created_at") or meta.get("created")
    if not value:
        return False
    try:
        last_active = parse_iso_datetime(value)
    except (TypeError, ValueError, OSError):
        return False
    return last_active < reference_time - timedelta(days=OLDER_INACTIVE_DAYS)


def _select_memories(
    all_buckets: list[dict],
    *,
    max_results: int,
    reference_time: datetime,
    exclude_older_id: str = "",
    daily_cited_bucket_ids: set[str] | None = None,
) -> tuple[list[tuple[dict, str]], int]:
    """Select recent handoff slots before one bounded passive association."""

    eligible = [bucket for bucket in all_buckets if _is_startup_memory(bucket)]
    latest_eligible = [
        bucket
        for bucket in all_buckets
        if _is_startup_memory(bucket, allow_digested=True)
    ]
    cited_ids = {
        str(bucket_id or "").strip()
        for bucket_id in (daily_cited_bucket_ids or set())
        if str(bucket_id or "").strip()
    }
    cutoff = reference_time - timedelta(hours=RECENT_HOURS)
    recent = []
    for bucket in latest_eligible:
        created = _created_datetime(bucket)
        if created is not None and cutoff <= created <= reference_time:
            recent.append(bucket)

    recent.sort(key=lambda bucket: (_created_timestamp(bucket), str(bucket.get("id") or "")), reverse=True)
    selected: list[tuple[dict, str]] = []
    selected_ids: set[str] = set()
    if recent and max_results > 0:
        latest = recent[0]
        selected.append((latest, "recent_latest"))
        selected_ids.add(str(latest.get("id") or ""))

    # Fill up to three ordinary-memory slots with recent detail first. Sources
    # already represented in yesterday's impression remain reachable, but
    # uncited material gets first use of the two additional recent slots.
    recent_cap = min(RECENT_LIMIT, max_results)
    remaining = [
        bucket
        for bucket in eligible
        if str(bucket.get("id") or "") not in selected_ids
        and (created := _created_datetime(bucket)) is not None
        and cutoff <= created <= reference_time
    ]
    remaining.sort(
        key=lambda bucket: (
            _importance(bucket),
            _created_timestamp(bucket),
            _score(bucket),
            str(bucket.get("id") or ""),
        ),
        reverse=True,
    )
    uncited = [
        bucket for bucket in remaining
        if str(bucket.get("id") or "") not in cited_ids
    ]
    cited = [
        bucket for bucket in remaining
        if str(bucket.get("id") or "") in cited_ids
    ]
    for bucket in [*uncited, *cited]:
        recent_selected = sum(
            1 for _selected, reason in selected
            if reason.startswith("recent_")
        )
        if len(selected) >= max_results or recent_selected >= recent_cap:
            break
        selected.append((bucket, "recent_important"))
        selected_ids.add(str(bucket.get("id") or ""))

    # The final ordinary-memory slot is associative rather than part of the
    # handoff.  Keep upstream OB's narrow passive pool: an older unresolved
    # memory must either be important and never activated, or very important
    # and inactive for at least seven days.  Randomness only rotates within the
    # best-scoring bounded pool, and the previous surfaced item is avoided when
    # another candidate exists.
    if len(selected) < max_results:
        older_unresolved = []
        for bucket in eligible:
            bucket_id = str(bucket.get("id") or "")
            if bucket_id in selected_ids:
                continue
            meta = bucket.get("metadata") or {}
            created = _created_datetime(bucket)
            if parse_bool(meta.get("resolved"), default=False):
                continue
            if created is not None and created >= cutoff:
                continue
            if not _is_neglected_older_memory(bucket, reference_time=reference_time):
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
            older = random.choice(pool)
            selected.append((older, "older_unresolved"))
            selected_ids.add(str(older.get("id") or ""))

    normally_eligible_recent_ids = {
        str(bucket.get("id") or "")
        for bucket in eligible
        if (created := _created_datetime(bucket)) is not None
        and cutoff <= created <= reference_time
    }
    if recent:
        normally_eligible_recent_ids.add(str(recent[0].get("id") or ""))
    return selected[:max_results], len(normally_eligible_recent_ids)


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


def _reflection_candidates(
    all_buckets: list[dict],
    *,
    exclude_ids: set[str],
    reference_time: datetime,
) -> list[dict]:
    """Return up to two recent, unresolved memories not already read by Breath."""

    cutoff = reference_time - timedelta(hours=REFLECTION_HOURS)
    candidates = []
    for bucket in all_buckets:
        bucket_id = str(bucket.get("id") or "")
        meta = bucket.get("metadata") or {}
        created = _created_datetime(bucket)
        if not bucket_id or bucket_id in exclude_ids:
            continue
        if not _is_startup_memory(bucket):
            continue
        if parse_bool(meta.get("resolved"), default=False):
            continue
        if parse_bool(meta.get("digested"), default=False):
            continue
        if created is None or created < cutoff or created > reference_time:
            continue
        candidates.append(bucket)
    candidates.sort(
        key=lambda bucket: (
            _importance(bucket),
            _created_timestamp(bucket),
            _score(bucket),
            str(bucket.get("id") or ""),
        ),
        reverse=True,
    )
    return candidates[:REFLECTION_LIMIT]


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
    daily_impression: str = "",
    daily_cited_bucket_ids: set[str] | None = None,
    reflection_tokens: int = REFLECTION_TOKEN_BUDGET,
    feel_tokens: int = STARTUP_FEEL_TOKEN_BUDGET,
) -> str:
    """Render one bounded startup briefing within soft/hard budgets."""

    # The annotation is intentionally narrow, but parse_iso_datetime still
    # normalizes timezone-aware datetime test hooks to OB's naive local-time
    # convention, matching the stored created/created_at parsing above.
    reference = parse_iso_datetime(reference_time) if reference_time is not None else datetime.now()
    hard_tokens = max(500, int(hard_tokens or DEFAULT_HARD_TOKENS))
    soft_tokens = max(500, min(int(soft_tokens or DEFAULT_SOFT_TOKENS), hard_tokens))
    max_results = max(1, int(max_results or 1))
    reflection_tokens = max(0, int(reflection_tokens or 0))
    feel_tokens = max(0, int(feel_tokens or 0))
    reflection_hard_tokens = hard_tokens + reflection_tokens
    total_hard_tokens = reflection_hard_tokens + feel_tokens

    core_results: list[str] = []
    daily_results: list[str] = []
    recent_results: list[str] = []
    reflection_results: list[str] = []
    unfinished_results: list[str] = []
    plan_results: list[str] = []
    feel_results: list[str] = []
    feel_note = ""
    pointers: list[str] = []
    notices: list[str] = [
        f"基础记忆软参考 {soft_tokens} token、硬上限 {hard_tokens} token；"
        f"自动精读另有 {reflection_tokens} token，相关 feel 另有 {feel_tokens} token；"
        f"总硬上限 {total_hard_tokens} token。"
        "最多三条近期交接与一条合格旧事联想均整桶尝试；"
        "硬上限内绝不截断。"
    ]

    def compose() -> str:
        parts = [
            "=== 一键睁眼 ===\n"
            "一次返回核心、日印象、近期交接、自动精读、旧事联想、计划与相关 feel。"
        ]
        if core_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(core_results))
        if daily_results:
            parts.append(
                DAILY_IMPRESSION_SENTINEL + "\n" + "\n---\n".join(daily_results)
            )
        if recent_results:
            parts.append("=== 最近24小时 ===\n" + "\n---\n".join(recent_results))
        if reflection_results:
            parts.append("=== 自动精读 ===\n" + "\n---\n".join(reflection_results))
        if unfinished_results:
            parts.append("=== 较早未完事项 ===\n" + "\n---\n".join(unfinished_results))
        if plan_results:
            parts.append("=== 活动计划 ===\n" + "\n---\n".join(plan_results))
        if feel_results:
            feel_body = "\n---\n".join(feel_results)
            parts.append(
                "=== 相关 feel ===\n"
                + (feel_note + "\n" if feel_note else "")
                + feel_body
            )
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

    def append_extension_if_fits(
        collection: list[str],
        rendered: str,
        *,
        section_limit: int,
        total_limit: int,
    ) -> bool:
        collection.append(rendered)
        section_text = "\n---\n".join(collection)
        if (
            count_tokens_approx(section_text) <= section_limit
            and count_tokens_approx(compose()) <= total_limit
        ):
            return True
        collection.pop()
        return False

    def append_notice_if_fits(notice: str, *, limit: int) -> bool:
        notices.append(notice)
        if count_tokens_approx(compose()) <= limit:
            return True
        notices.pop()
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

    daily_text = str(daily_impression or "").strip()
    if daily_text and not append_if_fits(
        daily_results,
        daily_text,
        limit=hard_tokens,
    ):
        append_if_fits(
            pointers,
            '↗ [未展开] 昨日印象（使用 breath_advanced(domain="daily_impression") 读取）',
            limit=hard_tokens,
        )

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
        daily_cited_bucket_ids=daily_cited_bucket_ids,
    )
    selected_ids = {str(bucket.get("id") or "") for bucket, _reason in selected}
    context_buckets: list[dict] = []
    returned_recent = 0
    for bucket, reason in selected:
        # The four ordinary-memory slots are a fixed startup contract: true
        # latest, up to two additional recent details, then one qualified old
        # association.  The soft target is diagnostic only for this fixed set;
        # only the hard cap may turn a whole body into a pointer.
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
        if append_if_fits(collection, rendered, limit=hard_tokens):
            context_buckets.append(bucket)
            if reason.startswith("recent_"):
                returned_recent += 1
            continue
        pointer = _render_pointer(
            bucket,
            estimated_tokens=entry_tokens,
            reason="hard_limit",
        )
        append_if_fits(pointers, pointer, limit=hard_tokens)

    reflection_candidates = _reflection_candidates(
        all_buckets,
        exclude_ids=selected_ids,
        reference_time=reference,
    )
    for bucket in reflection_candidates:
        rendered, entry_tokens = render_stored_bucket(
            bucket,
            f"🔎 [自动精读] [bucket_id:{bucket['id']}]",
        )
        if append_extension_if_fits(
            reflection_results,
            rendered,
            section_limit=reflection_tokens,
            total_limit=reflection_hard_tokens,
        ):
            context_buckets.append(bucket)
            continue
        pointer = (
            f"🔎 [自动精读] ↗ [未展开] [bucket_id:{bucket['id']}] "
            f"[estimated_tokens:{entry_tokens}] [reason:reflection_budget] "
            f"{(bucket.get('metadata') or {}).get('name') or bucket['id']}"
        )
        append_extension_if_fits(
            reflection_results,
            pointer,
            section_limit=reflection_tokens,
            total_limit=reflection_hard_tokens,
        )

    if context_buckets and feel_tokens > 0:
        feels = [
            bucket
            for bucket in all_buckets
            if (bucket.get("metadata") or {}).get("type") == "feel"
        ]
        source_ids = {str(bucket.get("id") or "") for bucket in context_buckets}
        reference_text = "\n\n".join(
            str(bucket.get("content") or "") for bucket in context_buckets
        )
        selected_feels, vector_ok, feel_diagnostics = await select_startup_feels(
            feels,
            source_ids=source_ids,
            reference_text=reference_text,
        )
        if reference_text and not vector_ok:
            feel_note = (
                "[检索降级：语义索引暂不可用，本次仅按关键词相关性补足。]"
            )
        feel_omitted = 0
        for index, (feel, reason) in enumerate(selected_feels):
            meta = feel.get("metadata") or {}
            created = str(meta.get("created") or "")
            if reason == "direct_source":
                source_bucket = str(meta.get("triggered_by") or "")
                prefix = (
                    f"💗 [直属感受] [bucket_id:{feel['id']}] "
                    f"[source_bucket:{source_bucket}] [{created}]"
                )
            else:
                prefix = f"💭 [相关感受] [bucket_id:{feel['id']}] [{created}]"
            rendered, _entry_tokens = render_stored_bucket(feel, prefix)
            if not append_extension_if_fits(
                feel_results,
                rendered,
                section_limit=feel_tokens,
                total_limit=total_hard_tokens,
            ):
                feel_omitted = len(selected_feels) - index
                break
        if feel_omitted:
            append_notice_if_fits(
                f"有 {feel_omitted} 条相关 feel 因独立预算不足未返回；正文未截断。",
                limit=total_hard_tokens,
            )
        same_theme = int(feel_diagnostics.get("same_theme") or 0)
        negative_saturation = int(
            feel_diagnostics.get("negative_saturation") or 0
        )
        if same_theme or negative_saturation:
            details = []
            if same_theme:
                details.append(f"{same_theme} 条同主题")
            if negative_saturation:
                details.append(f"{negative_saturation} 条同方向负面")
            append_notice_if_fits(
                "启动 feel 已跳过" + "、".join(details) + "候选；没有为凑数返回。",
                limit=total_hard_tokens,
            )

    if total_recent > returned_recent:
        notice = (
            f"最近24小时另有 {total_recent - returned_recent} 条记忆"
            f"未进入本次 {max_results} 条正文名额。"
        )
        append_notice_if_fits(notice, limit=total_hard_tokens)
    if total_plans > expanded_plans:
        notice = (
            f"有 {total_plans - expanded_plans} 条活动计划未展开，"
            "可用 breath_advanced(domain=\"plan\") 读取。"
        )
        append_notice_if_fits(notice, limit=total_hard_tokens)
    if pointers:
        notice = f"有 {len(pointers)} 条记忆只列索引；可按 bucket_id 精准读取正文。"
        append_notice_if_fits(notice, limit=total_hard_tokens)

    output = compose()
    if count_tokens_approx(output) > total_hard_tokens:  # defensive invariant
        rt.logger.error("startup breath envelope exceeded hard token cap")
        return "一键睁眼暂时无法在配置预算内安全返回。"
    return output
