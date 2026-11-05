"""Generated-only regression checks for native opaque recovery."""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import local_backup as backup


@unittest.skipUnless(os.name == "posix", "Native backup requires POSIX descriptor operations")
class LocalBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="ob-backup-tests-", dir="/private/tmp" if sys.platform == "darwin" else "/tmp"
        )
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.out = self.root / "outputs"
        self.vault.mkdir(mode=0o700)
        self.out.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, name, data=b"GENERATED SOURCE"):
        path = self.vault / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def database(self, name):
        path = self.put(name, b"")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE generated(value TEXT)")
        conn.execute("INSERT INTO generated VALUES (?)", ("GENERATED",))
        conn.commit()
        return conn

    def snapshot(self, **kwargs):
        path = self.out / "snapshot.zip"
        return path, backup.create_snapshot(self.vault, path, **kwargs)

    def test_regular_business_and_media_roundtrip_excludes_credentials(self):
        for name in (
            "dynamic/a.md",
            "feel/b.md",
            "plans/c.md",
            "letters/d.md",
            "daily_continuity/e.md",
            "private_continuity/state.json",
            "nightly_dreams/day.json",
            "_media/image.jpg",
        ):
            self.put(name)
        self.put("config.yaml", b"GENERATED-SECRET")
        self.put(".dashboard_sessions.json", b"GENERATED-SECRET")
        self.put(".night_clerk/deploy-backups/old.zip", b"GENERATED-SECRET")
        self.put("_ledger/events.json", b"GENERATED-SECRET")
        archive, receipt = self.snapshot()
        self.assertEqual(receipt["files"], 8)
        destination = self.out / "restored"
        restored = backup.restore_snapshot(archive, destination, expected_sha256=receipt["archive_sha256"])
        self.assertEqual(restored["files"], 8)
        self.assertEqual((destination / "dynamic/a.md").read_bytes(), b"GENERATED SOURCE")
        self.assertFalse((destination / "config.yaml").exists())
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in destination.rglob("*") if p.is_file()))

    def test_wal_sqlite_snapshots_and_optional_raw_shards(self):
        databases = [
            self.database(name)
            for name in (
                "embeddings.db",
                ".night_clerk/clerk.db",
                "_raw/raw_archive.sqlite3",
                "_raw/shards/day.SQLITE3",
            )
        ]
        try:
            archive, receipt = self.snapshot(include_raw=True)
            self.assertEqual(receipt["files"], 4)
            destination = self.out / "restored"
            backup.restore_snapshot(archive, destination, expected_sha256=receipt["archive_sha256"])
            for name in ("embeddings.db", ".night_clerk/clerk.db", "_raw/shards/day.SQLITE3"):
                with sqlite3.connect(destination / name) as conn:
                    self.assertEqual(conn.execute("SELECT value FROM generated").fetchall(), [("GENERATED",)])
        finally:
            for conn in databases:
                conn.close()

    def test_raw_omitted_by_explicit_false_policy(self):
        self.put("dynamic/a.md")
        self.put("_raw/private.json", b"GENERATED RAW")
        archive, receipt = self.snapshot(include_raw=False)
        self.assertEqual(receipt["files"], 1)
        with zipfile.ZipFile(archive) as z:
            self.assertNotIn("_raw/private.json", z.namelist())

    def test_source_symlink_refused(self):
        self.put("dynamic/a.md").unlink()
        external = self.root / "outside"
        external.write_text("GENERATED")
        (self.vault / "dynamic/a.md").symlink_to(external)
        with self.assertRaises(backup.Refused):
            self.snapshot()

    def test_ignored_credentials_are_not_followed(self):
        self.put("dynamic/a.md")
        (self.vault / "config.yaml").symlink_to(self.root / "missing")
        self.assertEqual(self.snapshot()[1]["files"], 1)

    def test_output_and_source_roots_must_not_overlap(self):
        self.put("dynamic/a.md")
        with self.assertRaises(backup.Refused):
            backup.create_snapshot(self.vault, self.vault / "snapshot.zip")

    def test_existing_archive_and_restore_directory_not_overwritten(self):
        self.put("dynamic/a.md")
        archive, receipt = self.snapshot()
        with self.assertRaises(backup.Refused):
            backup.create_snapshot(self.vault, archive)
        destination = self.out / "existing"
        destination.mkdir(mode=0o700)
        with self.assertRaises(backup.Refused):
            backup.restore_snapshot(archive, destination, expected_sha256=receipt["archive_sha256"])
        self.assertEqual(list(destination.iterdir()), [])

    def test_mac_casefold_and_nfc_collisions(self):
        for names in (
            ["dynamic/A.md", "dynamic/a.md"],
            ["dynamic/é.md", "dynamic/e\u0301.md"],
            ["dynamic/a.md", "dynamic/a.md/child.md"],
        ):
            with self.subTest(names=names), self.assertRaises(backup.Refused):
                backup.check_names(names)

    def test_unsafe_member_names(self):
        for name in (
            "../outside",
            "/absolute",
            "dynamic/../x",
            "dynamic\\x",
            "dynamic/a:stream",
            "dynamic//x",
            "dynamic/a.db-WAL",
            "dynamic/.git/x",
        ):
            with self.subTest(name=name), self.assertRaises(backup.Refused):
                backup.check_names([name])

    def test_corrupt_digest_does_not_publish_restore(self):
        self.put("dynamic/a.md")
        archive, _receipt = self.snapshot()
        destination = self.out / "restored"
        with self.assertRaises(backup.Refused):
            backup.restore_snapshot(archive, destination, expected_sha256="0" * 64)
        self.assertFalse(destination.exists())

    def test_tampered_payload_does_not_publish_restore(self):
        self.put("dynamic/a.md")
        archive, _receipt = self.snapshot()
        changed = self.out / "tampered.zip"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(changed, "w") as target:
            for name in source.namelist():
                target.writestr(name, b"TAMPERED" if name == "dynamic/a.md" else source.read(name))
        destination = self.out / "restored"
        with self.assertRaises(backup.Refused):
            backup.restore_snapshot(changed, destination, expected_sha256=backup.digest(changed))
        self.assertFalse(destination.exists())

    def test_archive_path_swap_after_verification_cannot_substitute_payload(self):
        self.put("dynamic/a.md", b"FIRST GENERATED")
        archive, receipt = self.snapshot()
        (self.vault / "dynamic/a.md").write_bytes(b"SECOND GENERATED")
        replacement = self.out / "replacement.zip"
        backup.create_snapshot(self.vault, replacement)
        original = zipfile.ZipFile

        def swapping(file, *args, **kwargs):
            # The path is swapped at decoder construction; it must already hold
            # the verified first snapshot rather than reopening the input path.
            os.replace(replacement, archive)
            return original(file, *args, **kwargs)

        destination = self.out / "restored"
        with patch.object(backup.zipfile, "ZipFile", side_effect=swapping):
            backup.restore_snapshot(archive, destination, expected_sha256=receipt["archive_sha256"])
        self.assertEqual((destination / "dynamic/a.md").read_bytes(), b"FIRST GENERATED")

    def test_size_limit_refuses_without_publishing_archive(self):
        self.put("dynamic/a.md", b"x" * 32)
        with patch.object(backup, "MAX_FILE", 16), self.assertRaises(backup.Refused):
            self.snapshot()
        self.assertFalse((self.out / "snapshot.zip").exists())

    def test_empty_and_non_boolean_raw_policy_refused(self):
        with self.assertRaises(backup.Refused):
            self.snapshot()
        self.put("dynamic/a.md")
        with self.assertRaises(backup.Refused):
            self.snapshot(include_raw="false")

    @unittest.skipUnless(
        os.environ.get("OB_TEST_AGE") and os.environ.get("OB_TEST_AGE_KEYGEN"), "explicit age tools required"
    )
    def test_generated_age_identity_real_encrypt_decrypt_restore(self):
        self.put("dynamic/a.md")
        archive, receipt = self.snapshot()
        age, keygen = os.environ["OB_TEST_AGE"], os.environ["OB_TEST_AGE_KEYGEN"]
        identity = self.root / "generated-identity"
        subprocess.run([keygen, "-o", str(identity)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        recipient = subprocess.check_output([keygen, "-y", str(identity)], stderr=subprocess.DEVNULL).decode().strip()
        cipher = self.out / "snapshot.age"
        sealed = backup.seal_snapshot(archive, cipher, age=age, recipient=recipient)
        self.assertFalse(sealed["private_identity_read"])
        recovered = self.out / "recovered.zip"
        subprocess.run(
            [age, "-d", "-i", str(identity), "-o", str(recovered), str(cipher)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(backup.digest(recovered), receipt["archive_sha256"])
        restored = backup.restore_snapshot(recovered, self.out / "restored", expected_sha256=receipt["archive_sha256"])
        self.assertEqual(restored["files"], 1)


if __name__ == "__main__":
    unittest.main()
