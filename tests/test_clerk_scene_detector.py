"""Synthetic automatic scene boundaries; no real source or labels."""

from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import unittest

from clerk_input import request_clerk
from clerk_pipeline import split_sources
from clerk_privacy import PrivacyInputError, PrivacyPolicy
from clerk_redaction import ClerkLexicon, prepare_scene_redacted_day, visible_source_revision
from clerk_scene_detector import SceneDetectionPolicy, detect_scene_ranges, prepare_automatic_scene_day, scene_features

START = datetime.fromisoformat("2002-01-01T00:00:00+08:00")
END = START + timedelta(days=1)
DAY = date(2002, 1, 1)
POLICY = SceneDetectionPolicy()
LEXICON = ClerkLexicon(1, ("合成替换词",), ("合成保留词",), False)
ACTION = "合成识别信号：我们正在做爱。"


def row(mid, minute, text, speaker="知知", conv="fixture", **extra):
    return {
        "msg_uuid": mid,
        "conv_uuid": conv,
        "speaker": speaker,
        "at": (START + timedelta(minutes=minute)).isoformat(),
        "text": text,
        **extra,
    }


def detect(rows, **kwargs):
    return detect_scene_ranges(rows, window_start=START, window_end=END, snapshot_complete=True, **kwargs)


def prepare(rows, **kwargs):
    return prepare_automatic_scene_day(
        rows, DAY, window_start=START, window_end=END, snapshot_complete=True, lexicon=LEXICON, cutoff_hour=0, **kwargs
    )


class AutomaticScenes(unittest.TestCase):
    def test_bare_words_lists_medical_notes_and_affection_do_not_start_scene(self):
        cases = [
            "阴蒂、阴道、龟头。",
            "医学说明：" + "普通讲解。" * 100 + "阴道、阴蒂。",
            "词表：" + "普通注释。" * 100 + "阴道、阴蒂、龟头。",
            "公开感受。" * 100 + "鸡巴。",
            "颤抖" * 30,
            "亲亲抱抱，晚上抱着睡，喝水晚安。",
        ]
        for text in cases:
            with self.subTest(length=len(text)):
                self.assertFalse(detect([row("a", 60, text)])[0].ranges)

    def test_strong_start_preroll_masks_both_speakers_and_other_conversations(self):
        rows = [
            row("normal", 49, "午饭"),
            row("pre", 51, "合成前奏", speaker="顾凛"),
            row("start", 60, ACTION),
            row("other", 65, "同时发生的消息", conv="other"),
            row("after", 91, "日常"),
        ]
        day, receipt = prepare(rows)
        self.assertEqual({s.source_id for s in day.messages}, {"normal", "after"})
        self.assertEqual(receipt["detected_ranges"], 1)
        self.assertEqual(receipt["scenes"][0]["reason"], "no_signal_fallback")

    def test_long_narration_needs_activity_and_multiple_cues(self):
        text = "合成叙述。" * 80 + "摸着阴蒂、阴道。"
        self.assertTrue(scene_features(text, POLICY)["opening"])
        self.assertTrue(detect([row("a", 60, text)])[0].ranges)

    def test_dense_short_activity_and_nearby_prelude_are_backfilled(self):
        rows = [row("hint", 60, "合成前奏。" * 80 + "阴蒂、阴道。"), row("activity", 65, ACTION)]
        manifest, _ = detect(rows)
        self.assertEqual(manifest.ranges[0][0], START + timedelta(minutes=50))
        self.assertFalse(detect(rows[:1])[0].ranges)

    def test_specific_care_closes_without_daily_fact_anchors(self):
        rows = [
            row("start", 60, ACTION),
            row("care", 65, "热毛巾，帮你清理。"),
            row("short", 66, "嗯。"),
            row("normal", 76, "蓝色风筝。"),
            row("late", 180, "抱着睡。"),
        ]
        day, receipt = prepare(rows)
        self.assertEqual({s.source_id for s in day.messages}, {"normal", "late"})
        self.assertEqual(datetime.fromisoformat(receipt["scenes"][0]["end"]), START + timedelta(minutes=75))

    def test_care_with_anatomical_terms_is_care_without_an_activity_action(self):
        rows = [row("start", 60, ACTION), row("care", 65, "热毛巾，擦干净阴道、阴蒂。"), row("after", 76, "日常")]
        _, receipt = prepare(rows)
        self.assertEqual(receipt["scenes"][0]["reason"], "aftercare_tail")
        self.assertEqual(datetime.fromisoformat(receipt["scenes"][0]["end"]), START + timedelta(minutes=75))

    def test_care_mixed_with_real_activity_does_not_close_scene(self):
        rows = [
            row("start", 60, ACTION),
            row("care", 65, "热毛巾。"),
            row("restart", 70, "疼不疼，我们继续做爱。"),
            row("during", 90, "未结束的承接"),
        ]
        day, receipt = prepare(rows)
        self.assertFalse(day.messages)
        self.assertEqual(datetime.fromisoformat(receipt["scenes"][0]["end"]), START + timedelta(minutes=100))

    def test_multi_family_contact_activity_overrides_care_without_anatomical_word(self):
        rows = [
            row("start", 60, ACTION),
            row("mixed", 65, "疼不疼？我含住你，舌尖轻轻打转，你喘息着颤抖。"),
            row("reply", 80, "未结束的含混承接"),
        ]
        day, receipt = prepare(rows)
        self.assertFalse(day.messages)
        self.assertEqual(datetime.fromisoformat(receipt["scenes"][0]["end"]), START + timedelta(minutes=95))

    def test_cross_conversation_pure_anatomical_care_does_not_refresh_activity(self):
        rows = [
            row("start", 60, ACTION),
            row("care", 65, "热毛巾。"),
            row("other_care", 70, "热毛巾，清理干净阴道。", conv="other"),
        ]
        manifest, _ = detect(rows)
        self.assertEqual(manifest.ranges[0][1], START + timedelta(minutes=75))

    def test_negated_hypothetical_past_and_quoted_mentions_do_not_open_scene(self):
        for text in (
            "我们没有在做爱。",
            "我们不是在做爱。",
            "假如我们在做爱。",
            "昨天我们在做爱。",
            "词表里的短语是“我们在做爱”。",
            '他说"我们在做爱"。',
            "幻想我舔阴蒂。",
        ):
            with self.subTest(text=text):
                self.assertFalse(detect([row("a", 60, text)])[0].ranges)
        self.assertTrue(detect([row("a", 60, "我们没有在做爱，但是我们现在开始做爱。")])[0].ranges)

    def test_past_and_conditional_scope_survives_commas_and_quoted_fragments(self):
        for text in (
            "昨天，我们在做爱。",
            "假如有一天，我们正在做爱。",
            "如果有空，我们就在家做爱。",
            "假如他说“继续”，我们在做爱。",
            "如果，他说没有，我舔阴蒂。",
        ):
            with self.subTest(text=text):
                self.assertFalse(detect([row("a", 60, text)])[0].ranges)
        self.assertTrue(detect([row("a", 60, "昨天的事不提了，不过我们现在开始做爱。")])[0].ranges)

    def test_short_replies_and_weak_activity_refresh_active_clock(self):
        for text in ("继续。", "嗯。", "合成孤立词：私处。"):
            rows = [row("start", 60, ACTION), row("continuation", 89, text), row("held", 100, "承接")]
            self.assertFalse(prepare(rows)[0].messages)
            self.assertEqual(detect(rows)[0].ranges[0][1], START + timedelta(minutes=119))

    def test_generic_water_goodnight_do_not_extend_active_or_care_clocks(self):
        for care in (False, True):
            rows = [row("start", 60, ACTION)]
            if care:
                rows.append(row("care", 65, "热毛巾。"))
            rows.extend([row("water", 70, "喝水。"), row("sleep", 74, "晚安。"), row("after", 91, "日常")])
            expected = 75 if care else 90
            self.assertEqual(detect(rows)[0].ranges[0][1], START + timedelta(minutes=expected))

    def test_expiry_is_settled_before_later_isolated_sensitive_words(self):
        rows = [row("start", 60, ACTION), row("much_later", 180, "合成替换词。"), row("care", 181, "抱着睡。")]
        day, receipt = prepare(rows)
        self.assertEqual(len(receipt["scenes"]), 1)
        self.assertEqual({s.source_id: s.text for s in day.messages}, {"much_later": "**。", "care": "抱着睡。"})

    def test_care_deadline_endpoint_and_new_care_are_explicit(self):
        for seconds in (-1, 0, 1):
            at = 75 + seconds / 60
            rows = [row("start", 60, ACTION), row("care", 65, "热毛巾。"), row("late_care", at, "抱着睡。")]
            expected = at + 10 if seconds <= 0 else 75
            self.assertEqual(detect(rows)[0].ranges[0][1], START + timedelta(minutes=expected))

    def test_activity_timeout_endpoint_and_next_word(self):
        for seconds in (-1, 0, 1):
            at = 90 + seconds / 60
            rows = [row("start", 60, ACTION), row("cue", at, "合成孤立词：私处。")]
            expected = at + 30 if seconds <= 0 else 90
            self.assertEqual(detect(rows)[0].ranges[0][1], START + timedelta(minutes=expected))

    def test_cross_conversation_weak_activity_refreshes_but_care_cannot_close(self):
        rows = [
            row("start", 60, ACTION),
            row("other_care", 65, "热毛巾。", conv="other"),
            row("other_activity", 89, "合成孤立词：私处。", conv="other"),
        ]
        manifest, receipt = detect(rows)
        self.assertEqual(manifest.ranges[0][1], START + timedelta(minutes=119))
        self.assertEqual(receipt["scenes"][0]["reason"], "no_signal_fallback")

    def test_open_right_scene_is_clipped_and_identified_as_unfinished(self):
        rows = [row("start", 1430, ACTION)]
        manifest, receipt = detect(rows)
        self.assertEqual(manifest.ranges[0][1], END)
        self.assertEqual(receipt["scenes"][0]["reason"], "window_open_scene")

    def test_window_scope_snapshot_and_policy_validation(self):
        with self.assertRaises(PrivacyInputError):
            detect_scene_ranges([], window_start=START, window_end=END)
        with self.assertRaises(PrivacyInputError):
            detect([row("outside", -1, "日常")])
        with self.assertRaises(PrivacyInputError):
            SceneDetectionPolicy(care_tail=timedelta(0))
        with self.assertRaises(PrivacyInputError):
            SceneDetectionPolicy(long_distinct_cues=True)

    def test_native_thinking_does_not_affect_detection_or_identity(self):
        class Native:
            def __str__(self):
                raise AssertionError("native must never be read")

        rows = [row("start", 60, ACTION)]
        self.assertEqual(prepare(rows), prepare([{**r, "thinking": Native()} for r in rows]))

    def test_manifest_binds_the_immutable_message_snapshot_actually_inspected(self):
        original = row("start", 60, ACTION)

        class ChangingRow(dict):
            reads = 0

            def get(self, key, default=None):
                if key == "text":
                    self.reads += 1
                    return ACTION if self.reads <= 2 else "changed after snapshot"
                return super().get(key, default)

        manifest, _ = detect([ChangingRow(original)])
        self.assertEqual(manifest.source_revision, visible_source_revision([original]))

    def test_detector_policy_and_source_revision_bind_automatic_manifest(self):
        rows = [row("start", 60, ACTION)]
        one, _ = prepare(rows)
        two, _ = prepare(rows, policy=SceneDetectionPolicy(care_tail=timedelta(minutes=11)))
        self.assertNotEqual(one.selection_revision, two.selection_revision)
        manifest, _ = detect(rows)
        with self.assertRaises(PrivacyInputError):
            prepare_scene_redacted_day(
                rows,
                DAY,
                window_start=START,
                window_end=END,
                scenes=replace(manifest, detector_revision=None),
                lexicon=LEXICON,
                snapshot_complete=True,
            )
        self.assertEqual(PrivacyPolicy().revision(), "9548eb4437d4abfb58cfc3f7130f850aaec4fd92259a2f09e70f34239a798050")

    def test_receipt_does_not_echo_source_or_private_lexicon(self):
        _, receipt = prepare([row("a", 60, ACTION + "SOURCE_SECRET")])
        encoded = json.dumps(receipt, ensure_ascii=False)
        self.assertNotIn("SOURCE_SECRET", encoded)
        self.assertNotIn("合成替换词", encoded)


class AutomaticDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_even_empty_detection_stays_locked(self):
        day, _ = prepare([row("ordinary", 60, "日常合成替换词")])
        self.assertTrue(day.messages)

        class Adapter:
            async def generate(self, payload):
                raise AssertionError("must not dispatch")

        with self.assertRaises(PrivacyInputError):
            day.payload()
        with self.assertRaises(PrivacyInputError):
            split_sources(day, 16000)
        with self.assertRaises(PrivacyInputError):
            await request_clerk(day, adapter=Adapter())


if __name__ == "__main__":
    unittest.main()
