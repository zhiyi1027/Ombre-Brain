import gzip
import json
from types import SimpleNamespace

import pytest

import raw_archive
from raw_archive import RawArchive, get_archive
from tools import _runtime as rt
from tools.raw import day, search


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


def test_import_is_idempotent_and_rejects_bad_rows(archive):
    assert archive.import_rows(rows()) == {"added": 3, "updated": 0, "skipped": 0}
    bad = [{"msg_uuid": "x", "conv_uuid": "c", "speaker": "路人", "at": "t", "day": "2026-05-18", "text": "a"},
           {"msg_uuid": "y", "conv_uuid": "c", "speaker": "知知", "at": "t", "day": "昨天", "text": "a"},
           "not a row"]
    assert archive.import_rows(rows() + bad) == {"added": 0, "updated": 3, "skipped": 3}
    assert archive.stats()["n"] == 3


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
    assert set(listed["raw_day"]["properties"]) == {"date", "page", "thinking"}
    assert listed["raw_day"]["required"] == ["date"]
    assert set(listed["raw_search"]["properties"]) == {"query", "max_results", "thinking", "speaker"}
    assert listed["raw_search"]["required"] == ["query"]


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
