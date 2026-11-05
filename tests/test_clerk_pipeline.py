"""End-to-end synthetic raw/archive/cache/clerk tests, no real providers."""
from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from clerk_backup import build_clerk_backup, restore_clerk_backup
from clerk_pipeline import (CAPS, ClerkError, NightClerkService, digest, encoded_size,
                            bounded_selection, render_impression, split_sources, validate_facts,
                            validate_selection)
from plan_dates import normalize_due_date, plan_date_state
from raw_archive import RawArchive
from utils import count_tokens_approx


START = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
NOW = START + timedelta(days=2)
DAY = date(2026, 8, 20)
SECRET = "PRIVATE_FIXTURE_SENTINEL"


def raw(mid, minutes, text, *, conv="fixture", speaker="知知", **extra):
    at = START + timedelta(minutes=minutes)
    return {"msg_uuid": mid, "conv_uuid": conv, "conv_name": "fictional", "speaker": speaker,
            "at": at.isoformat(), "day": (at + timedelta(hours=8)).date().isoformat(),
            "text": text, "thinking": "", **extra}


class FakeGPT:
    model = "synthetic-gpt-v1"
    def __init__(self):
        self.calls = []
        self.fail_at = None
        self.on_call = None
        self.forge = False

    async def generate(self, payload):
        self.calls.append(payload)
        if self.on_call:
            self.on_call()
            self.on_call = None
        if self.fail_at == len(self.calls):
            raise ClerkError("quota_exhausted")
        usage = {"input_tokens": 10, "output_tokens": 5}
        if payload["operation"] == "extract":
            facts = []
            for fragment in payload["sources"][:32]:
                text = fragment["text"]
                kind = next((k for prefix, k in (("身体", "body"), ("吃饭", "food"), ("心情", "mood"), ("候选", "commitment"), ("未定", "open_loops")) if text.startswith(prefix)), "events")
                facts.append({"kind": kind, "text": "她的原话（虚构时间）：编造" if self.forge else f"虚构事实 {fragment['source_id']}",
                              "fragment_ids": [fragment["fragment_id"]], "importance": 7, "due_date": None})
            return {"facts": facts, "usage": usage}
        if payload["operation"] == "refine":
            by_id = {e["id"]: e for e in payload["evidence"]}
            facts = []
            for card in payload["cards"]:
                text = card["text"]
                if card["kind"] != "commitment" and all(by_id[i]["speaker"] == "顾凛" for i in card["evidence_ids"]):
                    text = "顾凛说" + text
                facts.append({"source_card_ids": [card["id"]], "evidence_ids": card["evidence_ids"],
                              "kind": card["kind"], "scope": "day", "text": text,
                              "importance": card["importance"], "due_date": card["due_date"]})
            return {"facts": facts, "dropped": [], "usage": usage}
        selected, counts = [], {}
        for card in payload["cards"]:
            kind = card["kind"]
            if len(selected) < payload["limit"] and counts.get(kind, 0) < CAPS[kind]:
                selected.append(card["id"])
                counts[kind] = counts.get(kind, 0) + 1
        return {"selected_ids": selected, "event_groups": [[c["id"]] for c in payload["cards"] if c["kind"] == "events"], "usage": usage}


class PipelineScenarios(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="clerk-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {"buckets_dir": str(self.root), "night_clerk": {"enabled": True, "automatic": False}}
        self.archive = RawArchive(str(self.root / "_raw/raw_archive.sqlite3"))
        self.adapter = FakeGPT()
        self.service = NightClerkService(self.config, archive=self.archive, adapter=self.adapter)

    async def test_raw_to_five_sections_and_cross_day_fact_lookup(self):
        self.archive.import_rows([raw("event", 0, "虚构记录。"), raw("body", 1, "身体今天还好。"),
                                  raw("meal", 2, "吃饭今天吃面。"), raw("mood", 3, "心情今天开心。"),
                                  raw("open", 4, "未定还没决定买哪个。"), raw("todo", 5, "候选准备写一封信。")])
        result = await self.service.generate_day(DAY, now=NOW)
        self.assertTrue(result["ok"], result)
        publication = self.service.current_publication(DAY, NOW)
        for title in ("发生了什么", "没定的事", "身体", "饮食", "心情"):
            self.assertIn("## " + title, publication["rendered"])
        self.assertEqual(len(self.service.search_facts("虚构事实")), 5)
        candidates = self.service.search_facts(candidates_only=True)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["status"], "candidate")
        self.assertIsNone(candidates[0]["due_date"])
        self.assertNotIn(candidates[0]["id"], publication["selected_ids"])
        self.assertFalse(list(self.root.glob("**/*.md")))
        # Quotes are selected from source, not authored by the fake model.
        self.assertIn("心情今天开心。", publication["rendered"])

    async def test_selector_cannot_empty_known_sections_or_promote_candidates(self):
        self.archive.import_rows([raw("event", 0, "台灯修好了。"), raw("body", 1, "身体小腹疼。"),
                                  raw("meal", 2, "吃饭中午吃面。"), raw("mood", 3, "心情今天开心。"),
                                  raw("open", 4, "未定周末去哪里。"), raw("promise", 5, "候选准备还书。")])
        generate = self.adapter.generate
        for mode in ("only_events", "nothing"):
            async def drop_sections(payload, mode=mode):
                reply = await generate(payload)
                if payload["operation"] == "select":
                    reply["selected_ids"] = [c["id"] for c in payload["cards"] if mode == "only_events" and c["kind"] == "events"]
                return reply
            self.adapter.generate = drop_sections
            self.adapter.model = "synthetic-" + mode
            self.assertTrue((await self.service.generate_day(DAY, now=NOW))["ok"])
            publication = self.service.current_publication(DAY, NOW)
            by_id = {c["id"]: c for c in publication["cards"]}
            self.assertEqual({by_id[i]["kind"] for i in publication["selected_ids"]}, set(CAPS))
            self.assertNotIn("暂无整理条目", publication["rendered"])
            self.assertFalse(set(publication["candidate_ids"]) & set(publication["selected_ids"]))
            self.assertIn("心情今天开心。", publication["rendered"].split("## 心情", 1)[1])
            self.assertNotIn("她的原话", publication["rendered"].split("## 心情", 1)[0])

    def test_section_overflow_only_trims_existing_ids_by_importance_then_model_order(self):
        cards = [{"id": str(i), "kind": "mood", "importance": score}
                 for i, score in enumerate((3, 9, 9, 2))]
        reply = {"selected_ids": ["0", "2", "1", "3"], "usage": {}}
        with self.assertRaisesRegex(ClerkError, "summary_section_overflow"):
            validate_selection(reply, cards, 14)
        selected, repaired = bounded_selection(reply, cards, 14)
        self.assertTrue(repaired)
        self.assertEqual([c["id"] for c in selected], ["2", "1"])
        self.assertEqual(reply["selected_ids"], ["0", "2", "1", "3"])

    def test_section_repair_still_rejects_unknown_duplicate_extra_and_total_overflow(self):
        cards = [{"id": str(i), "kind": "mood", "importance": 5} for i in range(4)]
        for reply, limit in (({"selected_ids": ["0", "1", "2", "unknown"]}, 14),
                             ({"selected_ids": ["0", "1", "2", "2"]}, 14),
                             ({"selected_ids": ["0", "1", "2"], "extra": True}, 14),
                             ({"selected_ids": ["0", "1", "2"]}, 2)):
            with self.subTest(reply=reply, limit=limit):
                with self.assertRaisesRegex(ClerkError, "invalid_summary_selection"):
                    bounded_selection(reply, cards, limit)

    async def test_overfull_selection_publishes_and_replays_without_new_model_calls(self):
        self.archive.import_rows([raw(f"mood{i}", i, f"心情虚构感受{i}。") for i in range(4)]
                                 + [raw("promise", 8, "候选准备还书。")])
        generate = self.adapter.generate
        async def overfull(payload):
            reply = await generate(payload)
            if payload["operation"] == "select":
                reply["selected_ids"] = [c["id"] for c in payload["cards"]]
            return reply
        self.adapter.generate = overfull
        self.assertTrue((await self.service.generate_day(DAY, now=NOW))["ok"])
        publication = self.service.current_publication(DAY, NOW)
        self.assertEqual(publication["selection_repairs"], 1)
        self.assertEqual(len(publication["selected_ids"]), 2)
        self.assertFalse(set(publication["candidate_ids"]) & set(publication["selected_ids"]))
        count = len(self.adapter.calls)
        with self.service.store.connect() as conn:
            conn.execute("DELETE FROM publications WHERE day=?", (str(DAY),))
        self.assertTrue((await self.service.generate_day(DAY, now=NOW))["ok"])
        self.assertEqual(len(self.adapter.calls), count)
        self.assertEqual(self.service.current_publication(DAY, NOW)["selection_repairs"], 1)

    def render_cards(self, width, *, many_events=False):
        self.archive.import_rows([raw(kind, i, f"{kind}的合成原文。") for i, kind in enumerate(CAPS)])
        day = self.service.prepare(DAY, NOW)
        fragments = split_sources(day, 16000)[0]["sources"]
        by_id = {f["source_id"]: f["fragment_id"] for f in fragments}
        facts = []
        for kind in CAPS:
            for index in range(5 if many_events and kind == "events" else 1):
                facts.append({"kind": kind, "text": f"{kind}{index}" + "合成概括" * (width // 4),
                              "fragment_ids": [by_id[kind]], "importance": 10 if kind == "events" else 1})
        return day, validate_facts({"facts": facts}, fragments)

    async def test_budget_removes_repeated_events_before_only_body_food_or_mood(self):
        day, cards = self.render_cards(80, many_events=True)
        rendered, selected = render_impression(day, cards)
        kinds = {c["kind"] for c in cards if c["id"] in selected}
        self.assertEqual(kinds, set(CAPS))
        self.assertLess(len(selected), len(cards))
        self.assertLessEqual(len(rendered), 1000)
        self.assertLessEqual(count_tokens_approx(rendered), 900)

    async def test_unavoidable_budget_omission_is_a_pointer_not_a_false_empty_section(self):
        day, cards = self.render_cards(264)
        rendered, selected = render_impression(day, cards)
        self.assertLess(len(selected), len(cards))
        self.assertIn("本栏事实卡因篇幅限制未展开。", rendered)
        self.assertNotIn("暂无整理条目。", rendered)
        self.assertLessEqual(len(rendered), 1000)
        self.assertLessEqual(count_tokens_approx(rendered), 900)
        for card in cards:
            self.assertEqual(card["text"] in rendered, card["id"] in selected)

    async def test_previous_render_revision_is_invalidated_without_repeating_model_calls(self):
        self.archive.import_rows([raw("mood", 0, "心情今天开心。")])
        await self.service.generate_day(DAY, now=NOW)
        count = len(self.adapter.calls)
        publication = self.service.current_publication(DAY, NOW)
        day = self.service.prepare(DAY, NOW)
        from clerk_pipeline import PROMPT_VERSION
        old_revision = digest([day.selection_revision, PROMPT_VERSION, self.adapter.model,
                               self.service.adapter_identity(), "five-column-render-v1"])
        with self.service.store.connect() as conn:
            conn.execute("UPDATE publications SET revision=? WHERE day=?", (old_revision, str(DAY)))
        self.assertIsNone(self.service.current_publication(DAY, NOW))
        result = await self.service.generate_day(DAY, now=NOW)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.adapter.calls), count)
        self.assertEqual(self.service.current_publication(DAY, NOW)["revision"], publication["revision"])

    async def test_privacy_selection_precedes_actual_map_and_summary_requests(self):
        self.archive.import_rows([raw("safe", -30, "虚构日常。"), raw("pre", -9, SECRET),
                                  raw("start", 0, "测试标记：高潮"), raw("reply", 1, SECRET, speaker="顾凛"),
                                  raw("care", 12, "热毛巾喝水")])
        result = await self.service.generate_day(DAY, now=NOW)
        self.assertTrue(result["ok"], result)
        sent = json.dumps(self.adapter.calls, ensure_ascii=False)
        self.assertNotIn(SECRET, sent)
        self.assertNotIn("热毛巾", sent)
        sent_ids = {f["source_id"] for p in self.adapter.calls if p["operation"] == "extract" for f in p["sources"]}
        self.assertEqual(sent_ids, {"safe"})

    async def test_long_single_message_full_coverage_and_bounded_requests(self):
        text = "虚构日常记录。" * 3000
        self.archive.import_rows([raw("long", 0, text)])
        day = self.service.prepare(DAY, NOW)
        chunks = split_sources(day, 8000)
        self.assertGreater(len(chunks), 1)
        fragments = [f for p in chunks for f in p["sources"]]
        self.assertEqual("".join(f["text"] for f in fragments), text)
        self.assertEqual(fragments[0]["start"], 0)
        self.assertEqual(fragments[-1]["end"], len(text))
        self.assertTrue(all(encoded_size(p) <= 8000 for p in chunks))

    async def test_quota_failure_keeps_cache_and_durable_attempt_usage(self):
        self.service.budget = 8000
        self.archive.import_rows([raw("long", 0, "虚构记录。" * 1500)])
        self.adapter.fail_at = 2
        first = await self.service.generate_day(DAY, now=NOW)
        self.assertEqual(first["code"], "quota_exhausted")
        self.assertIsNone(self.service.store.publication(DAY.isoformat()))
        first_payload = digest(self.adapter.calls[0])
        self.adapter.fail_at = None
        second = await self.service.generate_day(DAY, now=NOW)
        self.assertTrue(second["ok"], second)
        self.assertEqual(sum(digest(p) == first_payload for p in self.adapter.calls), 1)
        ledger = self.service.store.usage(DAY.isoformat())
        self.assertEqual(len(ledger), len(self.adapter.calls))
        self.assertTrue(any(x["status"] == "failed_usage_unknown" for x in ledger))
        self.assertEqual(len(self.service.current_publication(DAY, NOW)["usage"]), len(ledger))

    async def test_same_revision_is_idempotent_but_new_model_reprocesses(self):
        self.archive.import_rows([raw("a", 0, "虚构记录。")])
        await self.service.generate_day(DAY, now=NOW)
        count = len(self.adapter.calls)
        current = await self.service.generate_day(DAY, now=NOW)
        self.assertEqual(current["skipped"], "current")
        self.assertEqual(len(self.adapter.calls), count)
        self.adapter.model = "synthetic-gpt-v2"
        self.assertIsNone(self.service.current_publication(DAY, NOW))
        self.assertTrue((await self.service.generate_day(DAY, now=NOW))["ok"])
        self.assertGreater(len(self.adapter.calls), count)

    async def test_prompt_revision_invalidates_publication(self):
        self.archive.import_rows([raw("a", 0, "虚构记录。")])
        await self.service.generate_day(DAY, now=NOW)
        with patch("clerk_pipeline.PROMPT_VERSION", "synthetic-new-prompt"):
            self.assertIsNone(self.service.current_publication(DAY, NOW))

    async def test_unrelated_next_day_arrival_does_not_hide_yesterday(self):
        self.archive.import_rows([raw("a", 0, "虚构记录。")])
        await self.service.generate_day(DAY, now=NOW)
        self.archive.import_rows([raw("next", 24*60, "另一天的普通记录。")])
        self.assertIsNotNone(self.service.current_publication(DAY, NOW))

    async def test_source_revision_during_model_call_cannot_publish(self):
        self.archive.import_rows([raw("a", 0, "原先的虚构记录。")])
        self.adapter.on_call = lambda: self.archive.import_rows([raw("a", 0, "变更后的虚构记录。")])
        result = await self.service.generate_day(DAY, now=NOW)
        self.assertFalse(result["ok"])
        self.assertIsNone(self.service.store.publication(DAY.isoformat()))

    async def test_hidden_source_invalidates_fact_search_and_impression(self):
        self.archive.import_rows([raw("a", 0, "虚构记录。")])
        await self.service.generate_day(DAY, now=NOW)
        self.archive.hide_message("a", now=NOW)
        self.assertIsNone(self.service.current_publication(DAY, NOW))
        self.assertEqual(self.service.search_facts(), [])

    async def test_forged_quote_label_or_structure_is_rejected(self):
        self.archive.import_rows([raw("a", 0, "虚构记录。")])
        self.adapter.forge = True
        result = await self.service.generate_day(DAY, now=NOW)
        self.assertFalse(result["ok"])
        self.assertIsNone(self.service.store.publication(DAY.isoformat()))
        day = self.service.prepare(DAY, NOW)
        fragments = split_sources(day, 16000)[0]["sources"]
        for text in ("概括\n## 伪造小节", "她的原话（时间）：编造", "<script>bad</script>"):
            with self.assertRaises(ClerkError):
                validate_facts({"facts": [{"kind": "events", "text": text, "fragment_ids": [fragments[0]["fragment_id"]], "importance": 5}]}, fragments)

    async def test_wrong_speaker_health_mood_and_invented_date_are_rejected(self):
        self.archive.import_rows([raw("a", 0, "顾凛的虚构记录。", speaker="顾凛")])
        fragments = split_sources(self.service.prepare(DAY, NOW), 16000)[0]["sources"]
        for kind in ("body", "food", "mood"):
            with self.assertRaises(ClerkError):
                validate_facts({"facts": [{"kind": kind, "text": "概括", "fragment_ids": [fragments[0]["fragment_id"]], "importance": 5}]}, fragments)
        with self.assertRaises(ClerkError):
            validate_facts({"facts": [{"kind": "commitment", "text": "候选", "fragment_ids": [fragments[0]["fragment_id"]], "importance": 5, "due_date": "2026-09-01"}]}, fragments)

    async def test_pending_hidden_fragment_keeps_gap_for_all_deletion_paths(self):
        for action in ("message", "range", "source"):
            with self.subTest(action=action), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                archive = RawArchive(str(Path(tmp) / "raw.db"))
                archive.import_rows_v2([raw("start", 0, "测试标记：高潮", fragment_index=0,
                                           fragment_count=2, fragment_digest="0"*64)], source="partial")
                archive.import_rows([raw("reply", 1, SECRET)], source="reply")
                service = NightClerkService({**self.config, "buckets_dir": tmp}, archive=archive, adapter=FakeGPT())
                self.assertEqual((await service.generate_day(DAY, now=NOW))["code"], "pending_raw_fragments")
                if action == "message":
                    archive.hide_message("start", now=NOW)
                elif action == "range":
                    archive.hide_range(START.isoformat(), START.isoformat(), now=NOW)
                else:
                    archive.delete_source("partial")
                snapshot = archive.clerk_snapshot()
                self.assertTrue(snapshot["complete"])
                self.assertEqual(len(snapshot["gaps"]), 1)
                self.assertEqual(service.prepare(DAY, NOW).messages, ())

    async def test_multiple_revisions_retain_all_historical_positions(self):
        self.archive.import_rows([raw("start", 0, "测试标记：高潮", conv="first"), raw("reply", 1, SECRET, conv="first")])
        self.archive.import_rows([raw("start", 60, "中间修订。", conv="second")])
        self.archive.import_rows([raw("start", 120, "最终修订。", conv="third")])
        self.assertEqual(len(self.archive.clerk_snapshot()["gaps"]), 2)
        self.assertNotIn("reply", [s.source_id for s in self.service.prepare(DAY, NOW).messages])

    async def test_pending_replaced_by_complete_or_new_layout_retains_gap(self):
        for replacement in ("complete", "index0", "digest"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                archive = RawArchive(str(Path(tmp) / "raw.db"))
                archive.import_rows_v2([raw("start", 0, "测试标记：高潮", fragment_index=0,
                                           fragment_count=2, fragment_digest="0"*64)])
                archive.import_rows([raw("reply", 1, SECRET)])
                if replacement == "complete":
                    archive.import_rows_v2([raw("start", 120, "最终普通修订。")])
                else:
                    archive.import_rows_v2([raw("start", 120, "新修订分片", fragment_index=0 if replacement == "index0" else 1,
                                               fragment_count=2, fragment_digest="1"*64)])
                    self.assertEqual(len(archive.clerk_snapshot()["gaps"]), 1)
                    archive.import_rows_v2([raw("start", 120, "最终普通修订。")])
                service = NightClerkService({**self.config, "buckets_dir": tmp}, archive=archive, adapter=FakeGPT())
                self.assertNotIn("reply", [s.source_id for s in service.prepare(DAY, NOW).messages])

    async def test_equivalent_legacy_assembly_does_not_create_false_gap(self):
        self.archive.import_rows([raw("home-msg-1-part-0", 0, "普通"), raw("home-msg-1-part-1", 0, "记录。")])
        self.archive.import_rows_v2([raw("home-msg-1", 0, "普通记录。")])
        self.assertEqual(self.archive.clerk_snapshot()["gaps"], [])
        self.assertEqual([s.source_id for s in self.service.prepare(DAY, NOW).messages], ["home-msg-1"])

    async def test_unverified_pending_digest_never_proves_content_equivalence(self):
        from raw_archive import _content_digest
        claimed = _content_digest("普通完整修订。", "")
        for mode in ("complete", "index0", "same_index"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                archive = RawArchive(str(Path(tmp) / "raw.db"))
                index = 1 if mode == "same_index" else 0
                archive.import_rows_v2([raw("start", 0, "测试标记：高潮", fragment_index=index,
                                           fragment_count=2, fragment_digest=claimed)])
                archive.import_rows([raw("reply", 1, SECRET)])
                replacement = raw("start", 0, "普通完整修订。")
                if mode != "complete":
                    replacement.update(fragment_index=index, fragment_count=2, fragment_digest=claimed)
                archive.import_rows_v2([replacement])
                self.assertTrue(archive.clerk_snapshot()["gaps"])
                if mode != "complete":
                    archive.import_rows_v2([raw("start", 0, "普通完整修订。")])
                service = NightClerkService({**self.config, "buckets_dir": tmp}, archive=archive, adapter=FakeGPT())
                self.assertNotIn("reply", [s.source_id for s in service.prepare(DAY, NOW).messages])

    async def test_equivalent_assembly_never_removes_an_existing_private_gap(self):
        rows = [raw("home-msg-1-part-0", 0, "普通"), raw("home-msg-1-part-1", 0, "记录。")]
        self.archive.import_rows(rows)
        with self.archive._connect() as conn:
            conn.execute("INSERT INTO raw_clerk_gaps VALUES (?,?,?)", (rows[0]["msg_uuid"], rows[0]["conv_uuid"], rows[0]["at"]))
        self.archive.import_rows_v2([raw("home-msg-1", 0, "普通记录。")])
        self.assertEqual(len(self.archive.clerk_snapshot()["gaps"]), 1)

    async def test_private_exclusion_survives_restart_and_trigger_purge(self):
        self.archive.import_rows([raw("start", 0, "测试标记：高潮"), raw("reply", 1, SECRET)])
        self.service.prepare(DAY, NOW)
        self.archive.hide_message("start", now=NOW)
        self.archive.purge_due(now=NOW + timedelta(days=40))
        restarted = NightClerkService(self.config, archive=self.archive, adapter=FakeGPT())
        self.assertEqual(restarted.prepare(DAY, NOW).messages, ())

    async def test_no_real_adapter_or_automatic_opt_in_means_no_request(self):
        service = NightClerkService(self.config, archive=self.archive)
        self.assertEqual((await service.generate_day(DAY, now=NOW))["skipped"], "adapter_unavailable")
        self.assertEqual((await self.service.generate_day(DAY, now=NOW, automatic=True))["skipped"], "disabled")
        self.assertEqual(self.adapter.calls, [])

    async def test_takeover_requires_success_actual_breath_and_confirmation(self):
        self.archive.import_rows([raw("a", 0, "虚构记录。")])
        await self.service.generate_day(DAY, now=NOW)
        publication = self.service.current_publication(DAY, NOW)
        with self.assertRaises(ValueError):
            self.service.confirm_takeover(DAY, confirmed_by="知知", expected_revision=publication["revision"])
        self.service.store.mark_breath_read(str(DAY), publication["revision"])
        with self.assertRaises(ValueError):
            self.service.confirm_takeover(DAY, confirmed_by="untrusted", expected_revision=publication["revision"])
        self.service.confirm_takeover(DAY, confirmed_by="顾凛", expected_revision=publication["revision"])
        self.assertTrue(self.service.store.takeover())

    async def test_expired_or_replaced_worker_cannot_publish(self):
        with self.service.store.generation_lease() as owner:
            with self.service.store.connect() as conn:
                conn.execute("UPDATE lease SET owner='new-worker'")
            with self.assertRaises(RuntimeError):
                self.service.store.publish(str(DAY), "revision", {}, [], owner=owner)
        self.assertIsNone(self.service.store.publication(str(DAY)))

    async def test_backup_restore_checks_integrity_and_current_fact_references(self):
        self.archive.import_rows([raw("a", 0, "虚构记录。")])
        await self.service.generate_day(DAY, now=NOW)
        data = build_clerk_backup(self.archive, self.service.store)
        target = self.root / "restored"
        restore_clerk_backup(data, target)
        restored = NightClerkService({**self.config, "buckets_dir": str(target)}, adapter=FakeGPT())
        self.assertIsNotNone(restored.current_publication(DAY, NOW))
        self.assertEqual(len(restored.search_facts()), 1)
        self.assertFalse(restored.store.takeover())
        broken = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(broken, "w") as output:
            for name in original.namelist():
                output.writestr(name, b"corrupt" if name == "raw.db" else original.read(name))
        with self.assertRaises(ValueError):
            restore_clerk_backup(broken.getvalue(), self.root / "bad")
        self.assertFalse((self.root / "bad").exists())


class PlanDateScenarios(unittest.TestCase):
    def test_missing_dates_are_undated_and_invalid_dates_rejected(self):
        self.assertIsNone(normalize_due_date(""))
        self.assertEqual(plan_date_state({})["date_status"], "undated")
        with self.assertRaises(ValueError):
            normalize_due_date("2026-02-30")

    def test_seven_calendar_days_include_today_and_overdue_is_explicit(self):
        now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        self.assertTrue(plan_date_state({"due_date": "2026-08-26"}, reference_time=now)["expand"])
        self.assertFalse(plan_date_state({"due_date": "2026-08-27"}, reference_time=now)["expand"])
        self.assertEqual(plan_date_state({"due_date": "2026-08-19"}, reference_time=now)["date_status"], "overdue")


if __name__ == "__main__":
    unittest.main()
