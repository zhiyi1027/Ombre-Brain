"""Explicit bounded-window previews; never claims the archive history is complete.

This opt-in helper is not registered with the scheduler or production routes.
Unknown left boundaries remain local holds until same-conversation aftercare
and a stable observed boundary: either two spaced ordinary messages after tail
buffering, or a confirmed in-window scene closure, plus reopening observation.
The ordinary keyword classifier remains a conservative heuristic, not a proof.
"""
from dataclasses import replace
from datetime import date, datetime
import unicodedata

from clerk_input import select_clerk_day
from clerk_privacy import (Decision, PrivacyInputError, PrivacyPolicy, _AFTERCARE,
                           _FORMATTING, _ORDINARY, _STRONG, _WEAK, _aware,
                           _digest, _filter_timeline, _messages)

WINDOW_VERSION = "bounded-window-preview-v2-timed-fallback"


def prepare_window_day(rows, memory_day: date, *, window_start: datetime,
                       window_end: datetime, snapshot_complete: bool = False,
                       timezone_name="Asia/Shanghai", cutoff_hour=4, policy=None):
    """Classify one complete *visible range* while holding unknown history.

    snapshot_complete means an atomic, assembled visible-range read with no
    pending fragments, not absence of historical deletion/withdrawal gaps.
    All outgoing extraction requests retain missing_context=True, regardless
    of whether the prefix holds happen to fall outside the selected memory day.
    """
    if snapshot_complete is not True or type(memory_day) is not date:
        raise PrivacyInputError("complete visible-window snapshot required")
    start, end = _aware(window_start), _aware(window_end)
    if start >= end:
        raise PrivacyInputError("invalid preview window")
    policy = policy or PrivacyPolicy(close_on_timeout=True)
    rows = tuple(rows)
    messages = _messages(rows)
    if any(m.at < start or m.at >= end for m in messages):
        raise PrivacyInputError("source outside authorized window")
    # No full-history assertion: actual detected scenes keep the existing global
    # mask, while additional per-conversation prefix holds represent uncertainty.
    result = _filter_timeline(rows, as_of=end, policy=policy)
    states = {}
    released = {}
    for message in messages:
        cid, at = message.conv_uuid, message.at
        state = states.setdefault(cid, {"care": None, "ordinary": [], "candidate": None})
        if cid in released:
            continue
        candidate = state["candidate"]
        if candidate is not None and at > candidate + policy.quiet_candidate:
            released[cid] = candidate + policy.quiet_candidate
            continue
        content = _FORMATTING.sub("", unicodedata.normalize("NFKC", message.text))
        signal = any(w in content for w in (*_STRONG, *_WEAK))
        care = any(w in content for w in _AFTERCARE)
        if signal:
            state.update(care=None, ordinary=[], candidate=None)
        elif care:
            state.update(care=at, ordinary=[], candidate=None)
        elif state["candidate"] is None:
            if state["care"] is not None and at >= state["care"] + policy.tail_buffer and any(w in content for w in _ORDINARY):
                state["ordinary"].append(at)
                if (len(state["ordinary"]) >= policy.ordinary_messages
                        and at - state["ordinary"][0] >= policy.ordinary_span):
                    state["candidate"] = at
            else:
                state["ordinary"].clear()
    for cid, state in states.items():
        candidate = state["candidate"]
        if cid not in released and candidate is not None and end > candidate + policy.quiet_candidate:
            released[cid] = candidate + policy.quiet_candidate
    # An actual confirmed in-window scene followed by observed aftercare and a
    # stable closure also establishes a boundary. This is not a fabricated
    # positive marker and cannot turn an unobserved start into a safe prefix.
    for interval in result.intervals:
        if (interval.confirmed and interval.opened_at >= start and interval.saw_aftercare
                and interval.end is not None and end > interval.end + policy.quiet_candidate):
            at = interval.end + policy.quiet_candidate
            released[interval.conv_uuid] = min(released.get(interval.conv_uuid, at), at)
    by_id = {m.msg_uuid: m for m in messages}
    coverage = []
    for decision in result.decisions:
        message = by_id[decision.msg_uuid]
        if decision.status == "allowed":
            boundary = released.get(message.conv_uuid)
            if boundary is None or message.at <= boundary:
                decision = Decision(message.msg_uuid, message.at, "uncertain_hold", "unknown_left_boundary")
            elif message.at >= end - max(policy.pre_roll, policy.quiet_candidate):
                decision = Decision(message.msg_uuid, message.at, "uncertain_hold", "window_tail_observation")
        coverage.append(decision)
    result = replace(result, decisions=tuple(coverage),
                     policy_revision=_digest([result.policy_revision, WINDOW_VERSION, start.isoformat(), end.isoformat()]))
    day = select_clerk_day(result, memory_day, timezone_name=timezone_name, cutoff_hour=cutoff_hour)
    return replace(day, context_complete=False,
                   selection_revision=_digest([day.selection_revision, WINDOW_VERSION, False]))


CONTEXTUAL_WINDOW_VERSION = "bounded-window-contextual-v10"


def prepare_contextual_window_day(rows, memory_day: date, *, window_start: datetime,
                                  window_end: datetime, snapshot_complete: bool = False,
                                  timezone_name="Asia/Shanghai", cutoff_hour=4):
    """Isolated candidate profile; no route/scheduler activation or model calls.

    Establish one same-conversation boundary from sustained concrete statements
    by the user, with no sexual/specific-care restart during observation. The
    core final mask takes precedence, including all provisional reopening and
    pre-roll. This never rescues an isolated health sentence from that mask.
    """
    from clerk_contextual import features
    if snapshot_complete is not True or type(memory_day) is not date:
        raise PrivacyInputError("complete visible-window snapshot required")
    start, end = _aware(window_start), _aware(window_end)
    if start >= end:
        raise PrivacyInputError("invalid preview window")
    rows = tuple(rows)
    messages = _messages(rows)
    if any(m.at < start or m.at >= end for m in messages):
        raise PrivacyInputError("source outside authorized window")
    policy = PrivacyPolicy(contextual_signals=True)
    result = _filter_timeline(rows, as_of=end, policy=policy)
    allowed = {d.msg_uuid for d in result.decisions if d.status == "allowed"}
    core_decisions = {d.msg_uuid: d for d in result.decisions}
    states, released, observations = {}, {}, {}
    for message in messages:
        cid, at = message.conv_uuid, message.at
        if cid in released:
            continue
        state = states.setdefault(cid, {"last_block": start, "anchors": [], "observed": None, "actual_seen": False})
        f = features(message.text)
        if state["observed"] is not None and at > state["observed"] + policy.quiet_candidate:
            released[cid] = state["anchors"][0]
            observations[cid] = state["observed"] + policy.quiet_candidate
            continue
        core_decision = core_decisions[message.msg_uuid]
        known_scene = core_decision.reason in ("private_interval", "unconfirmed_private_boundary", "post_private_observation")
        if known_scene and (f["explicit"] or f["families"] or f["specific_care"]):
            state.update(last_block=at, anchors=[], observed=None, actual_seen=False)
        elif known_scene and f["generic_care"] and state["observed"] is None:
            state["anchors"].clear()
            state["actual_seen"] = False
        elif (message.msg_uuid in allowed and message.speaker == "知知" and f["ordinary_anchor"]
                and at - state["last_block"] >= policy.timeout_candidate
                and state["observed"] is None):
            state["anchors"].append(at)
            state["actual_seen"] |= f["actual_action_anchor"]
            if (len(state["anchors"]) >= policy.ordinary_messages
                    and at - state["anchors"][0] >= policy.ordinary_span and state["actual_seen"]):
                state["observed"] = at
    for cid, state in states.items():
        if (cid not in released and state["observed"] is not None
                and end > state["observed"] + policy.quiet_candidate):
            released[cid] = state["anchors"][0]
            observations[cid] = state["observed"] + policy.quiet_candidate
    coverage = []
    by_id = {m.msg_uuid: m for m in messages}
    for decision in result.decisions:
        message = by_id[decision.msg_uuid]
        if decision.status == "allowed":
            boundary = released.get(message.conv_uuid)
            if boundary is None or message.at < boundary:
                decision = Decision(message.msg_uuid, message.at, "uncertain_hold", "unknown_left_boundary")
            elif (message.at <= observations[message.conv_uuid]
                    and not (message.speaker == "知知" and features(message.text)["ordinary_anchor"])):
                decision = Decision(message.msg_uuid, message.at, "uncertain_hold", "unknown_boundary_observation")
            elif message.at >= end - max(policy.pre_roll, policy.quiet_candidate):
                decision = Decision(message.msg_uuid, message.at, "uncertain_hold", "window_tail_observation")
        coverage.append(decision)
    result = replace(result, decisions=tuple(coverage), policy_revision=_digest([
        result.policy_revision, CONTEXTUAL_WINDOW_VERSION, start.isoformat(), end.isoformat()]))
    day = select_clerk_day(result, memory_day, timezone_name=timezone_name, cutoff_hour=cutoff_hour)
    return replace(day, context_complete=False, selection_revision=_digest([
        day.selection_revision, CONTEXTUAL_WINDOW_VERSION, False, "privacy-review-required"]),
        privacy_review_required=True)
