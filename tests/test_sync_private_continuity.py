"""The timer must never reopen a resolved conflict from an unchanged file."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync-private-continuity.py"
spec = importlib.util.spec_from_file_location("sync_private_continuity", SCRIPT)
sync = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(sync)


def args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        file=tmp_path / "conflict",
        url="https://ob.example/internal/private-continuity/conflict",
        token_file=tmp_path / "token",
        source_client="cc",
        timeout=1.0,
        resolve=False,
        confirm="",
        dry_run=False,
        allow_insecure_http=False,
        if_changed=True,
        state_file=tmp_path / "state.json",
    )


def fake_ob(monkeypatch):
    remote = {"open": False, "revision": 0}
    calls = []

    def request(_url, _token, *, method, payload, timeout):
        calls.append(method)
        if method == "GET":
            return dict(remote)
        assert method == "PUT"
        assert payload["expected_revision"] == remote["revision"]
        content = payload["content"]
        remote.update({
            "open": True,
            "revision": remote["revision"] + 1,
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        })
        return {"ok": True, "status": "updated", "revision": remote["revision"]}

    monkeypatch.setattr(sync, "_token", lambda _path: "secret")
    monkeypatch.setattr(sync, "_request", request)
    return remote, calls


def test_auto_sync_uploads_edits_but_does_not_reopen_resolved_state(tmp_path, monkeypatch):
    config = args(tmp_path)
    remote, calls = fake_ob(monkeypatch)
    config.file.write_text("仍没说开", encoding="utf-8")

    assert sync._sync(config) == 0
    assert remote["revision"] == 1
    assert calls == ["GET", "PUT"]
    assert sync._sync(config) == 0
    assert calls == ["GET", "PUT"]

    remote.update({"open": False, "revision": 0})
    assert sync._sync(config) == 0
    assert calls == ["GET", "PUT"]
    config.file.write_text("本地旧文件又被编辑", encoding="utf-8")
    with pytest.raises(sync.SyncError, match="resolved"):
        sync._sync(config)
    assert calls == ["GET", "PUT", "GET"]

    config.file.unlink()
    assert sync._sync(config) == 0
    assert not config.state_file.exists()
    assert calls == ["GET", "PUT", "GET"]


def test_auto_sync_rejects_a_different_remote_conflict(tmp_path, monkeypatch):
    config = args(tmp_path)
    remote, calls = fake_ob(monkeypatch)
    config.file.write_text("本地正文", encoding="utf-8")
    remote.update({"open": True, "revision": 3, "content_sha256": "other"})

    with pytest.raises(sync.SyncError, match="different open conflict"):
        sync._sync(config)
    assert calls == ["GET"]


def test_auto_sync_adopts_equal_remote_then_rejects_concurrent_edit(tmp_path, monkeypatch):
    config = args(tmp_path)
    remote, calls = fake_ob(monkeypatch)
    config.file.write_text("已有正文", encoding="utf-8")
    remote.update({
        "open": True,
        "revision": 4,
        "content_sha256": hashlib.sha256("已有正文".encode()).hexdigest(),
    })
    assert sync._sync(config) == 0
    assert calls == ["GET"]

    config.file.write_text("修改后的正文", encoding="utf-8")
    assert sync._sync(config) == 0
    assert remote["revision"] == 5

    remote["revision"] = 6
    config.file.write_text("又改了一次", encoding="utf-8")
    with pytest.raises(sync.SyncError, match="changed elsewhere"):
        sync._sync(config)
    assert calls == ["GET", "GET", "PUT", "GET"]
