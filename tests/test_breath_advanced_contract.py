"""Keep the advertised advanced breath surface in sync with dispatch."""

import inspect

import pytest


def _params(fn) -> set[str]:
    return set(inspect.signature(fn).parameters)


def test_breath_advanced_accepts_everything_breath_search_does():
    import server

    missing = sorted(_params(server.breath_search) - _params(server.breath_advanced))
    assert not missing, f"breath_advanced is missing search parameters: {missing!r}"


def test_breath_advanced_forwards_everything_dispatch_takes():
    import server
    from tools import breath as t_breath

    advanced = _params(server.breath_advanced)
    # ``startup`` selects the fork's private one-button startup pipeline. It is
    # deliberately owned by the zero-argument ``breath`` wrapper, not exposed
    # through the advanced search surface.
    dispatch = _params(t_breath.dispatch) - {"startup"}
    assert not sorted(dispatch - advanced)

    source = inspect.getsource(server.breath_advanced)
    missing = sorted(param for param in dispatch if f"{param}=" not in source)
    assert not missing, f"breath_advanced does not forward: {missing!r}"


@pytest.mark.asyncio
async def test_quotes_is_advertised_on_both_search_tools():
    import server

    listed = {tool.name: tool.inputSchema for tool in await server.mcp.list_tools()}
    for name in ("breath_search", "breath_advanced"):
        prop = listed[name]["properties"].get("quotes")
        assert prop is not None
        advertised_types = {prop.get("type")}
        advertised_types.update(
            branch.get("type") for branch in prop.get("anyOf", [])
        )
        assert "boolean" in advertised_types
