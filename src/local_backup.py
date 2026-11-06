"""Opaque native vault snapshots. No server imports, credentials or model calls.

This format covers the canonical vault, unlike the Markdown migration exporter.
SQLite uses a read-only online backup; files are stable per-file, not globally
atomic. Authentication, configuration, queues, logs and code are excluded.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import unicodedata
import zipfile

TOPS = {
    "permanent",
    "dynamic",
    "archive",
    "feel",
    "plans",
    "letters",
    "daily_continuity",
    "private_continuity",
    "nightly_dreams",
    "_media",
}
MANIFEST = "ob_snapshot_manifest.json"
MAX_FILES = 50000
MAX_FILE = 512 * 1024**2
MAX_TOTAL = 2 * 1024**3
MAX_MANIFEST = 16 * 1024**2


class Refused(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise Refused(code)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_directory(path):
    path = Path(path)
    require(path.is_absolute() and path.is_dir(), "private_directory_required")
    require(not any(p.is_symlink() for p in (path, *path.parents)), "directory_link")
    info = path.stat()
    require(info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o077, "private_directory_required")
    return path


def allowed(relative, include_raw):
    parts = PurePosixPath(relative).parts
    return (
        relative == "embeddings.db"
        or relative == ".night_clerk/clerk.db"
        or len(parts) >= 2
        and parts[0] in TOPS
        or include_raw
        and len(parts) >= 2
        and parts[0] == "_raw"
        and Path(relative).suffix.lower() in (".db", ".sqlite3", ".json")
    )


def safe_name(value):
    require(
        isinstance(value, str)
        and value
        and "\\" not in value
        and ":" not in value
        and "\x00" not in value
        and not value.startswith("/"),
        "unsafe_member",
    )
    require(all(p not in ("", ".", "..", ".git") for p in value.split("/")), "unsafe_member")
    require(not value.casefold().endswith(("-wal", "-shm", "-journal")), "sqlite_sidecar")
    return value


def check_names(names):
    canonical = {}
    files, parents = set(), set()
    for name in names:
        safe_name(name)
        require(name not in files, "duplicate_member")
        path = PurePosixPath(name)
        for length in range(1, len(path.parts) + 1):
            prefix = "/".join(path.parts[:length])
            key = unicodedata.normalize("NFC", prefix).casefold()
            require(canonical.setdefault(key, prefix) == prefix, "portable_path_collision")
        files.add(name)
        parents.update(str(p) for p in path.parents if str(p) != ".")
    require(not files & parents, "file_directory_collision")


def open_source(root, relative, *, limit=None):
    safe_name(relative)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        handle = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= (MAX_FILE if limit is None else limit):
            os.close(handle)
            raise Refused("source_file_invalid")
        return os.fdopen(handle, "rb")
    finally:
        os.close(descriptor)


def stable_copy(root, relative, target):
    for _ in range(3):
        with open_source(root, relative) as source:
            before = os.fstat(source.fileno())
            require(shutil.disk_usage(target.parent).free > before.st_size + 64 * 1024**2, "disk_reserve")
            with target.open("wb") as output:
                os.chmod(target, 0o600)
                copied = 0
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    copied += len(chunk)
                    require(copied <= before.st_size, "source_grew_during_copy")
                    output.write(chunk)
        with open_source(root, relative) as source:
            after = os.fstat(source.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            require(target.stat().st_size == before.st_size, "copy_size")
            return
    raise Refused("source_kept_changing")


def sqlite_snapshot(root, relative, target):
    with open_source(root, relative) as source:
        before = os.fstat(source.fileno())
        require(source.read(16) == b"SQLite format 3\x00", "invalid_sqlite")
        path = root.joinpath(*PurePosixPath(relative).parts)
        deadline = time.monotonic() + 60

        def progress(_status, _remaining, pages):
            require(time.monotonic() < deadline, "sqlite_snapshot_timeout")
            require(pages * page_size <= MAX_FILE, "sqlite_size")
            require(shutil.disk_usage(target.parent).free > 64 * 1024**2, "disk_reserve")

        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as src:
            src.execute("PRAGMA query_only=ON")
            page_size = src.execute("PRAGMA page_size").fetchone()[0]
            require(
                src.execute("PRAGMA page_count").fetchone()[0] * src.execute("PRAGMA page_size").fetchone()[0]
                <= MAX_FILE,
                "sqlite_size",
            )
            target.touch(mode=0o600, exist_ok=False)
            with sqlite3.connect(target) as dst:
                src.backup(dst, pages=256, progress=progress, sleep=0.05)
                require(dst.execute("PRAGMA quick_check").fetchall() == [("ok",)], "sqlite_integrity")
        with open_source(root, relative) as current:
            after = os.fstat(current.fileno())
        require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino), "sqlite_source_replaced")
        require(target.stat().st_size <= MAX_FILE, "sqlite_size")
        target.chmod(0o600)


def create_snapshot(vault, output, *, include_raw=False):
    require(type(include_raw) is bool, "explicit_raw_policy")
    vault, output = private_directory(vault), Path(output)
    parent = private_directory(output.parent)
    require(not parent.is_relative_to(vault) and not vault.is_relative_to(parent), "overlapping_backup_root")
    require(not output.exists() and not output.is_symlink(), "fresh_output_required")
    paths = []
    for top in (*sorted(TOPS), "embeddings.db", ".night_clerk", *(["_raw"] if include_raw else [])):
        base = vault / top
        require(not base.is_symlink(), "source_link")
        if not base.exists():
            continue
        candidates = [base] if base.is_file() else [base / "clerk.db"] if top == ".night_clerk" else base.rglob("*")
        for path in candidates:
            require(not path.is_symlink(), "source_link")
            relative = path.relative_to(vault).as_posix()
            if (
                path.is_file()
                and allowed(relative, include_raw)
                and not relative.casefold().endswith(("-wal", "-shm", "-journal"))
            ):
                paths.append(relative)
    require(0 < len(paths) <= MAX_FILES, "file_count")
    check_names([MANIFEST, *paths])
    total = 0
    with tempfile.TemporaryDirectory(prefix=".ob-snapshot-", dir=parent) as name:
        stage = Path(name)
        archive_path = stage / "snapshot.zip"
        entries = []
        with zipfile.ZipFile(archive_path, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive_path.chmod(0o600)
            for index, relative in enumerate(sorted(paths)):
                target = stage / ("file-" + str(index))
                if relative.casefold().endswith((".db", ".sqlite3")):
                    sqlite_snapshot(vault, relative, target)
                else:
                    stable_copy(vault, relative, target)
                size = target.stat().st_size
                total += size
                require(size <= MAX_FILE and total <= MAX_TOTAL, "snapshot_size")
                require(shutil.disk_usage(parent).free >= size + 64 * 1024**2, "disk_reserve")
                entries.append({"path": relative, "bytes": size, "sha256": digest(target)})
                archive.write(target, relative)
                target.unlink()
            manifest = {
                "schema": 1,
                "kind": "ob-native-snapshot",
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "include_raw": include_raw,
                "globally_atomic": False,
                "credentials_included": False,
                "files": entries,
                "file_count": len(entries),
                "total_bytes": total,
            }
            encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
            require(len(encoded) <= MAX_MANIFEST, "manifest_size")
            archive.writestr(MANIFEST, encoded)
        require(archive_path.stat().st_size <= MAX_TOTAL, "archive_size")
        os.link(archive_path, output)
    return {
        "files": len(paths),
        "expanded_bytes": total,
        "archive_bytes": output.stat().st_size,
        "archive_sha256": digest(output),
        "include_raw": include_raw,
        "globally_atomic": False,
        "credentials_included": False,
    }


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


@contextmanager
def verified_archive(path, parent, expected_sha256):
    """Pin one source descriptor, hash its private copy, then decode that copy."""
    path = Path(path)
    require(
        path.is_absolute() and isinstance(expected_sha256, str) and re.fullmatch("[a-f0-9]{64}", expected_sha256),
        "archive_identity",
    )
    with tempfile.TemporaryDirectory(prefix=".ob-input-", dir=parent) as name:
        staged = Path(name) / "input.zip"
        with open_source(Path("/"), path.relative_to("/").as_posix(), limit=MAX_TOTAL) as source:
            before = os.fstat(source.fileno())
            require(shutil.disk_usage(parent).free > before.st_size + 64 * 1024**2, "disk_reserve")
            h, total = hashlib.sha256(), 0
            with staged.open("xb") as output:
                staged.chmod(0o600)
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    total += len(chunk)
                    require(total <= before.st_size, "archive_changed")
                    output.write(chunk)
                    h.update(chunk)
            after = os.fstat(source.fileno())
            require(
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                and total == before.st_size
                and h.hexdigest() == expected_sha256,
                "archive_identity",
            )
        with staged.open("rb") as stream:
            yield stream


def restore_snapshot(archive_path, destination, *, expected_sha256):
    archive_path, destination = Path(archive_path), Path(destination)
    private_directory(destination.parent)
    require(
        destination.is_absolute() and not destination.exists() and not destination.is_symlink(),
        "fresh_destination_required",
    )
    with (
        verified_archive(archive_path, destination.parent, expected_sha256) as stream,
        zipfile.ZipFile(stream) as archive,
    ):
        infos = archive.infolist()
        require(1 < len(infos) <= MAX_FILES + 1, "member_count")
        check_names([info.filename for info in infos])
        require(
            all(
                not info.is_dir()
                and info.file_size <= MAX_FILE
                and info.flag_bits & 1 == 0
                and stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG)
                for info in infos
            ),
            "member_type",
        )
        require(sum(info.file_size for info in infos) <= MAX_TOTAL + MAX_MANIFEST, "expanded_size")
        require(archive.getinfo(MANIFEST).file_size <= MAX_MANIFEST, "manifest_size")
        value = json.loads(archive.read(MANIFEST), object_pairs_hook=unique_pairs)
        require(
            isinstance(value, dict)
            and type(value.get("schema")) is int
            and value["schema"] == 1
            and value.get("kind") == "ob-native-snapshot",
            "manifest_schema",
        )
        require(
            type(value.get("include_raw")) is bool and value.get("credentials_included") is False, "manifest_policy"
        )
        entries = value.get("files")
        require(
            isinstance(entries, list)
            and all(isinstance(entry, dict) for entry in entries)
            and type(value.get("file_count")) is int
            and len(entries) == value["file_count"],
            "manifest_count",
        )
        check_names([entry.get("path") for entry in entries])
        require(
            {entry["path"] for entry in entries} == {info.filename for info in infos} - {MANIFEST}, "manifest_members"
        )
        require(
            all(
                allowed(entry["path"], value["include_raw"])
                and type(entry.get("bytes")) is int
                and 0 <= entry["bytes"] <= MAX_FILE
                and isinstance(entry.get("sha256"), str)
                and re.fullmatch("[0-9a-f]{64}", entry["sha256"])
                for entry in entries
            ),
            "manifest_entry",
        )
        require(sum(e["bytes"] for e in entries) == value.get("total_bytes"), "manifest_total")
        require(shutil.disk_usage(destination.parent).free > value["total_bytes"] + 64 * 1024**2, "disk_reserve")
        # Verify every stream before publishing a destination, including SQLite.
        with tempfile.TemporaryDirectory(prefix=".ob-restore-", dir=destination.parent) as name:
            stage = Path(name)
            for entry in entries:
                info = archive.getinfo(entry["path"])
                require(info.file_size == entry["bytes"], "member_size")
                target = stage.joinpath(*PurePosixPath(entry["path"]).parts)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    target.chmod(0o600)
                    shutil.copyfileobj(source, output, 1024 * 1024)
                require(target.stat().st_size == entry["bytes"] and digest(target) == entry["sha256"], "member_digest")
                if entry["path"].casefold().endswith((".db", ".sqlite3")):
                    with sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True) as conn:
                        conn.execute("PRAGMA query_only=ON")
                        require(conn.execute("PRAGMA quick_check").fetchall() == [("ok",)], "restored_sqlite_integrity")
            # Exclusive mkdir prevents concurrent restores replacing an existing
            # empty directory. All streams are already validated before this.
            destination.mkdir(mode=0o700)
            for item in stage.iterdir():
                os.rename(item, destination / item.name)
    return {
        "files": len(entries),
        "expanded_bytes": value["total_bytes"],
        "all_member_hashes_verified": True,
        "sqlite_integrity_verified": True,
        "application_started": False,
    }


def seal_snapshot(archive, output, *, age, recipient):
    archive, output, age = Path(archive), Path(output), Path(age)
    private_directory(output.parent)
    require(
        archive.is_file() and not archive.is_symlink() and not output.exists() and not output.is_symlink(),
        "fresh_ciphertext_required",
    )
    require(age.is_absolute() and age.is_file() and not age.is_symlink() and os.access(age, os.X_OK), "age_executable")
    require(isinstance(recipient, str) and re.fullmatch("age1[0-9a-z]{58}", recipient), "public_recipient")
    with tempfile.TemporaryDirectory(prefix=".ob-encrypt-", dir=output.parent) as name:
        staged = Path(name) / "snapshot.age"
        process = subprocess.run(
            [str(age), "-r", recipient, "-o", str(staged), str(archive)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
            timeout=180,
        )
        require(process.returncode == 0 and staged.is_file(), "encryption_failed")
        staged.chmod(0o600)
        os.link(staged, output)
    return {
        "ciphertext_bytes": output.stat().st_size,
        "ciphertext_sha256": digest(output),
        "private_identity_read": False,
        "decrypted_real_ciphertext": False,
        "uploaded": False,
    }
