from pathlib import Path
from types import SimpleNamespace

import pytest

from media_store import MediaStore
from tools import _runtime as rt
from tools.media import read


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
