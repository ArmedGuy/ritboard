"""The MCP endpoint. One server for all workspaces; every data tool takes `workspace`."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from .store import Registry

INSTRUCTIONS = """\
Ritbord is a big-screen planning board. Use it to post findings, sort them, and lay them out.

Model:
- A workspace is one project. Every data tool takes `workspace` (id or name). Call
  list_workspaces first, or current_workspace to get the one open on screen.
- A finding is an item in the workspace tree (left sidebar). It has a title, a kind, content,
  an optional category and tags, and an optional parent (to make a tree). Kinds:
  md (markdown, tables allowed), text, csv (rendered as a table), html (full HTML document,
  shown in a sandboxed iframe), url (any page shown in an iframe, e.g. a published Claude
  artifact embed URL), image (URL or data URI), folder (a grouping node).
- A board is a tab with a fixed pixel size (e.g. 3840x2160). A finding only shows on a board
  once placed there. Coordinates are exact board pixels, origin top-left. Leave x/y out and
  Ritbord picks a free spot.
Prefer html or md content you write yourself over external URLs; many sites refuse framing.
Read get_board before rearranging so you don't stack cards on top of each other.
"""


class FindingIn(BaseModel):
    title: str
    content: str = ""
    kind: str = Field("md", description="md, text, csv, html, url, image or folder")
    parent_id: str | None = None
    category: str | None = None
    tags: list[str] | None = None


class PlacementIn(BaseModel):
    finding_id: str
    x: int | None = Field(None, description="Left edge in board pixels. Omit with y to auto-place.")
    y: int | None = Field(None, description="Top edge in board pixels.")
    w: int | None = Field(None, description="Width in pixels. Default depends on kind.")
    h: int | None = Field(None, description="Height in pixels.")
    z: int | None = Field(None, description="Stacking order; higher is on top.")


def build_mcp(reg: Registry, ui_state: dict[str, Any]) -> MCPServer:
    mcp = MCPServer("ritbord", title="Ritbord", instructions=INSTRUCTIONS)

    def _overview(ws: str) -> dict[str, Any]:
        st = reg.get(ws)
        placed: dict[str, list[str]] = {}
        for p in st.placements():
            placed.setdefault(p["finding_id"], []).append(p["board_id"])
        boards = st.list_boards()
        counts: dict[str, int] = {}
        for p in st.placements():
            counts[p["board_id"]] = counts.get(p["board_id"], 0) + 1
        return {
            "workspace": st.summary(),
            "settings": st.settings(),
            "boards": [
                {"id": b["id"], "name": b["name"], "width": b["width"], "height": b["height"], "placed": counts.get(b["id"], 0)}
                for b in boards
            ],
            "findings": [
                {
                    "id": f["id"],
                    "title": f["title"],
                    "kind": f["kind"],
                    "category": f["category"],
                    "parent_id": f["parent_id"],
                    "tags": f["tags"],
                    "on_boards": placed.get(f["id"], []),
                }
                for f in st.list_findings(with_content=False)
            ],
        }

    # ---- workspaces -----------------------------------------------------
    @mcp.tool()
    async def list_workspaces() -> dict[str, Any]:
        """List all workspaces (projects) with their ids and counts."""
        return {"workspaces": reg.list(), "on_screen": ui_state.get("workspace")}

    @mcp.tool()
    async def current_workspace() -> dict[str, Any]:
        """The workspace and board currently open on screen, if any browser has reported one."""
        return {"workspace": ui_state.get("workspace"), "board_id": ui_state.get("board")}

    @mcp.tool()
    async def create_workspace(name: str, first_board_name: str = "Main") -> dict[str, Any]:
        """Create a new workspace (project). It starts with one board."""
        st = reg.create(name, first_board_name or None)
        return _overview(st.id)

    @mcp.tool()
    async def update_workspace(
        workspace: str,
        name: str | None = None,
        default_board_width: int | None = None,
        default_board_height: int | None = None,
        grid: int | None = None,
        category_colors: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Rename a workspace or change its defaults. category_colors maps category name to CSS color."""
        st = reg.get(workspace)
        if name:
            reg.rename(st.id, name)
        patch: dict[str, Any] = {}
        if default_board_width:
            patch["board_width"] = default_board_width
        if default_board_height:
            patch["board_height"] = default_board_height
        if grid:
            patch["grid"] = grid
        if category_colors:
            patch["categories"] = category_colors
        if patch:
            st.update_settings(patch)
        return {"workspace": st.summary(), "settings": st.settings()}

    @mcp.tool()
    async def get_overview(workspace: str) -> dict[str, Any]:
        """Boards, categories and the full findings tree (without content) for a workspace."""
        return _overview(workspace)

    # ---- findings -------------------------------------------------------
    @mcp.tool()
    async def list_findings(
        workspace: str,
        category: str | None = None,
        parent_id: str | None = None,
        query: str | None = None,
        include_content: bool = False,
    ) -> dict[str, Any]:
        """List findings. Filter by category, parent (use "" for top level) or a text query."""
        st = reg.get(workspace)
        fs = st.list_findings(with_content=include_content, category=category, parent_id=parent_id, query=query)
        for f in fs:
            f.pop("rendered", None)
        return {"findings": fs}

    @mcp.tool()
    async def get_finding(workspace: str, finding_id: str) -> dict[str, Any]:
        """One finding with its full content and the boards it is placed on."""
        st = reg.get(workspace)
        f = st.get_finding(finding_id)
        f.pop("rendered", None)
        f["on_boards"] = [p for p in st.placements() if p["finding_id"] == finding_id]
        return f

    @mcp.tool()
    async def add_finding(
        workspace: str,
        title: str,
        content: str = "",
        kind: str = "md",
        parent_id: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
        board_id: str | None = None,
        x: int | None = None,
        y: int | None = None,
        w: int | None = None,
        h: int | None = None,
    ) -> dict[str, Any]:
        """Add a finding to the workspace tree. Give board_id to also place it on a board
        (x/y optional; omitted means auto-place). `source` names who posted it."""
        st = reg.get(workspace)
        f = st.add_finding(title, content, kind, parent_id, category, tags, source)
        f.pop("rendered", None)
        if board_id:
            f["placement"] = st.place(board_id, [{"finding_id": f["id"], "x": x, "y": y, "w": w, "h": h}])[0]
        return f

    @mcp.tool()
    async def add_findings(
        workspace: str, findings: list[FindingIn], source: str | None = None, board_id: str | None = None
    ) -> dict[str, Any]:
        """Add several findings at once. With board_id they are also auto-placed on that board."""
        st = reg.get(workspace)
        out = []
        for item in findings:
            f = st.add_finding(item.title, item.content, item.kind, item.parent_id, item.category, item.tags, source)
            out.append({"id": f["id"], "title": f["title"], "kind": f["kind"]})
        if board_id:
            st.place(board_id, [{"finding_id": f["id"]} for f in out])
        return {"added": out}

    @mcp.tool()
    async def update_finding(
        workspace: str,
        finding_id: str,
        title: str | None = None,
        content: str | None = None,
        kind: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """Change a finding. Only given fields change. category "" clears it.
        parent_id "" moves it to the top level."""
        st = reg.get(workspace)
        kw: dict[str, Any] = {"title": title, "content": content, "kind": kind, "tags": tags}
        if category is not None:
            kw["category"] = category
        if parent_id is not None:
            kw["parent_id"] = parent_id
        f = st.update_finding(finding_id, **kw)
        f.pop("rendered", None)
        return f

    @mcp.tool()
    async def delete_finding(workspace: str, finding_id: str, recursive: bool = False) -> dict[str, Any]:
        """Delete a finding from the workspace and every board. Children move up a level
        unless recursive is true, in which case they are deleted too."""
        return reg.get(workspace).delete_finding(finding_id, recursive)

    # ---- boards ---------------------------------------------------------
    @mcp.tool()
    async def create_board(
        workspace: str, name: str, width: int | None = None, height: int | None = None, background: str | None = None
    ) -> dict[str, Any]:
        """Create a board tab. Size defaults to the workspace default (pixels)."""
        return reg.get(workspace).create_board(name, width, height, background)

    @mcp.tool()
    async def update_board(
        workspace: str,
        board_id: str,
        name: str | None = None,
        width: int | None = None,
        height: int | None = None,
        background: str | None = None,
    ) -> dict[str, Any]:
        """Rename or resize a board (pixels). Resizing never moves cards; cards left outside are reported."""
        st = reg.get(workspace)
        kw: dict[str, Any] = {"name": name, "width": width, "height": height}
        if background is not None:
            kw["background"] = background
        st.update_board(board_id, **kw)
        return st.board_view(board_id)

    @mcp.tool()
    async def delete_board(workspace: str, board_id: str) -> dict[str, Any]:
        """Delete a board tab. Findings stay in the workspace tree."""
        reg.get(workspace).delete_board(board_id)
        return {"deleted": board_id}

    @mcp.tool()
    async def get_board(workspace: str, board_id: str) -> dict[str, Any]:
        """A board with every placement (pixel geometry, title, out-of-bounds flag)."""
        return reg.get(workspace).board_view(board_id)

    @mcp.tool()
    async def place_findings(workspace: str, board_id: str, placements: list[PlacementIn]) -> dict[str, Any]:
        """Put findings on a board or move/resize ones already there.
        Fields left out keep their current value; a new card without x/y is auto-placed."""
        st = reg.get(workspace)
        st.place(board_id, [p.model_dump() for p in placements])
        return st.board_view(board_id)

    @mcp.tool()
    async def remove_from_board(workspace: str, board_id: str, finding_ids: list[str]) -> dict[str, Any]:
        """Take findings off a board. They stay in the workspace tree."""
        return {"removed": reg.get(workspace).unplace(board_id, finding_ids)}

    return mcp
