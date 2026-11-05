"""Synthetic integration checks for Breath, takeover and manual plan boundaries."""
from datetime import datetime
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.responses import JSONResponse
from daily_continuity import DailyContinuityError, DailyContinuityService
from clerk_pipeline import ClerkError
from tools import _common, _runtime as rt
from tools.breath import dispatch
from tools.breath.startup import surface_startup, startup_total_hard_tokens
from tools.breath.surface import surface_default
from tools.plan.core import plan_create
from utils import count_tokens_approx
from web import _shared as sh, daily_continuity as web_daily
if __package__:
    from .test_clerk_pipeline import FakeGPT, raw, DAY
else:
    from test_clerk_pipeline import FakeGPT, raw, DAY


def bucket(mid, content, *, kind="dynamic", created="2026-08-20T19:00:00", **meta):
    return {"id": mid, "content": content, "metadata": {"id": mid, "name": mid, "type": kind,
            "created": created, "last_active": created, "importance": 8, "domain": ["daily"], "tags": [], **meta}}


class Registry:
    def __init__(self):
        self.routes = {}
    def custom_route(self, path, methods):
        def register(fn):
            for method in methods:
                self.routes[(method, path)] = fn
            return fn
        return register


class IntegrationScenarios(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = []
        for name, value in {"config": {"night_clerk": {"enabled": True}},
                            "decay_engine": SimpleNamespace(ensure_started=AsyncMock(), calculate_score=lambda m: m.get("importance", 0)),
                            "embedding_engine": None, "logger": MagicMock(), "mark_op": None,
                            "record_v3_tool_event": lambda *a, **k: None,
                            "nightly_dreams": None, "private_continuity": None}.items():
            self.patches.append(patch.object(rt, name, value))
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_layout_order_dates_dedup_independent_feel_and_old_cap(self):
        records = [bucket("core", "核心原文。", kind="permanent", pinned=True),
                   bucket("duplicate", "昨日已写的事实。"), bucket("recent", "近期独立细节。"),
                   bucket("old", "合格旧事。", created="2026-08-01T12:00:00", activation_count=0),
                   bucket("feel", "独立的感受原文。", kind="feel", triggered_by="duplicate"),
                   bucket("overdue", "过期待办全文。", kind="plan", due_date="2026-08-19"),
                   bucket("near", "七天内待办全文。", kind="plan", due_date="2026-08-26"),
                   bucket("far", "远期全文不要自动展开。", kind="plan", due_date="2026-09-10")]
        text = await surface_startup(records, max_results=4, hard_tokens=5000, soft_tokens=3000,
                                     reference_time=datetime(2026, 8, 20, 20), clerk_layout=True,
                                     daily_impression="五栏昨日印象。", daily_factual_texts=("昨日已写的事实。",),
                                     private_continuity="尚未和好的架。", nightly_dream="独立保留的梦。")
        ordered = ["=== 核心准则 ===", "=== 尚未解决的冲突 ===", "五栏昨日印象。", "=== 活动计划 ===",
                   "=== 最近24小时 ===", "=== 旧事联想 ===", "=== 相关 feel ===", "独立保留的梦。"]
        self.assertEqual([text.index(x) for x in ordered], sorted(text.index(x) for x in ordered))
        self.assertNotIn("昨日已写的事实。", text)
        self.assertIn("独立的感受原文。", text)
        self.assertIn("已过期 2026-08-19", text)
        self.assertIn("七天内待办全文。", text)
        self.assertNotIn("远期全文不要自动展开。", text)
        self.assertLessEqual(count_tokens_approx(text), startup_total_hard_tokens(5000))

    async def test_no_ordinary_context_does_not_suppress_feel(self):
        records = [bucket("duplicate", "同一事实。"), bucket("feel", "这份感受要保留。", kind="feel", triggered_by="duplicate")]
        text = await surface_startup(records, max_results=4, hard_tokens=5000, soft_tokens=3000,
                                     reference_time=datetime(2026, 8, 20, 20), clerk_layout=True,
                                     daily_impression="昨日印象", daily_factual_texts=("同一事实。",))
        self.assertIn("这份感受要保留。", text)

    async def test_pending_raw_keeps_core_breath_available(self):
        clerk = SimpleNamespace(current_publication=MagicMock(side_effect=ClerkError("pending_raw_fragments")))
        service = SimpleNamespace(enabled=True, clerk=clerk, previous_day=lambda: DAY)
        with patch.object(rt, "bucket_mgr", SimpleNamespace(list_all=AsyncMock(return_value=[bucket("core", "核心仍可读。", kind="permanent", pinned=True)]))), patch.object(rt, "daily_continuity", service):
            text = await surface_default(max_results=4, max_tokens=5000, startup=True, tag_filter=None)
        self.assertIn("核心仍可读。", text)

    async def test_publication_invalidated_during_render_is_not_returned(self):
        publication = {"rendered": "STALE_IMPRESSION_SENTINEL", "revision": "old", "cards": [], "selected_ids": []}
        clerk = SimpleNamespace(current_publication=MagicMock(side_effect=[publication, None]))
        service = SimpleNamespace(enabled=True, clerk=clerk, previous_day=lambda: DAY)
        with patch.object(rt, "bucket_mgr", SimpleNamespace(list_all=AsyncMock(return_value=[bucket("core", "核心仍可读。", kind="permanent", pinned=True)]))), patch.object(rt, "daily_continuity", service):
            text = await surface_default(max_results=4, max_tokens=5000, startup=True, tag_filter=None)
        self.assertNotIn("STALE_IMPRESSION_SENTINEL", text)
        self.assertIn("核心仍可读。", text)

    async def test_fact_tool_respects_complete_response_budget(self):
        cards = [{"id": str(i), "text": "完整事实条目。" * 50, "evidence": [{"source_id": "fixture"}]} for i in range(20)]
        clerk = SimpleNamespace(search_facts=lambda *a, **k: cards)
        with patch.object(rt, "daily_continuity", SimpleNamespace(clerk=clerk)):
            text = await dispatch(domain="fact", max_tokens=500, max_results=20)
        self.assertLessEqual(count_tokens_approx(text), 500)

    async def test_string_enabled_blocks_old_ds_auto_plan_updates(self):
        bucket_manager = SimpleNamespace(list_all=AsyncMock(side_effect=AssertionError("old plan loop must not run")))
        with patch.object(rt, "config", {"night_clerk": {"enabled": "true"}}), patch.object(rt, "bucket_mgr", bucket_manager):
            await _common.check_plan_resolution("新的普通事件")
        bucket_manager.list_all.assert_not_awaited()

    async def test_manual_plan_date_is_validated_and_saved_after_explicit_call(self):
        manager = SimpleNamespace(list_all=AsyncMock(return_value=[]), create=AsyncMock(return_value="plan-fixture"), update=AsyncMock(return_value=True))
        with patch.object(rt, "bucket_mgr", manager):
            await plan_create("虚构待办", due_date="2026-08-25")
            self.assertEqual(manager.update.call_args.kwargs["due_date"], "2026-08-25")
            manager.create.reset_mock()
            result = await plan_create("另一个虚构待办", due_date="2026-02-30")
            manager.create.assert_not_awaited()
            self.assertIn("day", result)

    async def test_unconfirmed_candidate_cannot_create_formal_plan(self):
        manager = SimpleNamespace(create=AsyncMock(side_effect=AssertionError("unconfirmed candidate cannot write")))
        with patch.object(rt, "bucket_mgr", manager):
            result = await plan_create("候选", candidate_id="candidate", confirmed_by="")
        manager.create.assert_not_awaited()
        self.assertIn("确认", result)

    async def test_clerk_facade_ignores_notes_buckets_and_ds_until_confirmed_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SimpleNamespace(list_all=AsyncMock(side_effect=AssertionError("raw-only clerk cannot read buckets")))
            ds = SimpleNamespace(_chat=AsyncMock(side_effect=AssertionError("clerk must not call DS")))
            service = DailyContinuityService({"buckets_dir": tmp, "night_clerk": {"enabled": True}},
                                             bucket_mgr=manager, dehydrator=ds, clerk_adapter=FakeGPT())
            service.previous_day = lambda reference=None: DAY
            self.assertFalse(service.notes_retired)
            service.ingest_note({"source_client": "cc", "memory_day": str(DAY),
                                 "content": f"# {DAY} 便签\nNOTE_SENTINEL"})
            service.clerk.archive.import_rows([raw("a", 0, "虚构原文。")])
            result = await service.generate_day(DAY)
            self.assertTrue(result["ok"], result)
            self.assertFalse(service.notes_retired)
            self.assertNotIn("NOTE_SENTINEL", str(service.clerk.adapter.calls))
            self.assertIn("NOTE_SENTINEL", service.get_day(DAY)["note_sources"][0]["content"])
            manager.list_all.assert_not_awaited()
            ds._chat.assert_not_awaited()
            publication = service.clerk.current_publication(DAY)
            self.assertTrue(service.acknowledge_breath(publication["rendered"]))
            service.clerk.confirm_takeover(DAY, confirmed_by="知知", expected_revision=publication["revision"])
            self.assertTrue(service.notes_retired)
            with self.assertRaisesRegex(DailyContinuityError, "retired"):
                service.ingest_note(object())
            registry = Registry()
            web_daily.register(registry)
            read_body = AsyncMock(side_effect=AssertionError("retired upload must not read body"))
            with patch.object(sh, "daily_continuity", service, create=True), patch.object(sh, "_require_auth", lambda r: None), patch.object(sh, "_read_json_object", read_body), patch.object(web_daily, "_authorized", lambda r: True):
                for path in ("/api/daily-continuity/notes", "/internal/daily-notes"):
                    response = await registry.routes[("POST", path)](SimpleNamespace())
                    self.assertEqual(response.status_code, 410)
            read_body.assert_not_awaited()

    async def test_new_http_endpoints_require_auth_before_read_or_processing(self):
        registry = Registry()
        web_daily.register(registry)
        request = SimpleNamespace()
        with patch.object(sh, "_require_auth", lambda r: JSONResponse({"error": "Unauthorized"}, status_code=401)), patch.object(sh, "_read_json_object", AsyncMock(side_effect=AssertionError("no body before auth"))):
            for (method, path), handler in registry.routes.items():
                if path.startswith("/api/night-clerk/"):
                    response = await handler(request)
                    self.assertEqual(response.status_code, 401)

    async def test_pending_raw_does_not_report_an_empty_impression_as_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DailyContinuityService({"buckets_dir": tmp}, bucket_mgr=object(), dehydrator=object())
            service.clerk = SimpleNamespace(
                current_publication=MagicMock(side_effect=ClerkError("pending_raw_fragments")),
                store=SimpleNamespace(status=lambda day: {"status": "ready", "code": ""}, publication=lambda day: {"revision": "old"}))
            state = service.get_day(DAY)
            self.assertEqual(state["generation_status"], "stale")
            self.assertEqual(state["generation_code"], "pending_raw_fragments")
            self.assertEqual(state["effective_content"], "")


if __name__ == "__main__":
    unittest.main()
