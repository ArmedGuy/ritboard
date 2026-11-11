"""Watch workspace folders and push changes to open boards and artifact pages.

Anything that changes the files (an editor, git, an agent writing directly, the MCP bridge
running without the server) shows up on screen within a moment. Changes inside an
artifact's .data folder only reach that artifact's pages; other changes refresh the board.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

import watchfiles

from .hub import Hub
from .store import NotFound, Registry

log = logging.getLogger("ritboard.watch")


class _Filter(watchfiles.DefaultFilter):
    def __call__(self, change: watchfiles.Change, path: str) -> bool:
        p = path.replace("\\", "/")
        if "/.ritboard/local/" in p or p.endswith("/.ritboard/local") or os.path.basename(p).startswith(".~"):
            return False
        return super().__call__(change, path)


def _classify(st, rels: list[str]) -> tuple[bool, dict[str, set[str]]]:
    """(board needs a refresh, {artifact path: {"version", "db"}})."""
    arts = sorted((p for p, e in st._entries.items() if e.kind == "artifact"), key=len, reverse=True)
    board = False
    events: dict[str, set[str]] = {}
    for rel in rels:
        art = next((a for a in arts if rel.startswith(a + "/")), None)
        if art and rel.startswith(art + "/.data/"):
            events.setdefault(art, set()).add("db")
            continue
        board = True
        if art:
            events.setdefault(art, set()).add("version")
    return board, events


async def _watch_one(reg: Registry, hub: Hub, wid: str, root: Path) -> None:
    poll = os.environ.get("RITBOARD_POLL") == "1"
    async for changes in watchfiles.awatch(
        root, watch_filter=_Filter(), debounce=250, step=50, force_polling=poll or None, ignore_permission_denied=True
    ):
        try:
            st = reg.get(wid)
        except NotFound:
            return
        rels = []
        for _, path in changes:
            try:
                rels.append(Path(path).resolve().relative_to(st.root).as_posix())
            except (ValueError, OSError):
                continue
        if not rels:
            continue
        board, events = _classify(st, rels)
        if board:
            st.mark_dirty()
            st._fresh(force=True)
            board, events = _classify(st, rels)  # new artifacts are known now
            hub.notify(wid, "files")
        for aid, kinds in events.items():
            for kind in sorted(kinds):
                hub.notify(wid, f"artifact:{kind}:{aid}")


async def watch_workspaces(reg: Registry, hub: Hub) -> None:
    """Keep one watcher per known workspace folder; notice workspaces added elsewhere."""
    tasks: dict[str, tuple[Path, asyncio.Task]] = {}
    known: list[str] | None = None
    wake = hub.workspaces_changed = asyncio.Event()
    try:
        while True:
            try:
                stores = {st.id: st for st in reg.open_stores()}
            except Exception:  # a broken workspaces.yaml shouldn't stop the server
                log.exception("reading the workspace list")
                stores = {}
            for wid, (root, task) in list(tasks.items()):
                if wid not in stores or stores[wid].root != root or task.done():
                    task.cancel()
                    del tasks[wid]
            for wid, st in stores.items():
                if wid not in tasks:
                    tasks[wid] = (st.root, asyncio.create_task(_watch_one(reg, hub, wid, st.root)))
            ids = sorted(reg.ids())
            if known is not None and ids != known and not wake.is_set():
                hub.notify("*", "workspaces")  # changed by another process
            known = ids
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), 2)
            wake.clear()
    finally:
        for _, task in tasks.values():
            task.cancel()
        for _, task in tasks.values():
            with contextlib.suppress(BaseException):
                await task
