"""Clerk startup order, sharing the existing complete Breath envelope cap."""
from datetime import datetime
import re

from ._envelope import DAILY_IMPRESSION_SENTINEL
from ._verbatim import render_stored_bucket
from .feel import select_startup_feels
from plan_dates import plan_date_sort, plan_date_state
from .._plan_state import active_plan, plan_reader
from utils import count_tokens_approx


def normalized(text):
    return re.sub(r"\s+", "", str(text or "")).strip()


async def surface_clerk_startup(all_buckets, *, max_results, hard_tokens, reference_time,
                                daily_impression, daily_factual_texts, private_continuity,
                                nightly_dream, continuity_tokens, dream_tokens,
                                reflection_tokens, feel_tokens, exclude_older_id):
    from .startup import _is_core, _created_timestamp, _select_memories, _render_plan, _render_plan_pointer
    reference = reference_time or datetime.now()
    cap = hard_tokens + continuity_tokens + dream_tokens + reflection_tokens + feel_tokens
    names = ["核心准则", "尚未解决的冲突", "昨日印象", "活动计划", "最近24小时", "旧事联想", "相关 feel", "昨夜梦境", "未展开（按需读取）"]
    sections = {name: [] for name in names}
    def compose():
        parts = ["=== 一键睁眼 ==="]
        for name in names:
            if sections[name]:
                header = DAILY_IMPRESSION_SENTINEL if name == "昨日印象" else f"=== {name} ==="
                parts.append(header + "\n" + "\n---\n".join(sections[name]))
        return "\n\n".join(parts)
    def append(name, text, *, section_cap=None):
        sections[name].append(text)
        if (count_tokens_approx(compose()) <= cap
                and (section_cap is None or count_tokens_approx("\n".join(sections[name])) <= section_cap)):
            return True
        sections[name].pop()
        return False
    def pointer(text):
        append("未展开（按需读取）", text)
    for bucket in sorted((b for b in all_buckets if _is_core(b)), key=_created_timestamp):
        body, _ = render_stored_bucket(bucket, f"📌 [核心准则] [bucket_id:{bucket['id']}]")
        if not append("核心准则", body, section_cap=hard_tokens):
            pointer(f"↗ 核心准则 [bucket_id:{bucket['id']}]")
    if private_continuity and not append("尚未解决的冲突", private_continuity, section_cap=continuity_tokens):
        pointer("⚠ 冲突正文待读取，不能把未展开当成和好。请在 Dashboard 的冲突页查看。")
    if daily_impression and not append("昨日印象", daily_impression):
        pointer('↗ 昨日印象：breath_search(domain="daily_impression")')
    read_plan = plan_reader(reference_time=reference)
    plans = [b for b in all_buckets if (b.get("metadata") or {}).get("type") == "plan"
             and active_plan(b, reader=read_plan)
             and not any((b.get("metadata") or {}).get(k) for k in ("deleted_at", "tombstone", "dont_surface"))]
    plans.sort(key=lambda b: (*plan_date_sort(b.get("metadata") or {}, reference_time=reference), str(b.get("id"))))
    for bucket in plans:
        state = plan_date_state(bucket.get("metadata") or {}, reference_time=reference)
        text = (_render_plan(bucket, reference_time=reference) if state["expand"]
                else _render_plan_pointer(bucket, reference_time=reference))
        if not append("活动计划", f"[{state['label']}] {text}", section_cap=1200):
            pointer("↗ 其余计划：breath_search(domain=\"plan\")（含日期待定／过期项目）")
            break
    # Conservative display-only dedup. A related feeling is never removed.
    represented = {normalized(text) for text in daily_factual_texts if normalized(text)}
    eligible = [b for b in all_buckets if (b.get("metadata") or {}).get("type") in ("feel", "plan", "permanent")
                or normalized(b.get("content")) not in represented]
    selected, _ = _select_memories(eligible, max_results=min(4, max_results), reference_time=reference,
                                    exclude_older_id=exclude_older_id)
    context = []
    for bucket, reason in selected:
        name = "最近24小时" if reason.startswith("recent_") else "旧事联想"
        text, _ = render_stored_bucket(bucket, f"[bucket_id:{bucket['id']}]")
        if append(name, text):
            context.append(bucket)
        else:
            pointer(f"↗ [bucket_id:{bucket['id']}]")
    if feel_tokens > 0:
        feels = [b for b in all_buckets if (b.get("metadata") or {}).get("type") == "feel"
                 and not any((b.get("metadata") or {}).get(k) for k in ("deleted_at", "tombstone", "dont_surface"))]
        deduplicated_ids = {str(b.get("id")) for b in all_buckets
                            if (b.get("metadata") or {}).get("type") != "feel" and normalized(b.get("content")) in represented}
        selected_feels, _, _ = await select_startup_feels(
            feels, source_ids={str(b.get("id")) for b in context} | deduplicated_ids,
            reference_text="\n".join(str(b.get("content") or "") for b in context) or daily_impression)
        if not selected_feels and feels:
            selected_feels = [(max(feels, key=_created_timestamp), "independent_feel")]
        if selected_feels:
            feel = selected_feels[0][0]
            text, _ = render_stored_bucket(feel, f"💗 [bucket_id:{feel['id']}]")
            if not append("相关 feel", text, section_cap=feel_tokens):
                pointer(f"↗ feel [bucket_id:{feel['id']}]")
    if nightly_dream and dream_tokens > 0 and not append("昨夜梦境", nightly_dream, section_cap=dream_tokens):
        pointer("↗ 昨夜梦境：请在 Dashboard 的梦境页读取。")
    output = compose()
    assert count_tokens_approx(output) <= cap
    return output
