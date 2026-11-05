"""Separate local backup for raw evidence and private clerk state.

This bundle contains private data. It is not a GitHub sync artifact and never
includes account/session/configuration files. Restore is only into a new root.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import zipfile

from backup_archive import snapshot_sqlite, validate_sqlite_bytes


FILES = {"raw.db", "clerk.db", "manifest.json"}
LIMIT = 512 * 1024 * 1024


def build_clerk_backup(archive, store):
    # A recovery bundle preserves the full archive, including optional thinking.
    # This is a backup operation, not input to fact extraction or any model.
    with store.generation_lease(), archive._lock:
        files = {"raw.db": snapshot_sqlite(archive.path), "clerk.db": snapshot_sqlite(str(store.path))}
    manifest = {"schema": 1, "kind": "raw-clerk-backup", "created_at": datetime.now(timezone.utc).isoformat(),
                "files": {name: {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()} for name, body in files.items()}}
    if any(not body or len(body) > LIMIT for body in files.values()):
        raise ValueError("clerk backup size invalid")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, body in files.items():
            bundle.writestr(name, body)
        bundle.writestr("manifest.json", json.dumps(manifest).encode())
    return output.getvalue()


def restore_clerk_backup(data, destination):
    destination = Path(destination)
    if destination.exists() or not destination.parent.is_dir() or len(data) > LIMIT:
        raise ValueError("clerk restore requires a new root within size limits")
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        entries = bundle.infolist()
        if len(entries) != 3 or {e.filename for e in entries} != FILES:
            raise ValueError("invalid clerk backup members")
        if any(e.file_size > (1024 * 1024 if e.filename == "manifest.json" else LIMIT) or e.flag_bits & 1 for e in entries):
            raise ValueError("invalid clerk backup size or encryption")
        manifest = json.loads(bundle.read("manifest.json"))
        if (manifest.get("kind") != "raw-clerk-backup" or manifest.get("schema") != 1
                or set(manifest.get("files", {})) != {"raw.db", "clerk.db"}):
            raise ValueError("invalid clerk backup manifest")
        bodies = {}
        for name in ("raw.db", "clerk.db"):
            body = bundle.read(name)
            expected = manifest["files"][name]
            if expected != {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}:
                raise ValueError("clerk backup integrity mismatch")
            validate_sqlite_bytes(body)
            bodies[name] = body
    with tempfile.TemporaryDirectory(prefix="clerk-restore-", dir=destination.parent) as tmp:
        staged = Path(tmp) / "root"
        staged.mkdir(mode=0o700)
        (staged / ".night_clerk").mkdir(mode=0o700)
        (staged / "_raw").mkdir(mode=0o700)
        for name, relative in (("raw.db", "_raw/raw_archive.sqlite3"), ("clerk.db", ".night_clerk/clerk.db")):
            path = staged / relative
            path.write_bytes(bodies[name])
            path.chmod(0o600)
        conn = sqlite3.connect(staged / ".night_clerk/clerk.db")
        try:
            conn.execute("DELETE FROM lease")
            # A restored package must pass takeover gates on its new instance.
            conn.execute("DELETE FROM meta WHERE key IN ('takeover','breath_read')")
            conn.commit()
        finally:
            conn.close()
        staged.rename(destination)
    return {"ok": True, "root": str(destination), "takeover_reset": True}
