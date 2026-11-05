"""One-way, raw-only input boundary for the future clerk adapter.

Not connected to the scheduler or existing daily-impression generator yet.
Callers supply a complete assembled snapshot; filtering happens before day
selection, quoting, splitting, or any call to an injected model adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import hashlib
import json
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from clerk_privacy import Decision, PositiveMarker, PrivacyInputError, PrivacyPolicy, PrivacyResult, POLICY_VERSION, filter_transcript


@dataclass(frozen=True)
class ClerkSource:
    source_id: str
    speaker: str
    at: str
    text: str = field(repr=False)
    conversation_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ClerkDayInput:
    day: str
    selection_revision: str
    messages: tuple[ClerkSource, ...] = field(repr=False)
    coverage: tuple[Decision, ...]
    context_complete: bool = True
    privacy_review_required: bool = False
    redaction_policy: str | None = None

    def require_dispatch_ready(self):
        if self.privacy_review_required:
            raise PrivacyInputError("privacy candidate requires explicit boundary review")

    def payload(self) -> dict[str, Any]:
        self.require_dispatch_ready()
        # Copy the whitelist, not the original row: thinking, titles, attachment
        # metadata and arbitrary fields can never hitchhike into the request.
        payload = {
            "memory_day": self.day,
            "selection_revision": self.selection_revision,
            "sources": [{"source_id": m.source_id, "speaker": m.speaker,
                         "at": m.at, "text": m.text} for m in self.messages],
            "missing_context": not self.context_complete or any(c.status != "allowed" for c in self.coverage),
            "source_rule": "Only these sources are evidence. Missing passages prove nothing; do not infer events, health, meals or feelings from gaps.",
        }
        if self.redaction_policy is not None:
            from clerk_redaction import MASKED_TEXT_RULE
            payload["source_rule"] += " " + MASKED_TEXT_RULE
        return payload

    def revision(self) -> str:
        return hashlib.sha256(json.dumps(self.payload(), sort_keys=True,
                                         ensure_ascii=False).encode()).hexdigest()


def prepare_clerk_day(
    rows: Iterable[Mapping[str, Any]], memory_day: date, *, as_of: datetime,
    history_complete: bool = False, timezone_name: str = "Asia/Shanghai",
    cutoff_hour: int = 4, policy: PrivacyPolicy | None = None,
    positive_markers: Iterable[datetime | PositiveMarker] = (),
    withheld_ids: Iterable[str] = (),
) -> ClerkDayInput:
    if type(memory_day) is not date or type(cutoff_hour) is not int or not 0 <= cutoff_hour <= 23:
        raise PrivacyInputError("invalid clerk day boundary")
    result = filter_transcript(rows, as_of=as_of, history_complete=history_complete,
                               policy=policy, positive_markers=positive_markers,
                               withheld_ids=withheld_ids)
    return select_clerk_day(result, memory_day, timezone_name=timezone_name, cutoff_hour=cutoff_hour)


def select_clerk_day(result: PrivacyResult, memory_day: date, *,
                     timezone_name: str = "Asia/Shanghai", cutoff_hour: int = 4) -> ClerkDayInput:
    """A day revision changes only when that day's actual selection changes."""
    tz = ZoneInfo(timezone_name)
    by_id = {m.msg_uuid: m for m in result.messages}
    messages: list[ClerkSource] = []
    coverage: list[Decision] = []
    for decision in result.decisions:
        if (decision.at.astimezone(tz) - timedelta(hours=cutoff_hour)).date() != memory_day:
            continue
        coverage.append(decision)
        if decision.status == "allowed":
            message = by_id[decision.msg_uuid]
            if message.text:
                messages.append(ClerkSource(message.msg_uuid, message.speaker,
                                            message.at.isoformat(), message.text, message.conv_uuid))
    identity = [result.policy_revision, memory_day.isoformat(), timezone_name, cutoff_hour,
                [[m.source_id, m.speaker, m.at, m.text] for m in messages],
                [[d.msg_uuid, d.at.isoformat(), d.status, d.reason] for d in coverage]]
    revision = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    # Only the established profile is dispatchable. Every alternate profile
    # carries its review gate through every public preparation entry point.
    return ClerkDayInput(memory_day.isoformat(), revision, tuple(messages), tuple(coverage),
                         privacy_review_required=result.policy_version != POLICY_VERSION)


async def request_clerk(day: ClerkDayInput, *, adapter: Any) -> Any:
    """Adapter sees only the selected whitelist. Empty inputs make no call.

    Model-provider integration, persistence and response validation are later
    stages; this function intentionally does not use the DS dehydrator.
    """
    day.require_dispatch_ready()
    payload = day.payload()
    if not payload["sources"]:
        return {"skip": True, "reason": "no_eligible_sources"}
    return await adapter.generate(payload)
