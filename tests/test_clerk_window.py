"""Synthetic scoped-preview boundary checks; no providers or real originals."""

from datetime import date, datetime, timedelta
import unittest

from clerk_input import prepare_clerk_day
from clerk_pipeline import split_sources
from clerk_privacy import PrivacyInputError
from clerk_privacy import PrivacyPolicy, filter_transcript
from clerk_window import prepare_window_day

START = datetime.fromisoformat("2000-01-01T00:00:00+08:00")
END = START + timedelta(days=1)
DAY = date(2000, 1, 1)


def row(mid, minutes, text, conv="c", **extra):
    return {
        "msg_uuid": mid,
        "conv_uuid": conv,
        "speaker": "知知",
        "at": (START + timedelta(minutes=minutes)).isoformat(),
        "text": text,
        **extra,
    }


def prepare(rows, **kw):
    return prepare_window_day(
        rows, DAY, window_start=START, window_end=END, snapshot_complete=True, cutoff_hour=0, **kw
    )


class WindowTests(unittest.TestCase):
    def established(self):
        return [
            row("first", 0, "未知前文的短句。"),
            row("care", 1, "热毛巾、喝水。"),
            row("ordinary1", 12, "代码测试完成。"),
            row("ordinary2", 15, "文件整理好了。"),
        ]

    def test_unknown_prefix_is_held_and_time_alone_never_releases(self):
        day = prepare([row("a", 1, "代码测试。"), row("b", 600, "午饭之后看文件。")])
        self.assertFalse(day.messages)
        self.assertTrue(all(d.status == "uncertain_hold" for d in day.coverage))

    def test_same_conversation_care_tail_ordinary_spacing_and_observation_release(self):
        day = prepare(self.established() + [row("during", 40, "开会。"), row("safe", 46, "午饭吃面。")])
        self.assertEqual([m.source_id for m in day.messages], ["safe"])
        self.assertFalse(day.context_complete)
        self.assertTrue(day.payload()["missing_context"])
        self.assertTrue(split_sources(day, 16000)[0]["missing_context"])

    def test_another_conversation_cannot_release_unknown_prefix(self):
        day = prepare(self.established() + [row("safe", 60, "午饭吃面。"), row("other", 60, "买菜。", conv="other")])
        self.assertEqual([m.source_id for m in day.messages], ["safe"])

    def test_private_reopening_during_observation_keeps_prefix_held(self):
        day = prepare(self.established() + [row("reopen", 30, "亲吻。"), row("later", 600, "文件整理。")])
        self.assertFalse(day.messages)

    def test_authorized_window_end_caps_context_not_current_wall_clock(self):
        day = prepare(self.established() + [row("safe", 60, "午饭。"), row("tail", 1420, "文件整理。")])
        self.assertEqual([m.source_id for m in day.messages], ["safe"])
        self.assertEqual(next(d.reason for d in day.coverage if d.msg_uuid == "tail"), "window_tail_observation")

    def test_scope_and_snapshot_completeness_are_mandatory(self):
        with self.assertRaises(PrivacyInputError):
            prepare_window_day([], DAY, window_start=START, window_end=END)
        with self.assertRaises(PrivacyInputError):
            prepare([row("outside", -1, "午饭。")])
        with self.assertRaises(PrivacyInputError):
            prepare([row("outside", 1440, "午饭。")])

    def test_native_payload_is_never_stringified_or_used(self):
        class Native:
            def __str__(self):
                raise AssertionError("must not read native")

        first = self.established() + [row("safe", 60, "午饭。")]
        self.assertEqual(prepare(first), prepare([{**r, "thinking": Native()} for r in first]))

    def test_full_history_entry_still_refuses_a_partial_window(self):
        with self.assertRaises(PrivacyInputError):
            prepare_clerk_day(self.established(), DAY, as_of=END)

    def test_observed_scene_and_aftercare_can_close_without_ordinary_topic_keywords(self):
        rows = [
            row("private", 0, "测试识别信号：高潮。"),
            row("care", 5, "热毛巾喝水。"),
            row("still-held", 40, "我们谈谈蓝色风筝。"),
            row("safe", 66, "我们谈谈蓝色风筝。"),
        ]
        day = prepare(rows)
        self.assertEqual([m.source_id for m in day.messages], ["safe"])
        default = filter_transcript(rows, as_of=END, history_complete=True)
        self.assertIsNone(default.intervals[0].end)
        self.assertFalse(PrivacyPolicy().close_on_timeout)

    def test_nearby_late_care_reopens_timeout_and_keeps_intervening_messages_private(self):
        rows = [
            row("private", 0, "测试识别信号：高潮。"),
            row("care1", 5, "热毛巾。"),
            row("in-between", 40, "蓝色风筝。"),
            row("care2", 50, "喝水、抱着睡。"),
            row("wait", 90, "蓝色风筝。"),
            row("safe", 111, "蓝色风筝。"),
        ]
        self.assertEqual([m.source_id for m in prepare(rows).messages], ["safe"])

    def test_timeout_without_care_does_not_release_unknown_left_boundary(self):
        rows = [row("private", 0, "测试识别信号：高潮。"), row("maybe", 120, "蓝色风筝。")]
        self.assertFalse(prepare(rows).messages)

    def test_care_at_29_or_30_minutes_after_closure_reopens_but_31_does_not(self):
        for delay in (29, 30, 31):
            rows = [
                row("private", 0, "测试识别信号：高潮。"),
                row("care1", 5, "热毛巾。"),
                row("middle", 40, "蓝色风筝。"),
                row("care2", 35 + delay, "喝水。"),
            ]
            result = filter_transcript(
                rows, as_of=END, history_complete=True, policy=PrivacyPolicy(close_on_timeout=True)
            )
            self.assertEqual(result.intervals[0].end, START + timedelta(minutes=65 + delay if delay <= 30 else 35))
            middle = next(d for d in result.decisions if d.msg_uuid == "middle")
            self.assertEqual(middle.status == "allowed", delay == 31)

    def test_new_signal_clears_earlier_care_for_unknown_prefix_release(self):
        rows = [
            row("private", 0, "测试识别信号：高潮。"),
            row("care", 5, "热毛巾。"),
            row("new", 10, "测试识别信号：高潮。"),
            row("later", 200, "蓝色风筝。"),
        ]
        self.assertFalse(prepare(rows).messages)

    def test_weak_only_scene_cannot_release_unknown_left_prefix(self):
        rows = [row("weak", 0, "吻。"), row("care", 5, "热毛巾。"), row("later", 200, "蓝色风筝。")]
        self.assertFalse(prepare(rows).messages)

    def test_ended_scene_without_new_same_conversation_message_does_not_mask_other_safe_conversation(self):
        rows = (
            [row("private", 0, "测试识别信号：高潮。", conv="other"), row("care-other", 5, "热毛巾。", conv="other")]
            + self.established()
            + [row("safe", 66, "午饭吃面。")]
        )
        self.assertEqual([m.source_id for m in prepare(rows).messages], ["safe"])


if __name__ == "__main__":
    unittest.main()
