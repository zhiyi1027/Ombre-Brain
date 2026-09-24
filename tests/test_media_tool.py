from pathlib import Path
from types import SimpleNamespace

import pytest

from media_store import MediaStore
from tools import _runtime as rt
from tools.media import catalog, read


@pytest.mark.asyncio
async def test_media_catalog_lists_all_non_deleted_image_buckets_without_paths(monkeypatch) -> None:
    class Buckets:
        async def list_all(self, include_archive=False):
            assert include_archive is True
            return [
                {
                    "id": "older",
                    "content": "海边日落合照",
                    "metadata": {
                        "created": "2026-08-01T10:00:00",
                        "tags": ["海边", "日落"],
                        "media": [{"path": "_media/older/one.jpg"}],
                    },
                },
                {
                    "id": "newer",
                    "content": "七夕穿的紫色蝴蝶吊带裙镜子自拍",
                    "metadata": {
                        "created_at": "2026-09-21T12:00:00",
                        "tags": ["七夕", "穿搭"],
                        "media": [
                            {"path": "_media/newer/one.jpg", "title": "紫色蝴蝶吊带裙"},
                            {"path": "_media/newer/two.jpg", "title": "_media/newer/two.jpg"},
                        ],
                    },
                },
                {
                    "id": "deleted",
                    "content": "不应出现",
                    "metadata": {
                        "deleted_at": "2026-09-22T00:00:00",
                        "media": [{"path": "_media/deleted/one.jpg"}],
                    },
                },
                {"id": "text-only", "content": "没有图片", "metadata": {}},
            ]

    rt.init(bucket_mgr=Buckets(), logger=SimpleNamespace(warning=lambda *_: None))
    monkeypatch.setattr("tools.media.core._extract_tags", None)

    result = await catalog()

    assert "2 个桶 / 3 张" in result
    assert result.index("bucket_id:newer") < result.index("bucket_id:older")
    assert "[2张; index:0-1]" in result
    assert "七夕 / 穿搭 / 紫色蝴蝶吊带裙" in result
    assert "deleted" not in result
    assert "text-only" not in result
    assert "_media/" not in result


@pytest.mark.asyncio
async def test_media_catalog_filters_by_all_query_terms_and_handles_empty_result() -> None:
    class Buckets:
        async def list_all(self, include_archive=False):
            assert include_archive is True
            return [{
                "id": "dress-photo",
                "content": "七夕那天的紫色蝴蝶吊带裙镜子自拍",
                "metadata": {
                    "created": "2026-09-21",
                    "domain": ["romance"],
                    "media": [{"path": "_media/dress-photo/one.jpg"}],
                },
            }]

    rt.init(bucket_mgr=Buckets(), logger=SimpleNamespace(warning=lambda *_: None))

    result = await catalog("紫色 吊带")
    missing = await catalog("紫色 海边")

    assert "bucket_id:dress-photo" in result
    assert "关键词“紫色 吊带”" in result
    assert "没有找到" in missing


@pytest.mark.asyncio
async def test_media_read_returns_persisted_image(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = MediaStore(str(vault), str(vault / "_media"))
    image = vault / "_media" / "bucket-a" / "photo.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\ncontent")

    class Buckets:
        media_store = store

        async def get(self, bucket_id):
            assert bucket_id == "bucket-a"
            return {"metadata": {"media": [{"path": "_media/bucket-a/photo.png"}]}}

    rt.init(bucket_mgr=Buckets(), logger=SimpleNamespace(warning=lambda *_: None))

    assert await read("bucket-a", 0) == (b"\x89PNG\r\n\x1a\ncontent", "png")


@pytest.mark.asyncio
async def test_media_read_reports_missing_and_bad_index() -> None:
    class Buckets:
        async def get(self, _bucket_id):
            return {"metadata": {"media": []}}

    rt.init(bucket_mgr=Buckets(), logger=SimpleNamespace(warning=lambda *_: None))

    assert "没有媒体附件" in await read("bucket-a", 0)
    assert "从 0 开始" in await read("bucket-a", -1)


@pytest.mark.asyncio
async def test_mcp_boundary_returns_fastmcp_image(monkeypatch) -> None:
    import server
    from mcp.server.fastmcp import Image

    async def fake_read(**_kwargs):
        return b"\x89PNG\r\n\x1a\ncontent", "png"

    monkeypatch.setattr(server._t_media, "read", fake_read)
    tool = server.mcp_extra._tool_manager.get_tool("media_read")
    listed = next(item for item in await server.mcp_extra.list_tools() if item.name == "media_read")

    assert set(listed.inputSchema["properties"]) == {"bucket_id", "index"}
    assert listed.inputSchema["required"] == ["bucket_id"]
    output = await tool.run({"bucket_id": "bucket-a", "index": 0})
    assert isinstance(output, Image)
    content = output.to_image_content()
    assert content.mimeType == "image/png"


@pytest.mark.asyncio
async def test_mcp_boundary_exposes_media_catalog(monkeypatch) -> None:
    import server

    async def fake_catalog(**_kwargs):
        return "catalog-result"

    monkeypatch.setattr(server._t_media, "catalog", fake_catalog)
    tool = server.mcp_extra._tool_manager.get_tool("media_catalog")
    listed = next(item for item in await server.mcp_extra.list_tools() if item.name == "media_catalog")

    assert set(listed.inputSchema["properties"]) == {"query"}
    assert listed.inputSchema.get("required", []) == []
    assert await tool.run({"query": "紫色"}) == "catalog-result"
