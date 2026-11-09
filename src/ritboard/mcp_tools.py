"""The MCP endpoint. One server for all workspaces; every data tool takes `workspace`."""

from __future__ import annotations

import base64
import functools
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from .store import ARTIFACT_MAX_BYTES, Registry

INSTRUCTIONS = """\
Ritboard is a big-screen planning board. Use it to post findings, sort them, and lay them out.

Model:
- A workspace is one project. Every data tool takes `workspace` (id or name). Call
  list_workspaces first, or current_workspace to get the one open on screen.
- A finding is an item in the workspace tree (left sidebar). It has a title, a kind, content,
  an optional category and tags, and an optional parent (to make a tree). Kinds:
  md (markdown, tables allowed), text, csv (rendered as a table), html (a one-off HTML
  snippet in a sandboxed frame), artifact (a card showing an artifact, see below),
  url (an external page in a frame), image (URL or data URI), folder (a grouping node).
- An artifact is a small website kept in the workspace: index.html plus any scripts, styles,
  data or images, with numbered versions and its own address. Use publish_artifact for any
  page, app, chart or report worth revising; update_artifact makes a new version and every
  card showing it reloads. Relative links between its files work. Pages run sandboxed: no
  access to Ritboard's API, no cookies or localStorage; libraries from CDNs load normally.
  Each artifact has its own small database, reached from the page the same way as in
  claude.ai artifacts: `const db = await window.claude.use("db");` then
  `db.collection("plan").doc(id).set({...})`, `.update()`, `.delete()`, `.get()`,
  `collection.add()`, `collection.get()` and `collection.onSnapshot(snap => snap.docChanges())`.
  Other claude.ai capabilities (window.claude.complete, user, assets) don't exist here.
  Agents read and write the same data with artifact_data.
- Big files: don't put large or binary content in publish_artifact/update_artifact. List
  them in shadow_files (path, sha256, size); the reply gives one PUT link per file. Send each
  with curl -T from your shell. Hashes are checked; files appear as they arrive. Each
  publish replaces the links; upload_links gives them again.
  If you can't reach the server over HTTP but can write files on the computer it runs on
  (a sandboxed session with a folder from that computer), save the file there and give its
  absolute path on that computer as local_path in shadow_files, or call
  fill_from_local_files later. Ritboard reads it from disk and checks the hash.
- Never post claude.ai or claude.site links (Claude artifacts) as url cards; they are refused,
  because claude.ai can't be shown inside another page. Publish the artifact's HTML with
  publish_artifact instead: get the HTML (from your own output, or by reading the artifact)
  and pass it as html=... or as files. To fix an existing claude.ai card, publish the HTML,
  then update_finding that card to kind="artifact", content=<new artifact id>; it keeps its
  place on the boards.
- A board is a tab with a fixed pixel size (e.g. 3840x2160). A finding only shows on a board
  once placed there. Coordinates are exact board pixels, origin top-left. Leave x/y out and
  Ritboard picks a free spot.
Prefer content you write yourself (md, artifacts) over external URLs; many sites refuse framing.
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


class ShadowFile(BaseModel):
    path: str = Field(description="Relative path in the artifact, e.g. data/grid.bin")
    sha256: str = Field(description="SHA-256 of the file, 64 hex characters")
    size: int = Field(description="Size in bytes")
    local_path: str | None = Field(
        None,
        description="Absolute path of the file on the computer running Ritboard (e.g. C:\\Users\\me\\Projects\\x\\grid.bin). "
        "Ritboard reads it there; no upload needed.",
    )


class LocalFile(BaseModel):
    sha256: str = Field(description="SHA-256 of the file, as registered in shadow_files")
    local_path: str = Field(description="Absolute path of the file on the computer running Ritboard")


class ArtifactFile(BaseModel):
    path: str = Field(description="Relative path, e.g. index.html, app.js, data/rows.json, img/logo.png")
    content: str
    encoding: str = Field("text", description="text, or base64 for binary files such as images")


def _decode(files: list[ArtifactFile] | None, html: str | None) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for f in files or []:
        if f.encoding == "base64":
            out[f.path] = base64.b64decode(f.content)
        elif f.encoding == "text":
            out[f.path] = f.content.encode("utf-8")
        else:
            raise ValueError(f"encoding must be text or base64, not {f.encoding!r}")
    if html is not None:
        out["index.html"] = html.encode("utf-8")
    return out


def read_local(path: str, roots: list[Path]) -> bytes:
    """Read a file on this computer for a shadow file. Only inside the allowed folders; the
    caller must also know the file's hash, so this can't be used to read unknown files."""
    if not roots:
        raise ValueError(
            "reading local files is off on this server; start Ritboard with --allow-files <folder>, or use the upload links"
        )
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError(f"local_path must be absolute on the computer running Ritboard: {path}")
    p = p.resolve()
    if not any(p.is_relative_to(r.resolve()) for r in roots):
        raise ValueError(f"{p} is outside the folders Ritboard may read ({', '.join(str(r) for r in roots)})")
    if not p.is_file():
        raise ValueError(f"no such file on the computer running Ritboard: {p}")
    if p.stat().st_size > ARTIFACT_MAX_BYTES:
        raise ValueError(f"{p} is larger than {ARTIFACT_MAX_BYTES} bytes")
    return p.read_bytes()


def build_mcp(
    reg: Registry, ui_state: dict[str, Any], base_url: str = "", file_roots: list[Path] | None = None
) -> MCPServer:
    roots = list(file_roots or [])
    mcp = MCPServer("ritboard", title="Ritboard", instructions=INSTRUCTIONS)

    def tool(fn):
        """Register a tool. Bad input and missing ids come back to the agent as readable errors
        (the SDK hides the text of other exceptions)."""

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except (ValueError, LookupError) as e:
                raise ToolError(str(e)) from e

        return mcp.tool()(wrapper)

    def art_url(ws: str, aid: str, version: int | None = None) -> str:
        return f"{base_url}/w/{ws}/a/{aid}{'@' + str(version) if version else ''}/"

    def art_out(ws: str, a: dict[str, Any], links: bool = True) -> dict[str, Any]:
        a = {k: v for k, v in a.items() if k != "versions"}
        a["url"] = art_url(ws, a["id"])
        a["version_url"] = art_url(ws, a["id"], a["latest"])
        if links and a.get("pending"):
            a["uploads"] = upload_out(ws, reg.get(ws).upload_links(a["id"]))
        return a

    def fill_locals(st, aid: str, items: list[tuple[str, str]]) -> list[dict[str, Any]]:
        """Fill shadow files from local paths: (sha256, local_path) pairs. Errors are reported per file."""
        done = []
        for digest, local in items:
            try:
                r = st.fill_shadow(aid, digest, read_local(local, roots))
                done.append({"local_path": local, "stored": r["stored"]})
            except (ValueError, LookupError, OSError) as e:
                done.append({"local_path": local, "error": str(e)})
        return done

    def upload_out(ws: str, links: dict[str, Any]) -> dict[str, Any]:
        files, seen = [], set()
        for f in links["pending"]:
            url = f"{base_url}/w/{ws}/u/{links['token']}/{f['sha256']}"
            item = {"path": f["path"], "size": f["size"], "put_url": url}
            if f["sha256"] in seen:
                item["note"] = "same content as another waiting file; one PUT fills both"
            else:
                item["curl"] = f'curl -sS -T "{f["path"]}" "{url}"'
                seen.add(f["sha256"])
            files.append(item)
        return {
            "waiting": files,
            "expires_in_minutes": links["expires_in_minutes"],
            "how": "PUT each file's bytes to its put_url (curl -T; in PowerShell use curl.exe). The hash and size "
                   "are checked. The page shows each file as soon as it arrives. These links stop working "
                   "at the next publish of this artifact.",
        }

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
    @tool
    async def list_workspaces() -> dict[str, Any]:
        """List all workspaces (projects) with their ids and counts."""
        return {"workspaces": reg.list(), "on_screen": ui_state.get("workspace")}

    @tool
    async def current_workspace() -> dict[str, Any]:
        """The workspace and board currently open on screen, if any browser has reported one."""
        return {"workspace": ui_state.get("workspace"), "board_id": ui_state.get("board")}

    @tool
    async def create_workspace(name: str, first_board_name: str = "Main") -> dict[str, Any]:
        """Create a new workspace (project). It starts with one board."""
        st = reg.create(name, first_board_name or None)
        return _overview(st.id)

    @tool
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

    @tool
    async def get_overview(workspace: str) -> dict[str, Any]:
        """Boards, categories and the full findings tree (without content) for a workspace."""
        return _overview(workspace)

    # ---- findings -------------------------------------------------------
    @tool
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

    @tool
    async def get_finding(workspace: str, finding_id: str) -> dict[str, Any]:
        """One finding with its full content and the boards it is placed on."""
        st = reg.get(workspace)
        f = st.get_finding(finding_id)
        f.pop("rendered", None)
        f["on_boards"] = [p for p in st.placements() if p["finding_id"] == finding_id]
        return f

    @tool
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
        (x/y optional; omitted means auto-place). `source` names who posted it.
        For a page, app or chart, use publish_artifact instead; claude.ai links are refused."""
        st = reg.get(workspace)
        f = st.add_finding(title, content, kind, parent_id, category, tags, source)
        f.pop("rendered", None)
        if board_id:
            f["placement"] = st.place(board_id, [{"finding_id": f["id"], "x": x, "y": y, "w": w, "h": h}])[0]
        return f

    @tool
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

    @tool
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

    @tool
    async def delete_finding(workspace: str, finding_id: str, recursive: bool = False) -> dict[str, Any]:
        """Delete a finding from the workspace and every board. Children move up a level
        unless recursive is true, in which case they are deleted too."""
        return reg.get(workspace).delete_finding(finding_id, recursive)

    # ---- boards ---------------------------------------------------------
    @tool
    async def create_board(
        workspace: str, name: str, width: int | None = None, height: int | None = None, background: str | None = None
    ) -> dict[str, Any]:
        """Create a board tab. Size defaults to the workspace default (pixels)."""
        return reg.get(workspace).create_board(name, width, height, background)

    @tool
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

    @tool
    async def delete_board(workspace: str, board_id: str) -> dict[str, Any]:
        """Delete a board tab. Findings stay in the workspace tree."""
        reg.get(workspace).delete_board(board_id)
        return {"deleted": board_id}

    @tool
    async def get_board(workspace: str, board_id: str) -> dict[str, Any]:
        """A board with every placement (pixel geometry, title, out-of-bounds flag)."""
        return reg.get(workspace).board_view(board_id)

    @tool
    async def place_findings(workspace: str, board_id: str, placements: list[PlacementIn]) -> dict[str, Any]:
        """Put findings on a board or move/resize ones already there.
        Fields left out keep their current value; a new card without x/y is auto-placed."""
        st = reg.get(workspace)
        st.place(board_id, [p.model_dump() for p in placements])
        return st.board_view(board_id)

    @tool
    async def remove_from_board(workspace: str, board_id: str, finding_ids: list[str]) -> dict[str, Any]:
        """Take findings off a board. They stay in the workspace tree."""
        return {"removed": reg.get(workspace).unplace(board_id, finding_ids)}

    # ---- artifacts ------------------------------------------------------
    @tool
    async def publish_artifact(
        workspace: str,
        title: str,
        html: str | None = None,
        files: list[ArtifactFile] | None = None,
        shadow_files: list[ShadowFile] | None = None,
        entry: str = "index.html",
        description: str = "",
        source: str | None = None,
        add_card: bool = True,
        parent_id: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        board_id: str | None = None,
        x: int | None = None,
        y: int | None = None,
        w: int | None = None,
        h: int | None = None,
    ) -> dict[str, Any]:
        """Publish a new artifact: a page or small site kept in this workspace, version 1.
        Give `html` for a single page (saved as index.html), or `files` for several files.
        For big files (over ~50 KB, or binary) list them in `shadow_files` with path, sha256
        and size instead: the reply has a PUT link for each, to send with curl so the bytes
        don't pass through this conversation.
        By default it also adds a card for it to the tree; give board_id to place that card."""
        st = reg.get(workspace)
        shadows = {s.path: s.model_dump() for s in shadow_files or []}
        a = st.create_artifact(title, _decode(files, html), entry, description, source, shadows=shadows)
        local = fill_locals(st, a["id"], [(s.sha256, s.local_path) for s in shadow_files or [] if s.local_path])
        if local:
            a = st.get_artifact(a["id"])
        out: dict[str, Any] = {"artifact": art_out(st.id, a)}
        if local:
            out["local_files"] = local
        if add_card:
            f = st.add_finding(title, a["id"], "artifact", parent_id, category, tags, source)
            out["finding_id"] = f["id"]
            if board_id:
                out["placement"] = st.place(board_id, [{"finding_id": f["id"], "x": x, "y": y, "w": w, "h": h}])[0]
        return out

    @tool
    async def update_artifact(
        workspace: str,
        artifact_id: str,
        html: str | None = None,
        files: list[ArtifactFile] | None = None,
        shadow_files: list[ShadowFile] | None = None,
        remove: list[str] | None = None,
        note: str = "",
        title: str | None = None,
        description: str | None = None,
        entry: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Make a new version of an artifact. It starts from the latest files; given files are
        added or replaced, `remove` deletes paths. `html` replaces index.html. Every card
        showing the artifact reloads. Title/description changes alone don't add a version.
        `shadow_files` (path, sha256, size) register big files to send afterwards with curl;
        the reply has their PUT links. Each publish replaces the upload links."""
        st = reg.get(workspace)
        shadows = {s.path: s.model_dump() for s in shadow_files or []}
        a = st.update_artifact(
            artifact_id, _decode(files, html), remove, note, title, description, entry, source, shadows=shadows
        )
        local = fill_locals(st, artifact_id, [(s.sha256, s.local_path) for s in shadow_files or [] if s.local_path])
        if local:
            a = st.get_artifact(artifact_id)
        out = art_out(st.id, a)
        if local:
            out["local_files"] = local
        return out

    @tool
    async def get_artifact(
        workspace: str, artifact_id: str, version: int | None = None, include_content: bool = True
    ) -> dict[str, Any]:
        """An artifact with its version history and the files of one version (latest by
        default). Binary files come back base64-encoded."""
        st = reg.get(workspace)
        a = st.get_artifact(artifact_id, version, include_content)
        return {**art_out(st.id, a), "versions": a["versions"]}

    @tool
    async def list_artifacts(workspace: str) -> dict[str, Any]:
        """All artifacts in a workspace: id, title, latest version, file count, size, address."""
        st = reg.get(workspace)
        return {"artifacts": [art_out(st.id, a, links=False) for a in st.list_artifacts()]}

    @tool
    async def fill_from_local_files(workspace: str, artifact_id: str, files: list[LocalFile]) -> dict[str, Any]:
        """Fill waiting shadow files from files on the computer running Ritboard, for agents
        that share the computer but can't reach the server over HTTP (sandboxed sessions that
        can write to a folder on it). Give each file's sha256 and its absolute path there."""
        st = reg.get(workspace)
        done = fill_locals(st, artifact_id, [(f.sha256, f.local_path) for f in files])
        return {"local_files": done, "artifact": art_out(st.id, st.get_artifact(artifact_id))}

    @tool
    async def upload_links(workspace: str, artifact_id: str, renew: bool = False) -> dict[str, Any]:
        """PUT links for an artifact's shadow files that haven't arrived yet. renew=true makes
        new links (needed once they expire; old ones stop working)."""
        st = reg.get(workspace)
        return upload_out(st.id, st.upload_links(artifact_id, rotate=renew))

    @tool
    async def artifact_data(
        workspace: str,
        artifact_id: str,
        action: str,
        collection: str | None = None,
        doc_id: str | None = None,
        data: dict[str, Any] | None = None,
        if_version: int | None = None,
    ) -> dict[str, Any]:
        """Read or write an artifact's own database (what its page sees through
        window.claude.use("db")). action: collections, list, get, set (replace), update (merge;
        {"__delete__": true} removes a field) or delete. Pass if_version from a read to avoid
        overwriting someone's change. Open pages pick up writes within a couple of seconds."""
        st = reg.get(workspace)
        aid = artifact_id
        if action == "collections":
            return {"collections": st.db_collections(aid)}
        if not collection:
            raise ValueError("collection is required")
        if action == "list":
            return {"collection": collection, "docs": st.db_list(aid, collection)}
        if not doc_id:
            raise ValueError("doc_id is required")
        if action == "get":
            return st.db_get(aid, collection, doc_id)
        if action in ("set", "update"):
            if data is None:
                raise ValueError("data is required")
            return st.db_write(aid, collection, doc_id, data, merge=action == "update", if_version=if_version)
        if action == "delete":
            st.db_delete(aid, collection, doc_id, if_version)
            return {"deleted": f"{collection}/{doc_id}"}
        raise ValueError("action must be collections, list, get, set, update or delete")

    @tool
    async def delete_artifact(workspace: str, artifact_id: str) -> dict[str, Any]:
        """Delete an artifact with all its versions, and the cards that show it."""
        return reg.get(workspace).delete_artifact(artifact_id)

    return mcp
