#!/usr/bin/env python3
"""Sync CC's fixed unresolved-conflict file into OB private continuity.

Missing files never resolve remote state implicitly.  Closing a conflict is an
explicit ``--resolve --confirm RESOLVE`` operation so a path or mount failure
cannot silently erase the shared state.  The private body is never printed.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_FILE = Path("/home/node/grey-ws/.conflict-unresolved")
DEFAULT_TOKEN_FILE = Path("/home/node/grey-ws/.ob-daily-note-token")
DEFAULT_STATE_FILE = Path("/home/node/grey-ws/.conflict-sync-state.json")
DEFAULT_URL = os.getenv("OMBRE_PRIVATE_CONTINUITY_URL", "").strip()


class SyncError(RuntimeError):
    pass


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(
            os.getenv(
                "OMBRE_PRIVATE_CONTINUITY_TOKEN_FILE",
                str(DEFAULT_TOKEN_FILE),
            )
        ),
    )
    parser.add_argument("--source-client", default="cc")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--if-changed", action="store_true",
                        help="for a timer: upload only a newly created or edited local file")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="allow cleartext HTTP to a non-loopback host (unsafe)",
    )
    return parser.parse_args()


def _validate_url(value: str, *, allow_insecure_http: bool) -> str:
    url = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise SyncError("private continuity URL is invalid") from exc
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise SyncError(
            "set --url or OMBRE_PRIVATE_CONTINUITY_URL to the OB internal endpoint"
        )
    if parsed.username or parsed.password:
        raise SyncError("private continuity URL must not contain credentials")
    host = str(parsed.hostname or "").lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and not loopback and not allow_insecure_http:
        raise SyncError(
            "refusing cleartext HTTP because it exposes the token and conflict body; "
            "use HTTPS or pass --allow-insecure-http explicitly"
        )
    return url


def _token(path: Path) -> str:
    direct = (
        os.getenv("OMBRE_PRIVATE_CONTINUITY_TOKEN", "").strip()
        or os.getenv("OMBRE_DAILY_NOTE_TOKEN", "").strip()
    )
    if direct:
        return direct
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SyncError(f"private continuity token file unavailable: {path}") from exc
    if not token:
        raise SyncError("private continuity token is empty")
    return token


def _request(
    url: str,
    token: str,
    *,
    method: str,
    payload: dict | None,
    timeout: float,
) -> dict:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "ombre-private-continuity-sync/1.0",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=max(1.0, timeout),
        ) as response:
            body = response.read(256_000)
    except urllib.error.HTTPError as exc:
        raise SyncError(f"OB rejected private continuity with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SyncError(
            f"OB private continuity endpoint unreachable: {type(exc).__name__}"
        ) from None
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SyncError("OB returned an invalid response") from exc
    if not isinstance(result, dict):
        raise SyncError("OB returned an invalid response object")
    return result


def _checkpoint(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        raise SyncError(f"sync checkpoint cannot be read: {path}") from exc
    if (not isinstance(data, dict)
            or not isinstance(data.get("sha256"), str)
            or not isinstance(data.get("revision"), int)):
        raise SyncError(f"sync checkpoint is invalid: {path}")
    return data


def _save_checkpoint(path: Path, digest: str, revision: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".conflict-sync-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"sha256": digest, "revision": revision}, stream)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sync(args: argparse.Namespace) -> int:
    if args.if_changed and args.resolve:
        raise SyncError("--if-changed cannot be combined with --resolve")
    checkpoint = _checkpoint(args.state_file) if args.if_changed else None
    if args.if_changed and not args.file.exists():
        # A missing file never closes OB. Forget the local observation so a
        # later, deliberately created file is treated as a new event.
        if checkpoint is not None:
            args.state_file.unlink()
        return 0

    args.url = _validate_url(
        args.url,
        allow_insecure_http=bool(args.allow_insecure_http),
    )
    if args.url.startswith("http://") and args.allow_insecure_http:
        print(
            "warning: sending the token and private conflict over cleartext HTTP",
            file=sys.stderr,
        )
    source = str(args.source_client or "").strip().lower()
    if not source or len(source) > 32:
        raise SyncError("source-client is invalid")
    if args.resolve and args.confirm != "RESOLVE":
        raise SyncError("--resolve requires --confirm RESOLVE")
    if not args.resolve:
        try:
            content = args.file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SyncError(
                f"conflict file unavailable: {args.file}; missing files never resolve remote state"
            ) from exc
        if not content:
            raise SyncError("conflict file is empty")
    else:
        content = ""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if args.if_changed and checkpoint and checkpoint["sha256"] == digest:
        return 0
    if args.dry_run:
        print(json.dumps({
            "mode": "resolve" if args.resolve else "upsert",
            "source_client": source,
            "content_chars": len(content),
        }, ensure_ascii=False))
        return 0
    token = _token(args.token_file)
    state = _request(
        args.url,
        token,
        method="GET",
        payload=None,
        timeout=args.timeout,
    )
    revision = int(state.get("revision") or 0)
    if args.if_changed:
        if checkpoint is not None:
            if not state.get("open"):
                raise SyncError("OB conflict was resolved; remove the stale local file before creating a new one")
            if (revision != checkpoint["revision"]
                    or state.get("content_sha256") != checkpoint["sha256"]):
                raise SyncError("OB conflict changed elsewhere; reconcile before uploading local edits")
        elif state.get("open"):
            if state.get("content_sha256") != digest:
                raise SyncError("OB already has a different open conflict; reconcile before first sync")
            _save_checkpoint(args.state_file, digest, revision)
            print("private continuity sync: current local file already in OB")
            return 0
    if args.resolve:
        if not state.get("open"):
            print("private continuity sync ok: already resolved")
            return 0
        target = args.url + ("&" if "?" in args.url else "?") + "confirm=true"
        result = _request(
            target,
            token,
            method="DELETE",
            payload={
                "source_client": source,
                "expected_revision": revision,
            },
            timeout=args.timeout,
        )
    else:
        result = _request(
            args.url,
            token,
            method="PUT",
            payload={
                "content": content,
                "source_client": source,
                "expected_revision": revision,
            },
            timeout=args.timeout,
        )
    if not result.get("ok"):
        raise SyncError("OB did not acknowledge private continuity")
    if args.if_changed:
        _save_checkpoint(args.state_file, digest, int(result.get("revision") or 0))
    print(
        "private continuity sync ok: "
        + str(result.get("status") or "updated")
        + f" revision {int(result.get('revision') or 0)}"
    )
    return 0


def main() -> int:
    args = _args()
    try:
        if args.if_changed:
            args.state_file.parent.mkdir(parents=True, exist_ok=True)
            lock_path = args.state_file.with_suffix(args.state_file.suffix + ".lock")
            with open(lock_path, "a", encoding="utf-8") as lock:
                os.chmod(lock_path, 0o600)
                fcntl.flock(lock, fcntl.LOCK_EX)
                return _sync(args)
        return _sync(args)
    except (SyncError, ValueError) as exc:
        print(f"private continuity sync failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
