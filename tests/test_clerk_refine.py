"""Synthetic evidence review, cache compatibility and cross-batch selection."""

import copy
import unittest
from unittest.mock import patch

from clerk_input import ClerkDayInput, ClerkSource
from clerk_pipeline import (
    ClerkError,
    bounded_selection,
    render_impression,
    encoded_size,
    labeled_selection,
    split_sources,
    validate_facts,
    validate_selection,
)
from clerk_refine import build_refinement, split_refinement, validate_refinement, valid_refinement_payload
from clerk_chatgpt import ChatGPTSubscriptionAdapter, request_body
try:
    from . import test_clerk_pipeline as fixtures
    from .test_clerk_pipeline import DAY, NOW, raw
except ImportError:
    import test_clerk_pipeline as fixtures
    from test_clerk_pipeline import DAY, NOW, raw


class ReviewContract(unittest.TestCase):
    def setUp(self):
        self.day = ClerkDayInput(
            "2026-08-20",
            "a" * 64,
            tuple(
                ClerkSource(str(i), speaker, "2026-08-20T12:00:00+00:00", text)
                for i, (speaker, text) in enumerate(
                    (
                        ("知知", "回来路上没晕。"),
                        ("知知", "前天喝过粥。"),
                        ("知知", "不喜欢豆腐口感。"),
                        ("知知", "买了积木。"),
                        ("知知", "还买了毛绒。"),
                        ("顾凛", "2026-08-22陪你理包。"),
                        ("顾凛", "2026-08-23陪你理包。"),
                    )
                )
            ),
            (),
            False,
        )
        fragments = split_sources(self.day, 16000)[0]["sources"]
        kinds = ("body", "food", "food", "events", "events", "commitment", "commitment")
        self.cards = validate_facts(
            {
                "facts": [
                    {
                        "kind": kind,
                        "text": f"合成草稿{i}",
                        "fragment_ids": [f["fragment_id"]],
                        "importance": 5,
                        "due_date": ("2026-08-22" if i == 5 else "2026-08-23" if i == 6 else None),
                    }
                    for i, (kind, f) in enumerate(zip(kinds, fragments))
                ]
            },
            fragments,
        )
        self.payload, self.context = build_refinement(self.day, self.cards)
        self.reply = {
            "facts": [
                {
                    "source_card_ids": [c["id"]],
                    "evidence_ids": c["evidence_ids"],
                    "kind": c["kind"],
                    "scope": "day",
                    "text": c["text"],
                    "importance": c["importance"],
                    "due_date": c["due_date"],
                }
                for c in self.payload["cards"]
            ],
            "dropped": [],
        }

    def test_corrects_and_merges_only_with_contributed_current_evidence(self):
        self.reply["facts"][0]["text"] = "知知说回来路上没晕。"
        self.reply["facts"][1]["scope"] = "past"
        self.reply["facts"][2]["scope"] = "preference"
        merged = self.reply["facts"][3]
        merged["source_card_ids"] += self.reply["facts"][4]["source_card_ids"]
        merged["evidence_ids"] += self.reply["facts"][4]["evidence_ids"]
        merged["text"] = "知知买了积木和毛绒。"
        self.reply["facts"].pop(4)
        cards, metrics = validate_refinement(self.reply, self.context)
        self.assertEqual(metrics, {"input_cards": 7, "output_cards": 6, "dropped_cards": 0, "merged_cards": 1})
        self.assertEqual(cards[0]["text"], "知知说回来路上没晕。")
        self.assertEqual([c["scope"] for c in cards[:3]], ["day", "past", "preference"])
        self.assertNotEqual(cards[0]["id"], self.cards[0]["id"])

    def test_cannot_borrow_other_card_evidence_or_consume_unrepresented_card(self):
        for mode in ("borrow", "consume"):
            reply = copy.deepcopy(self.reply)
            if mode == "borrow":
                reply["facts"][0]["evidence_ids"] = reply["facts"][1]["evidence_ids"]
            else:
                reply["facts"][0]["source_card_ids"] += reply["facts"][1]["source_card_ids"]
                reply["facts"].pop(1)
            with self.subTest(mode=mode), self.assertRaises(ClerkError):
                validate_refinement(reply, self.context)

    def test_missing_duplicate_unknown_card_and_extra_field_fail(self):
        for mode in ("missing", "duplicate", "unknown", "extra"):
            reply = copy.deepcopy(self.reply)
            if mode == "missing":
                reply["facts"].pop()
            elif mode == "duplicate":
                reply["facts"][1]["source_card_ids"] = reply["facts"][0]["source_card_ids"]
            elif mode == "unknown":
                reply["facts"][0]["source_card_ids"] = ["unknown"]
            else:
                reply["facts"][0]["thinking"] = "SYNTHETIC_NATIVE"
            with self.subTest(mode=mode), self.assertRaises(ClerkError):
                validate_refinement(reply, self.context)

    def test_explicit_drop_is_accounted_and_must_use_known_reason(self):
        card = self.reply["facts"].pop(0)
        self.reply["dropped"] = [{"card_id": card["source_card_ids"][0], "reason": "unsupported"}]
        _, metrics = validate_refinement(self.reply, self.context)
        self.assertEqual(metrics["dropped_cards"], 1)
        self.reply["dropped"][0]["reason"] = "free text"
        with self.assertRaises(ClerkError):
            validate_refinement(self.reply, self.context)

    def test_candidate_cannot_promote_mix_or_fabricate_due_date(self):
        for mode in ("promote", "new_candidate", "mix", "new_date", "conflict"):
            reply = copy.deepcopy(self.reply)
            if mode == "promote":
                reply["facts"][5]["kind"] = "events"
            elif mode == "new_candidate":
                reply["facts"][0]["kind"] = "commitment"
            elif mode in ("mix", "conflict"):
                other = 0 if mode == "mix" else 6
                reply["facts"][5]["source_card_ids"] += reply["facts"][other]["source_card_ids"]
                reply["facts"][5]["evidence_ids"] += reply["facts"][other]["evidence_ids"]
                reply["facts"][5]["due_date"] = None
                reply["facts"].pop(other)
            else:
                reply["facts"][5]["due_date"] = "2026-08-23"
            with self.subTest(mode=mode), self.assertRaises(ClerkError):
                validate_refinement(reply, self.context)

    def test_source_revision_native_and_wire_projection(self):
        self.assertTrue(valid_refinement_payload(self.payload))
        wire = request_body(self.payload, "synthetic")
        self.assertFalse(wire["store"])
        for field in ("thinking", "source", "conv_name"):
            payload = copy.deepcopy(self.payload)
            payload["evidence"][0][field] = "SYNTHETIC_PRIVATE"
            with self.subTest(field=field), self.assertRaises(ClerkError):
                request_body(payload, "synthetic")
        changed = copy.deepcopy(self.cards)
        changed[0]["evidence"][0]["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ClerkError, "stale_source_reference"):
            build_refinement(self.day, changed)

    def test_masked_refinement_wire_preserves_only_exact_known_instructions(self):
        from clerk_redaction import MASKED_TEXT_RULE
        from clerk_refine import REFINE_INSTRUCTIONS

        payload = copy.deepcopy(self.payload)
        payload["instructions"] = REFINE_INSTRUCTIONS + "\n" + MASKED_TEXT_RULE
        self.assertTrue(valid_refinement_payload(payload))
        self.assertEqual(request_body(payload, "synthetic")["instructions"], payload["instructions"])
        for suffix in ("\nunknown override", " ", "\n" + MASKED_TEXT_RULE):
            invalid = copy.deepcopy(payload)
            invalid["instructions"] += suffix
            with self.assertRaises(ClerkError):
                request_body(invalid, "synthetic")

    def test_strict_masked_refinement_reminder_preserves_candidate_checks(self):
        from dataclasses import replace
        from clerk_refine_slots import VERSION, INSTRUCTIONS

        day = replace(self.day, redaction_policy="a" * 64)
        payload, context = build_refinement(day, self.cards)
        self.assertEqual(payload["version"], VERSION)
        self.assertTrue(request_body(payload, "synthetic")["instructions"].startswith(INSTRUCTIONS))
        reply = copy.deepcopy(self.reply)
        reply["facts"][0]["kind"] = "commitment"
        with self.assertRaises(ClerkError):
            validate_refinement(reply, context)

    def test_scope_changes_card_identity_and_never_renders_old_food(self):
        day_cards, _ = validate_refinement(self.reply, self.context)
        self.reply["facts"][1]["scope"] = "past"
        self.reply["facts"][2]["scope"] = "preference"
        scoped, _ = validate_refinement(self.reply, self.context)
        self.assertNotEqual(day_cards[1]["id"], scoped[1]["id"])
        rendered, ids = render_impression(self.day, [scoped[1], scoped[2]])
        self.assertEqual(ids, [])
        self.assertNotIn(scoped[1]["text"], rendered)

    def test_short_labels_deterministic_and_budgeted(self):
        self.assertEqual(self.payload, build_refinement(self.day, self.cards)[0])

        def size(p):
            return len(str(p).encode())

        budget = (size(build_refinement(self.day, self.cards[:1])[0]) + size(self.payload)) // 2
        batches = split_refinement(self.day, self.cards, budget, size)
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(len(c["cards"]) for _, c in batches), len(self.cards))
        self.assertTrue(all(size(p) <= budget for p, _ in batches))

    def test_cross_batch_event_duplicates_are_bounded_after_group_review(self):
        cards = [
            {"id": "a", "kind": "events", "importance": 5},
            {"id": "b", "kind": "events", "importance": 9},
            {"id": "c", "kind": "events", "importance": 6},
        ]
        reply = {"selected_ids": ["a", "b", "c"], "event_groups": [["a", "b"], ["c"]]}
        with self.assertRaisesRegex(ClerkError, "summary_duplicate_event"):
            validate_selection(reply, cards, 14)
        selected, repaired = bounded_selection(reply, cards, 14)
        self.assertTrue(repaired)
        self.assertEqual([c["id"] for c in selected], ["b", "c"])
        for groups in ([["a", "b"]], [["a", "b"], ["b", "c"]], [["a", "b"], ["unknown"]]):
            with self.subTest(groups=groups), self.assertRaises(ClerkError):
                bounded_selection({**reply, "event_groups": groups}, cards, 14)

    def test_short_selection_labels_resolve_only_through_current_card_order(self):
        cards = [
            {"id": "a" * 64, "kind": "events", "importance": 5},
            {"id": "b" * 64, "kind": "events", "importance": 6},
        ]
        reply = {"selected_ids": ["c1"], "event_groups": [["c1"], ["c2"]]}
        chosen, _ = labeled_selection(reply, cards, 14)
        self.assertEqual(chosen[0]["id"], "a" * 64)
        self.assertNotEqual(
            fixtures.digest([c["id"] for c in cards]), fixtures.digest([c["id"] for c in reversed(cards)])
        )
        with self.assertRaises(ClerkError):
            labeled_selection({**reply, "selected_ids": ["c99"]}, cards, 14)

    def test_source_quote_display_is_shanghai_without_rewriting_source(self):
        cards, _ = validate_refinement(self.reply, self.context)
        rendered, _ = render_impression(self.day, [cards[0]])
        self.assertIn("2026-08-20 20:00 北京时间", rendered)
        self.assertEqual(self.day.messages[0].at, "2026-08-20T12:00:00+00:00")

    def test_maximum_budget_measures_oversized_trial_then_splits_before_sending(self):
        day = ClerkDayInput(
            "2026-08-20",
            "b" * 64,
            tuple(ClerkSource(str(i), "知知", "2026-08-20T12:00:00+00:00", "虚" * 15375) for i in range(3)),
            (),
            False,
        )
        meter = type("Meter", (), {"model": "synthetic"})()

        def size(p):
            return ChatGPTSubscriptionAdapter.request_size(meter, p)

        chunks = split_sources(day, 128000, size)
        self.assertEqual(len(chunks), 2)
        fragments = [f for p in chunks for f in p["sources"]]
        cards = validate_facts(
            {
                "facts": [
                    {
                        "kind": "events",
                        "text": "合成事件",
                        "fragment_ids": [f["fragment_id"]],
                        "importance": 5,
                        "due_date": None,
                    }
                    for f in fragments
                ]
            },
            fragments,
        )
        whole, _ = build_refinement(day, cards)
        self.assertGreater(size(whole), 128000)
        with self.assertRaisesRegex(ClerkError, "invalid_payload"):
            request_body(whole, "synthetic")
        batches = split_refinement(day, cards, 128000, size)
        self.assertEqual(len(batches), 2)
        self.assertTrue(all(size(p) <= 128000 for p, _ in batches))
        malformed = copy.deepcopy(whole)
        malformed["evidence"][0]["thinking"] = "NATIVE_SENTINEL"
        with self.assertRaisesRegex(ClerkError, "invalid_payload"):
            request_body(malformed, "synthetic", measure_only=True)

    def test_send_limit_counts_outer_json_escaping_too(self):
        payload = split_sources(self.day, 16000)[0]
        payload["sources"] = [payload["sources"][0]]
        payload["sources"][0].update(text="\x00" * 19000, start=0, end=19000)
        self.assertLess(encoded_size(payload), 128000)
        self.assertGreater(encoded_size(request_body(payload, "synthetic", measure_only=True)), 128000)
        with self.assertRaisesRegex(ClerkError, "invalid_payload"):
            request_body(payload, "synthetic")


class SlotContract(unittest.TestCase):
    def setUp(self):
        from dataclasses import replace

        ReviewContract.setUp(self)
        self.day = replace(self.day, redaction_policy="b" * 64)
        self.payload, self.context = build_refinement(self.day, self.cards)
        self.reply = {
            "decisions": {
                c["id"]: {
                    "action": "keep",
                    "kind": c["kind"],
                    "scope": "day",
                    "text": c["text"],
                    "importance": c["importance"],
                    "evidence_ids": c["evidence_ids"],
                }
                for c in self.payload["cards"]
            }
        }

    def test_keep_inherits_candidates_and_is_deterministic(self):
        self.reply["decisions"]["c6"].pop("kind")
        cards, metrics = validate_refinement(self.reply, self.context)
        self.assertEqual(cards[5]["kind"], "commitment")
        self.assertEqual(cards[5]["due_date"], "2026-08-22")
        self.assertEqual(metrics["output_cards"], 7)
        self.assertEqual((cards, metrics), validate_refinement(self.reply, self.context))

    def test_masked_batches_isolate_candidates_without_changing_legacy(self):
        batches = split_refinement(self.day, self.cards, 128000, encoded_size)
        self.assertEqual(len(batches), 2)
        self.assertEqual(sum(len(p["cards"]) for p, _ in batches), len(self.cards))
        for payload, _ in batches:
            self.assertEqual(len({c["kind"] == "commitment" for c in payload["cards"]}), 1)
        from dataclasses import replace

        self.assertEqual(
            len(split_refinement(replace(self.day, redaction_policy=None), self.cards, 128000, encoded_size)), 1
        )

    def test_union_owns_each_contribution_and_inherits_known_candidate_date(self):
        self.context["cards"]["c7"]["due_date"] = None
        self.reply["decisions"]["c7"] = {"action": "merge", "target": "c6", "evidence_ids": ["e7"]}
        cards, metrics = validate_refinement(self.reply, self.context)
        self.assertEqual(cards[-1]["due_date"], "2026-08-22")
        self.assertEqual(len(cards[-1]["evidence"]), 2)
        self.assertEqual(metrics["merged_cards"], 1)

    def test_keep_can_repeat_only_evidence_explicitly_contributed_by_its_direct_merges(self):
        self.reply["decisions"]["c5"] = {"action": "merge", "target": "c4", "evidence_ids": ["e5"]}
        self.reply["decisions"]["c4"]["evidence_ids"] = ["e4", "e5"]
        cards, _ = validate_refinement(self.reply, self.context)
        self.assertEqual(len(cards[3]["evidence"]), 2)
        for mode in ("other_keep", "unselected_merge_ref", "no_own_contribution"):
            reply = copy.deepcopy(self.reply)
            context = copy.deepcopy(self.context)
            if mode == "other_keep":
                reply["decisions"]["c4"]["evidence_ids"].append("e3")
            elif mode == "unselected_merge_ref":
                context["refs"]["c5"].add("e3")
                reply["decisions"]["c4"]["evidence_ids"].append("e3")
            else:
                reply["decisions"]["c4"]["evidence_ids"] = ["e5"]
            with self.subTest(mode=mode), self.assertRaises(ClerkError):
                validate_refinement(reply, context)

    def test_invalid_slots_fail_closed(self):
        for mode in (
            "missing",
            "unknown",
            "native",
            "demote",
            "promote",
            "date",
            "scope",
            "unknown_evidence",
            "foreign_evidence",
            "empty_evidence",
            "self",
            "chain",
            "drop_target",
            "mix",
            "conflict",
        ):
            reply = copy.deepcopy(self.reply)
            slots = reply["decisions"]
            if mode == "missing":
                slots.pop("c1")
            elif mode == "unknown":
                slots["c8"] = slots["c1"]
            elif mode == "native":
                slots["c1"]["thinking"] = {"native": "SYNTHETIC"}
            elif mode == "demote":
                slots["c6"]["kind"] = "open_loops"
            elif mode == "promote":
                slots["c1"]["kind"] = "commitment"
            elif mode == "date":
                slots["c6"]["due_date"] = "2026-09-01"
            elif mode == "scope":
                slots["c6"]["scope"] = "past"
            elif mode == "unknown_evidence":
                slots["c1"]["evidence_ids"] = ["e100"]
            elif mode == "foreign_evidence":
                slots["c1"]["evidence_ids"] = ["e2"]
            elif mode == "empty_evidence":
                slots["c1"]["evidence_ids"] = []
            else:
                cid = "c7" if mode == "conflict" else "c4"
                target = {"self": "c4", "chain": "c5", "drop_target": "c5", "mix": "c6", "conflict": "c6"}[mode]
                slots[cid] = {"action": "merge", "target": target, "evidence_ids": ["e7" if cid == "c7" else "e4"]}
                if mode == "chain":
                    slots["c5"] = {"action": "merge", "target": "c3", "evidence_ids": ["e5"]}
                if mode == "drop_target":
                    slots["c5"] = {"action": "drop", "reason": "uncertain"}
            with self.subTest(mode=mode), self.assertRaises(ClerkError):
                validate_refinement(reply, self.context)

    def test_group_limits_ownership_and_plain_summary_still_apply(self):
        for mode in ("owner", "quotes", "preference", "too_many"):
            reply = copy.deepcopy(self.reply)
            context = copy.deepcopy(self.context)
            if mode == "owner":
                reply["decisions"]["c6"].update(kind="body")
                context["cards"]["c6"]["kind"] = "events"
            elif mode == "quotes":
                reply["decisions"]["c1"]["text"] = "他说“虚构”。"
            elif mode == "preference":
                reply["decisions"]["c6"].update(kind="events", scope="preference")
                context["cards"]["c6"]["kind"] = "events"
            else:
                refs = []
                for i in range(9):
                    ref = "extra" + str(i)
                    refs.append(ref)
                    context["evidence"][ref] = {**context["evidence"]["e1"], "fragment_id": ref}
                context["refs"]["c1"] = set(refs)
                reply["decisions"]["c1"]["evidence_ids"] = refs
            with self.subTest(mode=mode), self.assertRaises(ClerkError):
                validate_refinement(reply, context)

    def test_duplicate_json_keys_and_version_downgrade_rejected(self):
        from clerk_chatgpt import _json
        from clerk_pipeline import parse_model_json

        with self.assertRaisesRegex(ClerkError, "subscription_invalid_response"):
            _json('{"decisions":{"c1":{},"c1":{}}}')
        with self.assertRaisesRegex(ClerkError, "invalid_model_json"):
            parse_model_json('{"decisions":{"c1":{},"c1":{}}}')
        self.assertEqual(parse_model_json('{"facts":[]}'), {"facts": []})
        from clerk_refine import REFINE_VERSION

        payload = copy.deepcopy(self.payload)
        payload["version"] = REFINE_VERSION
        self.assertFalse(valid_refinement_payload(payload))
        from dataclasses import replace

        self.assertEqual(
            build_refinement(replace(self.day, redaction_policy=None), self.cards)[0]["version"], REFINE_VERSION
        )

    def test_exact_kind_field_reminder_allowed_without_relaxing_validation(self):
        from clerk_refine_slots import KIND_FIELD_RULE

        payload = copy.deepcopy(self.payload)
        payload["instructions"] += "\n" + KIND_FIELD_RULE
        self.assertEqual(request_body(payload, "synthetic")["instructions"], payload["instructions"])
        payload["instructions"] += " arbitrary override"
        self.assertFalse(valid_refinement_payload(payload))
        self.reply["decisions"]["c1"]["kind"] = "preference"
        with self.assertRaises(ClerkError):
            validate_refinement(self.reply, self.context)


class ReviewPipeline(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PipelineScenarios.setUp

    async def test_scope_excluded_from_selector_and_preservation_but_still_searchable(self):
        self.archive.import_rows(
            [
                raw("today", 0, "今天的一件虚构小事。"),
                raw("past", 1, "吃饭前天吃过粥。"),
                raw("preference", 2, "吃饭口味偏好。"),
            ]
        )
        original = self.adapter.generate

        async def scoped(payload):
            reply = await original(payload)
            if payload["operation"] == "refine":
                for f in reply["facts"]:
                    if "past" in f["text"]:
                        f["scope"] = "past"
                    if "preference" in f["text"]:
                        f["scope"] = "preference"
            elif payload["operation"] == "select":
                self.assertTrue(all(c["kind"] != "food" for c in payload["cards"]))
                reply["selected_ids"] = []
            return reply

        self.adapter.generate = scoped
        self.assertTrue((await self.service.generate_day(DAY, now=NOW))["ok"])
        publication = self.service.current_publication(DAY, NOW)
        self.assertEqual(len(publication["selected_ids"]), 1)
        self.assertEqual(len(self.service.search_facts("虚构事实")), 3)
        self.assertNotIn("past", publication["rendered"])

    async def test_refinement_version_reruns_review_without_discarding_map_cache(self):
        self.archive.import_rows([raw("a", 0, "虚构记录。")])
        self.assertTrue((await self.service.generate_day(DAY, now=NOW))["ok"])
        extract_count = sum(p["operation"] == "extract" for p in self.adapter.calls)
        with patch("clerk_refine.REFINE_VERSION", "synthetic-new-review"):
            self.assertIsNone(self.service.current_publication(DAY, NOW))
            self.assertTrue((await self.service.generate_day(DAY, now=NOW))["ok"])
        self.assertEqual(sum(p["operation"] == "extract" for p in self.adapter.calls), extract_count)
        self.assertEqual(sum(p["operation"] == "refine" for p in self.adapter.calls), 2)


if __name__ == "__main__":
    unittest.main()
