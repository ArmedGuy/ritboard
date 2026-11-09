"""HTTP app: REST API for the UI, WebSocket push, MCP at /mcp, static UI."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .hub import Hub
from .mcp_tools import build_mcp
from .store import NotFound, Registry

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


@dataclass
class Config:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    token: str | None = None
    allowed_hosts: list[str] = field(default_factory=list)
    dev: bool = False  # reload open browsers when UI files change


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


def app_from_env():
    """Factory for `uvicorn --reload`, which cannot pass arguments."""
    import os

    return create_app(
        Config(
            data_dir=Path(os.environ["RITBORD_DATA"]),
            host=os.environ.get("RITBORD_HOST", "127.0.0.1"),
            port=int(os.environ.get("RITBORD_PORT", "8765")),
            token=os.environ.get("RITBORD_TOKEN") or None,
            allowed_hosts=[h for h in os.environ.get("RITBORD_ALLOWED_HOSTS", "").split(",") if h],
            dev=os.environ.get("RITBORD_DEV") == "1",
        )
    )


# ---- request bodies -------------------------------------------------------
class WorkspaceIn(BaseModel):
    name: str


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


class TokenGate:
    """Optional shared-token check for LAN use. Accepts a Bearer header, a cookie,
    or ?token= (which also sets the cookie so the browser UI keeps working)."""

    def __init__(self, app: ASGIApp, token: str | None):
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.token or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        given = None
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        if not given:
            for part in headers.get("cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "ritbord_token":
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
                            (b"set-cookie", f"ritbord_token={self.token}; Path=/; HttpOnly; SameSite=Strict".encode())
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
    reg = Registry(cfg.data_dir, notify=hub.notify)
    ui_state: dict[str, Any] = {"workspace": None, "board": None}

    if not reg.ids():
        reg.create("Scratch")

    mcp = build_mcp(reg, ui_state)
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
        watcher = asyncio.create_task(_watch_static(hub)) if cfg.dev else None
        async with mcp.session_manager.run():
            yield
        if watcher:
            watcher.cancel()
        reg.close()

    app = FastAPI(title="Ritbord", version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.registry = reg
    app.state.hub = hub
    app.state.ui = ui_state

    @app.exception_handler(NotFound)
    async def _nf(request: Request, exc: NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def _ve(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # ---- workspaces ---------------------------------------------------
    @app.get("/api/info")
    async def info():
        return {"version": __version__, "data_dir": str(reg.data_dir), "mcp_path": "/mcp", "ui": ui_state}

    @app.get("/api/workspaces")
    async def list_ws():
        return reg.list()

    @app.post("/api/workspaces")
    async def create_ws(body: WorkspaceIn):
        return reg.create(body.name).summary()

    @app.patch("/api/workspaces/{ws}")
    async def patch_ws(ws: str, body: WorkspacePatch):
        st = reg.get(ws)
        if body.name:
            reg.rename(st.id, body.name)
        if body.settings:
            st.update_settings(body.settings)
        return {"workspace": st.summary(), "settings": st.settings()}

    @app.delete("/api/workspaces/{ws}")
    async def delete_ws(ws: str):
        return {"trashed_to": reg.delete(ws)}

    @app.post("/api/ui")
    async def report_ui(body: dict[str, Any] = Body(...)):
        ui_state["workspace"] = body.get("workspace")
        ui_state["board"] = body.get("board")
        return ui_state

    @app.get("/api/w/{ws}/state")
    async def state(ws: str):
        return reg.get(ws).state()

    # ---- findings -----------------------------------------------------
    @app.post("/api/w/{ws}/findings")
    async def add_finding(ws: str, body: FindingCreate):
        return reg.get(ws).add_finding(**body.model_dump())

    @app.patch("/api/w/{ws}/findings/{fid}")
    async def patch_finding(ws: str, fid: str, body: dict[str, Any] = Body(...)):
        allowed = {"title", "content", "kind", "category", "tags", "parent_id", "sort"}
        return reg.get(ws).update_finding(fid, **{k: v for k, v in body.items() if k in allowed})

    @app.delete("/api/w/{ws}/findings/{fid}")
    async def delete_finding(ws: str, fid: str, recursive: bool = False):
        return reg.get(ws).delete_finding(fid, recursive)

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

    return TokenGate(app, cfg.token) if cfg.token else app  # type: ignore[return-value]
