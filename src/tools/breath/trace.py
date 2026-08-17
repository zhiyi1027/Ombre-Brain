"""Bounded, read-only audit trail for default breath surfacing runs.

The trace is intentionally process-local: it exists to show the Dashboard what
the currently running OB instance actually returned, without creating another
memory store or writing recalled private text to the service log.  Exact output
is retained only for the most recent bounded set of runs.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import re
from threading import RLock
from uuid import uuid4

from utils import count_tokens_approx


_TRACE_LIMIT = 50
_TRACE_LOCK = RLock()
_TRACE_RUNS: deque[dict] = deque(maxlen=_TRACE_LIMIT)


def new_run_id() -> str:
    """Return an opaque identifier suitable for one trace record."""

    return uuid4().hex


def utc_now_iso() -> str:
    """Return a stable UTC timestamp for Dashboard ordering."""

    return datetime.now(timezone.utc).isoformat()


def record_run(record: dict) -> dict:
    """Append one completed run and return a detached copy."""

    snapshot = deepcopy(record)
    with _TRACE_LOCK:
        _TRACE_RUNS.append(snapshot)
    return deepcopy(snapshot)


_SECTION_HEADERS = {
    "=== 核心准则 ===": "core",
    "=== 浮现记忆 ===": "dynamic",
    "=== 久未浮现 ===": "passive",
    "=== 偶然想起 ===": "encounter",
}
_BUCKET_ID_RE = re.compile(r"\[bucket_id:([^\]]+)\]")
_SCORE_RE = re.compile(r"\[权重:([0-9.]+)\]")
_OMITTED_RE = re.compile(r"有\s*(\d+)\s*条主要浮现记忆")
_USED_RE = re.compile(r"当前约使用\s*(\d+)\s*/\s*(\d+)\s*token")


def _parse_entries(output: str) -> list[dict]:
    """Recover the exact returned bucket order from the rendered payload."""

    # MAINTENANCE COUPLING: this adapter parses the human-readable envelope
    # emitted by surface.py.  If its section headings, bucket markers, weight
    # label, or budget notice change, update these patterns and the cross-section
    # parser contract in tests/test_breath_trace.py together.
    entries: list[dict] = []
    section = "unknown"
    text = str(output or "")
    # Walk line-by-line so the last bucket before a new section header is not
    # swallowed by a naive separator split.  Keep offsets to estimate each
    # returned bucket's rendered size afterward.
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped in _SECTION_HEADERS:
            section = _SECTION_HEADERS[stripped]
            offset += len(line)
            continue
        is_bucket_header = (
            (section == "core" and stripped.startswith("📌 [核心准则]"))
            or (
                section == "dynamic"
                and stripped.startswith(("[权重:", "💭 [权重:"))
            )
            or (
                section == "passive"
                and stripped.startswith(("💤 [久未浮现]", "🌙 [久未浮现]"))
            )
            or (section == "encounter" and stripped.startswith("✨ [偶遇]"))
        )
        if not is_bucket_header:
            offset += len(line)
            continue
        match = _BUCKET_ID_RE.search(line)
        if not match:
            offset += len(line)
            continue
        score_match = _SCORE_RE.search(line[: match.end()])
        entries.append({
            "bucket_id": match.group(1),
            "section": section,
            "status": "returned",
            "reason": {
                "core": "core_always_surface",
                "dynamic": "default_surface_order",
                "passive": "long_inactive_association",
                "encounter": "resolved_random_encounter",
            }.get(section, "default_surface_order"),
            "tokens": 0,
            "score": float(score_match.group(1)) if score_match else None,
            "_offset": offset,
        })
        offset += len(line)

    for index, entry in enumerate(entries):
        start = int(entry.pop("_offset"))
        end = (
            int(entries[index + 1]["_offset"])
            if index + 1 < len(entries)
            else len(text)
        )
        rendered = text[start:end]
        # Section headings and the budget footer belong to the envelope, not
        # to the preceding bucket.  The UI labels this value as approximate.
        boundaries = [
            position
            for marker in (*_SECTION_HEADERS.keys(), "token 预算不足：")
            if (position := rendered.find(marker)) >= 0
        ]
        if boundaries:
            rendered = rendered[: min(boundaries)]
        entry["tokens"] = count_tokens_approx(rendered.rstrip("\n-"))
    return entries


def record_surface_output(
    output: str,
    *,
    kind: str,
    max_results: int,
    max_tokens: int,
    run_id: str | None = None,
) -> dict:
    """Record one exact default-surface payload without rerunning selection."""

    text = str(output or "")
    entries = _parse_entries(text)
    omitted_match = _OMITTED_RE.search(text)
    used_match = _USED_RE.search(text)
    omitted = int(omitted_match.group(1)) if omitted_match else 0
    budgeted_tokens = (
        int(used_match.group(1))
        if used_match
        else sum(int(entry["tokens"]) for entry in entries)
    )
    effective_limit = int(used_match.group(2)) if used_match else int(max_tokens)
    return record_run({
        "run_id": run_id or new_run_id(),
        "kind": str(kind or "actual"),
        "started_at": utc_now_iso(),
        "completed_at": utc_now_iso(),
        "limits": {
            "max_results": int(max_results),
            "max_tokens": effective_limit,
        },
        "counts": {
            "returned": len(entries),
            "omitted_budget": omitted,
        },
        "budgeted_entry_tokens": budgeted_tokens,
        "output_tokens_estimate": count_tokens_approx(text),
        "entries": entries,
        "output": text,
    })


def list_runs(*, limit: int = 20, kind: str = "") -> list[dict]:
    """List newest-first run summaries without the potentially large output."""

    limit = max(1, min(int(limit), _TRACE_LIMIT))
    wanted = str(kind or "").strip()
    with _TRACE_LOCK:
        rows = [deepcopy(row) for row in reversed(_TRACE_RUNS)]
    if wanted:
        rows = [row for row in rows if row.get("kind") == wanted]
    summaries: list[dict] = []
    for row in rows[:limit]:
        row.pop("output", None)
        summaries.append(row)
    return summaries


def get_run(run_id: str) -> dict | None:
    """Return one exact run snapshot, including the rendered output."""

    wanted = str(run_id or "").strip()
    if not wanted:
        return None
    with _TRACE_LOCK:
        for row in reversed(_TRACE_RUNS):
            if row.get("run_id") == wanted:
                return deepcopy(row)
    return None


def clear_runs_for_tests() -> None:
    """Reset process-local state for isolated unit tests."""

    with _TRACE_LOCK:
        _TRACE_RUNS.clear()
