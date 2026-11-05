"""Thinking remains viewable archive data, never night-clerk evidence."""
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from clerk_backup import build_clerk_backup, restore_clerk_backup
from clerk_privacy import VisibilityStateError, filter_transcript
from clerk_store import ClerkStore
from raw_archive import RawArchive, _content_digest
from tools.raw.core import _line

if __package__:
    from .test_clerk_pipeline import FakeGPT, raw, DAY, NOW, NightClerkService
else:
    from test_clerk_pipeline import FakeGPT, raw, DAY, NOW, NightClerkService

NATIVE = "NATIVE_ONLY_SYNTHETIC_SENTINEL_517a"

class UnreadableNative:
    def __str__(self):
        raise AssertionError("clerk must not inspect native thinking")
    def __bool__(self):
        raise AssertionError("clerk must not inspect native thinking")

class VisibleTextScenarios(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="visible-text-fixture-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = RawArchive(str(self.root / "_raw/raw_archive.sqlite3"))
        self.config = {"buckets_dir": str(self.root), "night_clerk": {"enabled": True}}
        self.adapter = FakeGPT()
        self.service = NightClerkService(self.config, archive=self.archive, adapter=self.adapter)

    async def test_native_payload_cannot_change_decisions_or_revisions(self):
        visible = raw("a", 0, "【心声】我想陪你散步。")
        first = filter_transcript([visible], as_of=NOW, history_complete=True)
        second = filter_transcript([{**visible, "thinking": UnreadableNative()}], as_of=NOW, history_complete=True)
        self.assertEqual(first, second)
        self.assertEqual(first.allowed_messages()[0].text, visible["text"])

    async def test_native_only_message_never_enters_selection_or_source_identity(self):
        first = filter_transcript([], as_of=NOW, history_complete=True)
        second = filter_transcript([raw("native", 0, "", thinking=NATIVE)], as_of=NOW, history_complete=True)
        self.assertEqual(first, second)

    async def test_complete_imports_preserve_native_and_outward_heart_notes(self):
        for version, mid in ((1, "first"), (2, "second")):
            importer = self.archive.import_rows if version == 1 else self.archive.import_rows_v2
            importer([raw(mid, version, "【心声】我想陪你散步。", thinking=NATIVE)])
        self.archive.import_rows([raw("only", 0, "", thinking=NATIVE)])
        rows, count = self.archive.day(str(DAY))
        self.assertEqual(count, 3)
        self.assertTrue(all(r["thinking"] == NATIVE for r in rows))
        snapshot = self.archive.clerk_snapshot()["rows"]
        self.assertEqual(len(snapshot), 2)
        self.assertTrue(all(r["text"] == "【心声】我想陪你散步。" and "thinking" not in r for r in snapshot))

    async def test_mixed_fragments_reassemble_both_fields_without_entering_clerk(self):
        text = "【心声】今天想一起散步。"
        digest = _content_digest(text, NATIVE)
        self.assertNotEqual(digest, _content_digest(text))
        parts = [raw("long", 0, text[:5], thinking=NATIVE[:8], fragment_index=0, fragment_count=2, fragment_digest=digest),
                 raw("long", 0, text[5:], thinking=NATIVE[8:], fragment_index=1, fragment_count=2, fragment_digest=digest)]
        self.archive.import_rows_v2(parts[:1])
        self.assertFalse(self.archive.clerk_snapshot()["complete"])
        self.archive.import_rows_v2(parts[1:])
        self.assertEqual(self.archive.day(str(DAY))[0][0]["thinking"], NATIVE)
        self.assertEqual(self.archive.clerk_snapshot()["rows"][0]["text"], text)
        await self.service.generate_day(DAY, now=NOW)
        self.assertNotIn(NATIVE, json.dumps(self.adapter.calls))

    async def test_archive_checksum_still_authenticates_native_fragments(self):
        digest = _content_digest("公开正文。", NATIVE)
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.archive.import_rows_v2([raw("bad", 0, "公开正文。", thinking="CORRUPT", fragment_index=0,
                                             fragment_count=1, fragment_digest=digest)])
        self.assertEqual(self.archive.stats()["n"], 0)

    async def test_pending_native_only_fragment_cannot_prove_remaining_text_empty(self):
        text = "尚未到达的公开正文。"
        digest = _content_digest(text, NATIVE)
        first = raw("mixed", 0, "", thinking=NATIVE, fragment_index=0, fragment_count=2, fragment_digest=digest)
        self.archive.import_rows_v2([first])
        result = await self.service.generate_day(DAY, now=NOW)
        self.assertEqual(result["code"], "pending_raw_fragments")
        self.assertFalse(self.adapter.calls)
        self.archive.import_rows_v2([raw("mixed", 0, text, fragment_index=1, fragment_count=2, fragment_digest=digest)])
        self.assertEqual(self.archive.clerk_snapshot()["rows"][0]["text"], text)

    async def test_only_native_fragments_are_viewable_but_never_fact_sources(self):
        self.archive.import_rows_v2([raw("only", 0, "", thinking=NATIVE, fragment_index=0, fragment_count=1,
                                         fragment_digest=_content_digest("", NATIVE))])
        self.assertEqual(self.archive.day(str(DAY))[0][0]["thinking"], NATIVE)
        self.assertEqual(self.archive.clerk_snapshot()["rows"], [])
        await self.service.generate_day(DAY, now=NOW)
        self.assertFalse(self.adapter.calls)

    async def test_viewing_and_opt_in_search_keep_native_but_clerk_does_not_read_column(self):
        row = raw("public", 0, "公开心声和thinking这个词都保留。", thinking=NATIVE)
        self.archive.import_rows([row])
        self.assertEqual(self.archive.search(NATIVE), [])
        self.assertEqual(self.archive.search(NATIVE, include_thinking=True)[0]["thinking"], NATIVE)
        self.assertIn(NATIVE, _line(row, True))
        self.assertNotIn(NATIVE, _line(row, False))
        self.assertEqual(self.archive.range_days(str(DAY), str(DAY))[0][0]["thinking"], NATIVE)
        connect = self.archive._connect
        def no_native_read():
            conn = connect()
            def authorizer(action, table, column, _db, _trigger):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and column == "thinking" else sqlite3.SQLITE_OK
            conn.set_authorizer(authorizer)
            return conn
        with patch.object(self.archive, "_connect", no_native_read):
            self.assertNotIn("thinking", self.archive.clerk_snapshot()["rows"][0])
            result = await self.service.generate_day(DAY, now=NOW)
            self.assertTrue(result["ok"], result)
        self.assertNotIn(NATIVE, json.dumps(self.adapter.calls))
        with self.service.store.connect() as conn:
            for table in ("cache", "publications", "calls"):
                self.assertNotIn(NATIVE, repr([tuple(r) for r in conn.execute("SELECT * FROM " + table).fetchall()]))

    async def test_native_only_complete_and_fragment_revisions_do_not_invalidate_or_respend(self):
        public = raw("public", 0, "【心声】想陪你散步。", thinking="OLD")
        self.archive.import_rows([public])
        await self.service.generate_day(DAY, now=NOW)
        calls = len(self.adapter.calls)
        revision = self.service.current_publication(DAY, NOW)["revision"]
        for fragmented in (False, True):
            updated = {**public, "thinking": NATIVE + ("_FRAGMENT" if fragmented else "")}
            if fragmented:
                digest = _content_digest(updated["text"], updated["thinking"])
                parts = [updated | {"text": updated["text"][:5], "thinking": updated["thinking"][:8],
                                    "fragment_index": 0, "fragment_count": 2, "fragment_digest": digest},
                         updated | {"text": updated["text"][5:], "thinking": updated["thinking"][8:],
                                    "fragment_index": 1, "fragment_count": 2, "fragment_digest": digest}]
                self.archive.import_rows_v2(parts)
            else:
                self.archive.import_rows([{**updated, "thinking": "INTERMEDIATE"}])
                self.archive.import_rows_v2([updated])
            result = await self.service.generate_day(DAY, now=NOW)
            self.assertEqual(result["skipped"], "current")
            self.assertEqual(len(self.adapter.calls), calls)
            self.assertEqual(self.archive.clerk_snapshot()["gaps"], [])
            self.assertEqual(self.service.current_publication(DAY, NOW)["revision"], revision)

    async def test_blank_native_turn_deletion_does_not_create_clerk_gap(self):
        self.archive.import_rows([raw("after", 2, "【心声】公开的下一句。"), raw("native", 0, "\n\t\u3000", thinking=NATIVE)])
        self.archive.hide_message("native", now=NOW)
        self.assertEqual(self.archive.clerk_snapshot()["gaps"], [])
        self.assertEqual([s.source_id for s in self.service.prepare(DAY, NOW).messages], ["after"])

    async def test_structured_fields_are_not_stringified_as_spoken_text(self):
        for field in ("text", "thinking"):
            for payload in ({"thinking": NATIVE}, [{"type": "thinking", "thinking": NATIVE}], UnreadableNative()):
                row = raw("invalid", 0, "正文。") | {field: payload}
                self.archive.import_rows([row])
                with self.assertRaises(ValueError):
                    self.archive.import_rows_v2([row])
        self.assertEqual(self.archive.stats()["n"], 0)

    async def test_recovery_backup_preserves_viewable_thinking_not_in_clerk_state(self):
        self.archive.import_rows([raw("public", 0, "公开正文。", thinking=NATIVE)])
        await self.service.generate_day(DAY, now=NOW)
        data = build_clerk_backup(self.archive, self.service.store)
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            self.assertIn(NATIVE.encode(), bundle.read("raw.db"))
            self.assertNotIn(NATIVE.encode(), bundle.read("clerk.db"))
        target = self.root / "restored"
        restore_clerk_backup(data, target)
        restored = RawArchive(str(target / "_raw/raw_archive.sqlite3"))
        self.assertEqual(restored.day(str(DAY))[0][0]["thinking"], NATIVE)
        self.assertNotIn("thinking", restored.clerk_snapshot()["rows"][0])

    async def test_unknown_legacy_exclusion_state_is_not_silently_reused_or_erased(self):
        self.archive.import_rows([raw("a", 0, "公开正文。")])
        with self.service.store.connect() as conn:
            conn.execute("INSERT INTO withheld VALUES ('a')")
            conn.execute("DELETE FROM meta WHERE key='input_policy'")
        reopened = ClerkStore(self.root / ".night_clerk")
        with self.assertRaises(VisibilityStateError):
            reopened.privacy_state()
        result = await self.service.generate_day(DAY, now=NOW)
        self.assertEqual(result["code"], "privacy_state_revalidation_required")
        self.assertFalse(self.adapter.calls)
        with reopened.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM withheld").fetchone()[0], 1)

    async def test_unknown_legacy_raw_gap_state_requires_revalidation(self):
        with self.archive._connect() as conn:
            conn.execute("INSERT INTO raw_clerk_gaps VALUES ('old','fixture','2026-08-20T12:00:00+00:00')")
            conn.execute("DELETE FROM raw_clerk_meta WHERE key='input_policy'")
        reopened = RawArchive(self.archive.path)
        with self.assertRaises(VisibilityStateError):
            reopened.clerk_snapshot()

if __name__ == "__main__":
    unittest.main()
