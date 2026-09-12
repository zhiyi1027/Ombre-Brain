"""Low-cost process memory diagnostics for Linux and containers."""

from __future__ import annotations

import collections
import gc
import os
from typing import Any


_STATUS = "/proc/self/status"
_CGROUP_V2_MAX = "/sys/fs/cgroup/memory.max"
_CGROUP_V1_MAX = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_NO_LIMIT_ABOVE = 1 << 50


def _read_first_int(path: str) -> int | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
    except (OSError, ValueError):
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return None


def rss_bytes() -> int | None:
    """Return current resident memory, or None when unavailable."""

    try:
        with open(_STATUS, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def memory_limit_bytes() -> int | None:
    """Return the cgroup limit without falling back to host memory."""

    for path in (_CGROUP_V2_MAX, _CGROUP_V1_MAX):
        value = _read_first_int(path)
        if value is not None and 0 < value < _NO_LIMIT_ABOVE:
            return value
    return None


def top_object_types(limit: int = 12) -> list[dict[str, Any]]:
    """Count GC-tracked object types only when explicitly requested."""

    counter: collections.Counter = collections.Counter()
    for obj in gc.get_objects():
        counter[type(obj).__name__] += 1
    return [
        {"type": name, "count": count}
        for name, count in counter.most_common(limit)
    ]


def snapshot(*, include_objects: bool = False) -> dict[str, Any]:
    """Build a diagnostic snapshot suitable for the system report."""

    rss = rss_bytes()
    if rss is None:
        return {
            "available": False,
            "reason": "process RSS is unavailable on this platform",
        }
    limit = memory_limit_bytes()
    report: dict[str, Any] = {
        "available": True,
        "rss_bytes": rss,
        "rss_mb": round(rss / 1048576, 1),
        "limit_bytes": limit,
        "limit_mb": round(limit / 1048576, 1) if limit else None,
        "used_percent": round(rss / limit * 100, 1) if limit else None,
        "pid": os.getpid(),
        "gc_counts": list(gc.get_count()),
        "gc_tracked_objects": len(gc.get_objects()),
    }
    if include_objects:
        report["top_object_types"] = top_object_types()
    return report
