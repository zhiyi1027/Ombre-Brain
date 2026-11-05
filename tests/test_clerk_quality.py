"""Synthetic new family rules; never real sources, words, auth or models."""

import copy
from dataclasses import replace
from datetime import datetime, timezone
import tempfile
import unittest

from clerk_input import ClerkDayInput, ClerkSource
from clerk_privacy import Decision, PrivacyInputError
from clerk_pipeline import ClerkError, split_sources, validate_facts, render_impression, CAPS
from clerk_chatgpt import request_body
from clerk_quality import (
    mood_units,
    direct_category,
    split_mood,
    validate_mood,
    followup_cases,
    split_followup,
    validate_followup,
    immediate,
    deferred_discussion,
    decorate,
    priority_selection,
    rank_full_pool,
)


def day(*items):
    sources = tuple(
        ClerkSource(str(i), speaker, f"2026-08-20T12:{i:02d}:00+00:00", text, conv)
        for i, (speaker, text, conv) in enumerate(items)
    )
    coverage = tuple(Decision(s.source_id, datetime.fromisoformat(s.at), "allowed", "allowed") for s in sources)
    return ClerkDayInput("2026-08-20", "a" * 64, sources, coverage, True, False, "b" * 64)


def card(d, index, kind, text=None, importance=5):
    fragments = [f for p in split_sources(d, 16000) for f in p["sources"]]
    f = next(f for f in fragments if f["source_id"] == str(index))
    value = validate_facts(
        {
            "facts": [
                {
                    "kind": kind,
                    "text": text or f["text"],
                    "importance": importance,
                    "fragment_ids": [f["fragment_id"]],
                    "due_date": None,
                }
            ]
        },
        fragments,
    )[0]
    value["scope"] = "day"
    return value


class QualityContract(unittest.TestCase):
    def test_every_direct_sentence_keeps_exact_original_with_offset(self):
        d = day(("知知", "我喜欢你！爱你。想你啦！爸爸也太好喂啦。", "x"), ("顾凛", "我也喜欢你。", "x"))
        units = mood_units(d)
        cards = validate_mood({"matches": []}, units)
        self.assertEqual(len(cards), 4)
        self.assertEqual([c["verbatim"] for c in cards], ["我喜欢你！", "爱你。", "想你啦！", "爸爸也太好喂啦。"])
        for c in cards:
            e = c["evidence"][0]
            self.assertEqual(c["verbatim"], d.messages[0].text[e["start"] : e["end"]])
            self.assertEqual(e["speaker"], "知知")

    def test_no_substring_affection_for_actions_objects_negation_hypothetical_or_quotes(self):
        for text in (
            "爸爸我想吃面。",
            "我喜欢这件外套。",
            "不喜欢你。",
            "我不爱你。",
            "假如我爱你。",
            "要是我爱你呢。",
            "台词是“我爱你”。",
            "想你帮我调设备。",
            "你不温柔。",
            "你说我爱你。",
            "你爱爸爸。",
            "他爱你。",
            "小明喜欢你。",
            "她告诉我很喜欢你。",
            "她觉得你很温柔。",
            "要我爱你吗。",
        ):
            with self.subTest(text=text):
                self.assertIsNone(direct_category(text))

    def test_mood_model_can_only_select_current_units_and_known_category(self):
        units = mood_units(day(("知知", "你真贴心。", "x")))
        self.assertEqual(len(validate_mood({"matches": [{"id": "u1", "category": "praise"}]}, units)), 1)
        for item in (
            {"id": "unknown", "category": "praise"},
            {"id": "u1", "category": "thinking"},
            {"id": "u1", "category": "praise", "text": "injected"},
        ):
            with self.assertRaises(ClerkError):
                validate_mood({"matches": [item]}, units)
        with self.assertRaises(ClerkError):
            validate_mood({"matches": [{"id": "u1", "category": "praise"}] * 2}, units)

    def test_private_self_reference_profile_restores_explicit_love_without_inventing_text(self):
        d = day(("知知", "SELF_TAG爱你爸爸！", "x"))
        units = mood_units(d)
        self.assertEqual(validate_mood({"matches": []}, units), [])
        cards = validate_mood({"matches": []}, units, ["SELF_TAG"])
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["verbatim"], d.messages[0].text)
        self.assertEqual(cards[0]["local_affect_rule"], "explicit_first_person")
        for text in (
            "她告诉SELF_TAG很爱你。",
            "假如SELF_TAG爱你。",
            "SELF_TAG爱你爸爸的饭。",
            "SELF_TAG爱你爸爸 的饭。",
            "SELF_TAG喜欢你 的饭。",
            "SELF_TAG爱你爸爸， 的饭。",
        ):
            self.assertIsNone(direct_category(text, ["SELF_TAG"]))
        self.assertIsNone(direct_category("爸爸好帅 的衣服。"))
        self.assertIsNone(direct_category("SELF_TAG爱你。", ["SELF.*"]))

    def test_privacy_gate_and_wire_native_projection(self):
        d = day(("知知", "我喜欢你。", "x"))

        def size(p):
            return len(str(p).encode())

        payload, _ = split_mood(d, 16000, size)[0]
        self.assertNotIn("conversation_id", str(payload))
        request_body(payload, "synthetic")
        bad = copy.deepcopy(payload)
        bad["units"][0]["thinking"] = "NATIVE"
        with self.assertRaises(ClerkError):
            request_body(bad, "synthetic")
        with self.assertRaises(PrivacyInputError):
            mood_units(replace(d, privacy_review_required=True))

    def test_followup_reads_original_next_three_and_stops_at_gap(self):
        d = day(
            ("知知", "几点出发？", "x"),
            ("顾凛", "六点。", "x"),
            ("知知", "好。", "x"),
            ("顾凛", "记得带伞。", "x"),
            ("顾凛", "第四条不读取。", "x"),
        )
        q = card(d, 0, "open_loops")
        wire, ctx = followup_cases(d, [q])[0]
        self.assertEqual(len(wire["anchors"][0]["following"]), 3)
        self.assertNotIn("第四条", str(wire))
        coverage = list(d.coverage)
        coverage[2] = replace(coverage[2], status="intentional_skip")
        masked = replace(d, messages=tuple(s for s in d.messages if s.source_id != "2"), coverage=tuple(coverage))
        wire, _ = followup_cases(masked, [q])[0]
        self.assertEqual(len(wire["anchors"][0]["following"]), 1)
        self.assertTrue(wire["anchors"][0]["blocked"])

    def test_known_cross_conversation_and_unknown_metadata_cannot_close_question(self):
        for conv in ("different", None, ""):
            d = day(("知知", "几点出发？", conv if conv in (None, "") else "x"), ("顾凛", "六点。", "different"))
            q = card(d, 0, "open_loops")
            wire, ctx = followup_cases(d, [q])[0]
            self.assertFalse(ctx["answer_ids"])
            self.assertTrue(wire["anchors"][0]["blocked"])
            with self.assertRaises(ClerkError):
                validate_followup({"decisions": {"c1": {"state": "answered", "answer_ids": ["s2"]}}}, {"c1": ctx}, d)

    def test_answered_requires_a_following_grey_id_and_each_question_is_independent(self):
        d = day(("知知", "几点出发？买哪一种？", "x"), ("顾凛", "六点出发；另一件我还不知道。", "x"))
        q = card(d, 0, "open_loops")
        cases = followup_cases(d, [q])
        self.assertEqual(len(cases), 2)
        contexts = {w["id"]: c for w, c in cases}
        reply = {
            "decisions": {
                "c1": {"state": "answered", "answer_ids": ["s2"]},
                "c2": {"state": "unanswered", "answer_ids": []},
            }
        }
        kept, audit = validate_followup(reply, contexts, d)
        self.assertEqual(len(kept), 1)
        self.assertIn("买哪一种", kept[0]["text"])
        self.assertNotIn("几点", kept[0]["text"])
        bad = copy.deepcopy(reply)
        bad["decisions"]["c1"]["answer_ids"] = ["s1"]
        with self.assertRaises(ClerkError):
            validate_followup(bad, contexts, d)

    def test_uncertain_question_is_held_and_never_promoted_to_answered(self):
        d = day(("知知", "几点出发？", None), ("顾凛", "亲亲。", None))
        q = card(d, 0, "open_loops")
        _, ctx = followup_cases(d, [q])[0]
        kept, audit = validate_followup(
            {"decisions": {"c1": {"state": "unanswered", "answer_ids": []}}}, {"c1": ctx}, d
        )
        self.assertEqual(kept, [])
        self.assertEqual(audit[0]["state"], "uncertain")
        self.assertEqual(audit[0]["model_state"], "unanswered")

    def test_same_original_questions_from_multiple_drafts_are_reviewed_and_kept_once(self):
        d = day(("知知", "几点出发？买哪一种？", "x"), ("顾凛", "六点；另一件还不知道。", "x"))
        a = card(d, 0, "open_loops", "出发问题。")
        b = card(d, 0, "open_loops", "购买问题。")
        cases = followup_cases(d, [a, b])
        self.assertEqual(len(cases), 2)
        self.assertEqual(cases[0][1]["from_card_ids"], [a["id"], b["id"]])
        contexts = {w["id"]: ctx for w, ctx in cases}
        values, _ = validate_followup(
            {
                "decisions": {
                    "c1": {"state": "answered", "answer_ids": ["s2"]},
                    "c2": {"state": "unanswered", "answer_ids": []},
                }
            },
            contexts,
            d,
        )
        self.assertEqual(len(values), 1)
        self.assertIn("买哪一种", values[0]["verbatim"])
        self.assertEqual(values[0]["question_from"], [a["id"], b["id"]])

    def test_distinct_pending_matters_sharing_nonquestion_evidence_are_not_coalesced(self):
        d = day(("知知", "饭店和车型都还没定。", "x"), ("顾凛", "饭店选A。", "x"))
        a = card(d, 0, "open_loops", "饭店未决定")
        b = card(d, 0, "open_loops", "车型未决定")
        cases = followup_cases(d, [a, b])
        self.assertEqual(len(cases), 2)
        contexts = {w["id"]: ctx for w, ctx in cases}
        values, _ = validate_followup(
            {
                "decisions": {
                    "c1": {"state": "answered", "answer_ids": ["s2"]},
                    "c2": {"state": "unanswered", "answer_ids": []},
                }
            },
            contexts,
            d,
        )
        self.assertEqual(len(values), 1)
        self.assertIn("车型未决定", values[0]["text"])

    def test_immediate_actions_exit_candidates_without_becoming_facts(self):
        d = day(("知知", "马上去洗澡。", "x"), ("知知", "等下吃点面。", "x"), ("知知", "明天我马上去洗澡。", "x"))
        values = [card(d, i, "commitment") for i in range(3)]
        self.assertTrue(immediate(values[0], d))
        self.assertTrue(immediate(values[1], d))
        self.assertFalse(immediate(values[2], d))
        contexts = {w["id"]: ctx for w, ctx in followup_cases(d, values)}
        kept, audit = validate_followup(
            {"decisions": {k: {"state": "future", "answer_ids": []} for k in contexts}}, contexts, d
        )
        self.assertEqual([c["id"] for c in kept], [values[2]["id"]])
        self.assertEqual(kept[0]["kind"], "commitment")

    def test_followup_wire_rejects_native_and_unknown_source_refs(self):
        d = day(("知知", "几点出发？", "x"), ("顾凛", "六点。", "x"))
        q = card(d, 0, "open_loops")
        payload, _ = split_followup(d, [q], 16000, lambda p: len(str(p).encode()))[0]
        request_body(payload, "synthetic")
        for mode in ("native", "unknown"):
            bad = copy.deepcopy(payload)
            if mode == "native":
                bad["cases"][0]["sources"][0]["thinking"] = "NATIVE"
            else:
                bad["cases"][0]["anchors"][0]["following"] = ["unknown"]
            with self.assertRaises(ClerkError):
                request_body(bad, "synthetic")

    def test_deferred_same_day_discussion_is_not_a_durable_candidate(self):
        d = day(("知知", "太困了，睡醒再说。", "x"), ("知知", "明天下午开会后再聊。", "x"))
        a = card(d, 0, "commitment")
        b = card(d, 1, "commitment")
        self.assertTrue(deferred_discussion(a, d))
        self.assertFalse(deferred_discussion(b, d))
        cases = followup_cases(d, [a, b])
        contexts = {w["id"]: c for w, c in cases}
        kept, audit = validate_followup(
            {"decisions": {cid: {"state": "future", "answer_ids": []} for cid in contexts}}, contexts, d
        )
        self.assertEqual([c["id"] for c in kept], [b["id"]])
        self.assertEqual(audit[0]["state"], "not_commitment")
        self.assertEqual(audit[0]["model_state"], "future")
        self.assertEqual(audit[0]["local_eligibility_rule"], "deferred_discussion")

    def test_unrelated_action_in_same_evidence_does_not_delete_a_durable_promise(self):
        d = day(("知知", "这件事醒来再说；我会一直陪着你。", "x"), ("知知", "现在去洗澡；我会一直陪着你。", "x"))
        a = card(d, 0, "commitment", "知知承诺一直陪伴顾凛。")
        b = card(d, 1, "commitment", "知知承诺一直陪伴顾凛。")
        self.assertFalse(deferred_discussion(a, d))
        self.assertFalse(immediate(b, d))
        for text in ("每天醒来再聊。", "不要醒来再说。", "只是引用“醒来再说”。"):
            x = day(("知知", text, "x"))
            c = card(x, 0, "commitment", "待知知醒来后再说。")
            self.assertFalse(deferred_discussion(c, x))

    def test_priority_recovers_health_activity_affection_ahead_of_chores(self):
        d = day(
            ("知知", "眼睛红。", "x"),
            ("知知", "去了公园。", "x"),
            ("知知", "买了积木。", "x"),
            ("知知", "寄快递。", "x"),
        )
        cards = [decorate(card(d, i, "body" if i == 0 else "events", importance=1 if i == 0 else 10)) for i in range(4)]
        reply = {"selected_ids": ["c4"], "event_groups": [["c2", "c3", "c4"]]}
        chosen, _ = priority_selection(reply, cards, 3)
        self.assertEqual(chosen[0]["impression_class"], "health")
        self.assertEqual({c["impression_class"] for c in chosen[1:]}, {"outing", "shopping"})

    def test_render_budget_drops_chores_before_health_and_verbatim_cannot_be_forged(self):
        d = day(("知知", "眼睛红。", "x"), ("知知", "寄快递。", "x"), ("知知", "我喜欢你。", "x"))
        body = decorate(card(d, 0, "body", "健康概括" * 35, importance=1))
        chores = [decorate(card(d, 1, "events", "寄快递" + str(i) + "杂事" * 55, importance=10)) for i in range(5)]
        mood = decorate(validate_mood({"matches": []}, mood_units(d))[0])
        text, ids = render_impression(d, [body, *chores, mood])
        self.assertIn(body["id"], ids)
        self.assertLessEqual(len(text), 1000)
        bad = copy.deepcopy(mood)
        bad["verbatim"] = "伪造"
        with self.assertRaises(ClerkError):
            render_impression(d, [bad])

    def test_grey_question_verbatim_never_becomes_her_quote(self):
        d = day(("顾凛", "几点出发？", "x"), ("知知", "还不知道。", "x"))
        q = card(d, 0, "open_loops")
        _, ctx = followup_cases(d, [q])[0]
        values, _ = validate_followup({"decisions": {"c1": {"state": "unanswered", "answer_ids": []}}}, {"c1": ctx}, d)
        rendered, _ = render_impression(d, [decorate(c) for c in values])
        self.assertNotIn("她的原话", rendered)

    def test_final_priority_recovers_high_rank_cards_lost_at_an_intermediate_depth(self):
        d = day(("知知", "身体合成事实。", "x"))
        a = [decorate(card(d, 0, "body", f"身体事件{i}")) for i in range(2)]
        a += [decorate(card(d, 0, "food", f"吃饭事件{i}")) for i in range(2)]
        a += [decorate(card(d, 0, "events", f"去了合成地点{i}")) for i in range(CAPS["events"])]
        a += [decorate(card(d, 0, "events", f"寄快递合成事项A{i}")) for i in range(3)]
        b = [decorate(card(d, 0, "events", f"寄快递合成事项B{i}")) for i in range(5)]

        def choose(items, limit):
            reply = {
                "selected_ids": [],
                "event_groups": [[f"c{i}"] for i, c in enumerate(items, 1) if c["kind"] == "events"],
            }
            return priority_selection(reply, items, limit, include_groups=True)

        ca, _, ga = choose(a, 6)
        cb, _, gb = choose(b, 6)
        middle, _, gm = choose(ca + cb, 14)
        self.assertTrue(any(c["impression_class"] == "chore" for c in middle))
        final = rank_full_pool(a + b, ga + gb + gm, 14, middle)
        self.assertEqual(sum(c["impression_class"] == "outing" for c in final), CAPS["events"])
        self.assertFalse(any(c["impression_class"] == "chore" for c in final))

    def test_activity_classification_and_selector_can_see_bound_originals(self):
        from clerk_quality import impression_class

        self.assertEqual(impression_class({"kind": "events", "text": "参观展览。"}), "activity")
        self.assertEqual(impression_class({"kind": "events", "text": "在公园散步。"}), "activity")


class QualityPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_masked_pipeline_reuses_maps_adds_bound_mood_and_drops_answered_and_immediate(self):
        from clerk_pipeline import NightClerkService
        from datetime import date

        d = day(
            ("知知", "身体眼睛红。", "x"),
            ("知知", "去了公园。", "x"),
            ("知知", "买了积木。", "x"),
            ("知知", "寄快递。", "x"),
            ("知知", "我喜欢你！爱你。", "x"),
            ("知知", "几点出发？", "x"),
            ("顾凛", "六点出发。", "x"),
            ("知知", "马上洗澡。", "x"),
            ("知知", "明天整理书架。", "x"),
        )

        class Adapter:
            model = "synthetic-quality"

            def __init__(self):
                self.calls = []

            async def generate(self, p):
                request_body(p, self.model)
                self.calls.append(p)
                if p["operation"] == "extract":
                    facts = []
                    for f in p["sources"]:
                        if f["speaker"] != "知知":
                            continue
                        kind = (
                            "body"
                            if f["text"].startswith("身体")
                            else "open_loops"
                            if "?" in f["text"] or "？" in f["text"]
                            else "commitment"
                            if f["text"].startswith(("马上", "明天"))
                            else "events"
                        )
                        facts.append(
                            {
                                "kind": kind,
                                "text": f["text"],
                                "fragment_ids": [f["fragment_id"]],
                                "importance": 5,
                                "due_date": None,
                            }
                        )
                    return {"facts": facts}
                if p["operation"] == "refine":
                    return {
                        "decisions": {
                            c["id"]: {
                                "action": "keep",
                                "kind": c["kind"],
                                "scope": "day",
                                "text": c["text"],
                                "importance": c["importance"],
                                "evidence_ids": c["evidence_ids"],
                            }
                            for c in p["cards"]
                        }
                    }
                if p["operation"] == "mood_scan":
                    return {"matches": []}
                if p["operation"] == "followup_review":
                    decisions = {}
                    for c in p["cases"]:
                        if c["kind"] == "open_loops":
                            answers = [s["id"] for s in c["sources"] if s["speaker"] == "顾凛"]
                            decisions[c["id"]] = {"state": "answered", "answer_ids": answers}
                        else:
                            decisions[c["id"]] = {
                                "state": "immediate" if c["text"].startswith("马上") else "future",
                                "answer_ids": [],
                            }
                    return {"decisions": decisions}
                return {
                    "selected_ids": [c["id"] for c in p["cards"] if c["impression_class"] == "chore"],
                    "event_groups": [[c["id"] for c in p["cards"] if c["kind"] == "events"]],
                }

        with tempfile.TemporaryDirectory(prefix="quality-synthetic-") as tmp:
            adapter = Adapter()
            service = NightClerkService(
                {"buckets_dir": tmp, "night_clerk": {"enabled": True}}, archive=object(), adapter=adapter
            )
            service.prepare = lambda *_args, **_kwargs: d
            now = datetime(2026, 8, 22, tzinfo=timezone.utc)
            result = await service.generate_day(date(2026, 8, 20), now=now)
            self.assertTrue(result["ok"], result)
            pub = service.current_publication(date(2026, 8, 20), now)
            moods = [c for c in pub["cards"] if c.get("affect_category")]
            self.assertEqual([c["verbatim"] for c in moods], ["我喜欢你！", "爱你。"])
            self.assertFalse(any(c["kind"] == "open_loops" for c in pub["cards"]))
            candidates = [c for c in pub["cards"] if c["kind"] == "commitment"]
            self.assertEqual([c["text"] for c in candidates], ["明天整理书架。"])
            self.assertTrue(pub["quality"]["conversation_metadata_complete"])
            self.assertIn("我喜欢你！", pub["rendered"])
            self.assertTrue(any(c.get("affect_category") == "love" for c in service.search_facts("爱你。")))
            count = len(adapter.calls)
            self.assertTrue((await service.generate_day(date(2026, 8, 20), now=now))["ok"])
            self.assertEqual(len(adapter.calls), count)


if __name__ == "__main__":
    unittest.main()
