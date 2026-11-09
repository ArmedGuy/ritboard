"""Push change notices to open browsers over WebSocket.

Messages are small hints ("workspace X changed"); clients refetch state.
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

    def notify(self, workspace: str, what: str) -> None:
        """Called from store code. Safe from the event loop or from a worker thread."""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(self.broadcast(workspace, what))
        else:
            asyncio.run_coroutine_threadsafe(self.broadcast(workspace, what), loop)
