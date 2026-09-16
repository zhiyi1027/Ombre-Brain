from __future__ import annotations

import pytest


def _mirror(tmp_path):
    from ledger_mirror import LedgerMirror

    return LedgerMirror(tmp_path / "events.jsonl")


def _append(mirror, count: int, *, body: str = "memory") -> None:
    for index in range(count):
        mirror.append_event(
            event_type="TraceCreated",
            trace_id=f"b{index}",
            trace_kind="dynamic",
            body=body,
        )


def test_cached_latest_seq_does_not_replay_the_ledger(tmp_path, monkeypatch):
    from ledger_mirror import LedgerMirror

    mirror = _mirror(tmp_path)
    _append(mirror, 50)

    def refuse_replay(self):
        raise AssertionError("latest_seq scanned the complete ledger")

    monkeypatch.setattr(LedgerMirror, "iter_events", refuse_replay)

    assert mirror.latest_seq() == 50
    assert (
        mirror.append_event(
            event_type="TraceTouched",
            trace_id="b49",
            trace_kind="dynamic",
        )["seq"]
        == 51
    )


@pytest.mark.parametrize(
    "tail",
    [
        b'{"seq": 99, "broken"',
        b"\x00\x00",
        "坏行".encode(),
        b"{}\n",
        b"[]\n",
        b'{"seq": "bad"}\n',
    ],
)
def test_broken_tail_falls_back_to_previous_event(tmp_path, tail):
    mirror = _mirror(tmp_path)
    _append(mirror, 4)
    with mirror.path.open("ab") as handle:
        handle.write(tail)

    assert mirror.latest_seq() == 4
    assert (
        mirror.append_event(
            event_type="TraceTouched",
            trace_id="b3",
            trace_kind="dynamic",
        )["seq"]
        == 5
    )


def test_large_final_event_is_found(tmp_path):
    mirror = _mirror(tmp_path)
    _append(mirror, 1, body="x" * 20_000)

    assert mirror.latest_seq() == 1


def test_ledger_containing_only_garbage_starts_at_zero(tmp_path):
    mirror = _mirror(tmp_path)
    mirror.path.parent.mkdir(parents=True, exist_ok=True)
    mirror.path.write_bytes("垃圾\n更多垃圾\n".encode())

    assert mirror.latest_seq() == 0


def test_out_of_order_tail_preserves_the_maximum_sequence(tmp_path):
    mirror = _mirror(tmp_path)
    mirror.path.parent.mkdir(parents=True, exist_ok=True)
    mirror.path.write_text('{"seq": 100}\n{"seq": 2}\n', encoding="utf-8")

    assert mirror.latest_seq() == 100
    assert (
        mirror.append_event(
            event_type="TraceTouched",
            trace_id="b100",
            trace_kind="dynamic",
        )["seq"]
        == 101
    )


def test_external_append_during_sequence_allocation_forces_recalculation(tmp_path, monkeypatch):
    mirror = _mirror(tmp_path)
    _append(mirror, 1)
    original_latest_seq = mirror.latest_seq
    injected = False

    def latest_seq_with_race():
        nonlocal injected
        latest = original_latest_seq()
        if not injected:
            injected = True
            with mirror.path.open("a", encoding="utf-8") as handle:
                handle.write('{"seq": 2}\n')
        return latest

    monkeypatch.setattr(mirror, "latest_seq", latest_seq_with_race)

    event = mirror.append_event(
        event_type="TraceTouched",
        trace_id="b2",
        trace_kind="dynamic",
    )

    assert event["seq"] == 3
    assert mirror.latest_seq() == 3


def test_concurrent_append_after_write_invalidates_the_cache(tmp_path, monkeypatch):
    mirror = _mirror(tmp_path)
    _append(mirror, 1)
    original_signature = mirror._file_signature
    signature_calls = 0

    def signature_with_race():
        nonlocal signature_calls
        signature_calls += 1
        if signature_calls == 3:
            with mirror.path.open("a", encoding="utf-8") as handle:
                handle.write('{"seq": 3}\n')
        return original_signature()

    monkeypatch.setattr(mirror, "_file_signature", signature_with_race)

    event = mirror.append_event(
        event_type="TraceTouched",
        trace_id="b1",
        trace_kind="dynamic",
    )

    assert event["seq"] == 2
    assert mirror._latest_seq_cache is None
    assert mirror.latest_seq() == 3


def test_sustained_external_changes_use_bounded_fallback(tmp_path, monkeypatch):
    mirror = _mirror(tmp_path)
    _append(mirror, 1)
    real_signature = mirror._file_signature
    signature_calls = 0

    def always_changing_signature():
        nonlocal signature_calls
        signature_calls += 1
        signature = real_signature()
        if signature is None:
            return None
        return (signature[0], signature[1], signature[2] + signature_calls)

    monkeypatch.setattr(mirror, "_file_signature", always_changing_signature)

    event = mirror.append_event(
        event_type="TraceTouched",
        trace_id="b1",
        trace_kind="dynamic",
    )

    assert event["seq"] == 2
    assert signature_calls < 20
    assert mirror._latest_seq_cache is None
