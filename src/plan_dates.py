"""Calendar due dates; no invented deadline for legacy or undecided plans."""
from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo


def normalize_due_date(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("due_date must be YYYY-MM-DD or empty")
    return date.fromisoformat(value).isoformat()


def plan_date_state(metadata, *, reference_time=None, timezone_name="Asia/Shanghai"):
    reference = reference_time or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        # Existing OB callers use naive host-local datetimes.
        reference = reference.astimezone()
    today = reference.astimezone(ZoneInfo(timezone_name)).date()
    try:
        due = normalize_due_date(metadata.get("due_date"))
    except ValueError:
        due = None
    if not due:
        return {"due_date": None, "date_status": "undated", "label": "日期待定", "expand": True}
    when = date.fromisoformat(due)
    overdue = when < today
    return {"due_date": due, "date_status": "overdue" if overdue else "upcoming",
            "label": ("已过期 " if overdue else "到期 ") + due,
            "expand": when <= today + timedelta(days=6)}


def plan_date_sort(metadata, *, reference_time=None):
    state = plan_date_state(metadata, reference_time=reference_time)
    return (state["due_date"] is None, state["due_date"] or "9999-12-31")
