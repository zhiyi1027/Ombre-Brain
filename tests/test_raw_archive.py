import gzip
import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import raw_archive
from raw_archive import get_archive
from tools import _runtime as rt
from tools.raw import browse, day, parse_period, search


def rows():
    base = {"conv_uuid": "c1", "conv_name": "第一天", "day": "2026-05-18"}
    return [
        base | {"msg_uuid": "m1", "speaker": "知知", "at": "2026-05-18T20:00:00+08:00",
                "text": "你觉得是浪费吗", "thinking": ""},
        base | {"msg_uuid": "m2", "speaker": "顾凛", "at": "2026-05-18T20:00:05+08:00",
                "text": "你觉得呢", "thinking": "她在试探"},
        base | {"msg_uuid": "m3", "speaker": "知知", "at": "2026-05-18T20:01:00+08:00",
                "text": "我觉得是我爱你", "thinking": ""},
    ]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(raw_archive, "_instances", {})
    config = {"buckets_dir": str(tmp_path / "vault")}
    rt.init(config=config, logger=SimpleNamespace(warning=lambda *_: None))
    return get_archive(config)


def test_archive_lives_beside_media_and_is_private(archive, tmp_path):
    path = tmp_path / "vault" / "_raw" / "raw_archive.sqlite3"
    assert archive.path == str(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_existing_archive_schema_is_migrated_without_losing_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE raw_messages (msg_uuid TEXT PRIMARY KEY, conv_uuid TEXT NOT NULL, "
            "conv_name TEXT NOT NULL DEFAULT '', speaker TEXT NOT NULL, at TEXT NOT NULL, "
            "day TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', thinking TEXT NOT NULL DEFAULT '', "
            "source TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO raw_messages VALUES (?,?,?,?,?,?,?,?,?)",
            tuple(rows()[0][key] for key in ("msg_uuid", "conv_uuid", "conv_name", "speaker", "at", "day", "text", "thinking")) + ("old",),
        )
    archive = raw_archive.RawArchive(str(path))
    assert archive.day("2026-05-18")[1] == 1
    assert archive.hide_message("m1") == 1
    assert archive.stats()["n"] == 0


def test_import_is_idempotent_and_rejects_bad_rows(archive):
    assert archive.import_rows(rows()) == {"added": 3, "updated": 0, "skipped": 0}
    bad = [{"msg_uuid": "x", "conv_uuid": "c", "speaker": "路人", "at": "t", "day": "2026-05-18", "text": "a"},
           {"msg_uuid": "y", "conv_uuid": "c", "speaker": "知知", "at": "t", "day": "昨天", "text": "a"},
           "not a row"]
    assert archive.import_rows(rows() + bad) == {"added": 0, "updated": 3, "skipped": 3}
    assert archive.stats()["n"] == 3


def test_hide_one_excludes_all_read_paths_and_blocks_both_importers(archive):
    archive.import_rows(rows())
    now = datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert archive.hide_message("m2", now=now) == 1
    assert archive.stats()["n"] == 2
    assert archive.days() == [{"day": "2026-05-18", "n": 2}]
    assert [r["msg_uuid"] for r in archive.rows_for_days(["2026-05-18"])] == ["m1", "m3"]
    assert [r["msg_uuid"] for r in archive.day("2026-05-18")[0]] == ["m1", "m3"]
    assert archive.range_days("2026-05-18", "2026-05-18")[1] == 2
    assert archive.search("你觉得呢") == []
    assert archive.search("我爱你")[0]["before"]["text"] == "你觉得是浪费吗"
    assert archive.import_rows([rows()[1]])["skipped"] == 1
    assert archive.import_rows_v2([rows()[1]])["added"] == 0
    assert archive.deletion_log()[0]["affected_count"] == 1
    assert archive.purge_due(now=now + timedelta(days=29)) == 0
    assert archive.purge_due(now=now + timedelta(days=30)) == 1
    assert archive.deletion_log()[0]["purged_at"] is not None
    assert archive.import_rows([rows()[1]])["skipped"] == 1
    assert archive.stats()["n"] == 2


def test_hide_range_blocks_future_rows_and_fragments(archive):
    archive.import_rows(rows())
    assert archive.hide_range("2026-05-18T20:00:05+08:00", "2026-05-18T20:01:00+08:00") == 2
    assert archive.stats()["n"] == 1
    assert archive.search("我爱你") == []
    assert archive.import_rows(rows())["skipped"] == 2
    first, second = _fragments(msg_uuid="home-msg-9")
    first = first | {"at": "2026-05-18T20:00:05+08:00"}
    second = second | {"at": "2026-05-18T20:00:05+08:00"}
    assert archive.import_rows_v2([first, second])["pending"] == 0
    assert archive.stats()["n"] == 1
    assert archive.deletion_log()[0]["start_at"].startswith("2026-05-18")
    with pytest.raises(ValueError, match="开始时间"):
        archive.hide_range("2026-05-19", "2026-05-18")


def test_range_hides_legacy_offsets_and_blocks_both_reimport_versions(archive):
    offsets = (
        "2026-05-18T12:00:00+00:00",
        "2026-05-18T12:00:00Z",
        "2026-05-18T14:00:00+02:00",
    )
    # Existing archives retain imported timestamp text, including old offsets.
    with sqlite3.connect(archive.path) as conn:
        conn.executemany(
            "INSERT INTO raw_messages "
            "(msg_uuid, conv_uuid, conv_name, speaker, at, day, text, thinking) "
            "VALUES (?, 'c1', '', '知知', ?, '2026-05-18', '原文', '')",
            [(f"legacy-{i}", at) for i, at in enumerate(offsets)],
        )
    first, second = _fragments(msg_uuid="home-msg-33")
    first = first | {"at": offsets[0]}
    second = second | {"at": offsets[0]}
    assert archive.import_rows_v2([first])["pending"] == 1

    assert archive.hide_range("2026-05-18T20:00:00+08:00", "2026-05-18T20:00:00+08:00") == 3
    assert archive.stats()["n"] == 0
    with sqlite3.connect(archive.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_message_parts").fetchone()[0] == 0
    assert archive.import_rows([rows()[0] | {"msg_uuid": "new-utc", "at": offsets[0]}])["skipped"] == 1
    assert archive.import_rows_v2([first, second])["added"] == 0
    assert archive.stats()["n"] == 0


def test_hide_pending_message_by_uuid_removes_fragments_and_blocks_reimport(archive):
    first, second = _fragments(msg_uuid="home-msg-34")
    assert archive.import_rows_v2([first])["pending"] == 1
    assert archive.hide_message("home-msg-34") == 1
    with sqlite3.connect(archive.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_message_parts").fetchone()[0] == 0
    assert archive.import_rows_v2([first, second])["added"] == 0
    assert archive.stats()["n"] == 0


def test_invalid_timestamp_cannot_bypass_range_deletion(archive):
    bad = rows()[0] | {"at": "not-an-iso-time"}
    assert archive.import_rows([bad])["skipped"] == 1
    with pytest.raises(ValueError, match="invalid raw message"):
        archive.import_rows_v2([bad])


def test_whole_day_range_includes_fractional_last_second(archive):
    row = rows()[0] | {"at": "2026-05-18T23:59:59.500000+08:00"}
    archive.import_rows([row])
    assert archive.hide_range("2026-05-18", "2026-05-18") == 1
    assert archive.import_rows([row])["skipped"] == 1


def test_hide_fragment_alias_blocks_canonical_reimport(archive):
    legacy = rows()[0] | {"msg_uuid": "home-msg-7-part-000001"}
    archive.import_rows([legacy])
    archive.hide_message(legacy["msg_uuid"])
    canonical = legacy | {"msg_uuid": "home-msg-7"}
    assert archive.import_rows_v2([canonical])["added"] == 0
    assert archive.stats()["n"] == 0


def test_message_id_is_never_treated_as_a_glob(archive):
    archive.import_rows([rows()[0] | {"msg_uuid": "m*"}, rows()[1] | {"msg_uuid": "mX-part-1"}])
    assert archive.hide_message("m*") == 1
    assert [row["msg_uuid"] for row in archive.day("2026-05-18")[0]] == ["mX-part-1"]


@pytest.mark.asyncio
async def test_browse_calendar_ranges_and_scoped_search(archive):
    archive.import_rows(rows() + [rows()[0] | {"msg_uuid": "later", "day": "2026-05-25", "at": "2026-05-25T20:00:00+08:00"}])
    assert parse_period("2026年5月中旬") == ("2026-05-11", "2026-05-20")
    assert parse_period("最近7天", today=datetime(2026, 5, 25).date()) == ("2026-05-19", "2026-05-25")
    assert "共 3 条" in await browse(date="2026年5月中旬")
    assert "共 1 条" in await browse(date="2026-05-21..2026-05-31")
    assert "2026-05-25" in await browse(date="2026-05-21..2026-05-31", query="浪费")
    assert "没搜到" in await browse(date="2026年5月中旬", query="酸菜")
    assert "page 必须" in await browse(date="2026-05-18", page=-1)
    assert "翻过头" in await browse(date="2026-05-18..2026-05-25", page=100)


def _fragments(text="前半X" + "Y后半", msg_uuid="home-msg-9"):
    base = rows()[0] | {"msg_uuid": msg_uuid, "text": text, "thinking": ""}
    split = len(text) // 2
    digest = raw_archive._content_digest(text, "")
    return [base | {"text": part, "fragment_index": index, "fragment_count": 2,
                    "fragment_digest": digest}
            for index, part in enumerate((text[:split], text[split:]))]


def test_v2_fragments_become_one_searchable_message_only_when_complete(archive):
    first, second = _fragments()
    assert archive.import_rows_v2([first])["pending"] == 1
    assert archive.stats()["n"] == 0
    assert archive.rows_for_days(["2026-05-18"]) == []
    assert archive.import_rows_v2([second])["added"] == 1
    assert archive.stats()["n"] == 1
    assert archive.rows_for_days(["2026-05-18"])[0]["text"] == "前半XY后半"
    assert archive.search("XY")[0]["text"] == "前半XY后半"
    assert archive.import_rows_v2([first, second])["updated"] == 1
    assert archive.stats()["n"] == 1


def test_v2_shorter_revision_clears_legacy_parts(archive):
    first, second = _fragments(msg_uuid="home-msg-7")
    archive.import_rows([first | {"msg_uuid": "home-msg-7", "text": "旧开头"},
                         second | {"msg_uuid": "home-msg-7-part-000002", "text": "旧结尾"}])
    assert archive.stats()["n"] == 2
    archive.import_rows_v2([rows()[0] | {"msg_uuid": "home-msg-7", "text": "新短句"}])
    assert archive.stats()["n"] == 1
    assert archive.rows_for_days(["2026-05-18"])[0]["text"] == "新短句"
    assert archive.search("旧结尾") == []


def test_v2_rejects_inconsistent_fragments_without_exposing_half_message(archive):
    first, second = _fragments()
    archive.import_rows_v2([first])
    with pytest.raises(ValueError, match="digest mismatch"):
        archive.import_rows_v2([second | {"text": "篡改"}])
    assert archive.stats()["n"] == 0


def test_v2_new_first_fragment_discards_an_old_partial_layout(archive):
    first, second = _fragments()
    archive.import_rows_v2([first])
    text = first["text"] + second["text"]
    digest = raw_archive._content_digest(text, "")
    new_parts = [first | {"text": text[:2], "fragment_index": 0, "fragment_count": 3,
                          "fragment_digest": digest},
                 first | {"text": text[2:4], "fragment_index": 1, "fragment_count": 3,
                          "fragment_digest": digest},
                 first | {"text": text[4:], "fragment_index": 2, "fragment_count": 3,
                          "fragment_digest": digest}]
    archive.import_rows_v2(new_parts)
    assert archive.stats()["n"] == 1
    assert archive.rows_for_days(["2026-05-18"])[0]["text"] == text


@pytest.mark.asyncio
async def test_raw_day_pages_by_day_and_marks_itself_as_data(archive):
    archive.import_rows(rows())
    out = await day("2026-05-18")
    assert "[instructions:false]" in out
    assert "对话：第一天" in out
    assert out.index("你觉得是浪费吗") < out.index("我觉得是我爱你")
    assert "她在试探" not in out
    assert "她在试探" in await day("2026-05-18", thinking=True)
    assert "没有原话" in await day("2026-05-19")
    assert "YYYY-MM-DD" in await day("5月18")


@pytest.mark.asyncio
async def test_raw_search_finds_exact_words_with_neighbours(archive):
    archive.import_rows(rows())
    out = await search("我爱你")
    assert "▶" in out and "我觉得是我爱你" in out
    assert "你觉得呢" in out  # 前一句
    assert "没搜到" in await search("酸菜")
    assert "没搜到" in await search("试探")
    assert "试探" in await search("试探", thinking=True)
    only_me = await search("觉得", speaker="顾凛")
    hits = [line for line in only_me.splitlines() if line.startswith(" ▶ ")]
    assert len(hits) == 1 and "顾凛：你觉得呢" in hits[0]
    assert "至少两个字" in await search("爱")


@pytest.mark.asyncio
async def test_search_treats_wildcards_literally(archive):
    archive.import_rows(rows())
    assert "没搜到" in await search("%_")


@pytest.mark.asyncio
async def test_empty_drawer_says_so(archive):
    assert "还是空的" in await day("2026-05-18")


def _client(tmp_path, monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    import web._shared as sh
    from web import raw_import

    monkeypatch.setattr(raw_archive, "_instances", {})
    monkeypatch.setattr(sh, "config", {"buckets_dir": str(tmp_path / "vault")})

    class Routes:
        def __init__(self):
            self.app = Starlette()

        def custom_route(self, path, methods):
            def deco(fn):
                self.app.add_route(path, fn, methods=methods)
                return fn
            return deco

    routes = Routes()
    raw_import.register(routes)
    return TestClient(routes.app)


def _payload():
    return gzip.compress("\n".join(json.dumps(r, ensure_ascii=False) for r in rows()).encode())


def test_import_route_is_closed_without_its_own_token(tmp_path, monkeypatch):
    monkeypatch.delenv("OB_RAW_IMPORT_TOKEN", raising=False)
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/raw/import", content=_payload()).status_code == 404


def test_import_route_checks_token_and_imports(tmp_path, monkeypatch):
    monkeypatch.setenv("OB_RAW_IMPORT_TOKEN", "sesame")
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/raw/import", content=_payload(),
                       headers={"X-Raw-Import-Token": "wrong"}).status_code == 401
    ok = client.post("/api/raw/import", content=_payload(), headers={"X-Raw-Import-Token": "sesame"})
    assert ok.status_code == 200
    assert ok.json()["added"] == 3 and ok.json()["stats"]["n"] == 3
    bad = client.post("/api/raw/import", content=b"not gzip", headers={"X-Raw-Import-Token": "sesame"})
    assert bad.status_code == 400


def test_v2_route_requires_token_and_assembles_across_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("OB_RAW_IMPORT_TOKEN", "sesame")
    client = _client(tmp_path, monkeypatch)
    first, second = _fragments()

    def payload(part):
        return gzip.compress(json.dumps(part, ensure_ascii=False).encode())

    headers = {"X-Raw-Import-Token": "sesame"}
    assert client.post("/api/raw/import-v2", content=payload(first)).status_code == 401
    assert client.post("/api/raw/import", content=payload(first), headers=headers).status_code == 409
    legacy = first | {"msg_uuid": "home-msg-9-part-000002"}
    assert client.post("/api/raw/import", content=payload(legacy), headers=headers).status_code == 409
    one = client.post("/api/raw/import-v2", content=payload(first), headers=headers)
    assert one.status_code == 200 and one.json()["stats"]["n"] == 0
    two = client.post("/api/raw/import-v2", content=payload(second), headers=headers)
    assert two.status_code == 200 and two.json()["stats"]["n"] == 1


def test_v2_route_preserves_unicode_line_separator_inside_original(tmp_path, monkeypatch):
    monkeypatch.setenv("OB_RAW_IMPORT_TOKEN", "sesame")
    client = _client(tmp_path, monkeypatch)
    original = rows()[0] | {"text": "上句\u2028下句"}
    payload = gzip.compress(json.dumps(original, ensure_ascii=False).encode())
    response = client.post("/api/raw/import-v2", content=payload,
                           headers={"X-Raw-Import-Token": "sesame"})
    assert response.status_code == 200
    assert get_archive({"buckets_dir": str(tmp_path / "vault")}).day("2026-05-18")[0][0]["text"] == "上句\u2028下句"


@pytest.mark.asyncio
async def test_raw_tools_are_registered_read_only():
    import server

    listed = {t.name: t.inputSchema for t in await server.mcp_extra.list_tools()}
    assert set(listed["raw_browse"]["properties"]) == {
        "date", "query", "page", "max_results", "thinking", "speaker",
    }
    assert listed["raw_browse"].get("required", []) == []
    assert "raw_day" not in listed and "raw_search" not in listed


def test_delete_source_takes_back_one_import_only(tmp_path, monkeypatch):
    monkeypatch.setenv("OB_RAW_IMPORT_TOKEN", "sesame")
    client = _client(tmp_path, monkeypatch)
    client.post("/api/raw/import", content=_payload(),
                headers={"X-Raw-Import-Token": "sesame", "X-Raw-Source": "keep"})
    assert client.post("/api/raw/delete-source", json={"source": "x"},
                       headers={"X-Raw-Import-Token": "wrong"}).status_code == 401
    assert client.post("/api/raw/delete-source", json={},
                       headers={"X-Raw-Import-Token": "sesame"}).status_code == 400
    gone = client.post("/api/raw/delete-source", json={"source": "other"},
                       headers={"X-Raw-Import-Token": "sesame"})
    assert gone.json()["deleted"] == 0 and gone.json()["stats"]["n"] == 3
    gone = client.post("/api/raw/delete-source", json={"source": "keep"},
                       headers={"X-Raw-Import-Token": "sesame"})
    assert gone.json()["deleted"] == 3 and gone.json()["stats"]["n"] == 0
