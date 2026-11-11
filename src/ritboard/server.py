"""HTTP app: REST API for the UI, WebSocket push, MCP at /mcp, static UI."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .hub import Hub
from .mcp_tools import build_mcp
from .store import Conflict, NotFound, Registry, check_workspace_folder
from .watch import watch_workspaces

STATIC = Path(__file__).parent / "static"
ARTIFACT_PATH = re.compile(r"^/w/[^/]+/(a|u)/")  # artifact pages and upload links
# Served artifact pages get a throwaway origin even when opened full-window, so their
# scripts can't call Ritboard's API. allow-same-origin is deliberately absent.
ARTIFACT_CSP = (
    "sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox "
    "allow-modals allow-downloads allow-presentation"
)
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


@dataclass
class Config:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    token: str | None = None
    allowed_hosts: list[str] = field(default_factory=list)
    dev: bool = False  # reload open browsers when UI files change
    # Folders agents may point at with local_path to fill shadow files from this computer.
    # None: the home folder when listening on localhost only, otherwise off.
    file_roots: list[Path] | None = None
    # Where new workspaces go when no folder is given. None: ~/Ritboard.
    workspaces_dir: Path | None = None

    def local_file_roots(self) -> list[Path]:
        if self.file_roots is not None:
            return [Path(p).expanduser() for p in self.file_roots]
        return [Path.home()] if self.host in LOCAL_HOSTS else []


async def _watch_static(hub: Hub) -> None:
    def snapshot() -> dict[str, float]:
        return {str(p): p.stat().st_mtime for p in STATIC.rglob("*") if p.is_file()}

    last = snapshot()
    while True:
        await asyncio.sleep(0.7)
        try:
            now = snapshot()
        except OSError:
            continue
        if now != last:
            last = now
            await hub.broadcast_all({"type": "reload"})


def _write_beacon(cfg: Config) -> Path:
    """Leave the running server's address in the data folder so `ritboard mcp` finds it
    without being told the port."""
    import os

    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "", "::") else cfg.host
    if ":" in host:
        host = f"[{host}]"
    path = Path(cfg.data_dir) / "server.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": f"http://{host}:{cfg.port}", "pid": os.getpid(), "token": cfg.token}))
    return path


def _roots_from_env(value: str | None) -> list[Path] | None:
    import os

    if value is None or value == "":
        return None
    if value.lower() == "none":
        return []
    return [Path(v) for v in value.split(os.pathsep) if v]


def app_from_env():
    """Factory for `uvicorn --reload`, which cannot pass arguments."""
    import os

    return create_app(
        Config(
            data_dir=Path(os.environ["RITBOARD_DATA"]),
            host=os.environ.get("RITBOARD_HOST", "127.0.0.1"),
            port=int(os.environ.get("RITBOARD_PORT", "8765")),
            token=os.environ.get("RITBOARD_TOKEN") or None,
            allowed_hosts=[h for h in os.environ.get("RITBOARD_ALLOWED_HOSTS", "").split(",") if h],
            dev=os.environ.get("RITBOARD_DEV") == "1",
            file_roots=_roots_from_env(os.environ.get("RITBOARD_FILE_ROOTS")),
            workspaces_dir=Path(os.environ["RITBOARD_WORKSPACES"]) if os.environ.get("RITBOARD_WORKSPACES") else None,
        )
    )


# ---- request bodies -------------------------------------------------------
class WorkspaceIn(BaseModel):
    name: str
    path: str | None = None


class WorkspaceOpen(BaseModel):
    path: str
    name: str | None = None


class WorkspacePatch(BaseModel):
    name: str | None = None
    settings: dict[str, Any] | None = None


class FindingCreate(BaseModel):
    title: str
    content: str = ""
    kind: str = "md"
    parent_id: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    source: str | None = None


class BoardCreate(BaseModel):
    name: str
    width: int | None = None
    height: int | None = None
    background: str | None = None


class PlacementItem(BaseModel):
    finding_id: str
    x: int | None = None
    y: int | None = None
    w: int | None = None
    h: int | None = None
    z: int | None = None


RUNTIME_JS = STATIC / "artifact-runtime.js"
_HEAD_OPEN = re.compile(rb"<head\b[^>]*>", re.I)
_DOC_START = re.compile(rb"^\s*(<!doctype[^>]*>)?\s*(<html\b[^>]*>)?", re.I)


def inject_runtime(html: bytes, src: str, stamp: int = 0, pinned: bool = False) -> bytes:
    """Load the Ritboard runtime before any of the page's own scripts. It is told which
    state of the files it was served from, so it can reload when they change. A page
    opened as an older version (pinned) doesn't reload."""
    tag = f'<script src="{src}" data-version="{stamp}"{" data-pinned" if pinned else ""}></script>'.encode()
    m = _HEAD_OPEN.search(html)
    if m:
        return html[: m.end()] + tag + html[m.end():]
    m = _DOC_START.match(html)
    end = m.end() if m else 0
    return html[:end] + tag + html[end:]


class ArtifactCreate(BaseModel):
    title: str
    files: dict[str, str] = {}
    files_b64: dict[str, str] = {}  # binary files, base64
    description: str = ""
    source: str | None = None
    parent_id: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    board_id: str | None = None


class ArtifactUpdate(BaseModel):
    files: dict[str, str] = {}
    files_b64: dict[str, str] = {}
    remove: list[str] | None = None
    title: str | None = None
    description: str | None = None
    source: str | None = None


def _host_only(value: str) -> str:
    """'127.0.0.1:8765' -> '127.0.0.1', '[::1]:80' -> '::1', 'Board.LAN' -> 'board.lan'."""
    v = value.strip().lower()
    if v.startswith("["):
        return v[1:].split("]", 1)[0]
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


class HostGuard:
    """Refuse requests whose Host header isn't one this server is meant to be reached by.

    Without this, a web page could point its own domain at 127.0.0.1 (DNS rebinding) and then
    use the board API from the browser. WebSocket handshakes must also come from a page on an
    allowed host, since browsers don't apply CORS to them."""

    def __init__(self, app: ASGIApp, hosts: set[str]):
        self.app = app
        self.hosts = hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        ok = _host_only(headers.get("host", "")) in self.hosts
        origin = headers.get("origin")
        if ok and scope["type"] == "websocket" and origin:
            from urllib.parse import urlsplit

            ok = (urlsplit(origin).hostname or "").lower() in self.hosts
        if ok:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        body = b'{"detail": "this server does not answer to that host name; see --allowed-host"}'
        await send({"type": "http.response.start", "status": 421, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


class TokenGate:
    """Optional shared-token check for LAN use. Accepts a Bearer header, a cookie,
    or ?token= (which also sets the cookie so the browser UI keeps working)."""

    def __init__(self, app: ASGIApp, token: str | None):
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.token or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        # Artifact pages run in a sandbox with an opaque origin, so their requests carry no
        # cookie. They stay reachable without the token; their long random ids are the key.
        if scope["type"] == "http" and ARTIFACT_PATH.match(scope.get("path", "")):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        given = None
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        if not given:
            for part in headers.get("cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "ritboard_token":
                    given = v
        from_query = False
        if not given:
            from urllib.parse import parse_qs

            q = parse_qs(scope.get("query_string", b"").decode())
            if q.get("token"):
                given, from_query = q["token"][0], True
        if given and hmac.compare_digest(given, self.token):
            if from_query and scope["type"] == "http":
                async def send_with_cookie(msg):
                    if msg["type"] == "http.response.start":
                        msg.setdefault("headers", []).append(
                            (b"set-cookie", f"ritboard_token={self.token}; Path=/; HttpOnly; SameSite=Strict".encode())
                        )
                    await send(msg)

                return await self.app(scope, receive, send_with_cookie)
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        body = json.dumps({"detail": "token required"}).encode()
        await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


def create_app(cfg: Config) -> FastAPI:
    hub = Hub()
    reg = Registry(cfg.data_dir, notify=hub.notify, default_root=cfg.workspaces_dir)
    ui_state: dict[str, Any] = {"workspace": None, "board": None}
    roots = cfg.local_file_roots()

    from .migrate import migrate_sqlite

    for line in migrate_sqlite(reg):
        print(line)
    if not reg.ids():
        reg.create("Scratch")

    shown_host = "127.0.0.1" if cfg.host in ("0.0.0.0", "", "::") else cfg.host
    mcp = build_mcp(reg, ui_state, base_url=f"http://{shown_host}:{cfg.port}", file_roots=roots)
    if cfg.host in LOCAL_HOSTS and not cfg.allowed_hosts:
        security = None  # SDK default: DNS rebinding protection for localhost
    elif cfg.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=cfg.allowed_hosts,
            allowed_origins=[f"http://{h}" for h in cfg.allowed_hosts] + [f"https://{h}" for h in cfg.allowed_hosts],
        )
    else:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
        host=cfg.host,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(watch_workspaces(reg, hub))]
        if cfg.dev:
            tasks.append(asyncio.create_task(_watch_static(hub)))
        beacon = _write_beacon(cfg)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(BaseException):
                    await t
            with contextlib.suppress(OSError):
                beacon.unlink()
            reg.close()

    app = FastAPI(title="Ritboard", version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.registry = reg
    app.state.hub = hub
    app.state.ui = ui_state

    @app.exception_handler(NotFound)
    async def _nf(request: Request, exc: NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def _ve(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(OSError)
    async def _os(request: Request, exc: OSError):
        return JSONResponse({"detail": f"file system: {exc}"}, status_code=409)

    # ---- workspaces ---------------------------------------------------
    @app.get("/api/info")
    async def info():
        return {
            "version": __version__, "data_dir": str(reg.data_dir), "workspaces_dir": str(reg.default_root),
            "mcp_path": "/mcp", "ui": ui_state,
        }

    @app.get("/api/workspaces")
    async def list_ws():
        return reg.list()

    @app.post("/api/workspaces")
    async def create_ws(body: WorkspaceIn):
        path = check_workspace_folder(body.path, roots) if body.path else None
        return reg.create(body.name, path=path).summary()

    @app.post("/api/workspaces/open")
    async def open_ws(body: WorkspaceOpen):
        return reg.open(check_workspace_folder(body.path, roots), body.name).summary()

    @app.patch("/api/workspaces/{ws}")
    async def patch_ws(ws: str, body: WorkspacePatch):
        st = reg.get(ws)
        if body.name:
            reg.rename(st.id, body.name)
        if body.settings:
            st.update_settings(body.settings)
        return {"workspace": st.summary(), "settings": st.settings()}

    @app.delete("/api/workspaces/{ws}")
    async def remove_ws(ws: str):
        return {"removed": ws, "folder_kept": reg.remove(ws)}

    @app.post("/api/ui")
    async def report_ui(body: dict[str, Any] = Body(...)):
        ui_state["workspace"] = body.get("workspace")
        ui_state["board"] = body.get("board")
        return ui_state

    @app.get("/api/w/{ws}/state")
    async def state(ws: str):
        return reg.get(ws).state()

    # ---- findings (ids are paths, so they go in the query) -------------
    @app.post("/api/w/{ws}/findings")
    async def add_finding(ws: str, body: FindingCreate):
        return reg.get(ws).add_finding(**body.model_dump())

    @app.get("/api/w/{ws}/finding")
    async def get_finding(ws: str, id: str):
        return reg.get(ws).get_finding(id)

    @app.patch("/api/w/{ws}/finding")
    async def patch_finding(ws: str, id: str, body: dict[str, Any] = Body(...)):
        allowed = {"title", "content", "kind", "category", "tags", "parent_id", "source"}
        return reg.get(ws).update_finding(id, **{k: v for k, v in body.items() if k in allowed})

    @app.delete("/api/w/{ws}/finding")
    async def delete_finding(ws: str, id: str, recursive: bool = False):
        return reg.get(ws).delete_finding(id, recursive)

    @app.get("/api/w/{ws}/raw")
    async def raw(ws: str, path: str):
        p = reg.get(ws).raw_file(path)
        # Same origin as the board, so nothing in it may run: an SVG opened directly could.
        return FileResponse(p, headers={"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff",
                                        "Cache-Control": "no-cache"})

    # ---- boards -------------------------------------------------------
    @app.post("/api/w/{ws}/boards")
    async def add_board(ws: str, body: BoardCreate):
        return reg.get(ws).create_board(body.name, body.width, body.height, body.background)

    @app.patch("/api/w/{ws}/boards/{bid}")
    async def patch_board(ws: str, bid: str, body: dict[str, Any] = Body(...)):
        allowed = {"name", "width", "height", "background", "sort"}
        return reg.get(ws).update_board(bid, **{k: v for k, v in body.items() if k in allowed})

    @app.delete("/api/w/{ws}/boards/{bid}")
    async def delete_board(ws: str, bid: str):
        reg.get(ws).delete_board(bid)
        return {"deleted": bid}

    @app.get("/api/w/{ws}/boards/{bid}")
    async def board_view(ws: str, bid: str):
        return reg.get(ws).board_view(bid)

    @app.put("/api/w/{ws}/boards/{bid}/placements")
    async def place(ws: str, bid: str, items: list[PlacementItem]):
        return reg.get(ws).place(bid, [i.model_dump() for i in items])

    @app.post("/api/w/{ws}/boards/{bid}/unplace")
    async def unplace(ws: str, bid: str, finding_ids: list[str] = Body(...)):
        return {"removed": reg.get(ws).unplace(bid, finding_ids)}

    # ---- artifacts ----------------------------------------------------
    def _decode(files: dict[str, str], files_b64: dict[str, str]) -> dict[str, bytes]:
        out = {p: c.encode() for p, c in files.items()}
        out.update({p: base64.b64decode(c) for p, c in files_b64.items()})
        return out

    @app.get("/api/w/{ws}/artifacts")
    async def list_artifacts(ws: str):
        return reg.get(ws).list_artifacts()

    @app.get("/api/w/{ws}/artifact")
    async def get_artifact(ws: str, id: str, content: bool = False, version: int | None = None):
        return reg.get(ws).get_artifact(id, content, version)

    @app.post("/api/w/{ws}/artifacts")
    async def create_artifact(ws: str, body: ArtifactCreate):
        st = reg.get(ws)
        a = st.create_artifact(
            body.title, _decode(body.files, body.files_b64), description=body.description, source=body.source,
            parent_id=body.parent_id, category=body.category, tags=body.tags,
        )
        out: dict[str, Any] = {"artifact": a}
        if body.board_id:
            out["placement"] = st.place(body.board_id, [{"finding_id": a["id"]}])[0]
        return out

    @app.post("/api/w/{ws}/artifact/files")
    async def update_artifact(ws: str, id: str, body: ArtifactUpdate):
        return reg.get(ws).update_artifact(
            id, _decode(body.files, body.files_b64), body.remove, title=body.title, description=body.description,
            source=body.source,
        )

    @app.delete("/api/w/{ws}/artifact")
    async def delete_artifact(ws: str, id: str):
        return reg.get(ws).delete_artifact(id)

    @app.get("/w/{ws}/a/{key}")
    async def artifact_root(ws: str, key: str):
        # Relative links inside the artifact need the trailing slash.
        return RedirectResponse(f"/w/{ws}/a/{key}/", status_code=308)

    # The artifact's own database, used by pages through window.claude.use("db").
    # Pages run with an opaque origin, so these answer cross-origin requests.
    DB_CORS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, PUT, PATCH, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
        "Cache-Control": "no-store",
    }

    def db_json(data: Any, status: int = 200) -> Response:
        return JSONResponse(data, status_code=status, headers=DB_CORS)

    def db_call(fn):
        try:
            return fn()
        except NotFound as e:
            return db_json({"detail": str(e)}, 404)
        except Conflict as e:
            return db_json({"detail": str(e)}, 409)
        except ValueError as e:
            return db_json({"detail": str(e)}, 413 if "larger than" in str(e) else 400)

    def art(ws: str, key: str) -> tuple[Any, str]:
        st = reg.get(ws)
        return st, st.artifact_by_key(key.split("@", 1)[0])

    def art_version(key: str) -> int | None:
        """'<key>@v3' is version 3; the plain key is the current version."""
        _, _, v = key.partition("@")
        if not v:
            return None
        if not (v.startswith("v") and v[1:].isdigit()):
            raise NotFound("bad version in the address; use <key>@v3")
        return int(v[1:])

    @app.options("/w/{ws}/a/{key}/_db")
    @app.options("/w/{ws}/a/{key}/_db/doc")
    async def db_preflight(ws: str, key: str):
        return Response(status_code=204, headers=DB_CORS)

    @app.get("/w/{ws}/a/{key}/_db")
    async def db_list(ws: str, key: str, c: str, rev: int = -1):
        def run():
            st, aid = art(ws, key)
            cur = st.db_rev(aid)
            if rev == cur:
                return db_json({"rev": cur, "unchanged": True})
            return db_json({"rev": cur, "docs": st.db_list(aid, c)})

        return db_call(run)

    @app.get("/w/{ws}/a/{key}/_db/doc")
    async def db_get(ws: str, key: str, c: str, id: str):
        def run():
            st, aid = art(ws, key)
            return db_json(st.db_get(aid, c, id))

        return db_call(run)

    @app.put("/w/{ws}/a/{key}/_db/doc")
    async def db_set(ws: str, key: str, c: str, id: str, body: Any = Body(...)):
        def run():
            st, aid = art(ws, key)
            return db_json(st.db_write(aid, c, id, body))

        return db_call(run)

    @app.patch("/w/{ws}/a/{key}/_db/doc")
    async def db_update(ws: str, key: str, c: str, id: str, body: Any = Body(...)):
        def run():
            st, aid = art(ws, key)
            return db_json(st.db_write(aid, c, id, body, merge=True))

        return db_call(run)

    @app.delete("/w/{ws}/a/{key}/_db/doc")
    async def db_delete(ws: str, key: str, c: str, id: str):
        def run():
            st, aid = art(ws, key)
            st.db_delete(aid, c, id)
            return Response(status_code=204, headers=DB_CORS)

        return db_call(run)

    @app.get("/w/{ws}/a/{key}/_events")
    async def artifact_events(ws: str, key: str, request: Request):
        """Server-sent events for an open artifact page: version, db, deleted."""
        st, aid = art(ws, key)
        q = hub.watch_artifact(st.id, aid)

        def stamp() -> int | None:
            try:
                return reg.get(ws).stamp(aid)
            except (NotFound, ValueError):
                return None

        async def stream():
            try:
                hello = json.dumps({"latest": stamp(), "rev": st.db_rev(aid)})
                yield f"event: hello\ndata: {hello}\n\n"
                while True:
                    try:
                        kind = await asyncio.wait_for(q.get(), timeout=20)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ": keepalive\n\n"
                        continue
                    latest = stamp()
                    if latest is None:
                        kind = "deleted"
                    yield f"event: {kind}\ndata: {json.dumps({'latest': latest})}\n\n"
                    if kind == "deleted":
                        return
            finally:
                hub.unwatch_artifact(st.id, aid, q)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/w/{ws}/a/{key}/_ritboard.js")
    async def artifact_runtime(ws: str, key: str):
        return Response(
            RUNTIME_JS.read_bytes(),
            media_type="text/javascript; charset=utf-8",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
        )

    @app.get("/w/{ws}/a/{key}/{path:path}")
    async def artifact_file(ws: str, key: str, path: str):
        csp = {"Content-Security-Policy": ARTIFACT_CSP}
        try:
            st, aid = art(ws, key)
            version = art_version(key)
            data, ctype = st.artifact_file(aid, path, version)
        except (NotFound, ValueError) as e:
            return Response(str(e), status_code=404, media_type="text/plain", headers=csp)
        if ctype.startswith("text/html"):
            data = inject_runtime(data, f"/w/{ws}/a/{key.split('@', 1)[0]}/_ritboard.js", st.stamp(aid), pinned=version is not None)
        headers = {
            **csp,
            "Access-Control-Allow-Origin": "*",  # the sandboxed page fetches its own files from an opaque origin
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-cache",
        }
        return Response(data, media_type=ctype, headers=headers)

    # Upload links for shadow files: the secret in the path is the authorisation.
    @app.put("/w/{ws}/u/{token}/{digest}")
    @app.post("/w/{ws}/u/{token}/{digest}")
    async def upload_shadow(ws: str, token: str, digest: str, request: Request):
        return reg.get(ws).receive_upload(token, digest, await request.body())

    # ---- live updates -------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(sock: WebSocket):
        await hub.connect(sock)
        try:
            while True:
                msg = json.loads(await sock.receive_text())
                if "subscribe" in msg:
                    hub.subscribe(sock, msg["subscribe"])
        except (WebSocketDisconnect, json.JSONDecodeError):
            pass
        finally:
            hub.drop(sock)

    # ---- MCP + UI -----------------------------------------------------
    for route in mcp_app.routes:
        app.router.routes.append(route)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    outer: ASGIApp = TokenGate(app, cfg.token) if cfg.token else app
    hosts = {"localhost", "127.0.0.1", "::1"} | {_host_only(h) for h in cfg.allowed_hosts}
    if cfg.host in LOCAL_HOSTS or cfg.allowed_hosts:
        outer = HostGuard(outer, hosts)
    return outer  # type: ignore[return-value]
