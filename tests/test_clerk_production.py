"""Production-owned scene preparation and bounded automatic billing, synthetic."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import tempfile
import unittest

from clerk_production import prepare_production_day, approval_revision
from clerk_redaction import ClerkLexicon
from clerk_privacy import PrivacyInputError
from clerk_pipeline import NightClerkService, encoded_size
from daily_continuity import DailyContinuityService

DAY = date(2002, 1, 2)
NOW = datetime(2002, 1, 4, tzinfo=timezone.utc)
LEX = {"schema": 1, "version": 1, "replace": ["合成敏感词"], "whitelist": ["合成保留词"], "include_core": True}


def row(mid, at, text, speaker="知知"):
    return {"msg_uuid": mid, "conv_uuid": "fixture", "speaker": speaker, "at": at, "text": text}


class ProductionScenes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="production-scene-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.words = Path(self.tmp.name) / "words.json"
        self.words.write_text(json.dumps(LEX))
        self.words.chmod(0o600)
        self.approved = approval_revision(ClerkLexicon.load(self.words))

    def prepare(self, rows, **kw):
        return prepare_production_day(
            {"rows": rows, "gaps": kw.pop("gaps", []), "complete": kw.pop("complete", True)},
            DAY,
            lexicon_path=self.words,
            approved_revision=kw.pop("approval", self.approved),
            now=kw.pop("now", NOW),
            **kw,
        )

    def test_owned_profile_masks_words_but_preview_helper_stays_locked(self):
        source = [row("a", "2002-01-02T08:00:00+08:00", "合成敏感词，合成保留词，我喜欢你。")]
        prepared = self.prepare(source)
        self.assertFalse(prepared.privacy_review_required)
        self.assertIsNotNone(prepared.redaction_policy)
        self.assertIn("**，合成保留词", prepared.messages[0].text)

    def test_mismatch_missing_words_gap_pending_and_immature_boundary_all_reject(self):
        source = [row("a", "2002-01-02T08:00:00+08:00", "合成正文")]
        for kw in (
            {"approval": "wrong"},
            {"gaps": [{"at": "2002-01-02T08:30:00+08:00"}]},
            {"complete": False},
            {"now": datetime.fromisoformat("2002-01-03T04:59:59+08:00")},
        ):
            with self.subTest(kw=kw), self.assertRaises(PrivacyInputError):
                self.prepare(source, **kw)
        self.words.unlink()
        with self.assertRaises(PrivacyInputError):
            self.prepare(source)

    def test_cross_cutoff_scene_and_lookahead_preroll_are_excluded(self):
        source = [
            row("before", "2002-01-02T03:57:00+08:00", "我们正在做爱。"),
            row("during", "2002-01-02T04:01:00+08:00", "嗯。"),
            row("normal", "2002-01-02T10:00:00+08:00", "买了合成风筝。"),
            row("late-pre", "2002-01-03T03:59:00+08:00", "合成场景前奏。"),
            row("late-start", "2002-01-03T04:05:00+08:00", "我们正在做爱。"),
        ]
        prepared = self.prepare(source)
        self.assertEqual([s.source_id for s in prepared.messages], ["normal"])
        self.assertEqual(
            {d.msg_uuid for d in prepared.coverage if d.status == "intentional_skip"}, {"during", "late-pre"}
        )

    def test_new_later_day_and_wall_clock_do_not_churn_old_revision(self):
        source = [row("a", "2002-01-02T08:00:00+08:00", "合成公开原话。")]
        a = self.prepare(source)
        b = self.prepare(
            source + [row("future", "2002-01-04T12:00:00+08:00", "新的一天。")], now=NOW + timedelta(days=1)
        )
        self.assertEqual(a, b)


class Adapter:
    model = "synthetic-production"
    request_size = staticmethod(encoded_size)

    def __init__(self, invalid=False):
        self.calls = []
        self.invalid = invalid

    async def generate(self, p):
        self.calls.append(p)
        if p["operation"] == "extract":
            return {
                "facts": [
                    {
                        "kind": "open_loops" if "？" in f["text"] else "events",
                        "text": f["text"] if "？" in f["text"] else "合成外出买了风筝。",
                        "fragment_ids": [f["fragment_id"]],
                        "importance": 5,
                    }
                    for f in p["sources"]
                    if f["speaker"] == "知知"
                ]
            }
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
            return {
                "decisions": {
                    c["id"]: {
                        "state": "answered" if c["kind"] == "open_loops" else "future",
                        "answer_ids": [s["id"] for s in c["sources"] if s["speaker"] == "顾凛"]
                        if c["kind"] == "open_loops"
                        else [],
                    }
                    for c in p["cases"]
                }
            }
        return {
            "selected_ids": [c["id"] for c in p["cards"]],
            "event_groups": [] if self.invalid else [[i] for i in p["event_ids"]],
        }


class ProductionPipeline(unittest.IsolatedAsyncioTestCase):
    async def setup_service(self, tmp, adapter):
        words = Path(tmp) / "words.json"
        words.write_text(json.dumps(LEX))
        words.chmod(0o600)
        cfg = {
            "buckets_dir": str(Path(tmp) / "vault"),
            "night_clerk": {
                "enabled": True,
                "automatic": True,
                "input_profile": "scene_word_mask",
                "lexicon_path": str(words),
                "approved_input_revision": approval_revision(ClerkLexicon.load(words)),
                "automatic_start_day": "2002-01-02",
            },
        }
        service = NightClerkService(cfg, adapter=adapter)
        service.archive.import_rows(
            [
                {
                    **row("a", "2002-01-02T08:00:00+08:00", "我喜欢你，买了合成敏感词和合成风筝。"),
                    "day": "2002-01-02",
                    "thinking": "NATIVE_MUST_STAY_LOCAL",
                },
                {**row("b", "2002-01-02T08:01:00+08:00", "几点出发？"), "day": "2002-01-02"},
                {**row("c", "2002-01-02T08:02:00+08:00", "六点。", speaker="顾凛"), "day": "2002-01-02"},
            ]
        )
        return cfg, service

    async def test_real_preparer_reaches_masked_quality_and_preserves_native_archive(self):
        with tempfile.TemporaryDirectory(prefix="production-path-synthetic-") as tmp:
            a = Adapter()
            cfg, s = await self.setup_service(tmp, a)
            result = await s.generate_day(DAY, now=NOW)
            self.assertTrue(result["ok"], result)
            self.assertIn("mood_scan", [p["operation"] for p in a.calls])
            self.assertIsNotNone(s.prepare(DAY, NOW).redaction_policy)
            self.assertIn("followup_review", [p["operation"] for p in a.calls])
            self.assertNotIn("NATIVE_MUST_STAY_LOCAL", str(a.calls))
            self.assertNotIn("合成敏感词", str(a.calls))
            self.assertEqual(
                s.archive.search("NATIVE_MUST_STAY_LOCAL", include_thinking=True)[0]["thinking"],
                "NATIVE_MUST_STAY_LOCAL",
            )

    async def test_automatic_validation_failure_pauses_across_service_restart(self):
        with tempfile.TemporaryDirectory(prefix="production-pause-synthetic-") as tmp:
            a = Adapter(invalid=True)
            cfg, s = await self.setup_service(tmp, a)
            failed = await s.generate_day(DAY, now=NOW, automatic=True)
            self.assertFalse(failed["ok"])
            calls = len(a.calls)
            self.assertEqual(failed["code"], "invalid_event_groups")
            again = NightClerkService(cfg, archive=s.archive, adapter=a)
            result = await again.generate_day(DAY, now=NOW, automatic=True)
            self.assertEqual(result["skipped"], "paused")
            self.assertEqual(len(a.calls), calls)

    async def test_daily_limit_is_durable_and_manual_retry_does_not_reset_it(self):
        with tempfile.TemporaryDirectory(prefix="production-budget-synthetic-") as tmp:
            a = Adapter()
            cfg, s = await self.setup_service(tmp, a)
            s.daily_call_limit = 1
            result = await s.generate_day(DAY, now=NOW)
            self.assertEqual(result["code"], "daily_call_limit")
            self.assertEqual(len(a.calls), 1)
            again = NightClerkService(cfg, archive=s.archive, adapter=a)
            again.daily_call_limit = 1
            result = await again.generate_day(DAY, now=NOW)
            self.assertEqual(result["code"], "daily_call_limit")
            self.assertEqual(len(a.calls), 1)

    async def test_scheduler_requires_explicit_start_and_runs_one_day_per_tick(self):
        with tempfile.TemporaryDirectory(prefix="production-schedule-synthetic-") as tmp:
            cfg, s = await self.setup_service(tmp, Adapter())
            daily = DailyContinuityService(
                cfg, bucket_mgr=SimpleNamespace(), dehydrator=SimpleNamespace(), clerk_adapter=Adapter()
            )
            daily.clerk = SimpleNamespace(
                automatic=True,
                archive=SimpleNamespace(days=lambda: [{"day": "2002-01-01"}]),
                generate_day=AsyncMock(return_value={"ok": True, "status": "ready"}),
            )
            daily.pending_days = lambda **kw: [date(2002, 1, i) for i in range(1, 5)]
            await daily.ensure_pending()
            self.assertEqual(daily.clerk.generate_day.await_count, 1)
            self.assertEqual(daily.clerk.generate_day.call_args.args[0], DAY)
            daily.clerk.generate_day = AsyncMock(
                side_effect=[{"ok": True, "skipped": "current"}, {"ok": True, "status": "ready"}]
            )
            await daily.ensure_pending()
            self.assertEqual(daily.clerk.generate_day.await_count, 2)
            self.assertEqual(daily.clerk.generate_day.call_args.args[0], DAY + timedelta(days=1))
            daily.clerk.generate_day.reset_mock()
            cfg["night_clerk"].pop("automatic_start_day")
            await daily.ensure_pending()
            daily.clerk.generate_day.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
