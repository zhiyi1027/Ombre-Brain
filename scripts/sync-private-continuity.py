#!/usr/bin/env python3
"""Sync CC's fixed unresolved-conflict file into OB private continuity.

Missing files never resolve remote state implicitly.  Closing a conflict is an
explicit ``--resolve --confirm RESOLVE`` operation so a path or mount failure
cannot silently erase the shared state.  The private body is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_FILE = Path("/home/node/grey-ws/.conflict-unresolved")
DEFAULT_TOKEN_FILE = Path("/home/node/grey-ws/.ob-daily-note-token")
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


def main() -> int:
    args = _args()
    try:
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
        print(
            "private continuity sync ok: "
            + str(result.get("status") or "updated")
            + f" revision {int(result.get('revision') or 0)}"
        )
        return 0
    except (SyncError, ValueError) as exc:
        print(f"private continuity sync failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
