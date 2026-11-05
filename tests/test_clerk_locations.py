"""Synthetic visit expiry and eight-event regressions; no private data."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import tempfile
import unittest

from clerk_input import ClerkDayInput, ClerkSource
from clerk_privacy import Decision
from clerk_pipeline import NightClerkService, digest, render_impression
from clerk_quality import decorate, priority_selection, rank_full_pool
from clerk_locations import events_for_day, scope_for_card, expiry, plan_state, confirmation_scope
from tools import _runtime as rt
from tools.plan.core import plan_create
from tools.breath.surface import surface_plans
from tools.breath.startup import surface_startup
from web import plans as web_plans


def sample(*texts, start="2026-08-20T12:00:00+00:00", conv="fixture"):
    at = datetime.fromisoformat(start)
    sources = tuple(
        ClerkSource(f"{at.date()}-{i}", "知知", (at + timedelta(minutes=i)).isoformat(), text, conv)
        for i, text in enumerate(texts)
    )
    coverage = tuple(Decision(s.source_id, datetime.fromisoformat(s.at), "allowed", "allowed") for s in sources)
    return ClerkDayInput(
        at.date().isoformat(), digest([s.text for s in sources]), sources, coverage, True, False, "b" * 64
    )


def fact(day, index=0, kind="commitment", text=None, importance=5):
    s = day.messages[index]
    value = {
        "kind": kind,
        "scope": "day",
        "text": text or s.text,
        "importance": importance,
        "due_date": None,
        "status": "candidate" if kind == "commitment" else "fact",
        "evidence": [
            {
                "source_id": s.source_id,
                "source_sha256": digest(s.text),
                "at": s.at,
                "speaker": s.speaker,
                "start": 0,
                "end": len(s.text),
            }
        ],
    }
    return {"id": digest(value), **value}


def plan(content, scope=None, **metadata):
    meta = {"type": "plan", "status": "active", "created": "2026-08-20T12:00:00+00:00", **metadata}
    if scope:
        meta.update(clerk_location_scope=scope, clerk_location_content_sha256=digest(content))
    return {"id": "fixture-plan", "content": content, "metadata": meta}


class VisitContract(unittest.TestCase):
    def test_same_day_expiry_has_departure_evidence_and_preserves_original(self):
        d = sample("离开南京前喝杯茶。", "我已经离开南京了。")
        c = fact(d)
        scope = scope_for_card(c, d, events_for_day(d))
        before = dict(c)
        state = expiry(scope, events_for_day(d), as_of=d.messages[-1].at)
        self.assertEqual(state["place"], "南京")
        self.assertEqual(state["departure_evidence"]["source_id"], d.messages[1].source_id)
        self.assertEqual(c, before)
        self.assertIsNone(expiry(scope, events_for_day(d), as_of=d.messages[0].at))

    def test_only_actual_self_report_not_future_conditional_quote_negation_or_other(self):
        for text in (
            "我明天离开南京。",
            "如果我离开南京了。",
            "我没有离开南京。",
            "我还没离开南京。",
            "他说我已经离开南京了。",
            "小明已经离开南京了。",
            "顾凛已经离开南京了。",
            "我已经离开南京了？",
            "我打算离开南京。",
            "“我已经离开南京了”。",
            "我可能离开南京了。",
            "到了南京吗？",
        ):
            with self.subTest(text=text):
                self.assertEqual(events_for_day(sample(text)), [])
        self.assertEqual(events_for_day(sample("我已经离开南京了。"))[0]["place"], "南京")

    def test_nonreal_qualifier_applies_across_comma_to_reported_departure(self):
        for text in (
            "我想象一下，我们已经离开南京了。",
            "我希望，我们已经离开南京了。",
            "假装一下，我已经离开南京了。",
            "我以为，我们已经离开南京了。",
        ):
            self.assertEqual(events_for_day(sample(text)), [], text)

    def test_third_person_cannot_become_self_by_dropping_subject_after_comma(self):
        for text in (
            "小明刚给我发消息，已经离开南京了。",
            "小明刚跟我讲，已经离开南京了。",
            "他也动身了，离开南京了。",
            "已经离开南京了。",
        ):
            self.assertEqual(events_for_day(sample(text)), [], text)

    def test_historical_report_is_not_timestamped_as_current_departure(self):
        for text in (
            "前年这个时候，我已经离开南京了。",
            "回忆上次旅行，我已经离开南京了。",
            "去年我已经离开南京了。",
            "三天前，我已经离开南京了。",
        ):
            self.assertEqual(events_for_day(sample(text)), [], text)

    def test_bind_requires_current_visit_and_both_summary_and_evidence(self):
        for text in (
            "下次来南京再喝茶。",
            "以后每次在南京期间喝茶。",
            "如果在南京期间喝茶。",
            "明天去南京喝茶。",
            "明年在南京期间我会带你逛灯会。",
            "我每年在南京期间都会带你喝茶。",
            "下个月离开南京前喝茶。",
            "2027年离开南京前喝茶。",
        ):
            d = sample(text)
            self.assertIsNone(scope_for_card(fact(d), d, events_for_day(d)))
        d = sample("离开南京前喝茶，明天寄快递。")
        self.assertIsNone(scope_for_card(fact(d, text="明天寄快递。"), d, events_for_day(d)))
        d = sample("下次离开南京前喝茶。")
        self.assertIsNone(scope_for_card(fact(d, text="离开南京前喝茶。"), d, []))

    def test_unrelated_or_recurring_confirmation_does_not_inherit_visit(self):
        d = sample("离开南京前喝杯茶。")
        c = fact(d)
        c["location_scope"] = scope_for_card(c, d, [])
        for text in ("我会一直陪着你。", "下次离开南京前喝茶。", "离开杭州前喝茶。"):
            self.assertIsNone(confirmation_scope(c, text))
        self.assertEqual(confirmation_scope(c, c["text"]), c["location_scope"])
        self.assertEqual(confirmation_scope(c, "离开南京前一起喝杯茶。"), c["location_scope"])

    def test_visit_owner_and_present_visit_proof_are_required(self):
        for text in (
            "等我们到了南京，在南京期间我会带你逛灯会。",
            "小明在南京期间，我会每天给他做饭。",
            "你离开南京前我会把文件发给你。",
            "在南京期间我会带你逛灯会。",
            "小明明天就走，离开南京前我会把文件给他。",
        ):
            d = sample(text)
            self.assertIsNone(scope_for_card(fact(d), d, events_for_day(d)), text)
        d = sample("我现在在南京。", "我在南京期间带你逛灯会。")
        self.assertEqual(scope_for_card(fact(d, 1), d, events_for_day(d))["place"], "南京")

    def test_implicit_departure_binds_known_place_but_not_across_gap_or_conversation(self):
        d = sample("我现在在南京。", "明天离开前喝杯茶。")
        self.assertEqual(scope_for_card(fact(d, 1), d, events_for_day(d))["place"], "南京")
        for variant in (
            replace(d, messages=(replace(d.messages[0], conversation_id="other"), d.messages[1])),
            replace(d, coverage=(replace(d.coverage[0], status="intentional_skip"), d.coverage[1])),
        ):
            self.assertIsNone(scope_for_card(fact(variant, 1), variant, events_for_day(variant)))

    def test_cross_midnight_departure_return_and_new_visit_do_not_revive_old(self):
        d = sample("这次在南京期间喝杯茶。", start="2026-08-20T23:59:00+00:00")
        scope = scope_for_card(fact(d), d, [])
        later = sample("我已经离开南京了。", "我到了南京。", "离开南京前买把伞。", start="2026-08-21T00:01:00+00:00")
        events = events_for_day(later)
        self.assertIsNotNone(expiry(scope, events, as_of=later.messages[-1].at))
        newer = scope_for_card(fact(later, 2), later, events)
        self.assertNotEqual(scope["visit_id"], newer["visit_id"])
        self.assertIsNone(expiry(newer, events))
        self.assertIsNone(expiry(scope, events, as_of="2026-08-21T00:00:59+00:00"))

    def test_leaving_hotel_does_not_expire_city_and_resolved_is_preserved(self):
        d = sample("离开南京前喝茶。", "我已经离开星光酒店了。")
        scope = scope_for_card(fact(d), d, [])
        b = plan(d.messages[0].text, scope)
        self.assertEqual(plan_state(b, events_for_day(d))["effective_status"], "active")
        departed = events_for_day(sample("我已经离开南京了。", start="2026-08-21T12:00:00+00:00"))
        self.assertEqual(plan_state(b, departed)["effective_status"], "expired")
        self.assertEqual(b["metadata"]["status"], "active")
        b["metadata"]["status"] = "resolved"
        self.assertEqual(plan_state(b, departed)["effective_status"], "resolved")

    def test_raw_privacy_gate_applies_to_local_location_scan(self):
        from clerk_privacy import PrivacyInputError

        with self.assertRaises(PrivacyInputError):
            events_for_day(replace(sample("我已经离开南京了。"), privacy_review_required=True))

    def test_eight_concrete_events_and_renderer_cap_without_increasing_budget(self):
        d = sample(*[f"在公园拍到了第{i}只白鹭。" for i in range(10)])
        cards = [decorate(fact(d, i, "events", importance=1 if i < 8 else 10)) for i in range(10)]
        reply = {"selected_ids": [f"c{i}" for i in range(1, 9)], "event_groups": [[f"c{i}"] for i in range(1, 11)]}
        chosen, _ = priority_selection(reply, cards, 17)
        self.assertEqual([c["id"] for c in chosen], [c["id"] for c in cards[:8]])
        chosen = rank_full_pool(cards, [[c["id"]] for c in cards], 17, chosen)
        self.assertEqual([c["id"] for c in chosen], [c["id"] for c in cards[:8]])
        text, ids = render_impression(d, cards)
        self.assertEqual(len(ids), 8)
        self.assertLessEqual(len(text), 1000)
        self.assertEqual(len(render_impression(d, cards[:2])[1]), 2)

    def test_generic_observation_does_not_displace_specific_event_despite_model_preference(self):
        from clerk_quality import bare_event

        d = sample("知知说人很多。", *[f"去了湖边拍到了第{i}只白鹭。" for i in range(8)])
        values = [decorate(fact(d, i, "events", importance=10 if i == 0 else 1)) for i in range(9)]
        self.assertEqual({c["priority"] for c in values}, {1})
        self.assertTrue(bare_event(values[0]))
        for c in values[1:]:
            self.assertFalse(bare_event(c))
        final = rank_full_pool(values, [[c["id"]] for c in values], 17, values[:8])
        self.assertEqual(len(final), 8)
        self.assertNotIn(values[0]["id"], [c["id"] for c in final])
        self.assertEqual(len(values), 9)  # Re-ranking never deletes source facts.
        for text in ("桥边人很多，挤得走不动。", "在街角买了一只兔子玩偶。", "风景好得让她停下来拍照。"):
            self.assertFalse(bare_event({"kind": "events", "text": text}))


class Adapter:
    model = "synthetic-visit"

    def __init__(self):
        self.calls = []

    async def generate(self, p):
        from clerk_chatgpt import request_body

        request_body(p, self.model)
        self.calls.append(p)
        if p["operation"] == "extract":
            return {
                "facts": [
                    {
                        "kind": "commitment" if "前" in s["text"] or "期间" in s["text"] else "events",
                        "text": s["text"],
                        "fragment_ids": [s["fragment_id"]],
                        "importance": 5,
                        "due_date": None,
                    }
                    for s in p["sources"]
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
            return {"decisions": {c["id"]: {"state": "future", "answer_ids": []} for c in p["cases"]}}
        return {
            "selected_ids": [c["id"] for c in p["cards"]][:8],
            "event_groups": [[c["id"]] for c in p["cards"] if c["kind"] == "events"],
        }


class VisitPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_scope_survives_actual_bucket_storage_round_trip(self):
        from bucket_manager import BucketManager

        d = sample("离开南京前喝杯茶。")
        scope = scope_for_card(fact(d), d, [])
        with tempfile.TemporaryDirectory(prefix="visit-bucket-synthetic-") as tmp:
            manager = BucketManager({"buckets_dir": tmp})
            mid = await manager.create(content=d.messages[0].text, bucket_type="plan")
            await manager.update(
                mid,
                status="active",
                clerk_location_scope=scope,
                clerk_location_content_sha256=digest(d.messages[0].text),
            )
            reopened = BucketManager({"buckets_dir": tmp})
            stored = await reopened.get(mid)
            self.assertEqual(stored["metadata"]["clerk_location_scope"], scope)
            self.assertEqual(stored["metadata"]["status"], "active")

    async def test_multiple_published_days_filter_archive_only_once_per_snapshot(self):
        if __package__:
            from .test_clerk_pipeline import FakeGPT
        else:
            from test_clerk_pipeline import FakeGPT
        with tempfile.TemporaryDirectory(prefix="visit-batch-synthetic-") as tmp:
            service = NightClerkService({"buckets_dir": tmp, "night_clerk": {"enabled": True}}, adapter=FakeGPT())
            for i in range(2):
                at = datetime(2026, 8, 20 + i, 12, tzinfo=timezone.utc)
                service.archive.import_rows(
                    [
                        {
                            "msg_uuid": f"fixture-{i}",
                            "conv_uuid": "fixture",
                            "conv_name": "fixture",
                            "speaker": "知知",
                            "at": at.isoformat(),
                            "day": at.date().isoformat(),
                            "text": "我现在在南京。",
                            "thinking": "",
                        }
                    ]
                )
            now = datetime(2026, 8, 23, tzinfo=timezone.utc)
            for target in ("2026-08-20", "2026-08-21"):
                result = await service.generate_day(target, now=now)
                self.assertTrue(result["ok"], result)
            with patch.object(service.archive, "clerk_snapshot", wraps=service.archive.clerk_snapshot) as snapshot:
                state = service.location_snapshot()
                self.assertEqual(len(state["publications"]), 2)
                snapshot.assert_called_once()

    async def test_confirmed_plan_inherits_scope_then_both_evidence_sides_can_retract(self):
        first = sample("离开南京前喝杯茶。")
        second = sample("我已经离开南京了。", start="2026-08-21T12:00:00+00:00")
        days = {first.day: first, second.day: second}
        with tempfile.TemporaryDirectory(prefix="visit-confirm-synthetic-") as tmp:
            service = NightClerkService(
                {"buckets_dir": tmp, "night_clerk": {"enabled": True}}, archive=object(), adapter=Adapter()
            )
            service.prepare = lambda target, *a, **k: days[str(target)]
            now = datetime(2026, 8, 23, tzinfo=timezone.utc)
            self.assertTrue((await service.generate_day(first.day, now=now))["ok"])
            candidate = service.search_facts(candidates_only=True)[0]
            manager = SimpleNamespace(
                list_all=AsyncMock(return_value=[]), create=AsyncMock(return_value="confirmed"), update=AsyncMock()
            )
            decay = SimpleNamespace(ensure_started=AsyncMock())
            with (
                patch.object(rt, "daily_continuity", SimpleNamespace(clerk=service)),
                patch.object(rt, "bucket_mgr", manager),
                patch.object(rt, "decay_engine", decay),
                patch.object(rt, "logger", MagicMock()),
            ):
                await plan_create(candidate["text"], candidate_id=candidate["id"], confirmed_by="知知")
                metadata = manager.update.call_args.kwargs
                self.assertEqual(metadata["clerk_location_scope"], candidate["location_scope"])
                await plan_create("我会一直陪着你。", candidate_id=candidate["id"], confirmed_by="知知")
                self.assertNotIn("clerk_location_scope", manager.update.call_args.kwargs)
            b = {"id": "confirmed", "content": candidate["text"], "metadata": metadata}
            self.assertTrue((await service.generate_day(second.day, now=now))["ok"])
            self.assertEqual(service.plan_location_state(b)["effective_status"], "expired")
            days[first.day] = replace(first, selection_revision="c" * 64)
            self.assertEqual(service.plan_location_state(b)["effective_status"], "active")
            self.assertEqual(b["metadata"]["status"], "active")

    async def test_reader_short_circuits_unscoped_and_shares_one_full_snapshot(self):
        from tools._plan_state import plan_reader

        d = sample("离开南京前喝茶。", "我已经离开南京了。")
        events = events_for_day(d)
        clerk = SimpleNamespace(
            location_snapshot=MagicMock(return_value={"events": events}),
            plan_location_state=lambda bucket, as_of=None, snapshot=None: plan_state(
                bucket, snapshot["events"], as_of=as_of
            ),
        )
        with patch.object(rt, "daily_continuity", SimpleNamespace(clerk=clerk)):
            read = plan_reader()
            for i in range(20):
                read(plan("陪你吃饭。"))
            clerk.location_snapshot.assert_not_called()
            for i in range(20):
                self.assertEqual(read(plan("离开南京前喝茶。"))["effective_status"], "expired")
            clerk.location_snapshot.assert_called_once()

    async def test_cross_day_index_candidate_confirmation_and_source_retraction(self):
        first = sample("离开南京前喝杯茶。")
        second = sample("我已经离开南京了。", start="2026-08-21T12:00:00+00:00")
        days = {first.day: first, second.day: second}
        with tempfile.TemporaryDirectory(prefix="visit-synthetic-") as tmp:
            service = NightClerkService(
                {"buckets_dir": tmp, "night_clerk": {"enabled": True}}, archive=object(), adapter=Adapter()
            )
            service.prepare = lambda target, *a, **k: days[str(target)]
            now = datetime(2026, 8, 23, tzinfo=timezone.utc)
            for target in days:
                self.assertTrue((await service.generate_day(target, now=now))["ok"])
            self.assertEqual(service.search_facts(candidates_only=True), [])
            expired = service.search_facts(candidates_only=True, include_expired=True)[0]
            self.assertEqual(expired["effective_status"], "expired")
            self.assertEqual(len(service.search_facts(candidates_only=True, as_of=first.messages[0].at)), 1)
            manager = SimpleNamespace(create=AsyncMock())
            with (
                patch.object(rt, "daily_continuity", SimpleNamespace(clerk=service)),
                patch.object(rt, "bucket_mgr", manager),
            ):
                reply = await plan_create(expired["text"], candidate_id=expired["id"], confirmed_by="知知")
                self.assertIn("失效", reply)
                manager.create.assert_not_awaited()
            # Source revision drift invalidates the later evidence, so expiry
            # retracts instead of mutating the original active status.
            days[second.day] = replace(second, selection_revision="c" * 64)
            self.assertEqual(len(service.search_facts(candidates_only=True)), 1)

    async def test_every_plan_reader_and_dashboard_use_same_overlay(self):
        d = sample("离开南京前喝杯茶。", "我已经离开南京了。")
        scope = scope_for_card(fact(d), d, [])
        b = plan(d.messages[0].text, scope)
        events = events_for_day(d)
        clerk = SimpleNamespace(plan_location_state=lambda bucket, as_of=None: plan_state(bucket, events, as_of=as_of))
        manager = SimpleNamespace(list_all=AsyncMock(return_value=[b]))
        with (
            patch.object(rt, "daily_continuity", SimpleNamespace(clerk=clerk)),
            patch.object(rt, "bucket_mgr", manager),
            patch.object(rt, "config", {}),
            patch.object(rt, "logger", MagicMock()),
        ):
            self.assertEqual(await surface_plans(5000), "没有计划。")
            for layout in (False, True):
                text = await surface_startup(
                    [b],
                    max_results=4,
                    hard_tokens=5000,
                    soft_tokens=3000,
                    reference_time=datetime(2026, 8, 22, tzinfo=timezone.utc),
                    clerk_layout=layout,
                )
                self.assertNotIn("喝杯茶", text)

            class Registry:
                def __init__(self):
                    self.routes = {}

                def custom_route(self, path, methods):
                    def decorate(fn):
                        self.routes[path] = fn
                        return fn

                    return decorate

            registry = Registry()
            web_plans.register(registry)
            with (
                patch.object(web_plans.sh, "_require_auth", return_value=None),
                patch.object(web_plans.sh, "bucket_mgr", manager),
            ):
                import json

                response = await registry.routes["/api/plans"](SimpleNamespace())
                data = json.loads(response.body)
                self.assertEqual(data["active"], [])
                self.assertEqual(len(data["expired"]), 1)
                self.assertEqual(data["expired"][0]["status"], "active")
                self.assertEqual(data["expired"][0]["effective_status"], "expired")
        self.assertEqual(b["metadata"]["status"], "active")


if __name__ == "__main__":
    unittest.main()
