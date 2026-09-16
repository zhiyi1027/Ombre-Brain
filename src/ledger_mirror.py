from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator


LEDGER_SCHEMA_VERSION = 1
LEDGER_ROLE = "mirror"
_MAX_SEQUENCE_RETRIES = 3


class LedgerMirror:
    """Append-only JSONL mirror for successful memory mutations.

    Phase 1 mirror only: this is an audit/replay seed beside Markdown, not the
    canonical source of truth yet.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._latest_seq_cache: int | None = None
        self._ledger_signature: tuple[int, int, int] | None = None

    def append_event(
        self,
        *,
        event_type: str,
        trace_id: str,
        trace_kind: str,
        payload: dict[str, Any] | None = None,
        body: str = "",
    ) -> dict[str, Any]:
        body_hash = _hash_body(body)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_append_starts_on_new_line()

        for _attempt in range(_MAX_SEQUENCE_RETRIES):
            event = {
                "seq": self.latest_seq() + 1,
                "schema_version": LEDGER_SCHEMA_VERSION,
                "ledger_role": LEDGER_ROLE,
                "canonical": False,
                "event_type": str(event_type),
                "trace_id": str(trace_id),
                "trace_kind": str(trace_kind),
                "body_hash": body_hash,
                "payload": _json_safe(payload or {}),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            before_write = self._file_signature()
            if before_write != self._ledger_signature:
                # Another writer changed the ledger after sequence allocation.
                # Drop the high-water mark and recalculate before writing.
                self._latest_seq_cache = None
                self._ledger_signature = None
                continue

            line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line)

            after_write = self._file_signature()
            previous_size = before_write[1] if before_write is not None else 0
            expected_size = previous_size + len(line.encode("utf-8"))
            if after_write is not None and after_write[1] == expected_size:
                self._latest_seq_cache = int(event["seq"])
                self._ledger_signature = after_write
            else:
                # A concurrent append may have interleaved with ours.  The old
                # implementation could already allocate one duplicate in that
                # race; never let a stale cache extend the corruption further.
                self._latest_seq_cache = None
                self._ledger_signature = None
            return event

        # Sustained external writes must not hang the bucket mutation path.
        # Fall back to the old bounded behavior: scan once, append, and leave
        # the cache invalid so the next call rechecks the complete ledger.
        self._latest_seq_cache = None
        self._ledger_signature = None
        event = {
            "seq": self.latest_seq() + 1,
            "schema_version": LEDGER_SCHEMA_VERSION,
            "ledger_role": LEDGER_ROLE,
            "canonical": False,
            "event_type": str(event_type),
            "trace_id": str(trace_id),
            "trace_kind": str(trace_kind),
            "body_hash": body_hash,
            "payload": _json_safe(payload or {}),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line)
        self._latest_seq_cache = None
        self._ledger_signature = None
        return event

    def latest_seq(self) -> int:
        """Return the maximum sequence, caching the high-water mark safely.

        The first lookup scans the ledger so a parseable but out-of-order tail
        cannot make a later append reuse an existing sequence.  Subsequent
        appends update an in-memory high-water mark.  External edits invalidate
        the cache through the file's inode, size, and nanosecond mtime, causing
        the next lookup to rescan before allocating another sequence.
        """
        signature = self._file_signature()
        if self._latest_seq_cache is not None and signature == self._ledger_signature:
            return self._latest_seq_cache

        latest = 0
        for event in self.iter_events():
            if not isinstance(event, dict):
                continue
            try:
                latest = max(latest, int(event.get("seq", 0)))
            except (TypeError, ValueError):
                continue
        self._latest_seq_cache = latest
        self._ledger_signature = signature
        return latest

    def _file_signature(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def iter_events(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def verify_integrity(self) -> dict[str, Any]:
        valid_events = 0
        invalid_lines: list[int] = []
        latest_seq = 0
        schema_versions: set[int] = set()
        if not self.path.exists():
            return {
                "ok": True,
                "path": str(self.path),
                "ledger_role": LEDGER_ROLE,
                "canonical": False,
                "valid_events": 0,
                "invalid_lines": [],
                "latest_seq": 0,
                "schema_versions": [],
            }

        with self.path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    invalid_lines.append(lineno)
                    continue
                valid_events += 1
                try:
                    latest_seq = max(latest_seq, int(event.get("seq", 0)))
                except (TypeError, ValueError):
                    invalid_lines.append(lineno)
                try:
                    schema_versions.add(int(event.get("schema_version")))
                except (TypeError, ValueError):
                    invalid_lines.append(lineno)

        return {
            "ok": not invalid_lines,
            "path": str(self.path),
            "ledger_role": LEDGER_ROLE,
            "canonical": False,
            "valid_events": valid_events,
            "invalid_lines": invalid_lines,
            "latest_seq": latest_seq,
            "schema_versions": sorted(schema_versions),
        }

    def _ensure_append_starts_on_new_line(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open("rb") as f:
            f.seek(-1, 2)
            last_byte = f.read(1)
        if last_byte != b"\n":
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write("\n")


def _hash_body(body: str) -> str:
    digest = hashlib.sha256(str(body).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str))
