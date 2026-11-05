"""Conservative, source-bound expiry for promises limited to this visit.

No model calls, geolocation, bucket mutation or inferred travel. Only explicit
spoken self-reports produce events. Absence, elapsed time and another place
being mentioned never prove departure.
"""

from datetime import datetime, timezone
import hashlib
import json
import re

VERSION = "visit-expiry-v1"
_PLACE = r"[\u4e00-\u9fffA-Za-z0-9·]{2,24}?"
_NONREAL = r"想象|幻想|希望|但愿|假装|设想|脑补|做梦|梦见|以为|好像|似乎|倘若|万一|假设|如果|假如|要是"
_FUTURE_VISIT = r"明年|后年|下年|来年|明月|下月|下个月|下周|下星期|将来|暑假|寒假|每年|每月|每周|每次|每回|以后|下次|\d{4}[-年]|\d+月\d+日"
_PAST = re.compile(
    r"昨天|昨晚|前天|前年|去年|上次|上回|上趟|上一趟|上周|上星期|上个月|上月|回忆|曾经|以前|当年|当时|那时候|那时|那年|那天|前几天|[一二三四五六七八九十百两\d]+(?:天|年|个月|周)前"
)
_UNSAFE = re.compile(
    r'[“”‘’"\'「」『』？?]|'
    + _NONREAL
    + "|"
    + _FUTURE_VISIT
    + r"|可能|也许|大概|打算|准备|将要|明天|后天|不|没|未|别|听说|他说|她说|转述|台词"
)
_BIND_UNSAFE = re.compile(
    r'[“”‘’"\'「」『』？?]|' + _NONREAL + "|" + _FUTURE_VISIT + r"|永远|一直|不|没|未|别|听说|他说|她说|转述|台词"
)


def stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value))
        return (parsed.astimezone() if parsed.tzinfo is None else parsed).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _place(value):
    # Reject verbs/pronouns accidentally swallowed by a broad place capture.
    if (
        not isinstance(value, str)
        or not value
        or re.search(r"我|我们|你|爸爸|顾凛|怀里|身边|吃|喝|买|逛|想|打算|准备|已经|正在|期间|时候", value)
    ):
        return None
    return value


def _clauses(text):
    return re.finditer(r"[^，,；;。！？!?\n]+", text)


def _binding(text, *, strict_owner=True, speaker="知知", aliases=()):
    if (
        _BIND_UNSAFE.search(text)
        or _PAST.search(text)
        or re.search(r"等.{0,40}(?:到|来|去)|到时候|到了.{0,20}(?:之后|以后)", text)
    ):
        return None
    patterns = (
        ("departure", r"离开(?P<place>" + _PLACE + r")(?:之)?前"),
        ("this_visit", r"(?:这次|本次)在(?P<place>" + _PLACE + r")期间"),
        ("period", r"在(?P<place>" + _PLACE + r")期间"),
        ("this_visit", r"(?:这次|本次)(?:来|在)(?P<place>" + _PLACE + r")(?:时|的时候)"),
    )

    def owner_ok(match):
        if not strict_owner:
            return True
        parts = re.split(r"[，,；;。！？!?\n]", text[: match.start()])
        prefix = parts[-1].strip()
        subjects = ("你", "知知") if speaker == "顾凛" else ("我们", "我", "知知", *aliases)
        subject = "|".join(re.escape(v) for v in subjects)
        if len(parts) > 1 and not re.match(r"(?:" + subject + r")", prefix):
            return False  # A comma cannot reset a third person's visit to ours.
        modifiers = r"(?:明天|今天|后天|这次|本次|先|还|要|会|想|打算|决定|答应|提出|约好|说)*"
        return re.fullmatch(r"(?:(?:" + subject + r"))?" + modifiers, prefix) is not None

    for mode, pattern in patterns:
        match = re.search(pattern, text)
        if match and _place(match["place"]) and owner_ok(match):
            return match["place"], match.start(), match.end(), mode
    match = re.search(r"离开(?:之)?前", text)
    return ("", match.start(), match.end(), "departure") if match and owner_ok(match) else None


def events_for_day(day, aliases=()):
    day.require_dispatch_ready()
    subjects = "|".join(re.escape(v) for v in ("我们", "我", *aliases))
    own = r"(?:" + subjects + r")"
    patterns = (
        ("departed", own + r"(?:已经|刚刚|刚|现在)?离开(?:了)?(?P<place>" + _PLACE + r")(?:了|啦|咯)$"),
        ("departed", own + r"已经离开(?:了)?(?P<place>" + _PLACE + r")$"),
        ("departed", own + r"(?:已经|刚刚|刚)?从(?P<place>" + _PLACE + r")回到家了$"),
        ("arrived", own + r"(?:已经|刚刚|刚)?(?:到达|到了)(?P<place>" + _PLACE + r")(?:了|啦)?$"),
        ("present", own + r"(?:现在|还|正)?在(?P<place>" + _PLACE + r")$"),
    )
    result = []
    for source in day.messages:
        if (
            source.speaker != "知知"
            or not source.conversation_id
            or not stamp(source.at)
            or _UNSAFE.search(source.text)
            or _PAST.search(source.text)
        ):
            continue
        for clause in _clauses(source.text):
            text = clause.group().strip()
            for kind, pattern in patterns:
                match = re.fullmatch(pattern, text)
                if not match or not _place(match["place"]):
                    continue
                evidence = {
                    "source_id": source.source_id,
                    "source_sha256": _digest(source.text),
                    "at": source.at,
                    "speaker": source.speaker,
                    "start": clause.start(),
                    "end": clause.end(),
                }
                value = {
                    "kind": kind,
                    "place": match["place"],
                    "at": source.at,
                    "conversation_id": source.conversation_id,
                    "evidence": evidence,
                }
                result.append({"id": _digest(value), **value})
                break
    return result


def scope_for_card(card, day, events, aliases=()):
    """Bind only a matching explicit visit condition in summary AND evidence."""
    if card.get("kind") != "commitment":
        return None
    summary = _binding(card["text"], strict_owner=False)
    if summary is None:
        return None
    sources = {s.source_id: s for s in day.messages}
    positions = {d.msg_uuid: i for i, d in enumerate(day.coverage)}
    for ev in card["evidence"]:
        source = sources.get(ev["source_id"])
        if not source or not source.conversation_id or _digest(source.text) != ev["source_sha256"]:
            continue
        original = source.text[ev["start"] : ev["end"]]
        binding = _binding(original, speaker=source.speaker, aliases=aliases)
        if binding is None or (summary[0] and binding[0] != summary[0]):
            continue
        at = stamp(source.at)
        if at is None:
            continue
        context = []
        for event in events:
            evidence = event["evidence"]
            index = positions.get(evidence["source_id"])
            end = positions.get(source.source_id)
            if (
                event["conversation_id"] != source.conversation_id
                or index is None
                or end is None
                or index > end
                or stamp(event["at"]) > at
                or (index == end and evidence["end"] > ev["start"] + binding[1])
                or any(d.status != "allowed" for d in day.coverage[index : end + 1])
            ):
                continue
            context.append(event)
        context.sort(key=lambda e: (stamp(e["at"]), e["evidence"]["start"]))
        current = None
        for event in context:
            if event["kind"] in ("arrived", "present"):
                current = event
            elif current and event["place"] == current["place"]:
                current = None
        place = binding[0] or (current["place"] if current else None)
        if not place:
            continue
        if binding[3] == "period" and (not current or current["place"] != place):
            continue  # Bare "在X期间" alone does not prove this visit is underway.
        bound_end = ev["start"] + binding[2]
        anchor = {
            "source_id": source.source_id,
            "source_sha256": ev["source_sha256"],
            "at": source.at,
            "speaker": source.speaker,
            "start": ev["start"] + binding[1],
            "end": bound_end,
        }
        return {
            "version": VERSION,
            "place": place,
            "bound_at": source.at,
            "memory_day": day.day,
            "selection_revision": day.selection_revision,
            "conversation_id": source.conversation_id,
            "binding_evidence": anchor,
            "context_evidence": current["evidence"] if current and current["place"] == place else None,
            "visit_id": current["id"] if current and current["place"] == place else _digest([place, anchor]),
        }
    return None


def expiry(scope, events, *, as_of=None):
    now = stamp(as_of or datetime.now(timezone.utc))
    if not isinstance(scope, dict) or scope.get("version") != VERSION or not now:
        return None
    bound = stamp(scope.get("bound_at"))
    if not bound or not _place(scope.get("place")):
        return None
    anchor = scope.get("binding_evidence") or {}
    matches = []
    for event in events:
        at = stamp(event.get("at"))
        ev = event.get("evidence") or {}
        after = at and (
            at > bound
            or (
                at == bound
                and ev.get("source_id") == anchor.get("source_id")
                and ev.get("start", -1) >= anchor.get("end", float("inf"))
            )
        )
        if (
            event.get("kind") == "departed"
            and event.get("place") == scope["place"]
            and after
            and at <= now
            and (not scope.get("conversation_id") or event.get("conversation_id") == scope["conversation_id"])
        ):
            matches.append(event)
    if not matches:
        return None
    event = min(matches, key=lambda e: (stamp(e["at"]), e["evidence"]["start"]))
    return {
        "reason": "left_bound_location",
        "place": scope["place"],
        "expired_at": event["at"],
        "visit_id": scope.get("visit_id"),
        "departure_evidence": event["evidence"],
    }


def plan_scope(bucket):
    meta = bucket.get("metadata") or {}
    content = bucket.get("content") or ""
    saved = meta.get("clerk_location_scope")
    if isinstance(saved, dict) and meta.get("clerk_location_content_sha256") == _digest(content):
        return saved
    binding = _binding(content)
    at = meta.get("updated_at") or meta.get("created_at") or meta.get("created")
    if not binding or not binding[0] or binding[3] == "period" or not stamp(at):
        return None
    return {
        "version": VERSION,
        "place": binding[0],
        "bound_at": at,
        "conversation_id": None,
        "visit_id": _digest([bucket.get("id"), at, binding[0]]),
    }


def confirmation_scope(candidate, content):
    """Rewording a candidate never transfers its visit to unrelated promises."""
    scope = candidate.get("location_scope")
    if not isinstance(scope, dict) or _BIND_UNSAFE.search(content):
        return None
    if content.strip() == candidate["text"].strip():
        return scope
    binding = _binding(content)
    return scope if binding and binding[0] in ("", scope.get("place")) else None


def plan_state(bucket, events=(), *, as_of=None):
    status = str((bucket.get("metadata") or {}).get("status") or "active").lower()
    if status not in ("active", "resolved", "abandoned"):
        status = "active"
    expired = expiry(plan_scope(bucket), events, as_of=as_of) if status == "active" else None
    return {"effective_status": "expired" if expired else status, "expiration": expired}
