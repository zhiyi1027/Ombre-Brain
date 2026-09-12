import pytest

from ombrebrain.observability import process_memory as pm


def test_container_limit_comes_from_cgroup_v2(monkeypatch, tmp_path):
    v2 = tmp_path / "memory.max"
    v2.write_text("536870912", encoding="utf-8")
    monkeypatch.setattr(pm, "_CGROUP_V2_MAX", str(v2))
    monkeypatch.setattr(pm, "_CGROUP_V1_MAX", str(tmp_path / "missing"))

    assert pm.memory_limit_bytes() == 536870912


def test_cgroup_v1_is_used_when_v2_is_absent(monkeypatch, tmp_path):
    v1 = tmp_path / "memory.limit_in_bytes"
    v1.write_text("268435456", encoding="utf-8")
    monkeypatch.setattr(pm, "_CGROUP_V2_MAX", str(tmp_path / "missing"))
    monkeypatch.setattr(pm, "_CGROUP_V1_MAX", str(v1))

    assert pm.memory_limit_bytes() == 268435456


@pytest.mark.parametrize("sentinel", ["max", str(1 << 62), str(9223372036854771712)])
def test_unlimited_cgroup_sentinels_are_not_reported(monkeypatch, tmp_path, sentinel):
    path = tmp_path / "limit"
    path.write_text(sentinel, encoding="utf-8")
    monkeypatch.setattr(pm, "_CGROUP_V2_MAX", str(path))
    monkeypatch.setattr(pm, "_CGROUP_V1_MAX", str(tmp_path / "missing"))

    assert pm.memory_limit_bytes() is None


def test_snapshot_degrades_quietly_without_proc(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "_STATUS", str(tmp_path / "missing"))

    report = pm.snapshot()

    assert report["available"] is False
    assert report["reason"]


def test_snapshot_uses_container_limit_for_percentage(monkeypatch, tmp_path):
    status = tmp_path / "status"
    status.write_text("VmRSS:\t262144 kB\n", encoding="utf-8")
    limit = tmp_path / "memory.max"
    limit.write_text("536870912", encoding="utf-8")
    monkeypatch.setattr(pm, "_STATUS", str(status))
    monkeypatch.setattr(pm, "_CGROUP_V2_MAX", str(limit))
    monkeypatch.setattr(pm, "_CGROUP_V1_MAX", str(tmp_path / "missing"))

    report = pm.snapshot()

    assert report["rss_mb"] == 256.0
    assert report["limit_mb"] == 512.0
    assert report["used_percent"] == 50.0


def test_object_distribution_is_opt_in(monkeypatch, tmp_path):
    status = tmp_path / "status"
    status.write_text("VmRSS:\t1024 kB\n", encoding="utf-8")
    monkeypatch.setattr(pm, "_STATUS", str(status))

    assert "top_object_types" not in pm.snapshot()
    assert "top_object_types" in pm.snapshot(include_objects=True)
