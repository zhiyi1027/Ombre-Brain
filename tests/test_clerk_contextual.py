"""Synthetic privacy precision/recall cases; never opens real data or models."""

from datetime import date, datetime, timedelta
import json
import unittest

from clerk_contextual import features, longest_hits, AFFECTION
from clerk_input import prepare_clerk_day, request_clerk, select_clerk_day
from clerk_pipeline import split_sources
from clerk_privacy import PrivacyInputError, PrivacyPolicy, filter_transcript
from clerk_window import prepare_contextual_window_day

START = datetime.fromisoformat("2026-08-20T04:00:00+08:00")
END = START + timedelta(days=1)
DAY = date(2026, 8, 20)
SECRET = "SYNTHETIC_PRIVATE_NOT_FOR_CLERK"
POLICY = PrivacyPolicy(contextual_signals=True)


def row(mid, minute, text, *, speaker="知知", conv="fixture", **extra):
    return {
        "msg_uuid": mid,
        "conv_uuid": conv,
        "speaker": speaker,
        "at": (START + timedelta(minutes=minute)).isoformat(),
        "text": text,
        **extra,
    }


def scan(rows, *, now=END, **kwargs):
    return filter_transcript(rows, as_of=now, history_complete=True, policy=POLICY, **kwargs)


def window(rows, *, end=END, **kwargs):
    return prepare_contextual_window_day(
        rows, DAY, window_start=START, window_end=end, snapshot_complete=True, **kwargs
    )


class ContextualPrivacy(unittest.TestCase):
    def test_legacy_cache_identity_and_defaults_are_unchanged(self):
        self.assertFalse(PrivacyPolicy().contextual_signals)
        self.assertEqual(PrivacyPolicy().revision(), "9548eb4437d4abfb58cfc3f7130f850aaec4fd92259a2f09e70f34239a798050")
        self.assertNotEqual(POLICY.revision(), PrivacyPolicy().revision())
        with self.assertRaises(PrivacyInputError):
            PrivacyPolicy(contextual_signals=True, close_on_timeout=True)

    def test_longest_matching_and_soft_repetition_never_become_sexual_density(self):
        self.assertEqual(longest_hits("亲吻", AFFECTION), ("亲吻",))
        text = "合成日常叙述。" * 100 + "亲吻" * 10
        self.assertFalse(features(text)["families"])
        result = scan([row("soft", 1, text)])
        self.assertEqual(result.decisions[0].status, "allowed")

    def test_one_risk_family_repeated_does_not_promote_to_confirmed(self):
        result = scan([row("a", 0, "颤抖" * 10), row("b", 3, "颤抖" * 10)])
        self.assertFalse(result.intervals[0].confirmed)
        self.assertFalse(result.allowed_messages())

    def test_isolated_explicit_flirt_only_releases_independent_statement_not_following_reply(self):
        result = scan(
            [
                row("flirt", 0, "合成孤立词：私处。"),
                row("affection", 11, "我好喜欢你。"),
                row("ambiguous", 12, "继续。"),
                row("daily", 20, "我看中这个睡衣了。"),
                row("yes", 21, "嗯。"),
            ]
        )
        self.assertEqual({m.msg_uuid for m in result.allowed_messages()}, {"affection", "daily"})
        self.assertTrue(all(i.kind != "scene" for i in result.intervals))

    def test_long_nonsexual_heart_with_one_anatomy_word_does_not_open_full_scene(self):
        result = scan(
            [
                row("heart", 0, "合成普通感受。" * 80 + "私处。", speaker="顾凛"),
                row("daily", 15, "午饭点了粥。"),
                row("reply", 16, "继续。"),
            ]
        )
        self.assertEqual({m.msg_uuid for m in result.allowed_messages()}, {"daily"})
        self.assertTrue(all(i.kind != "scene" for i in result.intervals))

    def test_sensitive_message_and_known_scene_cannot_be_rescued(self):
        rows = [
            row("cue", 0, "合成孤立词：私处，午饭点了粥。"),
            row("buffer", 5, "继续。"),
            row("scene", 30, "合成测试信号：现在开始做爱。"),
            row("daily", 90, "午饭点了粥。"),
            row("short", 91, "继续。"),
        ]
        self.assertFalse(scan(rows).allowed_messages())

    def test_daily_topic_cannot_rescue_mixed_ambiguous_continuation(self):
        rows = [
            row("cue", 0, "合成孤立词：私处。"),
            row("mixed", 20, "午饭点了粥，慢点，我受不了了。"),
            row("deictic", 21, "我喜欢你这样。"),
        ]
        self.assertFalse(scan(rows).allowed_messages())

    def test_closed_discussion_keeps_short_continuations_in_observation(self):
        rows = [
            row("prefix1", 40, "眼药用好了。"),
            row("prefix2", 43, "午饭点了粥。"),
            row("cue", 100, "合成孤立词：私处。"),
            row("daily1", 120, "午饭点了粥。"),
            row("daily2", 123, "文件测试通过了。"),
            row("short", 124, "继续。"),
            row("affection", 125, "我好喜欢你。"),
            row("short2", 126, "嗯。"),
        ]
        for ids in ({m.msg_uuid for m in scan(rows).allowed_messages()}, {m.source_id for m in window(rows).messages}):
            self.assertNotIn("short", ids)
            self.assertNotIn("short2", ids)
            self.assertIn("affection", ids)

    def test_discussion_observation_applies_to_other_conversations(self):
        rows = [
            row("cue", 100, "合成孤立词：私处。"),
            row("a", 120, "午饭点了粥。"),
            row("b", 123, "文件测试通过了。"),
            row("short", 124, "继续。", conv="other"),
        ]
        self.assertNotIn("short", {m.msg_uuid for m in scan(rows).allowed_messages()})

    def test_clear_ongoing_action_always_starts_scene(self):
        rows = [row("scene", 0, "合成测试：我们在做爱。"), row("affection", 20, "我好喜欢你。")]
        self.assertFalse(scan(rows).allowed_messages())
        self.assertTrue(any(i.kind == "scene" for i in scan(rows).intervals))

    def topic_scene(self):
        return [
            row("scene", 0, "合成测试：我们在做爱。"),
            row("care", 5, "热毛巾。"),
            row("question1", 40, "明天午饭想吃什么？"),
            row("question2", 43, "午饭外卖选哪家餐厅？"),
        ]

    def test_concrete_daily_questions_can_prove_topic_without_becoming_facts(self):
        rows = self.topic_scene()
        self.assertFalse(features(rows[2]["text"])["ordinary_anchor"])
        self.assertEqual(features(rows[2]["text"])["transition_topics"], {"meal"})
        self.assertEqual({m.msg_uuid for m in scan(rows).allowed_messages()}, {"question1", "question2"})

    def test_topic_transition_requires_aftercare_two_user_questions_and_bounded_span(self):
        base = self.topic_scene()
        variants = [
            base[:3],
            [base[0], *base[2:]],
            [*base[:3], row("question2", 60, "午饭外卖选哪家餐厅？")],
            [*base[:3], row("question2", 43, "午饭外卖选哪家餐厅？", speaker="顾凛")],
            [*base[:3], row("question2", 43, "午饭外卖选哪家餐厅？", conv="other")],
            [*base[:2], row("noun1", 40, "午饭。"), row("noun2", 43, "餐厅。")],
        ]
        for rows in variants:
            with self.subTest(ids=[r["msg_uuid"] for r in rows]):
                self.assertFalse(scan(rows).allowed_messages())

    def test_daily_question_mixed_with_ambiguous_body_or_fantasy_is_not_topic_proof(self):
        for text in (
            "明天午饭吃什么，继续。",
            "明天午饭吃什么，摸着我的腰。",
            "幻想明天午饭吃什么。",
            "你说一句“明天午饭吃什么”。",
            "昨天午饭吃什么？",
            "午饭吃什么，帮你擦。",
        ):
            self.assertFalse(features(text)["transition_topics"], text)

    def test_topic_observation_restarts_revoke_and_short_replies_stay_held(self):
        base = self.topic_scene()
        for text in ("合成孤立词：私处。", "疼不疼？"):
            self.assertFalse(scan(base + [row("restart", 72, text)]).allowed_messages())
        result = scan(base + [row("short", 44, "再来。", conv="other"), row("daily", 45, "晚饭要选哪家餐厅？")])
        self.assertNotIn("short", {m.msg_uuid for m in result.allowed_messages()})
        self.assertIn("daily", {m.msg_uuid for m in result.allowed_messages()})

    def test_topic_questions_do_not_unlock_unknown_left_boundary(self):
        rows = [row("a", 40, "明天午饭想吃什么？"), row("b", 43, "午饭外卖选哪家餐厅？")]
        self.assertFalse(window(rows).messages)

    def test_person_as_object_metaphors_cannot_close_scene(self):
        rows = [
            *self.topic_scene()[:2],
            row("metaphor1", 40, "你是不是我的礼物？"),
            row("metaphor2", 43, "你是我最喜欢的盲盒吗？"),
        ]
        self.assertFalse(features(rows[2]["text"])["transition_topics"])
        self.assertFalse(features(rows[3]["text"])["transition_topics"])
        self.assertFalse(scan(rows).allowed_messages())

    def test_cross_conversation_restart_revokes_entire_topic_boundary(self):
        rows = [
            row("prefix1", 40, "眼药用好了。"),
            row("prefix2", 43, "午饭点了粥。"),
            row("scene", 100, "合成测试：我们在做爱。"),
            row("care", 105, "热毛巾。"),
            row("q1", 140, "明天午饭想吃什么？"),
            row("q2", 143, "午饭外卖选哪家餐厅？"),
            row("restart", 172, "合成测试：我们在做爱。", conv="other"),
        ]
        for ids in ({m.msg_uuid for m in scan(rows).allowed_messages()}, {m.source_id for m in window(rows).messages}):
            self.assertNotIn("q1", ids)
            self.assertNotIn("q2", ids)

    def test_cross_conversation_single_cue_refreshes_active_scene(self):
        rows = [
            row("scene", 0, "合成测试：我们在做爱。"),
            row("care", 5, "热毛巾。"),
            row("cross_cue", 39, "合成孤立词：私处。", conv="other"),
            row("q1", 40, "明天午饭想吃什么？"),
            row("q2", 43, "午饭外卖选哪家餐厅？"),
        ]
        self.assertFalse(scan(rows).allowed_messages())

    def test_post_scene_observation_is_global_across_conversations(self):
        rows = [
            row("b1", 40, "眼药用好了。", conv="other"),
            row("b2", 43, "午饭点了粥。", conv="other"),
            row("scene", 100, "合成测试：我们在做爱。"),
            row("a1", 160, "午饭点了粥。"),
            row("a2", 163, "文件测试通过了。"),
            row("short", 164, SECRET, speaker="顾凛", conv="other"),
        ]
        self.assertNotIn("short", {m.msg_uuid for m in scan(rows).allowed_messages()})
        self.assertNotIn("short", {m.source_id for m in window(rows).messages})

    def test_multi_family_dense_short_and_long_starts_withhold_pre_roll(self):
        for text in ("舌尖、喘息、颤抖", "合成占位。" * 80 + "舌尖、颤抖"):
            with self.subTest(text_length=len(text)):
                result = scan([row("pre", -9, SECRET), row("start", 0, text), row("reply", 1, SECRET, speaker="顾凛")])
                self.assertTrue(all(d.status == "intentional_skip" for d in result.decisions))

    def test_explicit_with_only_soft_care_and_short_replies_never_times_out(self):
        rows = [
            row("start", 0, "合成测试信号：现在开始做爱。"),
            row("soft", 45, "亲吻。"),
            row("short", 90, SECRET, speaker="顾凛"),
            row("water", 120, "喝水。"),
            row("sleep", 180, "晚安。"),
            row("late", 300, "嗯"),
        ]
        result = scan(rows)
        self.assertIsNone(result.intervals[0].end)
        self.assertFalse(result.allowed_messages())

    def test_one_health_sentence_is_not_rescued_inside_known_scene(self):
        result = scan([row("start", 0, "合成测试信号：现在开始做爱。"), row("health", 60, "眼睛有点痒。")])
        self.assertFalse(result.allowed_messages())

    def test_specific_care_cannot_be_cancelled_by_appending_daily_words(self):
        rows = [
            row("start", 0, "合成测试信号：现在开始做爱。"),
            row("care1", 60, "疼不疼？午饭点了粥。"),
            row("care2", 63, "热毛巾，文件测试通过了。"),
        ]
        self.assertTrue(features(rows[1]["text"])["specific_care"])
        self.assertFalse(scan(rows).allowed_messages())
        self.assertFalse(window(rows).messages)

    def test_bare_nouns_and_questions_cannot_prove_a_transition(self):
        rows = [
            row("start", 0, "合成测试信号：现在开始做爱。"),
            row("noun", 60, "眼睛。"),
            row("question", 63, "眼睛？"),
        ]
        self.assertFalse(features("眼睛。")["ordinary_anchor"])
        self.assertFalse(features("眼睛痒吗？")["ordinary_anchor"])
        self.assertFalse(window(rows).messages)

    def test_romantic_gaze_and_metaphor_are_not_actual_daily_transitions(self):
        rows = [
            row("start", 0, "合成测试信号：现在开始做爱。"),
            row("gaze1", 60, "我的眼睛只看你，亲亲。"),
            row("gaze2", 63, "我的双眼一直看着你。"),
        ]
        self.assertFalse(window(rows).messages)
        self.assertFalse(features("我感觉你的心像一个景区。")["ordinary_anchor"])

    def test_hypothetical_or_future_actions_do_not_unlock_actual_scene(self):
        for text in (
            "明天文件测试通过就好了。",
            "如果午饭点了粥就好了。",
            "幻想我已经到酒店了。",
            "明天，午饭点了粥就好了。",
            "明天，到了酒店就好了。",
            "昨天，午饭点了粥。",
            "假如，文件测试通过了就好了。",
        ):
            self.assertFalse(features(text)["ordinary_anchor"], text)
        self.assertTrue(features("午饭点了粥，不想出门了。")["ordinary_anchor"])
        rows = [
            row("start", 0, "合成测试信号：现在开始做爱。"),
            row("future1", 60, "明天，午饭点了粥就好了。"),
            row("future2", 63, "明天，到了酒店就好了。"),
        ]
        self.assertFalse(window(rows).messages)

    def established(self):
        return [
            row("start", 0, "合成测试信号：现在开始做爱。"),
            row("care", 5, "热毛巾。"),
            row("topic1", 60, "午饭点了粥，亲亲。"),
            row("topic2", 63, "文件测试通过了，抱抱。"),
        ]

    def test_sustained_user_transition_retrospectively_preserves_whole_normal_chain(self):
        result = scan(self.established() + [row("soft", 65, "亲吻。"), row("later", 100, "午饭吃粥。")])
        allowed = {m.msg_uuid for m in result.allowed_messages()}
        self.assertEqual(allowed, {"topic1", "topic2", "later"})
        self.assertEqual(next(d.reason for d in result.decisions if d.msg_uuid == "soft"), "post_private_observation")

    def test_waits_through_full_observation_after_second_anchor(self):
        result = scan(self.established(), now=START + timedelta(minutes=80))
        self.assertFalse(result.allowed_messages())

    def test_real_restart_during_observation_revokes_earlier_entire_chain(self):
        rows = self.established() + [row("gap", 64, SECRET), row("restart", 88, "合成测试信号：现在开始做爱。")]
        self.assertFalse(scan(rows).allowed_messages())

    def test_specific_aftercare_reopens_and_revokes_normal_chain(self):
        self.assertFalse(scan(self.established() + [row("care2", 80, "疼不疼。")]).allowed_messages())

    def test_generic_care_breaks_proof_without_restarting_sexual_clock(self):
        rows = [
            row("start", 0, "合成测试信号：现在开始做爱。"),
            row("first", 60, "午饭点了粥。"),
            row("water", 61, "喝水。"),
            row("second", 63, "文件测试通过。"),
            row("third", 66, "眼药用好了。"),
        ]
        self.assertEqual({m.msg_uuid for m in scan(rows).allowed_messages()}, {"second", "third"})

    def test_ordinary_water_and_wiping_do_not_open_scene(self):
        rows = [row("a", 1, "用毛巾擦桌子。"), row("b", 10, "午饭以后记得喝水。"), row("c", 15, "晚安亲亲。")]
        self.assertFalse(scan(rows).intervals)
        self.assertEqual(len(scan(rows).allowed_messages()), 3)

    def test_grey_long_ordinary_heart_notes_cannot_close_a_scene(self):
        rows = [
            row("start", 0, "合成测试信号：现在开始做爱。"),
            row("a", 60, "幻想上班、文件测试和午饭。" * 40, speaker="顾凛"),
            row("b", 64, "再幻想一次点餐和快递。" * 40, speaker="顾凛"),
        ]
        self.assertFalse(scan(rows).allowed_messages())

    def test_other_conversation_cannot_close_or_override_global_mask(self):
        rows = [
            row("start", 0, "合成测试信号：现在开始做爱。"),
            row("a", 60, "午饭点了粥。", conv="other"),
            row("b", 63, "文件测试通过。", conv="other"),
        ]
        self.assertFalse(scan(rows).allowed_messages())

    def test_prior_exclusions_survive_improved_detection(self):
        result = scan([row("a", 10, "午饭点了粥。")], withheld_ids=["a"])
        self.assertEqual(result.decisions[0].status, "intentional_skip")

    def test_unknown_left_can_use_verified_user_daily_chain_without_aftercare(self):
        rows = [
            row("prefix", 1, SECRET),
            row("a", 40, "眼睛痒，眼药用好了。"),
            row("b", 43, "午饭点了粥。"),
            row("soft", 80, "亲亲抱抱。"),
        ]
        day = window(rows)
        self.assertEqual({s.source_id for s in day.messages}, {"a", "b", "soft"})
        self.assertFalse(day.context_complete)
        self.assertTrue(day.privacy_review_required)
        with self.assertRaises(PrivacyInputError):
            day.payload()
        with self.assertRaises(PrivacyInputError):
            split_sources(day, 16000)

    def test_unknown_left_never_releases_by_single_health_or_time_alone(self):
        for rows in ([row("a", 40, "眼睛痒。")], [row("a", 1, SECRET), row("b", 600, "嗯")]):
            with self.subTest(count=len(rows)):
                self.assertFalse(window(rows).messages)

    def test_unknown_left_cannot_be_released_by_grey_only_or_one_user_plus_grey(self):
        for first_speaker in ("顾凛", "知知"):
            rows = [row("a", 40, "眼药用好了。", speaker=first_speaker), row("b", 43, "午饭点了粥。", speaker="顾凛")]
            self.assertFalse(window(rows).messages)

    def test_unknown_left_observation_restart_revokes_prefix_transition(self):
        rows = [
            row("a", 40, "眼药用好了。"),
            row("b", 43, "午饭点了粥。"),
            row("restart", 60, "合成测试信号：现在开始做爱。"),
        ]
        self.assertFalse(window(rows).messages)

    def test_unknown_left_observation_keeps_generic_care_and_short_replies_local(self):
        rows = [
            row("prefix", 1, SECRET),
            row("a", 40, "眼药用好了。"),
            row("b", 43, "午饭点了粥。"),
            row("care", 50, "喝水。"),
            row("short", 55, SECRET),
            row("later", 80, "亲亲。"),
        ]
        day = window(rows)
        self.assertEqual({s.source_id for s in day.messages}, {"a", "b", "later"})
        reasons = {d.msg_uuid: d.reason for d in day.coverage}
        self.assertEqual(reasons["care"], "unknown_boundary_observation")
        self.assertEqual(reasons["short"], "unknown_boundary_observation")

    def test_scope_pending_and_recent_tail_are_not_bypassed(self):
        rows = [row("a", 40, "眼药用好了。"), row("b", 43, "午饭点了粥。"), row("tail", 1420, "文件测试。")]
        self.assertNotIn("tail", {s.source_id for s in window(rows).messages})
        with self.assertRaises(PrivacyInputError):
            prepare_contextual_window_day(rows, DAY, window_start=START, window_end=END)
        with self.assertRaises(PrivacyInputError):
            window(rows + [row("outside", 1440, SECRET)])

    def test_native_thinking_never_affects_signal_or_boundary_or_revisions(self):
        class Native:
            def __str__(self):
                raise AssertionError("native must not be read")

        rows = [row("a", 40, "眼药用好了。"), row("b", 43, "午饭点了粥。")]
        self.assertEqual(window(rows), window([{**r, "thinking": Native()} for r in rows]))

    def test_receipt_and_repr_do_not_echo_source_content(self):
        result = scan([row("start", 0, "合成测试信号：现在开始做爱。" + SECRET)])
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(SECRET, json.dumps(result.receipt(), ensure_ascii=False))


class ContextualDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_unreviewed_new_profile_cannot_reach_even_fake_model(self):
        day = window([row("a", 40, "眼药用好了。"), row("b", 43, "午饭点了粥。")])
        self.assertTrue(day.messages)

        class Adapter:
            async def generate(self, payload):
                raise AssertionError("must never dispatch unreviewed candidate")

        with self.assertRaises(PrivacyInputError):
            await request_clerk(day, adapter=Adapter())

    async def test_all_public_preparation_paths_inherit_new_profile_gate(self):
        rows = [row("a", 40, "眼药用好了。"), row("b", 43, "午饭点了粥。")]
        result = scan(rows)
        days = [
            select_clerk_day(result, DAY),
            prepare_clerk_day(rows, DAY, as_of=END, history_complete=True, policy=POLICY),
        ]

        class Adapter:
            async def generate(self, payload):
                raise AssertionError("review gate lost")

        for day in days:
            self.assertTrue(day.privacy_review_required)
            with self.assertRaises(PrivacyInputError):
                await request_clerk(day, adapter=Adapter())


if __name__ == "__main__":
    unittest.main()
