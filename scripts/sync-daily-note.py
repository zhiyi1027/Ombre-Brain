#!/usr/bin/env python3
"""Upload one finalized CC/Codex daily note with idempotent local retry.

The note body is never printed.  Transient failures keep only the latest
revision for each note_id in a mode-0600 spool, so autoswap can continue and a
later invocation can catch up safely.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo


DEFAULT_NOTE_PATH = Path("/home/node/grey-ws/.daily-note")
DEFAULT_TOKEN_PATH = Path("/home/node/grey-ws/.ob-daily-note-token")
DEFAULT_SPOOL_PATH = Path("/home/node/grey-ws/.daily-note-upload-spool.json")
DEFAULT_URL = os.getenv("OMBRE_DAILY_NOTE_URL", "").strip()
TITLE_RE = re.compile(r"^#\s*(\d{4}-\d{2}-\d{2})\s+便签(?:\s|（|\(|$)")


class SyncError(RuntimeError):
    pass


class PermanentSyncError(SyncError):
    pass


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=DEFAULT_NOTE_PATH)
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(os.getenv("OMBRE_DAILY_NOTE_TOKEN_FILE", str(DEFAULT_TOKEN_PATH))),
    )
    parser.add_argument(
        "--spool",
        type=Path,
        default=Path(os.getenv("OMBRE_DAILY_NOTE_SPOOL", str(DEFAULT_SPOOL_PATH))),
    )
    parser.add_argument("--source-client", default="cc")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--cutoff-hour", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def _read_spool(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, dict)}


def _token(path: Path) -> str:
    direct = os.getenv("OMBRE_DAILY_NOTE_TOKEN", "").strip()
    if direct:
        return direct
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PermanentSyncError(f"daily note token file unavailable: {path}") from exc
    if not value:
        raise PermanentSyncError("daily note token is empty")
    return value


def _payload(args: argparse.Namespace) -> dict:
    try:
        content = args.file.read_text(encoding="utf-8")
        stat = args.file.stat()
    except OSError as exc:
        raise PermanentSyncError(f"daily note file unavailable: {args.file}") from exc
    if not content.strip():
        raise PermanentSyncError("daily note is empty")
    first_line = content.splitlines()[0].strip()
    match = TITLE_RE.match(first_line)
    if not match:
        raise PermanentSyncError("daily note must start with '# YYYY-MM-DD 便签'")
    memory_day = match.group(1)
    try:
        timezone_info = ZoneInfo(args.timezone)
    except Exception as exc:
        raise PermanentSyncError(f"unknown timezone: {args.timezone}") from exc
    if not 0 <= args.cutoff_hour <= 23:
        raise PermanentSyncError("cutoff-hour must be between 0 and 23")
    updated = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    expected_day = (
        updated.astimezone(timezone_info) - timedelta(hours=args.cutoff_hour)
    ).date().isoformat()
    if memory_day != expected_day:
        raise PermanentSyncError(
            f"note title day {memory_day} does not match {args.timezone} "
            f"{args.cutoff_hour}:00 logical day {expected_day}"
        )
    source_client = str(args.source_client or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", source_client):
        raise PermanentSyncError("source-client is invalid")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "note_id": f"{source_client}-daily-note:{memory_day}",
        "source_client": source_client,
        "memory_day": memory_day,
        "source_updated_at": updated.isoformat(),
        "content_sha256": digest,
        "content": content,
    }


def _post(url: str, token: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ombre-daily-note-sync/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, timeout)) as response:
            body = response.read(256_000)
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500 and exc.code != 429:
            raise PermanentSyncError(f"OB rejected daily note with HTTP {exc.code}") from None
        raise SyncError(f"OB temporarily returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SyncError(f"OB temporarily unreachable: {type(exc).__name__}") from None
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SyncError("OB returned an invalid response") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise SyncError("OB did not acknowledge the daily note")
    return result


def _deliver_pending(
    url: str,
    token: str,
    pending: dict[str, dict],
    timeout: float,
) -> tuple[dict[str, dict], list[str], list[str], list[str]]:
    remaining = dict(pending)
    delivered: list[str] = []
    transient_errors: list[str] = []
    permanent_errors: list[str] = []
    for note_id, item in sorted(pending.items()):
        try:
            _post(url, token, item, timeout)
        except PermanentSyncError as exc:
            permanent_errors.append(f"{note_id}: {exc}")
        except SyncError as exc:
            transient_errors.append(f"{note_id}: {exc}")
        else:
            remaining.pop(note_id, None)
            delivered.append(note_id)
    return remaining, delivered, transient_errors, permanent_errors


def main() -> int:
    args = _args()
    try:
        if not str(args.url or "").startswith(("https://", "http://")):
            raise PermanentSyncError(
                "set --url or OMBRE_DAILY_NOTE_URL to the OB /internal/daily-notes endpoint"
            )
        payload = _payload(args)
        token = "<dry-run>" if args.dry_run else _token(args.token_file)
    except PermanentSyncError as exc:
        print(f"daily note sync failed: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps({
            "note_id": payload["note_id"],
            "source_client": payload["source_client"],
            "memory_day": payload["memory_day"],
            "source_updated_at": payload["source_updated_at"],
            "content_sha256": payload["content_sha256"],
            "content_chars": len(payload["content"]),
        }, ensure_ascii=False))
        return 0

    pending = _read_spool(args.spool)
    pending[payload["note_id"]] = payload
    _atomic_json(args.spool, pending)
    pending, delivered, transient_errors, permanent_errors = _deliver_pending(
        args.url,
        token,
        pending,
        args.timeout,
    )
    _atomic_json(args.spool, pending)
    if transient_errors:
        print(
            f"daily note sync queued {len(transient_errors)} note(s) for retry",
            file=sys.stderr,
        )
    if permanent_errors:
        print(
            f"daily note sync rejected {len(permanent_errors)} note(s): "
            + "; ".join(permanent_errors),
            file=sys.stderr,
        )
        return 1
    result = "unchanged" if not delivered else f"uploaded {len(delivered)} note(s)"
    print(f"daily note sync ok: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
