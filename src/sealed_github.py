"""Opaque business-only snapshots and ciphertext-only Git transport.

Never imports the server, raw archive or a model client. Only an existing
public age recipient is needed for backup. Recovery always uses a fresh root.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
import stat

import local_backup as core

SCOPE = "ob-business-no-images-no-raw-v1"
TEXT = {".md", ".json", ".txt"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".heic", ".heif", ".bmp", ".tiff", ".tif", ".svg", ".ico"}
SIDECARS = ("-wal", "-shm", "-journal")
MAX_CIPHER = 90 * 1024**2
MAX_REMOTE = 512 * 1024**2
GENERATION = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}")


@contextmanager
def sidecar_source(vault, relative, selected_paths):
    core.require(
        any(relative == p + suffix for p in selected_paths if p.endswith(".db") for suffix in ("-wal", "-journal")),
        "sidecar_scope",
    )
    parent = os.open(vault, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = following
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            core.require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "sidecar_type")
            yield stream
    finally:
        os.close(parent)


def selected(vault):
    vault = core.private_directory(vault)
    result, images = [], 0
    for top in sorted(core.TOPS):
        folder = vault / top
        if not folder.exists():
            continue
        core.require(not folder.is_symlink() and folder.is_dir(), "scope_directory")
        for directory, dirs, files in os.walk(folder, followlinks=False):
            core.require(not any((Path(directory) / d).is_symlink() for d in dirs), "scope_link")
            for name in files:
                path = Path(directory) / name
                core.require(not path.is_symlink() and path.is_file(), "scope_file")
                if path.suffix.lower() in IMAGES:
                    images += 1
                    continue
                core.require(path.suffix.lower() in TEXT, "unsupported_business_file")
                result.append(path.relative_to(vault).as_posix())
    for relative in ("embeddings.db", ".night_clerk/clerk.db"):
        path = vault / relative
        if path.exists():
            with core.open_source(vault, relative):
                pass
            result.append(relative)
    core.require(result and len(result) <= core.MAX_FILES, "scope_count")
    core.check_names(result)
    return sorted(result), images


def fingerprint(vault, paths):
    """Hash actual source bytes, including committed WAL changes, not just mtime."""
    rows = []
    for relative in paths:
        candidates = [relative]
        if relative.endswith(".db"):
            candidates += [
                relative + suffix for suffix in ("-wal", "-journal") if (vault / (relative + suffix)).exists()
            ]
        for name in candidates:
            with core.open_source(vault, name) if name == relative else sidecar_source(vault, name, paths) as stream:
                before = os.fstat(stream.fileno())
                h, total = hashlib.sha256(), 0
                for chunk in iter(lambda: stream.read(1024**2), b""):
                    total += len(chunk)
                    core.require(total <= core.MAX_FILE, "scope_size")
                    h.update(chunk)
                after = os.fstat(stream.fileno())
                core.require(
                    (before.st_ino, before.st_mtime_ns, before.st_size)
                    == (after.st_ino, after.st_mtime_ns, after.st_size)
                    and total == before.st_size,
                    "source_changed",
                )
            rows.append((name, total, h.hexdigest()))
    return hashlib.sha256(json.dumps([SCOPE, rows], separators=(",", ":")).encode()).hexdigest()


def clean_clerk(path):
    """Keep derived facts/publications and budgets, never transient model cache/lease."""
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA trusted_schema=OFF")
        objects = conn.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        }
        core.require(
            tables
            == {"markers", "withheld", "cache", "publications", "attempts", "run_guards", "lease", "meta", "calls"},
            "clerk_schema_review_required",
        )
        core.require(set(objects) == {(name, "table") for name in tables}, "clerk_schema_review_required")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("DELETE FROM cache")
        conn.execute("DELETE FROM lease")
        conn.commit()
        conn.execute("VACUUM")
        core.require(conn.execute("PRAGMA quick_check").fetchall() == [("ok",)], "clerk_integrity")


def create_snapshot(vault, output):
    vault, output = Path(vault), Path(output)
    paths, images = selected(vault)
    before = fingerprint(vault, paths)
    with tempfile.TemporaryDirectory(prefix=".ob-gh-source-", dir=output.parent) as name:
        work = Path(name)
        subset = work / "source"
        subset.mkdir(mode=0o700)
        packed = work / "packed"
        packed.mkdir(mode=0o700)
        for relative in paths:
            target = subset / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if relative.endswith(".db"):
                core.sqlite_snapshot(vault, relative, target)
                if relative == ".night_clerk/clerk.db":
                    clean_clerk(target)
            else:
                core.stable_copy(vault, relative, target)
        again, _ = selected(vault)
        core.require(paths == again and fingerprint(vault, paths) == before, "source_changed")
        result = core.create_snapshot(subset, packed / "snapshot.zip", include_raw=False)
        core.require(selected(vault)[0] == paths and fingerprint(vault, paths) == before, "source_changed")
        with (packed / "snapshot.zip").open("rb") as src, output.open("xb") as dst:
            output.chmod(0o600)
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        core.require(core.digest(output) == result["archive_sha256"], "published_snapshot_identity")
    result.update(
        scope=SCOPE,
        source_fingerprint=before,
        image_files_excluded=images,
        source_unchanged_during_copy=True,
        clerk_transient_cache_and_lease_excluded=True,
    )
    return result


def git_environment(key, known_hosts):
    for path in (key, known_hosts):
        path = Path(path)
        core.require(path.is_absolute() and path.is_file() and not path.is_symlink(), "git_private_file")
        info = path.stat()
        core.require(
            info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o600 and info.st_nlink == 1,
            "git_private_permissions",
        )
    ssh = [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + str(known_hosts),
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
    ]
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": shlex.join(ssh),
        "GIT_SSH_VARIANT": "ssh",
    }


def run_git(folder, args, env, data=None, limit=MAX_CIPHER):
    """Bound stdout on disk before reading; errors never echo URLs/paths/content."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.run(
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=folder,
            env=env,
            input=data,
            stdout=stdout,
            stderr=stderr,
            timeout=120,
        )
        core.require(process.returncode == 0, "git_command_failed")
        core.require(stdout.tell() <= limit, "git_output_size")
        stdout.seek(0)
        return stdout.read()


def verify_head(*, repo, branch, key, known_hosts, expected_head):
    core.require(
        re.fullmatch("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        and re.fullmatch("[A-Za-z0-9][A-Za-z0-9_./-]{0,100}", branch)
        and re.fullmatch("[a-f0-9]{40}", expected_head),
        "verify_destination",
    )
    env = git_environment(key, known_hosts)
    with tempfile.TemporaryDirectory(prefix="ob-gh-head-") as name:
        head = (
            run_git(
                Path(name),
                ["ls-remote", "--heads", "git@github.com:" + repo + ".git", "refs/heads/" + branch],
                env,
                limit=256,
            )
            .decode()
            .split()
        )
    core.require(head and head[0] == expected_head, "remote_writer_or_bootstrap_mismatch")
    return True


def validate_index(value):
    core.require(
        isinstance(value, dict) and value.get("scope") == SCOPE and value.get("format") == 1,
        "encrypted_repository_required",
    )
    items = value.get("snapshots")
    core.require(isinstance(items, list) and 1 <= len(items) <= 7, "remote_retention")
    generations = []
    for item in items:
        core.require(isinstance(item, dict), "remote_snapshot_metadata")
        core.require(
            set(item)
            <= {"generation", "ciphertext_sha256", "ciphertext_bytes", "archive_sha256", "files", "source_fingerprint"},
            "remote_metadata_fields",
        )
        core.require(
            isinstance(item, dict)
            and GENERATION.fullmatch(item.get("generation", ""))
            and re.fullmatch("[a-f0-9]{64}", item.get("ciphertext_sha256", ""))
            and type(item.get("ciphertext_bytes")) is int
            and 0 < item["ciphertext_bytes"] <= MAX_CIPHER,
            "remote_snapshot_metadata",
        )
        generations.append(item["generation"])
        for key in ("archive_sha256", "source_fingerprint"):
            if key in item:
                core.require(isinstance(item[key], str) and re.fullmatch("[a-f0-9]{64}", item[key]), "remote_digest")
        if "files" in item:
            core.require(type(item["files"]) is int and 1 <= item["files"] <= core.MAX_FILES, "remote_file_count")
    core.require(len(set(generations)) == len(items), "remote_duplicates")
    return items


def publish(cipher, metadata, *, repo, branch, key, known_hosts, keep=7, expected_head=""):
    """Root commits retain at most seven ciphertexts; no plaintext ancestor history.

    First migration must target a new branch. Old main/history is separately
    held until actual protected-identity recovery and explicit cleanup gates.
    """
    core.require(
        re.fullmatch("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is not None
        and re.fullmatch("[A-Za-z0-9][A-Za-z0-9_./-]{0,100}", branch) is not None
        and ".." not in branch
        and type(keep) is int
        and 1 <= keep <= 7,
        "git_destination",
    )
    core.require(
        GENERATION.fullmatch(metadata.get("generation", ""))
        and re.fullmatch("[a-f0-9]{64}", metadata.get("ciphertext_sha256", ""))
        and type(metadata.get("ciphertext_bytes")) is int
        and 0 < metadata["ciphertext_bytes"] <= MAX_CIPHER,
        "ciphertext_identity",
    )
    cipher = Path(cipher)
    core.private_directory(cipher.parent)
    env = git_environment(key, known_hosts)
    remote = "git@github.com:" + repo + ".git"
    with tempfile.TemporaryDirectory(prefix=".ob-gh-git-", dir=Path(cipher).parent) as name:
        folder = Path(name)
        pinned = folder / "incoming.age"
        with (
            core.open_source(Path("/"), cipher.relative_to("/").as_posix(), limit=MAX_CIPHER) as src,
            pinned.open("xb") as dst,
        ):
            first = os.fstat(src.fileno())
            h, total = hashlib.sha256(), 0
            for chunk in iter(lambda: src.read(1024**2), b""):
                total += len(chunk)
                core.require(total <= metadata["ciphertext_bytes"], "ciphertext_changed")
                h.update(chunk)
                dst.write(chunk)
            last = os.fstat(src.fileno())
            core.require(
                (first.st_ino, first.st_size, first.st_mtime_ns) == (last.st_ino, last.st_size, last.st_mtime_ns)
                and total == metadata["ciphertext_bytes"]
                and h.hexdigest() == metadata["ciphertext_sha256"],
                "ciphertext_changed",
            )
        with pinned.open("rb") as stream:
            core.require(stream.read(22) == b"age-encryption.org/v1\n", "age_ciphertext_required")
        run_git(folder, ["init", "-q"], env)
        run_git(folder, ["remote", "add", "origin", remote], env)
        head = (
            run_git(folder, ["ls-remote", "--heads", "origin", "refs/heads/" + branch], env, limit=256).decode().strip()
        )
        expected = head.split()[0] if head else ""
        core.require(expected == expected_head, "remote_writer_or_bootstrap_mismatch")
        old = []
        if expected:
            core.require(re.fullmatch("[a-f0-9]{40}", expected), "remote_head")
            run_git(folder, ["fetch", "-q", "--depth=1", "origin", expected], env)
            names = (
                run_git(folder, ["ls-tree", "-rz", "--name-only", "FETCH_HEAD"], env, limit=8192).decode().split("\0")
            )
            core.require(
                all(
                    n == "index.json" or re.fullmatch(r"backups/[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}/vault\.age", n)
                    for n in names
                    if n
                ),
                "plaintext_remote_refused",
            )
            index = json.loads(run_git(folder, ["show", "FETCH_HEAD:index.json"], env, limit=65536))
            old = validate_index(index)
            allowed = {"index.json", *(f"backups/{x['generation']}/vault.age" for x in old)}
            core.require(set(filter(None, names)) == allowed, "remote_member_set")
            for item in old[-(keep - 1) :] if keep > 1 else []:
                relative = f"backups/{item['generation']}/vault.age"
                declared = int(run_git(folder, ["cat-file", "-s", "FETCH_HEAD:" + relative], env, limit=32))
                core.require(declared == item["ciphertext_bytes"], "remote_cipher_size")
                data = run_git(folder, ["show", "FETCH_HEAD:" + relative], env)
                core.require(hashlib.sha256(data).hexdigest() == item["ciphertext_sha256"], "remote_cipher_digest")
                target = folder / relative
                target.parent.mkdir(mode=0o700, parents=True)
                target.write_bytes(data)
            old = old[-(keep - 1) :] if keep > 1 else []
        items = [*old, metadata]
        validate_index({"format": 1, "scope": SCOPE, "snapshots": items})
        relative = f"backups/{metadata['generation']}/vault.age"
        target = folder / relative
        target.parent.mkdir(mode=0o700, parents=True)
        shutil.copyfile(pinned, target)
        core.require(core.digest(target) == metadata["ciphertext_sha256"], "staged_cipher_digest")
        (folder / "index.json").write_text(
            json.dumps({"format": 1, "scope": SCOPE, "snapshots": items}, separators=(",", ":"))
        )
        core.require(
            sum(p.stat().st_size for p in folder.glob("backups/*/vault.age")) <= MAX_REMOTE, "remote_total_size"
        )
        run_git(folder, ["add", "--", "index.json", "backups"], env)
        staged = run_git(folder, ["show", ":" + relative], env)
        core.require(hashlib.sha256(staged).hexdigest() == metadata["ciphertext_sha256"], "git_blob_identity")
        tree = run_git(folder, ["write-tree"], env, limit=64).decode().strip()
        commit_env = dict(
            env,
            GIT_AUTHOR_NAME="OB encrypted backup",
            GIT_AUTHOR_EMAIL="ob-backup@localhost",
            GIT_COMMITTER_NAME="OB encrypted backup",
            GIT_COMMITTER_EMAIL="ob-backup@localhost",
        )
        commit = (
            run_git(folder, ["commit-tree", tree], commit_env, data=b"Encrypted OB business snapshot\n", limit=64)
            .decode()
            .strip()
        )
        run_git(
            folder,
            [
                "push",
                "-q",
                "--force-with-lease=refs/heads/" + branch + ":" + expected,
                "origin",
                commit + ":refs/heads/" + branch,
            ],
            env,
        )
        fetched = (
            run_git(folder, ["ls-remote", "--heads", "origin", "refs/heads/" + branch], env, limit=256)
            .decode()
            .split()[0]
        )
        core.require(fetched == commit, "remote_readback_head")
        # Fresh object storage prevents readback from reusing the sender's blob.
        with tempfile.TemporaryDirectory(prefix=".ob-gh-readback-", dir=cipher.parent) as name:
            fresh = Path(name)
            run_git(fresh, ["init", "-q"], env)
            run_git(fresh, ["remote", "add", "origin", remote], env)
            run_git(fresh, ["fetch", "-q", "--depth=1", "origin", commit], env)
            size = int(run_git(fresh, ["cat-file", "-s", "FETCH_HEAD:" + relative], env, limit=32))
            core.require(size == metadata["ciphertext_bytes"], "uploaded_cipher_size")
            readback = run_git(fresh, ["show", "FETCH_HEAD:" + relative], env)
            core.require(
                hashlib.sha256(readback).hexdigest() == metadata["ciphertext_sha256"], "uploaded_cipher_digest"
            )
    return {
        "uploaded": True,
        "commit": commit,
        "ciphertext_readback_verified": True,
        "snapshots_in_current_tree": len(items),
        "history_has_no_parents": True,
        "actual_decryption_verified": False,
    }
