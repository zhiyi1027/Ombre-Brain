"""Derived review state for active plans.

Plan lifecycle remains driven exclusively by ``status``.  This module only
answers whether an active plan has gone long enough without an explicit human
confirmation to deserve a reminder.  The derived ``is_stale`` flag is never
persisted, so deploying or changing the threshold cannot silently resolve or
abandon anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from utils import parse_iso_datetime


DEFAULT_PLAN_STALE_AFTER_DAYS = 30
MIN_PLAN_STALE_AFTER_DAYS = 1
MAX_PLAN_STALE_AFTER_DAYS = 3650


def plan_stale_after_days(config: dict | None = None) -> int:
    """Return the bounded plan review interval from surfacing config."""

    raw: Any = DEFAULT_PLAN_STALE_AFTER_DAYS
    if isinstance(config, dict):
        surfacing = config.get("surfacing") or {}
        if isinstance(surfacing, dict):
            raw = surfacing.get(
                "plan_stale_after_days",
                DEFAULT_PLAN_STALE_AFTER_DAYS,
            )
    try:
        days = int(raw)
    except (TypeError, ValueError, OverflowError):
        days = DEFAULT_PLAN_STALE_AFTER_DAYS
    return max(MIN_PLAN_STALE_AFTER_DAYS, min(MAX_PLAN_STALE_AFTER_DAYS, days))


def confirmation_timestamp() -> str:
    """Create one local ISO timestamp compatible with existing bucket dates."""

    return datetime.now().isoformat(timespec="seconds")


def _parse_optional_datetime(value: Any) -> datetime | None:
    try:
        return parse_iso_datetime(value)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _history_confirmation(metadata: dict) -> datetime | None:
    """Recover a useful confirmation time for plans created before this feature."""

    history = metadata.get("change_log") or []
    if not isinstance(history, list):
        return None
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action") or "").strip().lower()
        confirms_active = (
            action in {"confirmed", "edit"}
            or (action == "status" and entry.get("to") == "active")
            or (action == "created" and entry.get("to", "active") == "active")
        )
        if not confirms_active:
            continue
        parsed = _parse_optional_datetime(entry.get("ts"))
        if parsed is not None:
            return parsed
    return None


def plan_review_state(
    bucket: dict,
    *,
    reference_time: datetime | None = None,
    stale_after_days: int = DEFAULT_PLAN_STALE_AFTER_DAYS,
) -> dict[str, Any]:
    """Return non-mutating review metadata for one plan bucket."""

    meta = bucket.get("metadata") or {}
    status = str(meta.get("status") or "active").strip().lower()
    if status not in {"active", "resolved", "abandoned"}:
        status = "active"
    try:
        threshold = int(stale_after_days)
    except (TypeError, ValueError, OverflowError):
        threshold = DEFAULT_PLAN_STALE_AFTER_DAYS
    threshold = max(
        MIN_PLAN_STALE_AFTER_DAYS,
        min(MAX_PLAN_STALE_AFTER_DAYS, threshold),
    )
    now = reference_time or datetime.now()
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)

    last_confirmed = _parse_optional_datetime(meta.get("last_confirmed_at"))
    if last_confirmed is None:
        last_confirmed = _history_confirmation(meta)
    if last_confirmed is None:
        last_confirmed = _parse_optional_datetime(
            meta.get("created_at") or meta.get("created")
        )

    if last_confirmed is None:
        return {
            "is_stale": False,
            "days_since_confirmation": None,
            "last_confirmed_at": None,
            "next_review_at": None,
            "stale_after_days": threshold,
        }

    age_seconds = max(0.0, (now - last_confirmed).total_seconds())
    days_since = int(age_seconds // 86_400)
    next_review = last_confirmed + timedelta(days=threshold)
    return {
        "is_stale": status == "active" and now >= next_review,
        "days_since_confirmation": days_since,
        "last_confirmed_at": last_confirmed.isoformat(timespec="seconds"),
        "next_review_at": next_review.isoformat(timespec="seconds"),
        "stale_after_days": threshold,
    }
