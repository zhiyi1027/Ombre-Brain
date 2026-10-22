"""Public MCP manifest and merged dispatch, using synthetic data only."""

import pytest


@pytest.mark.asyncio
async def test_manifest_has_current_eleven_tools():
    import server

    tools = (await server.mcp.list_tools()) + (await server.mcp_extra.list_tools())
    names = {tool.name for tool in tools}
    assert names == {
        "breath", "hold", "breath_search", "trace", "plan", "quarrel", "I",
        "letter_write", "letter_read", "media", "raw_browse",
    }
    schemas = {tool.name: tool.inputSchema for tool in tools}
    assert set(schemas["media"]["properties"]) == {"bucket_id", "index", "query"}
    assert schemas["media"].get("required", []) == []


@pytest.mark.asyncio
async def test_breath_search_routes_feel_and_plan_without_advanced(monkeypatch):
    import server

    calls = []

    async def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return "synthetic"

    async def passthrough(awaitable, **_kwargs):
        return await awaitable

    monkeypatch.setattr(server._t_breath, "dispatch", fake_dispatch)
    monkeypatch.setattr(server, "_with_notice", passthrough)
    assert await server.breath_search(domain="plan") == "synthetic"
    assert calls[-1]["domain"] == "plan" and calls[-1]["query"] == ""
    assert await server.breath_search(domain="daily_impression") == "synthetic"
    assert calls[-1]["domain"] == "daily_impression"
    assert await server.breath_search(domain="feel", query="[bucket_id:b1] 感受") == "synthetic"
    assert calls[-1]["max_tokens"] == 2000
    assert "需要 query" in await server.breath_search(domain="feel")


@pytest.mark.asyncio
async def test_media_merged_modes(monkeypatch):
    import server

    async def fake_catalog(query=""):
        return f"catalog:{query}"

    async def fake_read(bucket_id, index):
        return b"\x89PNG\r\n\x1a\n", "png"

    async def passthrough(awaitable, **_kwargs):
        return await awaitable

    monkeypatch.setattr(server._t_media, "catalog", fake_catalog)
    monkeypatch.setattr(server._t_media, "read", fake_read)
    monkeypatch.setattr(server, "_with_notice", passthrough)
    assert await server.media(query="花") == "catalog:花"
    image = await server.media(bucket_id="b1")
    assert image.to_image_content().mimeType == "image/png"
