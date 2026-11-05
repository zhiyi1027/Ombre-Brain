"""Synthetic scene-mask/word-replacement contract; no real words or accounts."""

from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from clerk_input import request_clerk
from clerk_pipeline import split_sources, validate_facts, source_excerpt, digest, render_impression
from clerk_refine import build_refinement
from clerk_privacy import PrivacyInputError, PrivacyPolicy
from clerk_redaction import (
    ClerkLexicon,
    MASKED_TEXT_RULE,
    SceneManifest,
    prepare_scene_redacted_day,
    visible_source_revision,
)

START = datetime.fromisoformat("2001-01-01T00:00:00+08:00")
END = START + timedelta(days=1)
DAY = date(2001, 1, 1)


def row(mid, minute, text, speaker="知知", conv="fixture", **extra):
    return {
        "msg_uuid": mid,
        "conv_uuid": conv,
        "speaker": speaker,
        "at": (START + timedelta(minutes=minute)).isoformat(),
        "text": text,
        **extra,
    }


def prepare(rows, *, ranges=(), lexicon=None, **overrides):
    lexicon = lexicon or ClerkLexicon(1, ("甲乙", "甲", "乙"), ("保留甲乙",), False)
    scenes = SceneManifest(visible_source_revision(rows), START, END, ranges, "user_confirmed")
    options = {
        "window_start": START,
        "window_end": END,
        "scenes": scenes,
        "lexicon": lexicon,
        "snapshot_complete": True,
        "cutoff_hour": 0,
        **overrides,
    }
    return prepare_scene_redacted_day(rows, DAY, **options)


class LexiconTests(unittest.TestCase):
    def test_longest_word_and_two_literal_stars_preserve_entire_sentence(self):
        value = ClerkLexicon(1, ("乙", "甲", "甲乙"), (), False)
        self.assertEqual(value.redact("前句。今天甲乙，后句照旧！"), ("前句。今天**，后句照旧！", 1))
        self.assertEqual(value.redact("甲乙甲"), ("****", 2))

    def test_whitelist_has_priority_in_every_overlap_direction(self):
        cases = [
            (("甲乙",), ("甲乙丙",), "甲乙丙", "甲乙丙"),
            (("甲乙丙丁",), ("乙丙",), "甲乙丙丁", "**乙丙**"),
            (("甲乙丙",), ("丙丁",), "甲乙丙丁", "**丙丁"),
            (("甲乙",), ("甲乙",), "甲乙", "甲乙"),
        ]
        for black, white, text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(ClerkLexicon(1, black, white, False).redact(text)[0], expected)

    def test_formatting_and_whitespace_are_preserved_outside_matched_words(self):
        value = ClerkLexicon(1, ("甲乙",), ("保留甲乙",), False)
        self.assertEqual(
            value.redact("**标题**\n- 甲\u200b乙，保留甲乙  后文。"), ("**标题**\n- **，保留甲乙  后文。", 1)
        )

    def test_nfkc_offsets_and_protected_compatibility_character(self):
        value = ClerkLexicon(1, ("ABC",), (), False)
        self.assertEqual(value.redact("前ＡＢＣ后"), ("前**后", 1))
        value = ClerkLexicon(1, ("i",), ("ff",), False)
        self.assertEqual(value.redact("ﬃ"), ("ﬃ", 0))

    def test_invisible_format_controls_and_variation_selectors_do_not_hide_word(self):
        value = ClerkLexicon(1, ("甲乙",), (), False)
        for invisible in ("\u00ad", "\u2066", "\u2069", "\u034f", "\ufe0f", "\U000e0100"):
            self.assertEqual(value.redact("前甲" + invisible + "乙后"), ("前**后", 1))
        protected = ClerkLexicon(1, ("甲乙",), ("甲乙",), False)
        self.assertEqual(protected.redact("甲\u2066乙"), ("甲\u2066乙", 0))

    def test_full_user_list_capacity_still_allows_builtin_core_terms(self):
        value = ClerkLexicon(1, tuple(f"word{i:04d}" for i in range(2048)), (), True)
        self.assertEqual(value.redact("word2047"), ("**", 1))

    def test_health_and_nickname_whitelist_is_not_a_fact_classifier(self):
        value = ClerkLexicon(1, ("合成昵称", "合成健康词"), ("合成昵称", "合成健康词"), False)
        self.assertEqual(value.redact("合成昵称说合成健康词？"), ("合成昵称说合成健康词？", 0))

    def test_invalid_configuration_refuses_without_echoing_entries(self):
        for values in (("",), ("**",), (None,), "甲", ("甲\n乙",)):
            with self.subTest(kind=type(values).__name__):
                with self.assertRaises(PrivacyInputError):
                    ClerkLexicon(1, values, (), False)
        with self.assertRaises(PrivacyInputError):
            ClerkLexicon(True, (), (), False)
        with self.assertRaises(PrivacyInputError):
            ClerkLexicon.from_data({"replace": []})
        self.assertNotIn("SECRET", repr(ClerkLexicon(1, ("SECRET",), (), False)))

    def test_private_loader_requires_explicit_valid_file_and_no_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "words.json"
            with self.assertRaises(PrivacyInputError):
                ClerkLexicon.load(p)
            p.write_text(
                json.dumps({"schema": 1, "version": 1, "replace": ["甲"], "whitelist": [], "include_core": False})
            )
            p.chmod(0o600)
            self.assertEqual(ClerkLexicon.load(p).redact("甲"), ("**", 1))
            link = Path(tmp) / "link.json"
            link.symlink_to(p)
            with self.assertRaises(PrivacyInputError):
                ClerkLexicon.load(link)
            p.chmod(0o644)
            with self.assertRaises(PrivacyInputError):
                ClerkLexicon.load(p)
            p.chmod(0o600)
            p.write_text('{"schema":1,"schema":1}')
            with self.assertRaises(PrivacyInputError):
                ClerkLexicon.load(p)

    def test_dictionary_edits_and_version_change_revision(self):
        a = ClerkLexicon(1, ("甲",), (), False)
        for b in (
            ClerkLexicon(2, ("甲",), (), False),
            ClerkLexicon(1, ("甲", "乙"), (), False),
            ClerkLexicon(1, ("甲",), ("甲",), False),
        ):
            self.assertNotEqual(a.revision(), b.revision())


class SceneInputTests(unittest.TestCase):
    def test_only_whole_confirmed_scene_is_excluded_across_both_speakers(self):
        rows = [
            row("before", 9, "甲乙"),
            row("first", 10, "保留甲乙"),
            row("other", 15, "日常也不捞出", speaker="顾凛", conv="other"),
            row("last", 19.999, "保留甲乙"),
            row("after", 20, "甲乙"),
            row("reply", 21, "继续。"),
        ]
        day, receipt = prepare(rows, ranges=((START + timedelta(minutes=10), START + timedelta(minutes=20)),))
        self.assertEqual(
            {s.source_id: s.text for s in day.messages}, {"before": "**", "after": "**", "reply": "继续。"}
        )
        self.assertEqual(receipt["scene_skipped"], 3)
        self.assertTrue(all(d.status in ("allowed", "intentional_skip") for d in day.coverage))

    def test_no_prefix_tail_gray_or_neighbor_penalty(self):
        rows = [
            row("early", 0, "嗯"),
            row("cue", 1, "甲乙"),
            row("next", 2, "午饭点了粥"),
            row("tail", 1439, "亲亲抱抱"),
        ]
        day, _ = prepare(rows)
        self.assertEqual(len(day.messages), 4)
        self.assertFalse(day.context_complete)

    def test_scene_mask_precedes_word_whitelist_and_day_cutoff(self):
        rows = [row("a", 239, "保留甲乙"), row("b", 240, "保留甲乙")]
        day, _ = prepare(
            rows, ranges=((START + timedelta(minutes=235), START + timedelta(minutes=245)),), cutoff_hour=4
        )
        self.assertFalse(day.messages)
        self.assertEqual(len(day.coverage), 1)

    def test_bound_manifest_rejects_changed_source_window_and_invalid_range(self):
        rows = [row("a", 20, "正文")]
        manifest = SceneManifest("0" * 64, START, END, (), "user_confirmed")
        with self.assertRaises(PrivacyInputError):
            prepare(rows, scenes=manifest)
        for ranges in (
            ((START - timedelta(minutes=1), START),),
            ((START, END + timedelta(seconds=1)),),
            ((START, START),),
        ):
            with self.assertRaises(PrivacyInputError):
                prepare(rows, ranges=ranges)
        with self.assertRaises(PrivacyInputError):
            prepare(rows, snapshot_complete=False)
        with self.assertRaises(PrivacyInputError):
            prepare_scene_redacted_day(
                rows, DAY, window_start=START, window_end=END, scenes=manifest, lexicon=None, snapshot_complete=True
            )

    def test_native_thinking_is_never_inspected_or_hashed(self):
        class Native:
            def __str__(self):
                raise AssertionError("native must not be read")

        rows = [row("a", 1, "甲乙正文")]
        self.assertEqual(prepare(rows), prepare([{**r, "thinking": Native(), "title": Native()} for r in rows]))

    def test_revisions_bind_dictionary_source_scene_and_redacted_text(self):
        rows = [row("a", 1, "甲乙正文")]
        a, _ = prepare(rows)
        for b, _ in (
            prepare(rows, lexicon=ClerkLexicon(2, ("甲乙",), (), False)),
            prepare([row("a", 1, "甲乙不同正文")]),
            prepare(rows, ranges=((START, START + timedelta(minutes=2)),)),
        ):
            self.assertNotEqual(a.selection_revision, b.selection_revision)

    def test_downstream_evidence_and_quotes_use_sanitized_coordinates(self):
        day, _ = prepare([row("a", 1, "甲乙，午饭点了粥。")])
        # Synthetic-only gate removal to inspect the eventual wire contract.
        approved = replace(day, privacy_review_required=False)
        chunk = split_sources(approved, 16000)[0]
        fragment = chunk["sources"][0]
        self.assertEqual(fragment["text"], "**，午饭点了粥。")
        self.assertEqual(fragment["fragment_id"], "f1")
        self.assertIn(MASKED_TEXT_RULE, chunk["instructions"])
        cards = validate_facts(
            {
                "facts": [
                    {
                        "kind": "food",
                        "text": "点了午饭",
                        "fragment_ids": [fragment["fragment_id"]],
                        "importance": 5,
                        "due_date": None,
                    }
                ]
            },
            chunk["sources"],
        )
        sources = {s.source_id: s for s in approved.messages}
        self.assertEqual(source_excerpt(cards[0]["evidence"][0], sources), fragment["text"])
        payload, _ = build_refinement(approved, cards)
        wire = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("甲乙", wire)
        self.assertIn(MASKED_TEXT_RULE, payload["instructions"])
        self.assertEqual(digest(approved.messages[0].text), cards[0]["evidence"][0]["source_sha256"])
        rendered, _ = render_impression(approved, cards)
        self.assertNotIn("甲乙", rendered)
        self.assertIn("**，午饭点了粥。", rendered)

    def test_short_extract_ids_are_exactly_bound_to_current_fragment_sources(self):
        from clerk_pipeline import SHORT_FRAGMENT_RULE
        from clerk_chatgpt import request_body

        day, _ = prepare([row("a", 1, "甲乙，午饭点了粥。"), row("b", 2, "文件完成了。")])
        chunks = split_sources(replace(day, privacy_review_required=False), 16000)
        self.assertEqual([f["fragment_id"] for f in chunks[0]["sources"]], ["f1", "f2"])
        self.assertIn(SHORT_FRAGMENT_RULE, request_body(chunks[0], "synthetic")["instructions"])
        reply = {
            "facts": [{"kind": "food", "text": "点了粥", "fragment_ids": ["f1"], "importance": 5, "due_date": None}]
        }
        card = validate_facts(reply, chunks[0]["sources"])[0]
        self.assertEqual(card["evidence"][0]["source_id"], "a")
        self.assertEqual(card["evidence"][0]["source_sha256"], digest("**，午饭点了粥。"))
        reply["facts"][0]["fragment_ids"] = ["f999"]
        from clerk_pipeline import ClerkError

        with self.assertRaises(ClerkError):
            validate_facts(reply, chunks[0]["sources"])

    def test_strict_speaker_instruction_is_exact_and_default_chunks_unchanged(self):
        from clerk_pipeline import SPEAKER_EVIDENCE_RULE, NightClerkService, encoded_size, ClerkError
        from clerk_chatgpt import request_body

        day, _ = prepare([row("a", 1, "甲乙，午饭点了粥。")])
        approved = replace(day, privacy_review_required=False)
        chunks = split_sources(approved, 16000)
        service = NightClerkService.__new__(NightClerkService)
        service.budget = 16000
        service.request_size = encoded_size
        self.assertEqual(service.extraction_chunks(approved), chunks)
        payload = {**chunks[0], "instructions": chunks[0]["instructions"] + "\n" + SPEAKER_EVIDENCE_RULE}
        self.assertEqual(request_body(payload, "synthetic")["instructions"], payload["instructions"])
        payload["instructions"] += "\nunknown override"
        with self.assertRaises(ClerkError):
            request_body(payload, "synthetic")

    def test_plain_summary_instruction_does_not_relax_quote_validation(self):
        from clerk_pipeline import SPEAKER_EVIDENCE_RULE, PLAIN_SUMMARY_RULE, ClerkError
        from clerk_chatgpt import request_body

        day, _ = prepare([row("a", 1, "提到合成书名。")])
        payload = split_sources(replace(day, privacy_review_required=False), 16000)[0]
        payload = {
            **payload,
            "instructions": payload["instructions"] + "\n" + SPEAKER_EVIDENCE_RULE + "\n" + PLAIN_SUMMARY_RULE,
        }
        self.assertEqual(request_body(payload, "synthetic")["instructions"], payload["instructions"])
        reply = {
            "facts": [
                {
                    "kind": "events",
                    "text": "提到“合成书名”。",
                    "fragment_ids": ["f1"],
                    "importance": 5,
                    "due_date": None,
                }
            ]
        }
        with self.assertRaises(ClerkError):
            validate_facts(reply, payload["sources"])

    def test_legacy_default_identity_is_unchanged(self):
        self.assertEqual(PrivacyPolicy().revision(), "9548eb4437d4abfb58cfc3f7130f850aaec4fd92259a2f09e70f34239a798050")


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_payload_chunk_refine_and_adapter_all_stay_locked(self):
        day, _ = prepare([row("a", 1, "甲乙正文")])
        self.assertTrue(day.privacy_review_required)

        class Adapter:
            async def generate(self, payload):
                raise AssertionError("must not dispatch")

        with self.assertRaises(PrivacyInputError):
            day.payload()
        with self.assertRaises(PrivacyInputError):
            split_sources(day, 16000)
        with self.assertRaises(PrivacyInputError):
            build_refinement(day, [])
        with self.assertRaises(PrivacyInputError):
            await request_clerk(day, adapter=Adapter())


if __name__ == "__main__":
    unittest.main()
