"""Local, conservative transcript selection for the future night clerk.

This module has no storage, model, network, or logging side effects. It takes
an explicitly complete, assembled timeline, detects intervals before day or
chunk boundaries are applied, and returns decisions without quoting content.
Keyword detection is a heuristic, not a guarantee of perfect recall.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import heapq
import json
import re
import unicodedata
from typing import Any, Iterable, Mapping


POLICY_VERSION = "clerk-privacy-v3-visible-text"
VISIBLE_TEXT_POLICY = "visible-text-only-v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,159}$")
_STRONG = (
    "做爱", "性爱", "口交", "性交", "抽插", "高潮", "射精",
    "阴蒂", "阴道", "龟头", "鸡巴", "肉棒", "穴口", "私处",
)
# Even one of these starts an uncertain interval. Dense/long signals promote
# it to an intentional skip; isolated ambiguous passages are held locally.
_WEAK = (
    "舌尖", "喘息", "呻吟", "腿间", "大腿内侧", "褪下", "脱掉",
    "贴紧", "抚摸", "含住", "欲望", "硬挺", "湿润", "颤抖", "亲吻", "吻",
)
_AFTERCARE = (
    "热毛巾", "毛巾", "擦", "喝水", "疼不疼", "还疼", "抱着睡",
    "抱好", "清理", "不舒服吗", "累不累", "睡一会", "晚安",
)
_ORDINARY = (
    "上班", "开会", "买菜", "早餐", "午饭", "晚饭", "代码", "测试",
    "文件", "快递", "天气", "公交", "机票", "课程", "超市", "洗衣服",
)
_FORMATTING = re.compile(r"[\s*_~\u200b-\u200f\u2060\ufeff]+")


class PrivacyInputError(ValueError):
    """Invalid or incomplete input. Error messages never contain source text."""


class VisibilityStateError(ValueError):
    code = "privacy_state_revalidation_required"


def _aware(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise PrivacyInputError("privacy timeline requires aware timestamps") from None


@dataclass(frozen=True)
class PrivacyPolicy:
    pre_roll: timedelta = timedelta(minutes=10)
    tail_buffer: timedelta = timedelta(minutes=10)
    timeout_candidate: timedelta = timedelta(minutes=30)
    quiet_candidate: timedelta = timedelta(minutes=30)
    ordinary_span: timedelta = timedelta(minutes=2)
    ordinary_messages: int = 2
    long_chars: int = 300
    dense_hits: int = 3
    rolling_window: timedelta = timedelta(minutes=10)
    close_on_timeout: bool = False
    contextual_signals: bool = False

    def __post_init__(self) -> None:
        if type(self.close_on_timeout) is not bool:
            raise PrivacyInputError("invalid timeout closure policy")
        if type(self.contextual_signals) is not bool or self.contextual_signals and self.close_on_timeout:
            raise PrivacyInputError("contextual privacy requires topic-confirmed closure")
        durations = (self.pre_roll, self.tail_buffer, self.timeout_candidate,
                     self.quiet_candidate, self.ordinary_span, self.rolling_window)
        if any(not isinstance(v, timedelta) or v <= timedelta(0) for v in durations):
            raise PrivacyInputError("privacy durations must be positive")
        if (type(self.ordinary_messages) is not int or self.ordinary_messages < 2
                or type(self.long_chars) is not int or self.long_chars < 1
                or type(self.dense_hits) is not int or self.dense_hits < 2):
            raise PrivacyInputError("invalid privacy thresholds")

    def revision(self) -> str:
        values = {key: value.total_seconds() if isinstance(value, timedelta) else value
                  for key, value in vars(self).items()}
        if self.contextual_signals:
            from clerk_contextual import CONTEXT_VERSION, policy_terms
            return _digest([CONTEXT_VERSION, values, _STRONG, _FORMATTING.pattern, policy_terms()])
        # Preserve the exact legacy/cache identity while the new profile is
        # being evaluated separately. No historical exclusion state is erased.
        values.pop("contextual_signals")
        return _digest([POLICY_VERSION, values, _STRONG, _WEAK, _AFTERCARE, _ORDINARY,
                        _FORMATTING.pattern])


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Message:
    msg_uuid: str
    conv_uuid: str
    speaker: str
    at: datetime
    text: str = field(repr=False)


@dataclass(frozen=True)
class Decision:
    msg_uuid: str
    at: datetime
    status: str
    reason: str


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime | None
    confirmed: bool
    last_sensitive: datetime
    saw_aftercare: bool
    opened_at: datetime
    conv_uuid: str | None
    observed_close: datetime | None = None
    kind: str = "scene"


@dataclass(frozen=True)
class PositiveMarker:
    at: datetime
    conv_uuid: str | None = None


@dataclass(frozen=True)
class PrivacyResult:
    messages: tuple[Message, ...] = field(repr=False)
    decisions: tuple[Decision, ...]
    intervals: tuple[Interval, ...]
    policy_revision: str
    source_revision: str
    selection_revision: str
    policy_version: str = POLICY_VERSION

    def allowed_messages(self) -> tuple[Message, ...]:
        allowed = {d.msg_uuid for d in self.decisions if d.status == "allowed"}
        return tuple(m for m in self.messages if m.msg_uuid in allowed)

    def receipt(self) -> dict[str, Any]:
        """Restricted metadata only; caller must keep this outside public logs."""
        return {
            "policy_version": self.policy_version,
            "policy_revision": self.policy_revision,
            "source_revision": self.source_revision,
            "selection_revision": self.selection_revision,
            "counts": dict(Counter(d.status for d in self.decisions)),
            "intervals": [{"start": i.start.isoformat(),
                           "end": i.end.isoformat() if i.end else None,
                           "opened_at": i.opened_at.isoformat(), "conv_uuid": i.conv_uuid}
                          for i in self.intervals],
            "coverage": [{"msg_uuid": d.msg_uuid, "at": d.at.isoformat(),
                          "status": d.status, "reason": d.reason}
                         for d in self.decisions],
        }


def _messages(rows: Iterable[Mapping[str, Any]]) -> tuple[Message, ...]:
    seen: dict[str, Message] = {}
    for row in rows:
        if (not isinstance(row, Mapping)
                or any(key in row for key in ("fragment_index", "fragment_count", "fragment_digest"))):
            raise PrivacyInputError("privacy input requires complete assembled messages")
        text = row.get("text")
        if not isinstance(text, str):
            raise PrivacyInputError("privacy input requires visible text")
        if not text.strip():
            continue  # A native-thinking-only turn is not a spoken message.
        mid, cid, speaker = (row.get(k) for k in ("msg_uuid", "conv_uuid", "speaker"))
        if (not isinstance(mid, str) or not _ID.fullmatch(mid)
                or not isinstance(cid, str) or not _ID.fullmatch(cid)
                or speaker not in ("知知", "顾凛")
                or not isinstance(row.get("text"), str)):
            raise PrivacyInputError("privacy input has invalid message fields")
        message = Message(mid, cid, speaker, _aware(row.get("at")), text)
        if mid in seen and seen[mid] != message:
            raise PrivacyInputError("privacy input has conflicting message revisions")
        seen[mid] = message
    return tuple(sorted(seen.values(), key=lambda m: (m.at, m.msg_uuid)))


def filter_transcript(
    rows: Iterable[Mapping[str, Any]], *, as_of: datetime,
    history_complete: bool = False, policy: PrivacyPolicy | None = None,
    positive_markers: Iterable[datetime | PositiveMarker] = (),
    withheld_ids: Iterable[str] = (),
) -> PrivacyResult:
    """Classify a full snapshot; never infer an initial state from a day slice.

    A positive marker is a timestamped, trusted start signal. There is no
    dependency on a local flag file, and absent markers never mean 'safe'.
    Every invocation recomputes the selection. Revisions MUST be checked by
    any future cache before reusing a derived artifact.
    """
    if history_complete is not True:
        raise PrivacyInputError("complete timeline required before clerk selection")
    return _filter_timeline(rows, as_of=as_of, policy=policy,
                            positive_markers=positive_markers, withheld_ids=withheld_ids)


def _filter_timeline(rows, *, as_of, policy=None, positive_markers=(), withheld_ids=()):
    """Shared classifier; callers must supply either full-history or window guards."""
    policy = policy or PrivacyPolicy()
    if policy.contextual_signals:
        from clerk_contextual import filter_contextual
        return filter_contextual(rows, as_of=as_of, policy=policy,
                                 positive_markers=positive_markers, withheld_ids=withheld_ids)
    now = _aware(as_of)
    messages = _messages(rows)
    markers = set()
    for value in positive_markers:
        marker = value if isinstance(value, PositiveMarker) else PositiveMarker(_aware(value))
        if marker.conv_uuid is not None and not _ID.fullmatch(marker.conv_uuid):
            raise PrivacyInputError("invalid positive marker scope")
        markers.add((_aware(marker.at), marker.conv_uuid))
    markers = tuple(sorted(markers, key=lambda m: (m[0], m[1] or "")))
    withheld = frozenset(withheld_ids)
    if any(not isinstance(mid, str) or not _ID.fullmatch(mid) for mid in withheld):
        raise PrivacyInputError("invalid prior exclusion IDs")
    events = [(m.at, 1, m.msg_uuid, m) for m in messages]
    events.extend((at, 0, cid or "", None) for at, cid in markers)
    events.sort(key=lambda event: event[:3])
    segments: list[dict[str, Any]] = []
    active: dict[str | None, dict[str, Any]] = {}
    recently_closed: dict[str | None, dict[str, Any]] = {}
    weak_windows: dict[str | None, list[tuple[datetime, int]]] = {}
    previous_times: dict[str | None, datetime] = {}
    for at, event_kind, event_scope, message in events:
        cid = message.conv_uuid if message is not None else event_scope or None
        original = "" if message is None else message.text
        content = _FORMATTING.sub("", unicodedata.normalize("NFKC", original))
        visible = "" if message is None else _FORMATTING.sub("", unicodedata.normalize("NFKC", message.text))
        hits = sum(content.count(word) for word in _WEAK)
        strong = message is None or any(word in content for word in _STRONG)
        weak_window = weak_windows.setdefault(cid, [])
        weak_window = [(t, n) for t, n in weak_window if at - t <= policy.rolling_window]
        if hits:
            weak_window.append((at, hits))
        dense = (hits >= policy.dense_hits
                 or (len(content) >= policy.long_chars and hits >= 2)
                 or sum(n for _, n in weak_window) >= policy.dense_hits)
        signal = strong or hits > 0
        care = any(word in content for word in _AFTERCARE)
        visible_care = any(word in visible for word in _AFTERCARE)
        scene = active.get(cid)
        previous_scene = recently_closed.get(cid)
        if (policy.close_on_timeout and scene is not None
                and at > scene["last_sensitive"] + policy.timeout_candidate):
            # Explicit preview fallback: a deadline closes without requiring
            # the finite ordinary-topic vocabulary. Nearby care still reopens
            # this interval below, retrospectively withholding short replies.
            scene["end"] = scene["last_sensitive"] + policy.timeout_candidate
            recently_closed[cid] = scene
            active.pop(cid, None)
            previous_scene, scene = scene, None
            weak_window.clear()
        if (scene is None and previous_scene is not None and care
                and at - previous_scene["end"] <= policy.quiet_candidate):
            # Closing remains provisional: known nearby care resumes the same
            # interval, retrospectively excluding any intervening short replies.
            scene = previous_scene
            scene["end"] = None
            scene["ordinary"].clear()
            active[cid] = scene
            recently_closed.pop(cid, None)
        if signal:
            if scene is None:
                scene = {"start": at - policy.pre_roll, "end": None,
                         "confirmed": strong or dense, "last_sensitive": at,
                         "saw_aftercare": False, "ordinary": [], "opened_at": at,
                         "conv_uuid": cid}
                active[cid] = scene
                segments.append(scene)
            else:
                scene["confirmed"] |= strong or dense
                scene["last_sensitive"] = at
                scene["ordinary"].clear()
            # A fresh intimate signal after aftercare opens the scene again.
            scene["saw_aftercare"] = False
        elif scene is not None:
            if care:
                scene["saw_aftercare"] |= visible_care
                scene["last_sensitive"] = at
                scene["ordinary"].clear()
            else:
                previous_at = previous_times.get(cid)
                quiet = previous_at is not None and at - previous_at >= policy.quiet_candidate
                timed_out = at - scene["last_sensitive"] >= policy.timeout_candidate
                tail_done = at - scene["last_sensitive"] >= policy.tail_buffer
                # Timeout and silence only arm a possible closing phase.
                can_close = tail_done and (scene["saw_aftercare"] or timed_out or quiet)
                ordinary = any(word in visible for word in _ORDINARY)
                if can_close and ordinary:
                    scene["ordinary"].append(at)
                    evidence = scene["ordinary"]
                    if (len(evidence) >= policy.ordinary_messages
                            and at - evidence[0] >= policy.ordinary_span):
                        scene["end"] = at
                        recently_closed[cid] = scene
                        active.pop(cid, None)
                        weak_window.clear()
                else:
                    scene["ordinary"].clear()
        weak_windows[cid] = weak_window
        previous_times[cid] = at
    if policy.close_on_timeout:
        for scene in active.values():
            deadline = scene["last_sensitive"] + policy.timeout_candidate
            if now > deadline:
                scene["end"] = deadline
    intervals = sorted((Interval(s["start"], s["end"], s["confirmed"],
                                 s["last_sensitive"], s["saw_aftercare"],
                                 s["opened_at"], s["conv_uuid"]) for s in segments),
                       key=lambda i: i.start)

    decisions = _mask_intervals(messages, intervals, now, withheld, policy)
    source_revision = _digest([
        [m.msg_uuid, m.conv_uuid, m.speaker, m.at.isoformat(), m.text]
        for m in messages
    ])
    policy_revision = policy.revision()
    selection_revision = _digest([
        policy_revision, source_revision, [[at.isoformat(), cid] for at, cid in markers],
        sorted(withheld),
        [[d.msg_uuid, d.status, d.reason] for d in decisions],
    ])
    return PrivacyResult(messages, tuple(decisions), tuple(intervals),
                         policy_revision, source_revision, selection_revision)


def _mask_intervals(messages, intervals, now, withheld, policy):
    # The interval mask is global across conversations in this snapshot. This
    # deliberately over-excludes contemporaneous daily chat and duplicate feeds.
    decisions: list[Decision] = []
    hold_windows = sorted((i.end, (i.observed_close or i.end) + policy.quiet_candidate) for i in intervals
                          if i.kind == "scene" and i.end is not None
                          and now <= (i.observed_close or i.end) + policy.quiet_candidate)
    hold_index = 0
    hold_until = datetime.min.replace(tzinfo=timezone.utc)
    interval_index = 0
    active_intervals: set[int] = set()
    ending: list[tuple[float, int]] = []
    certain_until: list[tuple[float, int]] = []
    for message in messages:
        while hold_index < len(hold_windows) and hold_windows[hold_index][0] <= message.at:
            hold_until = max(hold_until, hold_windows[hold_index][1])
            hold_index += 1
        while interval_index < len(intervals) and intervals[interval_index].start <= message.at:
            interval = intervals[interval_index]
            active_intervals.add(interval_index)
            heapq.heappush(ending, (interval.end.timestamp() if interval.end else float("inf"), interval_index))
            if interval.confirmed:
                deadline = float("inf") if policy.contextual_signals else (interval.last_sensitive + policy.tail_buffer).timestamp()
                heapq.heappush(certain_until, (-deadline, interval_index))
            interval_index += 1
        while ending and ending[0][0] < message.at.timestamp():
            _, expired = heapq.heappop(ending)
            active_intervals.discard(expired)
        while certain_until and certain_until[0][1] not in active_intervals:
            heapq.heappop(certain_until)
        if message.msg_uuid in withheld:
            status, reason = "intentional_skip", "prior_private_exclusion"
        elif active_intervals:
            scenes = [intervals[i] for i in active_intervals if intervals[i].kind == "scene"]
            if policy.contextual_signals and not scenes:
                contents = [intervals[i] for i in active_intervals if intervals[i].kind == "content"]
                exact = any(i.opened_at == message.at and i.conv_uuid == message.conv_uuid for i in contents)
                status = "intentional_skip" if exact else "uncertain_hold"
                reason = ("private_content_message" if exact else "private_content_buffer") if contents else "unresolved_sensitive_context"
            else:
                certain = bool(certain_until and -certain_until[0][0] >= message.at.timestamp())
                status = "intentional_skip" if certain else "uncertain_hold"
                reason = "private_interval" if certain else "unconfirmed_private_boundary"
        elif message.at <= hold_until:
            status, reason = "uncertain_hold", "awaiting_close_context"
        elif message.at >= now - policy.pre_roll:
            status, reason = "uncertain_hold", "awaiting_context"
        else:
            status, reason = "allowed", "ordinary_context"
        decisions.append(Decision(message.msg_uuid, message.at, status, reason))
    return decisions
