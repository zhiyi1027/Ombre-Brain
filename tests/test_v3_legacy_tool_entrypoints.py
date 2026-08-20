import pytest

from tools import _runtime as rt
import tools.breath as breath_mod
import tools.hold as hold_mod
import tools.trace.core as trace_mod


class _Decay:
    async def ensure_started(self):
        return None


@pytest.mark.asyncio
async def test_breath_dispatch_records_v3_tool_event(monkeypatch) -> None:
    calls = []

    async def fake_surface_default(**_kwargs):
        return "breath result"

    rt.init(config={"surfacing": {}}, decay_engine=_Decay(), mark_op=None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda name, payload: calls.append((name, payload)))
    monkeypatch.setattr(breath_mod, "surface_default", fake_surface_default)

    result = await breath_mod.dispatch(query="", max_results=2)

    assert result == "breath result"
    assert calls[0][0] == "breath"
    assert calls[0][1]["query"] == ""
    assert calls[0][1]["max_results"] == 2


@pytest.mark.asyncio
async def test_hold_dispatch_records_v3_tool_event_without_content_body(monkeypatch) -> None:
    calls = []

    async def fake_store_core(**_kwargs):
        return "hold result"

    rt.init(config={}, decay_engine=_Decay(), mark_op=None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda name, payload: calls.append((name, payload)))
    monkeypatch.setattr(hold_mod, "check_content_size", lambda _content: None)
    monkeypatch.setattr(hold_mod, "store_core", fake_store_core)

    result = await hold_mod.dispatch(content="private memory body", tags="x,y", importance=7)

    assert result == "hold result"
    assert calls[0][0] == "hold"
    assert calls[0][1]["content_length"] == len("private memory body")
    assert "content" not in calls[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("importance", "expected"),
    [
        (None, "普通 hold 的 importance 必填"),
        ("5", "普通 hold 的 importance 必须是 1-10 的整数"),
        (5.5, "普通 hold 的 importance 必须是 1-10 的整数"),
        (True, "普通 hold 的 importance 必须是 1-10 的整数"),
        (0, "普通 hold 的 importance 必须在 1-10 之间"),
        (11, "普通 hold 的 importance 必须在 1-10 之间"),
    ],
)
async def test_hold_rejects_missing_or_invalid_importance_before_write(
    monkeypatch, importance, expected
) -> None:
    writes = []

    async def fake_store_core(**kwargs):
        writes.append(kwargs)
        return "unexpected write"

    monkeypatch.setattr(hold_mod, "store_core", fake_store_core)

    result = await hold_mod.dispatch(content="must not be written", importance=importance)

    assert expected in result
    assert writes == []


@pytest.mark.asyncio
async def test_hold_feel_needs_no_importance(monkeypatch) -> None:
    writes = []

    async def fake_store_feel(**kwargs):
        writes.append(kwargs)
        return "feel result"

    rt.init(config={}, decay_engine=_Decay(), mark_op=None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hold_mod, "check_content_size", lambda _content: None)
    monkeypatch.setattr(hold_mod, "store_feel", fake_store_feel)

    result = await hold_mod.dispatch(
        content="first-person feeling",
        feel=True,
        source_bucket="abc123def456",
    )

    assert result == "feel result"
    assert "importance" not in writes[0]


@pytest.mark.asyncio
async def test_hold_pinned_needs_no_importance(monkeypatch) -> None:
    writes = []

    async def fake_store_pinned(**kwargs):
        writes.append(kwargs)
        return "pinned result"

    async def fake_pinned_quota(_pinned):
        return True

    rt.init(config={}, decay_engine=_Decay(), mark_op=None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hold_mod, "check_content_size", lambda _content: None)
    monkeypatch.setattr(hold_mod, "enforce_pinned_quota", fake_pinned_quota)
    monkeypatch.setattr(hold_mod, "store_pinned", fake_store_pinned)

    result = await hold_mod.dispatch(
        content="permanent rule",
        pinned=True,
    )

    assert result == "pinned result"
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_trace_core_records_v3_tool_event_without_content_body(monkeypatch) -> None:
    calls = []

    rt.init(config={}, mark_op=None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda name, payload: calls.append((name, payload)))

    result = await trace_mod.trace_core(bucket_id="", content="private replacement", delete=True)

    assert "bucket_id" in result
    assert calls[0][0] == "trace"
    assert calls[0][1]["delete"] is True
    assert calls[0][1]["content_length"] == len("private replacement")
    assert "content" not in calls[0][1]


@pytest.mark.asyncio
async def test_trace_core_records_v3_tool_event_without_patch_bodies(monkeypatch) -> None:
    calls = []

    rt.init(config={}, mark_op=None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda name, payload: calls.append((name, payload)))

    result = await trace_mod.trace_core(
        bucket_id="",
        old_str="private old fragment",
        new_str="private new fragment",
    )

    assert "bucket_id" in result
    assert calls[0][0] == "trace"
    assert calls[0][1]["content_length"] == 0
    assert calls[0][1]["old_str_length"] == len("private old fragment")
    assert calls[0][1]["new_str_length"] == len("private new fragment")
    assert "content" not in calls[0][1]
    assert "old_str" not in calls[0][1]
    assert "new_str" not in calls[0][1]
