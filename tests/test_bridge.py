"""The stdio bridge lists tools offline and forwards calls to a running server."""

import asyncio
import sys

from mcp import Client, StdioServerParameters

from test_live import data, server  # noqa: F401  (fixture)


def _bridge(url):
    return StdioServerParameters(command=sys.executable, args=["-m", "ritbord.cli", "mcp", "--url", url])


def test_bridge_forwards(server):  # noqa: F811
    async def run():
        async with Client(_bridge(server)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert "add_finding" in names
            ws = data(await c.call_tool("create_workspace", {"name": "Bridge"}))
            assert ws["workspace"]["id"] == "bridge"

    asyncio.run(run())


def test_bridge_without_server():
    async def run():
        async with Client(_bridge("http://127.0.0.1:9")) as c:
            assert (await c.list_tools()).tools
            res = await c.call_tool("list_workspaces", {})
            assert res.is_error and "Could not reach Ritbord" in res.content[0].text

    asyncio.run(run())


def test_bridge_finds_server_via_beacon(server, tmp_path_factory):  # noqa: F811
    import httpx

    data_dir = httpx.get(server + "/api/info").json()["data_dir"]

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "ritbord.cli", "mcp", "--data", data_dir])
        async with Client(params) as c:
            res = data(await c.call_tool("list_workspaces", {}))
            assert res["workspaces"]

    asyncio.run(run())
