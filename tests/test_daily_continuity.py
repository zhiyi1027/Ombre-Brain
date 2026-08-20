from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from daily_continuity import (  # noqa: E402
    DailyContinuityError,
    DailyContinuityService,
    logical_day,
)
from tools.breath.startup import surface_startup  # noqa: E402
from web import _shared as web_shared  # noqa: E402
from web import daily_continuity as daily_web  # noqa: E402


class FakeBuckets:
    def __init__(self, buckets=None):
        self.buckets = list(buckets or [])

    async def list_all(self, include_archive=False):
        assert include_archive is False
        return list(self.buckets)


class FakeDehydrator:
    model = "deepseek-chat"

    def __init__(self, response=None):
        self.response = response or {
            "skip": False,
            "events": [
                {
                    "text": "知知和我确认了日印象的时间归属。",
                    "source_ids": ["note:cc:2026-08-20"],
                }
            ],
            "open_loops": [
                {
                    "text": "还要把 CC 上传器接入换窗流程。",
                    "source_ids": ["note:cc:2026-08-20"],
                }
            ],
            "impressions": [],
        }
        self.calls = []

    async def _chat(self, system, user, **kwargs):
        self.calls.append((system, json.loads(user), kwargs))
        return json.dumps(self.response, ensure_ascii=False)


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class JsonRequest:
    cookies = {}

    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return self._body


def make_service(tmp_path, *, dehydrator=None, buckets=None):
    return DailyContinuityService(
        {
            "buckets_dir": str(tmp_path),
            "daily_continuity": {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "cutoff_hour": 4,
                "poll_seconds": 30,
            },
        },
        bucket_mgr=FakeBuckets(buckets),
        dehydrator=dehydrator or FakeDehydrator(),
    )


def note_payload(content, *, day="2026-08-20", digest=""):
    value = {
        "note_id": f"cc-daily-note:{day}",
        "source_client": "cc",
        "memory_day": day,
        "source_updated_at": "2026-08-20T16:30:00+00:00",
        "content": content,
    }
    if digest:
        value["content_sha256"] = digest
    return value


def test_logical_day_changes_at_four_in_shanghai():
    tz = ZoneInfo("Asia/Shanghai")
    assert logical_day(datetime(2026, 8, 21, 0, 30, tzinfo=tz), tz, 4) == date(2026, 8, 20)
    assert logical_day(datetime(2026, 8, 21, 3, 59, tzinfo=tz), tz, 4) == date(2026, 8, 20)
    assert logical_day(datetime(2026, 8, 21, 4, 0, tzinfo=tz), tz, 4) == date(2026, 8, 21)


def test_ingest_upserts_one_revision_per_client_day(tmp_path):
    service = make_service(tmp_path)
    first_note = "# 2026-08-20 便签（周四）\n\n## 今天聊了什么\n第一版"
    second_note = "# 2026-08-20 便签（周四）\n\n## 今天聊了什么\n第二版"

    created = service.ingest_note(note_payload(first_note))
    unchanged = service.ingest_note(note_payload(first_note))
    updated = service.ingest_note(note_payload(second_note))

    assert created["status"] == "created" and created["dirty"] is True
    assert unchanged["status"] == "unchanged" and unchanged["dirty"] is False
    assert updated["status"] == "updated" and updated["dirty"] is True
    paths = list(service.notes_dir.glob("*.md"))
    assert len(paths) == 1
    assert "第二版" in paths[0].read_text(encoding="utf-8")


def test_ingest_rejects_title_day_or_hash_mismatch(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(DailyContinuityError, match="title date"):
        service.ingest_note(
            note_payload("# 2026-08-19 便签（周三）\n正文", day="2026-08-20")
        )
    with pytest.raises(DailyContinuityError, match="sha256"):
        service.ingest_note(
            note_payload("# 2026-08-20 便签（周四）\n正文", digest="0" * 64)
        )


@pytest.mark.asyncio
async def test_generate_uses_latest_note_and_regenerates_after_revision(tmp_path):
    dehydrator = FakeDehydrator()
    service = make_service(tmp_path, dehydrator=dehydrator)
    first = "# 2026-08-20 便签（周四）\n\n## 换窗交接\n第一版"
    second = "# 2026-08-20 便签（周四）\n\n## 换窗交接\n第二版"
    service.ingest_note(note_payload(first))

    generated = await service.generate_day("2026-08-20")
    current = await service.generate_day("2026-08-20")
    service.ingest_note(note_payload(second))
    regenerated = await service.generate_day("2026-08-20")

    assert generated["status"] == "ready"
    assert current["skipped"] == "current"
    assert regenerated["status"] == "ready"
    assert len(dehydrator.calls) == 2
    assert "第一版" in dehydrator.calls[0][1]["sources"][0]["content"]
    assert "第二版" in dehydrator.calls[1][1]["sources"][0]["content"]
    body = service.read_day("2026-08-20")
    assert "=== 昨日印象 · 2026-08-20 ===" in body
    assert "发生了什么" in body
    assert "还停在哪里" in body


@pytest.mark.asyncio
async def test_late_bucket_regenerates_without_note_revision(tmp_path):
    dehydrator = FakeDehydrator()
    buckets = FakeBuckets()
    service = DailyContinuityService(
        {
            "buckets_dir": str(tmp_path),
            "daily_continuity": {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "cutoff_hour": 4,
            },
        },
        bucket_mgr=buckets,
        dehydrator=dehydrator,
    )
    service.ingest_note(note_payload("# 2026-08-20 便签（周四）\n正文"))
    await service.generate_day("2026-08-20")
    buckets.buckets.append(
        {
            "id": "late-memory",
            "content": "在切日之后才补存的当天记忆",
            "metadata": {
                "type": "dynamic",
                "created": "2026-08-20T12:00:00+00:00",
            },
        }
    )

    regenerated = await service.generate_day("2026-08-20")

    assert regenerated["status"] == "ready"
    assert len(dehydrator.calls) == 2
    assert any(
        source["source_id"] == "memory_bucket:late-memory"
        for source in dehydrator.calls[1][1]["sources"]
    )


@pytest.mark.asyncio
async def test_generation_drops_entries_with_unknown_sources(tmp_path):
    dehydrator = FakeDehydrator(
        {
            "skip": False,
            "events": [{"text": "伪造内容", "source_ids": ["unknown"]}],
            "open_loops": [],
            "impressions": [],
        }
    )
    service = make_service(tmp_path, dehydrator=dehydrator)
    service.ingest_note(note_payload("# 2026-08-20 便签（周四）\n正文"))
    result = await service.generate_day("2026-08-20")
    assert result["status"] == "skipped"
    assert service.read_day("2026-08-20") == ""


@pytest.mark.asyncio
async def test_old_plan_changed_that_day_is_included(tmp_path):
    dehydrator = FakeDehydrator()
    plan = {
        "id": "plan-old",
        "content": "把日印象接进启动流程",
        "metadata": {
            "type": "plan",
            "name": "日印象",
            "status": "resolved",
            "created": "2026-08-01T00:00:00",
            "change_log": [
                {
                    "ts": "2026-08-20T08:00:00+00:00",
                    "action": "status",
                    "from": "active",
                    "to": "resolved",
                }
            ],
        },
    }
    service = make_service(tmp_path, dehydrator=dehydrator, buckets=[plan])
    service.ingest_note(note_payload("# 2026-08-20 便签（周四）\n正文"))
    await service.generate_day("2026-08-20")
    sources = dehydrator.calls[0][1]["sources"]
    plan_source = next(source for source in sources if source["source_id"] == "plan:plan-old")
    assert '"to": "resolved"' in plan_source["content"]


def test_pending_day_waits_until_after_cutoff(tmp_path):
    service = make_service(tmp_path)
    service.ingest_note(note_payload("# 2026-08-20 便签（周四）\n正文"))
    shanghai = ZoneInfo("Asia/Shanghai")
    assert service.pending_days(datetime(2026, 8, 21, 3, 59, tzinfo=shanghai)) == []
    assert service.pending_days(datetime(2026, 8, 21, 4, 1, tzinfo=shanghai)) == [date(2026, 8, 20)]


@pytest.mark.asyncio
async def test_startup_places_daily_impression_after_core_before_recent(monkeypatch):
    import tools._runtime as rt

    monkeypatch.setattr(rt, "decay_engine", SimpleNamespace(calculate_score=lambda _meta: 1.0))
    monkeypatch.setattr(rt, "logger", SimpleNamespace(error=lambda *_args: None))
    reference = datetime(2026, 8, 21, 8, 0)
    buckets = [
        {
            "id": "core",
            "content": "核心正文",
            "metadata": {
                "type": "permanent",
                "created": "2026-08-01T00:00:00",
                "pinned": True,
            },
        },
        {
            "id": "recent",
            "content": "近期正文",
            "metadata": {
                "type": "dynamic",
                "created": "2026-08-21T07:00:00",
                "importance": 8,
            },
        },
    ]
    output = await surface_startup(
        buckets,
        max_results=4,
        hard_tokens=5000,
        soft_tokens=3000,
        reference_time=reference,
        daily_impression="=== 昨日印象 · 2026-08-20 ===\n发生了什么：\n- 昨日正文",
    )
    assert output.index("核心正文") < output.index("昨日正文") < output.index("近期正文")


def _load_sync_module():
    path = ROOT / "scripts" / "sync-daily-note.py"
    spec = importlib.util.spec_from_file_location("sync_daily_note", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_uploader_uses_title_day_and_four_am_cutoff(tmp_path):
    module = _load_sync_module()
    note = tmp_path / ".daily-note"
    note.write_text("# 2026-08-20 便签（周四）\n正文", encoding="utf-8")
    # 2026-08-21 00:30 Asia/Shanghai: still the 2026-08-20 logical day.
    timestamp = datetime(2026, 8, 20, 16, 30, tzinfo=timezone.utc).timestamp()
    note.touch()
    import os
    os.utime(note, (timestamp, timestamp))
    args = argparse.Namespace(
        file=note,
        source_client="cc",
        timezone="Asia/Shanghai",
        cutoff_hour=4,
    )
    payload = module._payload(args)
    assert payload["memory_day"] == "2026-08-20"
    assert payload["note_id"] == "cc-daily-note:2026-08-20"

    os.utime(
        note,
        (
            datetime(2026, 8, 21, 4, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp(),
            datetime(2026, 8, 21, 4, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp(),
        ),
    )
    with pytest.raises(module.PermanentSyncError, match="logical day"):
        module._payload(args)


def test_uploader_bad_old_note_does_not_block_newer_note(monkeypatch):
    module = _load_sync_module()
    pending = {
        "cc-daily-note:2026-08-19": {"memory_day": "2026-08-19"},
        "cc-daily-note:2026-08-20": {"memory_day": "2026-08-20"},
    }

    def fake_post(_url, _token, payload, _timeout):
        if payload["memory_day"] == "2026-08-19":
            raise module.PermanentSyncError("HTTP 400")
        return {"ok": True}

    monkeypatch.setattr(module, "_post", fake_post)
    remaining, delivered, transient, permanent = module._deliver_pending(
        "https://ob.invalid/internal/daily-notes",
        "token",
        pending,
        1.0,
    )

    assert delivered == ["cc-daily-note:2026-08-20"]
    assert list(remaining) == ["cc-daily-note:2026-08-19"]
    assert transient == []
    assert permanent == ["cc-daily-note:2026-08-19: HTTP 400"]


@pytest.mark.asyncio
async def test_private_ingest_route_requires_token_and_never_echoes_content(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)
    monkeypatch.setattr(web_shared, "daily_continuity", service, raising=False)
    monkeypatch.setattr(web_shared, "config", {"daily_continuity": {}}, raising=False)
    monkeypatch.setattr(web_shared, "_is_authenticated", lambda _request: False)
    monkeypatch.setenv("OMBRE_DAILY_NOTE_TOKEN", "secret-token")
    mcp = FakeMCP()
    daily_web.register(mcp)
    handler = mcp.routes[("POST", "/internal/daily-notes")]
    content = "# 2026-08-20 便签（周四）\n绝不能在HTTP响应里回显"
    payload = note_payload(content)

    denied = await handler(JsonRequest(payload))
    accepted = await handler(
        JsonRequest(payload, {"authorization": "Bearer secret-token"})
    )

    assert denied.status_code == 401
    assert accepted.status_code == 200
    response = json.loads(accepted.body)
    assert response["ok"] is True
    assert "绝不能" not in accepted.body.decode("utf-8")
