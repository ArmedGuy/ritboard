"""`ritboard mcp`: a stdio MCP server that forwards to a running Ritboard.

Claude Desktop starts local MCP servers as commands over stdio; Ritboard serves MCP over
HTTP. This bridge sits between them and forwards each call, connecting fresh per call so it
survives server restarts.

The tool list comes from the running server, not from the bridge's own copy of the code.
So when Ritboard is updated and restarted, the new tools show up without restarting the
app that launched the bridge: the bridge notices the change and tells the client the list
changed.

When the server is down, the bridge runs the tools itself, straight on the workspace
folders. Agents keep working; open boards catch up when the server starts again.
"""

from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import quote

from mcp import Client, types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from pathlib import Path

from .mcp_tools import INSTRUCTIONS, build_mcp
from .store import Registry

CHECK_EVERY_S = 10.0


def _endpoint(base_url: str, token: str | None) -> str:
    url = base_url.rstrip("/")
    if not url.endswith("/mcp"):
        url += "/mcp"
    if token:
        url += "?token=" + quote(token)
    return url


def _fingerprint(tools: list[types.Tool]) -> str:
    blob = json.dumps([t.model_dump(mode="json", exclude_none=True) for t in tools], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


async def run_bridge(
    base_url: str | None,
    token: str | None = None,
    locate=None,
    data_dir: Path | None = None,
    workspaces_dir: Path | None = None,
) -> None:
    """Forward to base_url, or when locate is given, ask it for (url, token) on every call
    so a server restarted on another port is still found. With data_dir, calls made while
    the server is down run here, on the files."""

    local = {"mcp": None}

    def local_mcp():
        if local["mcp"] is None:
            reg = Registry(data_dir, default_root=workspaces_dir) if data_dir else None
            local["mcp"] = build_mcp(reg, {}, base_url="", file_roots=[Path.home()])  # type: ignore[arg-type]
        return local["mcp"]

    local_tools = await local_mcp().list_tools()
    state = {"listed": None, "checked": 0.0}

    def where() -> tuple[str, str | None]:
        w, t = locate() if locate else (base_url, token)
        return w, token or t

    async def server_tools() -> list[types.Tool] | None:
        w, t = where()
        try:
            async with Client(_endpoint(w, t)) as client:
                return list((await client.list_tools()).tools)
        except Exception:
            return None

    async def list_tools(ctx, params) -> types.ListToolsResult:
        tools = await server_tools() or local_tools
        state["listed"] = _fingerprint(tools)
        state["checked"] = time.monotonic()
        return types.ListToolsResult(tools=tools)

    async def maybe_announce(ctx) -> None:
        """If the server's tools differ from what the client was given, say so."""
        if time.monotonic() - state["checked"] < CHECK_EVERY_S:
            return
        state["checked"] = time.monotonic()
        tools = await server_tools()
        if tools is not None and state["listed"] is not None and _fingerprint(tools) != state["listed"]:
            try:
                await ctx.session.send_tool_list_changed()
            except Exception:
                pass

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        w, t = where()
        try:
            async with Client(_endpoint(w, t)) as client:
                result = await client.call_tool(params.name, params.arguments or {})
        except Exception as e:  # server down, wrong port, token refused
            if data_dir is None:
                msg = (
                    f"Could not reach Ritboard at {w} ({type(e).__name__}: {e}). "
                    "Start it with `ritboard serve`, then try again."
                )
                return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], is_error=True)
            async with Client(local_mcp()) as client:  # work on the files directly
                return await client.call_tool(params.name, params.arguments or {})
        await maybe_announce(ctx)
        return result

    server = Server("ritboard", title="Ritboard", instructions=INSTRUCTIONS, on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (read, write):
        await server.run(
            read, write, server.create_initialization_options(notification_options=NotificationOptions(tools_changed=True))
        )
