"""The stdio bridge lists tools offline and forwards calls to a running server."""

import asyncio
import sys

from mcp import Client, StdioServerParameters

from test_live import data, server  # noqa: F401  (fixture)


def _bridge(url):
    return StdioServerParameters(command=sys.executable, args=["-m", "ritboard.cli", "mcp", "--url", url])


def test_bridge_forwards(server):  # noqa: F811
    async def run():
        async with Client(_bridge(server)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert "add_finding" in names
            ws = data(await c.call_tool("create_workspace", {"name": "Bridge"}))
            assert ws["workspace"]["id"] == "bridge"

    asyncio.run(run())


def test_bridge_without_server_works_on_the_files(tmp_path):
    """With the server down, the bridge runs the tools itself, on the folders."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ritboard.cli", "mcp", "--url", "http://127.0.0.1:9", "--data", str(tmp_path / "data"),
              "--workspaces-dir", str(tmp_path / "ws")],
    )

    async def run():
        async with Client(params) as c:
            assert (await c.list_tools()).tools
            ov = data(await c.call_tool("create_workspace", {"name": "Offline"}))
            f = data(await c.call_tool("add_finding", {"workspace": "offline", "title": "Note", "content": "hi",
                                                       "board_id": "Main"}))
            assert f["id"] == "Note.md"
            a = data(await c.call_tool("publish_artifact", {"workspace": "offline", "title": "Page", "html": "<p>x</p>"}))
            assert a["artifact"]["url"] is None and "isn't running" in a["artifact"]["note"]
            return ov["workspace"]["path"]

    root = asyncio.run(run())
    from pathlib import Path

    assert (Path(root) / "Note.md").read_text() == "hi"
    assert "Note.md" in (Path(root) / ".ritboard" / "boards" / "main.yaml").read_text()


def test_bridge_finds_server_via_beacon(server, tmp_path_factory):  # noqa: F811
    import httpx

    data_dir = httpx.get(server + "/api/info").json()["data_dir"]

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "ritboard.cli", "mcp", "--data", data_dir])
        async with Client(params) as c:
            res = data(await c.call_tool("list_workspaces", {}))
            assert res["workspaces"]

    asyncio.run(run())


def test_bridge_lists_the_servers_tools(server):  # noqa: F811
    async def run():
        async with Client(server + "/mcp") as direct:
            want = sorted(t.name for t in (await direct.list_tools()).tools)
        async with Client(_bridge(server)) as c:
            got = sorted(t.name for t in (await c.list_tools()).tools)
        assert got == want

    asyncio.run(run())
