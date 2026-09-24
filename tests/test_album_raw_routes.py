from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import raw_archive
from media_store import MediaStore
from tools import _runtime as rt


class Routes:
    def __init__(self):
        self.app = Starlette()

    def custom_route(self, path, methods):
        def deco(fn):
            self.app.add_route(path, fn, methods=methods)
            return fn
        return deco


@pytest.fixture
def client(tmp_path, monkeypatch):
    import web._shared as sh
    from web import album_raw

    vault = tmp_path / "vault"
    store = MediaStore(str(vault), str(vault / "_media"))
    image = vault / "_media" / "b1" / "p.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\ncontent")

    class Buckets:
        media_store = store

        async def list_all(self, include_archive=False):
            return [
                {"id": "b1", "content": "她在窗边", "metadata": {"created": "2026-09-21T20:00:00", "media": [{"path": "_media/b1/p.png"}]}},
                {"id": "b2", "content": "没图", "metadata": {"created": "2026-09-22T20:00:00"}},
                {"id": "b3", "content": "删了", "metadata": {"deleted_at": "x", "media": [{"path": "_media/b1/p.png"}]}},
            ]

        async def get(self, bucket_id):
            return {"b1": {"metadata": {"media": [{"path": "_media/b1/p.png"}]}}}.get(bucket_id)

    buckets = Buckets()
    config = {"buckets_dir": str(vault)}
    monkeypatch.setattr(raw_archive, "_instances", {})
    monkeypatch.setattr(sh, "config", config)
    monkeypatch.setattr(sh, "bucket_mgr", buckets)
    rt.init(bucket_mgr=buckets, config=config, logger=SimpleNamespace(warning=lambda *_: None))
    state = {"authed": True}
    from starlette.responses import JSONResponse
    monkeypatch.setattr(sh, "_require_auth", lambda _r: None if state["authed"] else JSONResponse({}, status_code=401))
    raw_archive.get_archive(config).import_rows([
        {"msg_uuid": "m1", "conv_uuid": "c", "conv_name": "三", "speaker": "知知", "at": "2026-05-18T09:55:00+08:00",
         "day": "2026-05-18", "text": "我觉得是我爱你", "thinking": ""},
        {"msg_uuid": "m2", "conv_uuid": "c", "conv_name": "三", "speaker": "顾凛", "at": "2026-05-18T09:56:00+08:00",
         "day": "2026-05-18", "text": "那就不是浪费", "thinking": "心跳"},
    ])
    routes = Routes()
    album_raw.register(routes)
    return TestClient(routes.app), state


def test_everything_needs_dashboard_login(client):
    c, state = client
    state["authed"] = False
    for path in ["/api/album", "/api/album/b1/0", "/api/raw/days", "/api/raw/day?date=2026-05-18", "/api/raw/search?q=爱你"]:
        assert c.get(path).status_code == 401


def test_album_lists_only_live_image_buckets_and_serves_images(client):
    c, _ = client
    items = c.get("/api/album").json()["items"]
    assert [i["id"] for i in items] == ["b1"]
    assert items[0]["count"] == 1 and items[0]["content"] == "她在窗边"
    img = c.get("/api/album/b1/0")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert img.content.startswith(b"\x89PNG")
    assert "private" in img.headers["cache-control"]
    assert c.get("/api/album/b1/1").status_code == 404
    assert c.get("/api/album/nope/0").status_code == 404
    assert c.get("/api/album/b1/x").status_code == 400


def test_raw_days_day_and_search(client):
    c, _ = client
    assert c.get("/api/raw/days").json()["days"] == [{"day": "2026-05-18", "n": 2}]
    day = c.get("/api/raw/day?date=2026-05-18").json()
    assert day["total"] == 2 and [r["text"] for r in day["rows"]] == ["我觉得是我爱你", "那就不是浪费"]
    assert c.get("/api/raw/day?date=5-18").status_code == 400
    hits = c.get("/api/raw/search?q=爱你").json()["hits"]
    assert len(hits) == 1 and hits[0]["after"]["text"] == "那就不是浪费"
    assert c.get("/api/raw/search?q=心跳").json()["hits"] == []
    assert len(c.get("/api/raw/search?q=心跳&thinking=1").json()["hits"]) == 1
    assert c.get("/api/raw/search?q=爱").status_code == 400
