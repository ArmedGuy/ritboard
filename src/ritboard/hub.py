"""Push change notices to open browsers.

Boards get small hints over WebSocket ("workspace X changed") and refetch state.
Artifact pages get their own events (new version, database changed) over a server-sent
event stream, which works from their sandboxed, opaque origin.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket


class Hub:
    def __init__(self) -> None:
        self.clients: dict[WebSocket, str | None] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.artifact_subs: dict[tuple[str, str], set[asyncio.Queue]] = {}

    def watch_artifact(self, workspace: str, aid: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.artifact_subs.setdefault((workspace, aid), set()).add(q)
        return q

    def unwatch_artifact(self, workspace: str, aid: str, q: asyncio.Queue) -> None:
        subs = self.artifact_subs.get((workspace, aid))
        if subs:
            subs.discard(q)
            if not subs:
                self.artifact_subs.pop((workspace, aid), None)

    def _to_artifact_pages(self, workspace: str, kind: str, aid: str) -> None:
        for q in list(self.artifact_subs.get((workspace, aid), ())):
            try:
                q.put_nowait(kind)
            except asyncio.QueueFull:
                pass

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients[ws] = None

    def subscribe(self, ws: WebSocket, workspace: str | None) -> None:
        self.clients[ws] = workspace

    def drop(self, ws: WebSocket) -> None:
        self.clients.pop(ws, None)

    async def broadcast(self, workspace: str, what: str) -> None:
        msg: dict[str, Any] = {"type": "workspaces" if workspace == "*" else "changed", "workspace": workspace, "what": what}
        text = json.dumps(msg)
        dead = []
        for ws, sub in list(self.clients.items()):
            if workspace != "*" and sub != workspace:
                continue
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.drop(ws)

    async def broadcast_all(self, msg: dict[str, Any]) -> None:
        text = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                self.drop(ws)

    def notify(self, workspace: str, what: str) -> None:
        """Called from store code. Safe from the event loop or from a worker thread."""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        if what.startswith("artifact:"):
            _, kind, aid = what.split(":", 2)
            loop.call_soon_threadsafe(self._to_artifact_pages, workspace, kind, aid)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(self.broadcast(workspace, what))
        else:
            asyncio.run_coroutine_threadsafe(self.broadcast(workspace, what), loop)
