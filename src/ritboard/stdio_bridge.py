"""`ritboard mcp`: a stdio MCP server that forwards to a running Ritboard.

Claude Desktop starts local MCP servers as commands over stdio; Ritboard serves MCP over
HTTP. This bridge sits between them. It lists the tools without the server, so the client
always sees them, and forwards each call to the server, connecting fresh per call so it
survives server restarts.
"""

from __future__ import annotations

from urllib.parse import quote

from mcp import Client, types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .mcp_tools import INSTRUCTIONS, build_mcp


def _endpoint(base_url: str, token: str | None) -> str:
    url = base_url.rstrip("/")
    if not url.endswith("/mcp"):
        url += "/mcp"
    if token:
        url += "?token=" + quote(token)
    return url


async def run_bridge(base_url: str | None, token: str | None = None, locate=None) -> None:
    """Forward to base_url, or when locate is given, ask it for (url, token) on every call
    so a server restarted on another port is still found."""

    # Tool definitions come from the same code the server uses; no registry needed to list them.
    local = build_mcp(None, {})  # type: ignore[arg-type]
    tools = await local.list_tools()

    async def list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        where, tok = locate() if locate else (base_url, token)
        tok = token or tok
        try:
            async with Client(_endpoint(where, tok)) as client:
                return await client.call_tool(params.name, params.arguments or {})
        except Exception as e:  # server down, wrong port, token refused
            msg = (
                f"Could not reach Ritboard at {where} ({type(e).__name__}: {e}). "
                "Start it with `ritboard serve`, then try again."
            )
            return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], is_error=True)

    server = Server("ritboard", title="Ritboard", instructions=INSTRUCTIONS, on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
