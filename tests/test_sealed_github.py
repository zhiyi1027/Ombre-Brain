"""Generated-only backup and real local Git roundtrips; no service/accounts."""

import asyncio
import hashlib
import os
from pathlib import Path
import shlex
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import local_backup as core
import sealed_github as sealed


@unittest.skipUnless(os.name == "posix" and Path("/usr/bin/ssh").is_file(), "OpenSSH parser required")
class SSHConfigTests(unittest.TestCase):
    def test_openssh_loads_exact_known_hosts_file_with_spaces_and_quotes(self):
        with tempfile.TemporaryDirectory(prefix="ob ssh config ") as name:
            root = Path(name)
            key = root / "generated-key"
            subprocess.run(
                ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
            )
            public = key.with_suffix(".pub").read_text().split()
            for filename in ("known hosts", 'known "hosts"', "known \\hosts"):
                with self.subTest(filename=filename), socket.socket() as server:
                    server.bind(("127.0.0.1", 0))
                    server.listen(1)
                    server.settimeout(5)
                    port = server.getsockname()[1]
                    hosts = root / filename
                    hosts.write_text(f"[127.0.0.1]:{port} {public[0]} {public[1]}\n")
                    hosts.chmod(0o600)

                    def banner_only():
                        try:
                            connection, _ = server.accept()
                            with connection:
                                connection.settimeout(5)
                                connection.sendall(b"SSH-2.0-GENERATED_CONFIG_PROBE\r\n")
                                data = b""
                                while b"\n" not in data:
                                    chunk = connection.recv(4096)
                                    if not chunk:
                                        return
                                    data += chunk
                                if not data.split(b"\n", 1)[1]:
                                    connection.recv(4096)
                        except OSError:
                            pass

                    worker = threading.Thread(target=banner_only, daemon=True)
                    worker.start()
                    try:
                        env = sealed.git_environment(key, hosts)
                        result = subprocess.run(
                            [*shlex.split(env["GIT_SSH_COMMAND"]), "-vvv", "-o", "GlobalKnownHostsFile=/dev/null",
                             "-p", str(port), "-T", "git@127.0.0.1"],
                            capture_output=True, text=True, timeout=8,
                        )
                        # The local banner-only peer never authenticates or receives application data.
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(f"loaded 1 keys from [127.0.0.1]:{port}", result.stderr)
                    finally:
                        server.close()
                        worker.join(timeout=6)
                    self.assertFalse(worker.is_alive())

    def test_control_character_path_refused_before_ssh(self):
        with self.assertRaisesRegex(core.Refused, "git_private_path"):
            sealed.git_environment("/GENERATED\nKEY", "/MUST-NOT-READ")


@unittest.skipUnless(os.name == "posix", "Private POSIX backup entry")
class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp" if os.uname().sysname == "Darwin" else "/tmp")
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, body=b"GENERATED"):
        path = self.vault / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(0o600)
        return path

    def test_complete_business_scope_without_raw_images_or_credentials(self):
        wanted = [
            "dynamic/a.md",
            "feel/a.md",
            "plans/a.md",
            "letters/a.txt",
            "daily_continuity/notes/old.md",
            "private_continuity/state.json",
            "nightly_dreams/a.json",
            "_media/caption.md",
        ]
        for name in wanted:
            self.write(name)
        for name in ["_raw/raw.md", "unknown/secret.md", "config.yaml", "dynamic/image.JPG", "_media/photo.png"]:
            self.write(name)
        db = self.vault / "embeddings.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE generated(value)")
        wanted.append("embeddings.db")
        result = sealed.create_snapshot(self.vault, self.root / "out.zip")
        with zipfile.ZipFile(self.root / "out.zip") as archive:
            self.assertEqual(set(archive.namelist()), set(wanted) | {core.MANIFEST})
        restored = core.restore_snapshot(
            self.root / "out.zip", self.root / "restored", expected_sha256=result["archive_sha256"]
        )
        self.assertEqual(restored["files"], len(wanted))
        self.assertEqual(result["image_files_excluded"], 2)

    def test_open_wal_preserved_and_not_archived_as_sidecar(self):
        conn = sqlite3.connect(self.vault / "embeddings.db")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE generated(value)")
            conn.execute("INSERT INTO generated VALUES (7)")
            conn.commit()
            result = sealed.create_snapshot(self.vault, self.root / "out.zip")
            core.restore_snapshot(
                self.root / "out.zip", self.root / "restored", expected_sha256=result["archive_sha256"]
            )
            with sqlite3.connect(self.root / "restored/embeddings.db") as copy:
                self.assertEqual(copy.execute("SELECT value FROM generated").fetchone(), (7,))
        finally:
            conn.close()

    def test_new_file_during_copy_refuses_complete_success(self):
        self.write("dynamic/a.md")
        original = core.stable_copy

        def changed(*args):
            value = original(*args)
            self.write("dynamic/new.md")
            return value

        with patch.object(core, "stable_copy", side_effect=changed):
            with self.assertRaisesRegex(core.Refused, "source_changed"):
                sealed.create_snapshot(self.vault, self.root / "out.zip")
        self.assertFalse((self.root / "out.zip").exists())

    def test_unknown_business_type_or_link_is_not_silently_skipped(self):
        path = self.write("dynamic/a.bin")
        with self.assertRaisesRegex(core.Refused, "unsupported_business_file"):
            sealed.selected(self.vault)
        path.unlink()
        (self.vault / "dynamic/link.md").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(core.Refused, "scope_file"):
            sealed.selected(self.vault)

    def test_transient_clerk_cache_does_not_survive_projection(self):
        from clerk_store import ClerkStore

        store = ClerkStore(self.vault / ".night_clerk")
        store.put_cache("key", "2026-01-01", {"raw": "GENERATED_TRANSIENT_SECRET"})
        with store.connect() as conn:
            conn.execute("INSERT INTO publications VALUES ('2026-01-01','revision','GENERATED_FACT')")
        result = sealed.create_snapshot(self.vault, self.root / "out.zip")
        core.restore_snapshot(self.root / "out.zip", self.root / "restored", expected_sha256=result["archive_sha256"])
        db = self.root / "restored/.night_clerk/clerk.db"
        self.assertNotIn(b"GENERATED_TRANSIENT_SECRET", db.read_bytes())
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cache").fetchone(), (0,))
            self.assertEqual(conn.execute("SELECT data FROM publications").fetchone(), ("GENERATED_FACT",))

    def test_legacy_upload_and_merge_import_refused_before_read(self):
        from github_sync import GitHubSync

        sync = GitHubSync("GENERATED", "owner/repo")
        with (
            patch.dict(os.environ, {"OB_GITHUB_SEALED_ONLY": "1"}),
            patch.object(sync, "_collect_files", side_effect=AssertionError),
        ):
            self.assertFalse(asyncio.run(sync.sync("/MUST-NOT-READ"))["ok"])
            self.assertFalse(asyncio.run(sync.import_from_github("/MUST-NOT-READ"))["ok"])

    def test_legacy_web_routes_refuse_before_plain_zip_or_config_write(self):
        from web import github as routes

        class Registry:
            def __init__(self):
                self.routes = {}

            def custom_route(self, path, **kwargs):
                def decorator(fn):
                    self.routes[path] = fn
                    return fn

                return decorator

        registry = Registry()
        routes.register(registry)
        with (
            patch.dict(os.environ, {"OB_GITHUB_SEALED_ONLY": "1"}),
            patch.object(routes.sh, "_require_auth", return_value=None),
            patch.object(routes, "_pre_import_backup", side_effect=AssertionError("must not create plaintext")),
            patch.object(routes, "_save_github_config_to_disk", side_effect=AssertionError("must not save old token")),
        ):
            for path in ("/api/github/config", "/api/github/sync", "/api/github/import"):
                self.assertEqual(asyncio.run(registry.routes[path](None)).status_code, 403)


@unittest.skipUnless(os.name == "posix", "Private POSIX backup entry")
class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp" if os.uname().sysname == "Darwin" else "/tmp")
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        subprocess.run(["/usr/bin/git", "init", "--bare", "-q", str(self.remote)], check=True)
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
        self.original = sealed.run_git

    def tearDown(self):
        self.temp.cleanup()

    def redirected(self, folder, args, env, *a, **kw):
        if args[:3] == ["remote", "add", "origin"]:
            args = [*args[:3], str(self.remote)]
        return self.original(folder, args, env, *a, **kw)

    def publish(self, number, head=""):
        path = self.root / "input.age"
        body = b"age-encryption.org/v1\n" + bytes([number]) * 100
        path.write_bytes(body)
        path.chmod(0o600)
        data = {
            "generation": f"20260101T000000Z-{number:012x}",
            "ciphertext_sha256": hashlib.sha256(body).hexdigest(),
            "ciphertext_bytes": len(body),
        }
        with (
            patch.object(sealed, "git_environment", return_value=self.env),
            patch.object(sealed, "run_git", side_effect=self.redirected),
        ):
            return sealed.publish(
                path,
                data,
                repo="owner/repo",
                branch="encrypted",
                key="GENERATED",
                known_hosts="GENERATED",
                keep=2,
                expected_head=head,
            )

    def test_real_git_upload_readback_orphan_history_and_retention(self):
        first = self.publish(1)
        second = self.publish(2, first["commit"])
        third = self.publish(3, second["commit"])
        self.assertTrue(third["ciphertext_readback_verified"])
        self.assertEqual(
            subprocess.check_output(
                ["/usr/bin/git", "--git-dir", str(self.remote), "rev-list", "--count", "encrypted"]
            ).strip(),
            b"1",
        )
        names = subprocess.check_output(
            ["/usr/bin/git", "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "encrypted"]
        ).decode()
        self.assertNotIn("000000000001", names)
        self.assertIn("000000000002", names)
        self.assertIn("000000000003", names)

    def test_unexpected_head_rejected_without_fetch(self):
        self.publish(1)
        with self.assertRaisesRegex(core.Refused, "remote_writer_or_bootstrap_mismatch"):
            self.publish(2)

    def test_source_path_replacement_after_initial_pin_cannot_upload_plaintext(self):
        original = self.redirected

        def swap(folder, args, env, *a, **kw):
            if args[:1] == ["ls-remote"]:
                (self.root / "input.age").write_bytes(b"GENERATED PLAINTEXT")
            return original(folder, args, env, *a, **kw)

        self.redirected = swap
        result = self.publish(1)
        self.assertTrue(result["ciphertext_readback_verified"])
