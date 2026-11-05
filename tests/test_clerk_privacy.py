"""Synthetic-only privacy scenarios; no real archive, files or model calls."""

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
import json
import unittest

from clerk_input import prepare_clerk_day, request_clerk
from clerk_privacy import PrivacyInputError, PrivacyPolicy, filter_transcript


START = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
NOW = START + timedelta(days=2)
DAY = date(2026, 8, 20)
SECRET = "PRIVATE_SYNTHETIC_SENTINEL"


def row(mid, minute, text, *, speaker="知知", conv="fixture", **extra):
    return {"msg_uuid": mid, "conv_uuid": conv, "speaker": speaker,
            "at": (START + timedelta(minutes=minute)).isoformat(),
            "text": text, "thinking": "", **extra}


def select(rows, **kwargs):
    return filter_transcript(rows, as_of=NOW, history_complete=True, **kwargs)


def statuses(result):
    return {d.msg_uuid: d.status for d in result.decisions}


class PrivacyScenarios(unittest.TestCase):
    def test_ordinary_day_and_water_are_allowed(self):
        result = select([row("a", 0, "今天上班，记得喝水"),
                         row("b", 5, "用毛巾擦桌子，然后去超市买菜")])
        self.assertEqual(set(statuses(result).values()), {"allowed"})

    def test_length_alone_does_not_open_interval(self):
        self.assertEqual(statuses(select([row("a", 0, "今天写代码。" * 100)]))["a"], "allowed")

    def test_database_insert_word_alone_is_not_a_private_signal(self):
        result = select([row("a", 0, "代码测试通过，向数据库插入一条记录")])
        self.assertEqual(statuses(result)["a"], "allowed")

    def test_simple_markdown_spacing_does_not_hide_signal(self):
        for text in ("高**潮", "高 潮", "高\u200b潮"):
            with self.subTest(text=text):
                self.assertEqual(statuses(select([row("a", 0, text)]))["a"], "intentional_skip")

    def test_long_dense_start_includes_preceding_flirt_window(self):
        rows = [row("early", -11, "晚饭准备好了"), row("pre", -9, SECRET),
                row("start", 0, "虚构占位。" * 70 + "舌尖、颤抖"),
                row("short", 3, "嗯", speaker="顾凛")]
        decisions = statuses(select(rows))
        self.assertEqual(decisions["early"], "allowed")
        self.assertEqual({decisions[m] for m in ("pre", "start", "short")}, {"intentional_skip"})

    def test_short_dense_message_starts_without_300_characters(self):
        result = select([row("a", 0, "舌尖、喘息、颤抖"), row("b", 1, SECRET)])
        self.assertEqual(set(statuses(result).values()), {"intentional_skip"})

    def test_single_ambiguous_signal_is_held(self):
        result = select([row("a", 0, "虚构测试：亲吻"), row("b", 1, "嗯")])
        self.assertEqual(set(statuses(result).values()), {"uncertain_hold"})

    def test_weak_chain_promotes_whole_interval(self):
        result = select([row("a", 0, "舌尖"), row("b", 3, "喘息"), row("c", 6, "颤抖")])
        self.assertEqual(set(statuses(result).values()), {"intentional_skip"})

    def test_both_speakers_and_keyword_free_messages_stay_inside(self):
        rows = [row("a", 0, "测试标记：高潮"), row("b", 1, "好", speaker="顾凛"),
                row("c", 2, SECRET), row("d", 3, "嗯", speaker="顾凛")]
        self.assertEqual(len(select(rows).allowed_messages()), 0)

    def test_entire_aftercare_and_tail_remain_excluded(self):
        rows = [row("a", 0, "测试标记：做爱"), row("care", 12, "热毛巾，喝水"),
                row("care2", 16, "疼不疼", speaker="顾凛"), row("tail", 22, SECRET),
                row("daily1", 30, "明天上班"), row("daily2", 33, "代码测试通过"),
                row("safe", 34, "晚饭吃面")]
        decisions = statuses(select(rows))
        self.assertTrue(all(decisions[m] != "allowed" for m in ("a", "care", "care2", "tail", "daily1", "daily2")))
        self.assertEqual(decisions["safe"], "allowed")

    def test_half_hour_and_long_silence_do_not_release_short_replies(self):
        result = select([row("a", 0, "测试标记：做爱"), row("b", 40, SECRET),
                         row("c", 100, "嗯", speaker="顾凛")])
        self.assertEqual(statuses(result)["b"], "uncertain_hold")
        self.assertEqual(statuses(result)["c"], "uncertain_hold")

    def test_no_aftercare_requires_multiple_spaced_daily_messages(self):
        rows = [row("a", 0, "测试标记：高潮"), row("daily1", 60, "今天上班"),
                row("daily2", 63, "快递到了"), row("safe", 64, "准备晚饭")]
        decisions = statuses(select(rows))
        self.assertNotEqual(decisions["daily1"], "allowed")
        self.assertNotEqual(decisions["daily2"], "allowed")
        self.assertEqual(decisions["safe"], "allowed")

    def test_same_time_daily_messages_do_not_confirm_closing(self):
        rows = [row("a", 0, "测试标记：高潮"), row("b", 60, "今天上班"),
                row("c", 60, "快递到了"), row("d", 61, SECRET)]
        self.assertEqual(len(select(rows).allowed_messages()), 0)

    def test_restarting_during_care_extends_exclusion(self):
        rows = [row("a", 0, "测试标记：高潮"), row("care", 8, "喝水"),
                row("new", 15, "测试标记：做爱"), row("b", 20, SECRET)]
        self.assertEqual(len(select(rows).allowed_messages()), 0)

    def test_new_scene_backtracking_overlaps_prior_closing_interval(self):
        rows = [row("start", 0, "测试标记：高潮"), row("care", 10, "热毛巾"),
                row("daily1", 25, "今天上班"), row("daily2", 28, "快递到了"),
                row("pre", 30, SECRET), row("new", 35, "测试标记：做爱")]
        self.assertNotEqual(statuses(select(rows))["pre"], "allowed")

    def test_future_message_is_held(self):
        result = filter_transcript([row("a", 20, "晚饭吃面")], as_of=START,
                                   history_complete=True)
        self.assertEqual(statuses(result)["a"], "uncertain_hold")

    def test_simultaneous_other_conversation_is_excluded(self):
        rows = [row("a", 0, "测试标记：做爱", conv="first"),
                row("copy", 1, SECRET, conv="second")]
        self.assertEqual(len(select(rows).allowed_messages()), 0)

    def test_timestamped_positive_marker_works_without_keyword(self):
        result = select([row("before", -11, "晚饭吃面"), row("a", 1, SECRET)],
                        positive_markers=[START])
        self.assertEqual(statuses(result)["before"], "allowed")
        self.assertEqual(statuses(result)["a"], "intentional_skip")

    def test_complete_history_must_be_explicit(self):
        with self.assertRaises(PrivacyInputError):
            filter_transcript([row("a", 0, "晚饭吃面")], as_of=NOW)

    def test_duplicate_copy_is_processed_once(self):
        message = row("a", 0, "晚饭吃面")
        self.assertEqual(len(select([message, dict(message)]).decisions), 1)

    def test_conflicting_revisions_abort_without_echoing_body(self):
        with self.assertRaises(PrivacyInputError) as error:
            select([row("a", 0, "晚饭吃面"), row("a", 0, SECRET)])
        self.assertNotIn(SECRET, str(error.exception))

    def test_bad_times_abort_instead_of_losing_interval_context(self):
        for value in (None, "invalid", "2026-08-20T12:00:00"):
            with self.subTest(value=value), self.assertRaises(PrivacyInputError):
                select([row("a", 0, SECRET, at=value)])

    def test_unassembled_fragment_is_rejected(self):
        with self.assertRaises(PrivacyInputError):
            select([row("a", 0, SECRET, fragment_index=0, fragment_count=2)])

    def test_order_of_import_does_not_change_selection(self):
        rows = [row("a", -9, SECRET), row("b", 0, "测试标记：做爱"), row("c", 1, "嗯")]
        self.assertEqual(select(rows), select(reversed(rows)))

    def test_same_timestamp_before_trigger_is_still_excluded(self):
        result = select([row("a", 0, SECRET), row("z", 0, "测试标记：高潮")])
        self.assertEqual(len(result.allowed_messages()), 0)

    def test_recent_tail_is_held_to_allow_future_backtracking(self):
        result = filter_transcript([row("a", 0, "晚饭吃面")], as_of=START + timedelta(minutes=3),
                                   history_complete=True)
        self.assertEqual(statuses(result)["a"], "uncertain_hold")

    def test_native_thinking_does_not_exclude_visible_text(self):
        result = select([row("a", 0, "普通可见文字", thinking="测试标记：高潮")])
        self.assertEqual(len(result.allowed_messages()), 1)
        self.assertFalse(hasattr(result.messages[0], "thinking"))

    def test_thinking_cannot_close_private_interval(self):
        rows = [row("a", 0, "测试标记：高潮"),
                row("b", 31, SECRET, thinking="明天上班"),
                row("c", 34, SECRET, thinking="代码测试通过"), row("d", 35, SECRET)]
        self.assertEqual(len(select(rows).allowed_messages()), 0)

    def test_other_conversation_cannot_close_private_interval(self):
        rows = [row("a", 0, "测试标记：高潮", conv="private"),
                row("b", 31, "明天上班", conv="work"), row("c", 34, "代码测试通过", conv="work"),
                row("d", 35, SECRET, conv="private")]
        self.assertEqual(len(select(rows).allowed_messages()), 0)

    def test_nearby_care_reopens_provisional_close_and_intervening_replies(self):
        rows = [row("a", 0, "测试标记：高潮"), row("care", 10, "热毛巾"),
                row("b", 25, "明天上班"), row("c", 28, "快递到了"),
                row("gap", 29, SECRET), row("care2", 30, "疼不疼")]
        self.assertEqual(len(select(rows).allowed_messages()), 0)

    def test_prior_exclusion_survives_revised_trigger_or_time(self):
        result = select([row("a", 0, SECRET)], withheld_ids=["a"])
        self.assertEqual(statuses(result)["a"], "intentional_skip")

    def test_receipt_and_reprs_do_not_contain_source_body(self):
        result = select([row("a", 0, "测试标记：做爱" + SECRET)])
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(SECRET, repr(result.messages))
        self.assertNotIn(SECRET, json.dumps(result.receipt(), ensure_ascii=False))
        self.assertNotIn("做爱", json.dumps(result.receipt(), ensure_ascii=False))

    def test_policy_and_source_revisions_change_cache_identity(self):
        rows = [row("a", 0, "晚饭吃面")]
        a = select(rows)
        b = select(rows, policy=PrivacyPolicy(pre_roll=timedelta(minutes=8)))
        c = select([row("a", 0, "晚饭吃饭")])
        self.assertNotEqual(a.selection_revision, b.selection_revision)
        self.assertNotEqual(a.selection_revision, c.selection_revision)

    def test_late_arrival_retroactively_removes_preceding_message(self):
        initial = [row("a", 0, SECRET)]
        self.assertEqual(statuses(select(initial))["a"], "allowed")
        revised = select(initial + [row("late", 9, "测试标记：高潮")])
        self.assertNotEqual(statuses(revised)["a"], "allowed")
        self.assertNotEqual(select(initial).selection_revision, revised.selection_revision)

    def test_invalid_policy_is_rejected(self):
        with self.assertRaises(PrivacyInputError):
            PrivacyPolicy(pre_roll=timedelta(0))


class ClerkInputScenarios(unittest.IsolatedAsyncioTestCase):
    async def test_provisional_close_window_cannot_send_before_later_care(self):
        rows = [row("a", 0, "测试标记：高潮"), row("care", 10, "热毛巾"),
                row("b", 25, "明天上班"), row("c", 28, "快递到了"), row("gap", 29, SECRET)]
        class Adapter:
            async def generate(self, payload):
                raise AssertionError("provisional private context must not be sent")
        for minute, snapshot in [(45, rows), (51, rows + [row("care2", 50, "疼不疼")])]:
            day = prepare_clerk_day(snapshot, DAY, as_of=START + timedelta(minutes=minute),
                                    history_complete=True)
            self.assertFalse(day.messages)
            await request_clerk(day, adapter=Adapter())

    async def test_review_regressions_do_not_reach_actual_adapter_payload(self):
        scenarios = [
            [row("a", 0, "测试标记：高潮"), row("b", 31, SECRET, thinking="明天上班"),
             row("c", 34, SECRET, thinking="代码测试通过"), row("d", 35, SECRET)],
            [row("a", 0, "测试标记：高潮", conv="private"), row("b", 31, "上班", conv="work"),
             row("c", 34, "代码", conv="work"), row("d", 35, SECRET, conv="private")],
            [row("a", 0, "测试标记：高潮"), row("care", 10, "热毛巾"), row("b", 25, "明天上班"),
             row("c", 28, "快递到了"), row("gap", 29, SECRET), row("care2", 30, "疼不疼")],
        ]
        class Adapter:
            async def generate(self, payload):
                self.payload = payload
                return payload
        for rows in scenarios:
            adapter = Adapter()
            day = prepare_clerk_day([row("safe", -30, "晚饭吃面")] + rows, DAY,
                                    as_of=NOW, history_complete=True)
            payload = await request_clerk(day, adapter=adapter)
            self.assertEqual([s["source_id"] for s in payload["sources"]], ["safe"])
            self.assertNotIn(SECRET, json.dumps(payload))

    async def test_actual_adapter_payload_contains_only_allowed_raw_ids_and_text(self):
        class Adapter:
            def __init__(self):
                self.calls = []

            async def generate(self, payload):
                self.calls.append(payload)
                return {"accepted_ids": [m["source_id"] for m in payload["sources"]]}

        rows = [row("safe", -30, "晚饭吃面", thinking="THINKING_SENTINEL",
                    conv_name=SECRET, extra={"quote": SECRET}),
                row("pre", -9, SECRET), row("start", 0, "测试标记：做爱"),
                row("short", 1, SECRET, speaker="顾凛"), row("care", 12, "热毛巾喝水")]
        day = prepare_clerk_day(rows, DAY, as_of=NOW, history_complete=True)
        adapter = Adapter()
        result = await request_clerk(day, adapter=adapter)
        self.assertEqual(result["accepted_ids"], ["safe"])
        self.assertEqual(len(adapter.calls), 1)
        serialized = json.dumps(adapter.calls, ensure_ascii=False)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("THINKING_SENTINEL", serialized)
        self.assertNotIn("做爱", serialized)
        self.assertNotIn("热毛巾", serialized)
        self.assertTrue(adapter.calls[0]["missing_context"])

    async def test_private_only_day_does_not_call_adapter(self):
        class Adapter:
            async def generate(self, payload):
                raise AssertionError("empty eligible input must not call model")

        day = prepare_clerk_day([row("a", 0, "测试标记：高潮")], DAY,
                                as_of=NOW, history_complete=True)
        self.assertEqual(await request_clerk(day, adapter=Adapter()),
                         {"skip": True, "reason": "no_eligible_sources"})

    async def test_full_timeline_is_filtered_before_four_am_day_split(self):
        # START is 20:00 Shanghai. The next day's 04:00 is 480 minutes later.
        rows = [row("start", 470, "测试标记：做爱"), row("after_cutoff", 481, SECRET),
                row("care", 490, "热毛巾喝水")]
        day = prepare_clerk_day(rows, date(2026, 8, 21), as_of=NOW, history_complete=True)
        self.assertEqual(day.messages, ())
        self.assertEqual({d.msg_uuid for d in day.coverage}, {"after_cutoff", "care"})

    async def test_midnight_does_not_reset_interval(self):
        rows = [row("start", 235, "测试标记：做爱"), row("after_midnight", 245, SECRET)]
        day = prepare_clerk_day(rows, DAY, as_of=NOW, history_complete=True)
        self.assertEqual(day.messages, ())

    async def test_original_rows_and_payload_copies_cannot_mutate_selected_sources(self):
        original = row("a", 0, "晚饭吃面")
        day = prepare_clerk_day([original], DAY, as_of=NOW, history_complete=True)
        original["text"] = SECRET
        payload = day.payload()
        payload["sources"][0]["text"] = SECRET
        self.assertEqual(day.payload()["sources"][0]["text"], "晚饭吃面")
        with self.assertRaises(FrozenInstanceError):
            day.messages[0].text = SECRET

    async def test_day_payload_revision_changes_when_source_is_revised(self):
        initial = prepare_clerk_day([row("a", 0, "晚饭吃面")], DAY,
                                    as_of=NOW, history_complete=True)
        revised = prepare_clerk_day([row("a", 0, "晚饭吃饭")], DAY,
                                    as_of=NOW, history_complete=True)
        self.assertNotEqual(initial.revision(), revised.revision())

    async def test_all_day_sources_are_original_text_without_budget_truncation(self):
        text = "纯虚构的普通日常记录。" * 1000
        day = prepare_clerk_day([row("a", 0, text)], DAY, as_of=NOW, history_complete=True)
        self.assertEqual(day.payload()["sources"][0]["text"], text)


if __name__ == "__main__":
    unittest.main()
