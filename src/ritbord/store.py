"""Storage: one SQLite file per workspace.

A finding exists in the workspace tree whether or not it is on a board.
A placement puts a finding on one board at exact pixel coordinates.
Links belong to a board and connect two findings; they are drawn only
while both ends are placed on that board.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .render import normalize_kind, render

Notify = Callable[[str, str], None]

DEFAULT_SETTINGS: dict[str, Any] = {
    "board_width": 3840,
    "board_height": 2160,
    "grid": 10,
    "categories": {},  # name -> color
}

DEFAULT_SIZES = {
    "md": (420, 300),
    "text": (420, 300),
    "csv": (520, 300),
    "html": (640, 480),
    "url": (640, 480),
    "image": (400, 300),
    "folder": (300, 220),
}

PALETTE = ["#d1495b", "#edae49", "#00798c", "#30638e", "#6a994e", "#8d6a9f", "#c97b63", "#4f6d7a"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES findings(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'md',
    content TEXT NOT NULL DEFAULT '',
    rendered TEXT,
    category TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    source TEXT,
    sort REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS findings_parent ON findings(parent_id);
CREATE TABLE IF NOT EXISTS boards (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    background TEXT,
    sort REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS placements (
    board_id TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    x INTEGER NOT NULL, y INTEGER NOT NULL,
    w INTEGER NOT NULL, h INTEGER NOT NULL,
    z INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (board_id, finding_id)
);
CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    from_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    to_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    color TEXT,
    created_at TEXT NOT NULL
);
"""


class NotFound(LookupError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return prefix + "_" + "".join(secrets.choice(alphabet) for _ in range(8))


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s[:48] or "workspace"


class WorkspaceStore:
    def __init__(self, path: Path, ws_id: str, notify: Notify | None = None):
        self.path = path
        self.id = ws_id
        self._notify = notify or (lambda ws, what: None)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    # ---- plumbing -------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self.db.close()

    def _tx(self):
        store = self

        class Tx:
            def __enter__(self_inner):
                store._lock.acquire()
                store.db.execute("BEGIN IMMEDIATE")
                return store.db

            def __exit__(self_inner, exc_type, exc, tb):
                try:
                    store.db.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    store._lock.release()
                return False

        return Tx()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def changed(self, what: str) -> None:
        self._touch()
        self._notify(self.id, what)

    def _touch(self) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES('updated_at',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (now(),),
            )

    # ---- meta / settings --------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        rows = self._q("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    @property
    def name(self) -> str:
        return self.get_meta("name", self.id) or self.id

    def settings(self) -> dict[str, Any]:
        raw = self.get_meta("settings")
        s = dict(DEFAULT_SETTINGS)
        s["categories"] = {}
        if raw:
            s.update(json.loads(raw))
        return s

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        s = self.settings()
        for k, v in patch.items():
            if k == "categories" and isinstance(v, dict):
                cats = dict(s.get("categories") or {})
                for name, color in v.items():
                    if color is None:
                        cats.pop(name, None)
                    else:
                        cats[name] = color
                s["categories"] = cats
            elif k in ("board_width", "board_height", "grid"):
                s[k] = max(1, int(v))
            else:
                s[k] = v
        self.set_meta("settings", json.dumps(s))
        self.changed("settings")
        return s

    def _ensure_category(self, category: str | None) -> None:
        if not category:
            return
        s = self.settings()
        if category not in s["categories"]:
            s["categories"][category] = PALETTE[len(s["categories"]) % len(PALETTE)]
            self.set_meta("settings", json.dumps(s))

    def summary(self) -> dict[str, Any]:
        counts = self._q(
            "SELECT (SELECT COUNT(*) FROM findings) f, (SELECT COUNT(*) FROM boards) b"
        )[0]
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.get_meta("created_at"),
            "updated_at": self.get_meta("updated_at"),
            "findings": counts["f"],
            "boards": counts["b"],
        }

    # ---- findings -------------------------------------------------------
    @staticmethod
    def _finding(row: sqlite3.Row, with_content: bool = True) -> dict[str, Any]:
        d = dict(row)
        d["tags"] = json.loads(d.get("tags") or "[]")
        if not with_content:
            d.pop("content", None)
            d.pop("rendered", None)
        return d

    def list_findings(
        self,
        *,
        with_content: bool = True,
        category: str | None = None,
        parent_id: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM findings WHERE 1=1"
        args: list[Any] = []
        if category is not None:
            sql += " AND category=?"
            args.append(category)
        if parent_id is not None:
            sql += " AND parent_id IS ?" if parent_id == "" else " AND parent_id=?"
            args.append(None if parent_id == "" else parent_id)
        if query:
            sql += " AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)"
            q = f"%{query}%"
            args += [q, q, q]
        sql += " ORDER BY sort, created_at"
        return [self._finding(r, with_content) for r in self._q(sql, tuple(args))]

    def get_finding(self, fid: str) -> dict[str, Any]:
        rows = self._q("SELECT * FROM findings WHERE id=?", (fid,))
        if not rows:
            raise NotFound(f"finding {fid} not found")
        return self._finding(rows[0])

    def _check_parent(self, fid: str | None, parent_id: str | None) -> None:
        if parent_id is None:
            return
        self.get_finding(parent_id)
        if fid is None:
            return
        cur: str | None = parent_id
        while cur:
            if cur == fid:
                raise ValueError("cannot move a finding inside itself")
            row = self._q("SELECT parent_id FROM findings WHERE id=?", (cur,))
            cur = row[0]["parent_id"] if row else None

    def add_finding(
        self,
        title: str,
        content: str = "",
        kind: str = "md",
        parent_id: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        kind = normalize_kind(kind)
        parent_id = parent_id or None
        self._check_parent(None, parent_id)
        fid = new_id("f")
        t = now()
        with self._tx() as db:
            sort = db.execute(
                "SELECT COALESCE(MAX(sort),0)+1 FROM findings WHERE parent_id IS ?", (parent_id,)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO findings(id,parent_id,title,kind,content,rendered,category,tags,source,sort,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fid,
                    parent_id,
                    title,
                    kind,
                    content or "",
                    render(kind, content or ""),
                    category or None,
                    json.dumps(tags or []),
                    source,
                    sort,
                    t,
                    t,
                ),
            )
        self._ensure_category(category)
        self.changed("findings")
        return self.get_finding(fid)

    _UNSET: Any = object()

    def update_finding(
        self,
        fid: str,
        *,
        title: str | None = None,
        content: str | None = None,
        kind: str | None = None,
        category: Any = _UNSET,
        tags: list[str] | None = None,
        parent_id: Any = _UNSET,
        sort: float | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        cur = self.get_finding(fid)
        sets: dict[str, Any] = {}
        if title is not None:
            sets["title"] = title
        if kind is not None:
            sets["kind"] = normalize_kind(kind)
        if content is not None:
            sets["content"] = content
        if category is not self._UNSET:
            sets["category"] = category or None
        if tags is not None:
            sets["tags"] = json.dumps(tags)
        if source is not None:
            sets["source"] = source
        if sort is not None:
            sets["sort"] = float(sort)
        if parent_id is not self._UNSET:
            parent_id = parent_id or None
            self._check_parent(fid, parent_id)
            sets["parent_id"] = parent_id
            if sort is None:
                sets["sort"] = self._q(
                    "SELECT COALESCE(MAX(sort),0)+1 m FROM findings WHERE parent_id IS ?", (parent_id,)
                )[0]["m"]
        if "kind" in sets or "content" in sets:
            sets["rendered"] = render(sets.get("kind", cur["kind"]), sets.get("content", cur["content"]))
        if not sets:
            return cur
        sets["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in sets)
        with self._tx() as db:
            db.execute(f"UPDATE findings SET {cols} WHERE id=?", (*sets.values(), fid))
        if sets.get("category"):
            self._ensure_category(sets["category"])
        self.changed("findings")
        return self.get_finding(fid)

    def delete_finding(self, fid: str, recursive: bool = False) -> dict[str, Any]:
        cur = self.get_finding(fid)
        with self._tx() as db:
            if not recursive:
                db.execute(
                    "UPDATE findings SET parent_id=? WHERE parent_id=?", (cur["parent_id"], fid)
                )
            n = db.execute(
                "WITH RECURSIVE sub(id) AS (SELECT ? UNION ALL SELECT f.id FROM findings f JOIN sub ON f.parent_id=sub.id)"
                " SELECT COUNT(*) FROM sub",
                (fid,),
            ).fetchone()[0]
            db.execute("DELETE FROM findings WHERE id=?", (fid,))
        self.changed("findings")
        return {"deleted": n}

    # ---- boards ---------------------------------------------------------
    def list_boards(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM boards ORDER BY sort, created_at")]

    def get_board_row(self, bid: str) -> dict[str, Any]:
        rows = self._q("SELECT * FROM boards WHERE id=?", (bid,))
        if not rows:
            # Allow lookup by name, which agents tend to use.
            rows = self._q("SELECT * FROM boards WHERE lower(name)=lower(?)", (bid,))
        if not rows:
            raise NotFound(f"board {bid} not found")
        return dict(rows[0])

    def create_board(
        self, name: str, width: int | None = None, height: int | None = None, background: str | None = None
    ) -> dict[str, Any]:
        s = self.settings()
        bid = new_id("b")
        t = now()
        with self._tx() as db:
            sort = db.execute("SELECT COALESCE(MAX(sort),0)+1 FROM boards").fetchone()[0]
            db.execute(
                "INSERT INTO boards(id,name,width,height,background,sort,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (bid, name, int(width or s["board_width"]), int(height or s["board_height"]), background, sort, t, t),
            )
        self.changed("boards")
        return self.get_board_row(bid)

    def update_board(
        self,
        bid: str,
        *,
        name: str | None = None,
        width: int | None = None,
        height: int | None = None,
        background: Any = _UNSET,
        sort: float | None = None,
    ) -> dict[str, Any]:
        bid = self.get_board_row(bid)["id"]
        sets: dict[str, Any] = {}
        if name is not None:
            sets["name"] = name
        if width is not None:
            sets["width"] = max(100, int(width))
        if height is not None:
            sets["height"] = max(100, int(height))
        if background is not self._UNSET:
            sets["background"] = background or None
        if sort is not None:
            sets["sort"] = float(sort)
        if sets:
            sets["updated_at"] = now()
            cols = ", ".join(f"{k}=?" for k in sets)
            with self._tx() as db:
                db.execute(f"UPDATE boards SET {cols} WHERE id=?", (*sets.values(), bid))
            self.changed("boards")
        return self.get_board_row(bid)

    def delete_board(self, bid: str) -> None:
        bid = self.get_board_row(bid)["id"]
        with self._tx() as db:
            db.execute("DELETE FROM boards WHERE id=?", (bid,))
        self.changed("boards")

    # ---- placements -----------------------------------------------------
    def placements(self, bid: str | None = None) -> list[dict[str, Any]]:
        if bid:
            return [dict(r) for r in self._q("SELECT * FROM placements WHERE board_id=? ORDER BY z", (bid,))]
        return [dict(r) for r in self._q("SELECT * FROM placements ORDER BY board_id, z")]

    def _free_spot(self, board: dict[str, Any], w: int, h: int, taken: list[dict[str, Any]]) -> tuple[int, int]:
        gap, step = 20, 20
        for y in range(40, max(41, board["height"] - h), step):
            for x in range(40, max(41, board["width"] - w), step):
                if all(
                    x + w + gap <= p["x"] or p["x"] + p["w"] + gap <= x or y + h + gap <= p["y"] or p["y"] + p["h"] + gap <= y
                    for p in taken
                ):
                    return x, y
        n = len(taken)
        return 40 + (n * 30) % 400, 40 + (n * 30) % 400

    def place(self, bid: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Upsert placements. Each item: finding_id, and optional x, y, w, h, z.

        Missing x/y on a new placement means "find a free spot".
        Missing fields on an existing placement keep their value.
        """
        board = self.get_board_row(bid)
        bid = board["id"]
        out = []
        with self._tx() as db:
            taken = [dict(r) for r in db.execute("SELECT * FROM placements WHERE board_id=?", (bid,))]
            zmax = max([p["z"] for p in taken], default=0)
            for it in items:
                fid = it["finding_id"]
                f = db.execute("SELECT kind FROM findings WHERE id=?", (fid,)).fetchone()
                if not f:
                    raise NotFound(f"finding {fid} not found")
                cur = next((p for p in taken if p["finding_id"] == fid), None)
                dw, dh = DEFAULT_SIZES.get(f["kind"], (420, 300))
                w = int(it.get("w") or (cur["w"] if cur else dw))
                h = int(it.get("h") or (cur["h"] if cur else dh))
                w, h = max(40, w), max(30, h)
                if it.get("x") is not None and it.get("y") is not None:
                    x, y = int(it["x"]), int(it["y"])
                elif cur:
                    x = int(it["x"]) if it.get("x") is not None else cur["x"]
                    y = int(it["y"]) if it.get("y") is not None else cur["y"]
                else:
                    x, y = self._free_spot(board, w, h, taken)
                if it.get("z") is not None:
                    z = int(it["z"])
                elif cur:
                    z = cur["z"]
                else:
                    zmax += 1
                    z = zmax
                db.execute(
                    "INSERT INTO placements(board_id,finding_id,x,y,w,h,z) VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(board_id,finding_id) DO UPDATE SET x=excluded.x,y=excluded.y,w=excluded.w,h=excluded.h,z=excluded.z",
                    (bid, fid, x, y, w, h, z),
                )
                p = {"board_id": bid, "finding_id": fid, "x": x, "y": y, "w": w, "h": h, "z": z}
                taken = [t for t in taken if t["finding_id"] != fid] + [p]
                out.append(p)
        self.changed("placements")
        return out

    def unplace(self, bid: str, finding_ids: list[str]) -> int:
        bid = self.get_board_row(bid)["id"]
        with self._tx() as db:
            n = 0
            for fid in finding_ids:
                n += db.execute(
                    "DELETE FROM placements WHERE board_id=? AND finding_id=?", (bid, fid)
                ).rowcount
        self.changed("placements")
        return n

    # ---- links ----------------------------------------------------------
    def links(self, bid: str | None = None) -> list[dict[str, Any]]:
        if bid:
            return [dict(r) for r in self._q("SELECT * FROM links WHERE board_id=? ORDER BY created_at", (bid,))]
        return [dict(r) for r in self._q("SELECT * FROM links ORDER BY created_at")]

    def add_link(
        self, bid: str, from_id: str, to_id: str, label: str = "", color: str | None = None
    ) -> dict[str, Any]:
        bid = self.get_board_row(bid)["id"]
        if from_id == to_id:
            raise ValueError("a link needs two different findings")
        self.get_finding(from_id)
        self.get_finding(to_id)
        existing = self._q(
            "SELECT * FROM links WHERE board_id=? AND from_id=? AND to_id=?", (bid, from_id, to_id)
        )
        if existing:
            return self.update_link(existing[0]["id"], label=label, color=color) if (label or color) else dict(existing[0])
        lid = new_id("l")
        with self._tx() as db:
            db.execute(
                "INSERT INTO links(id,board_id,from_id,to_id,label,color,created_at) VALUES(?,?,?,?,?,?,?)",
                (lid, bid, from_id, to_id, label or "", color, now()),
            )
        self.changed("links")
        return dict(self._q("SELECT * FROM links WHERE id=?", (lid,))[0])

    def update_link(self, lid: str, *, label: str | None = None, color: str | None = None) -> dict[str, Any]:
        if not self._q("SELECT 1 FROM links WHERE id=?", (lid,)):
            raise NotFound(f"link {lid} not found")
        with self._tx() as db:
            if label is not None:
                db.execute("UPDATE links SET label=? WHERE id=?", (label, lid))
            if color is not None:
                db.execute("UPDATE links SET color=? WHERE id=?", (color or None, lid))
        self.changed("links")
        return dict(self._q("SELECT * FROM links WHERE id=?", (lid,))[0])

    def delete_link(self, lid: str) -> None:
        with self._tx() as db:
            if not db.execute("DELETE FROM links WHERE id=?", (lid,)).rowcount:
                raise NotFound(f"link {lid} not found")
        self.changed("links")

    # ---- views ----------------------------------------------------------
    def state(self) -> dict[str, Any]:
        """Everything the UI needs for this workspace."""
        return {
            "workspace": self.summary(),
            "settings": self.settings(),
            "findings": self.list_findings(),
            "boards": self.list_boards(),
            "placements": self.placements(),
            "links": self.links(),
        }

    def board_view(self, bid: str) -> dict[str, Any]:
        """A board as an agent sees it: placements with titles, links, bounds problems."""
        board = self.get_board_row(bid)
        titles = {
            r["id"]: (r["title"], r["kind"], r["category"])
            for r in self._q("SELECT id,title,kind,category FROM findings")
        }
        placed = []
        for p in self.placements(board["id"]):
            t = titles.get(p["finding_id"], ("?", "?", None))
            out = p["x"] < 0 or p["y"] < 0 or p["x"] + p["w"] > board["width"] or p["y"] + p["h"] > board["height"]
            placed.append(
                {
                    "finding_id": p["finding_id"],
                    "title": t[0],
                    "kind": t[1],
                    "category": t[2],
                    "x": p["x"],
                    "y": p["y"],
                    "w": p["w"],
                    "h": p["h"],
                    "z": p["z"],
                    "out_of_bounds": out,
                }
            )
        on_board = {p["finding_id"] for p in placed}
        links = [
            {**l, "visible": l["from_id"] in on_board and l["to_id"] in on_board}
            for l in self.links(board["id"])
        ]
        return {
            "board": board,
            "placements": placed,
            "links": links,
            "out_of_bounds": [p["finding_id"] for p in placed if p["out_of_bounds"]],
        }


class Registry:
    """All workspaces under a data directory."""

    def __init__(self, data_dir: Path, notify: Notify | None = None):
        self.data_dir = Path(data_dir)
        self.ws_dir = self.data_dir / "workspaces"
        self.trash_dir = self.data_dir / "trash"
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        self.notify: Notify = notify or (lambda ws, what: None)
        self._stores: dict[str, WorkspaceStore] = {}
        self._lock = threading.RLock()

    def _notify(self, ws: str, what: str) -> None:
        self.notify(ws, what)

    def _open(self, ws_id: str) -> WorkspaceStore:
        with self._lock:
            st = self._stores.get(ws_id)
            if st is None:
                path = self.ws_dir / f"{ws_id}.sqlite"
                if not path.exists():
                    raise NotFound(f"workspace {ws_id} not found")
                st = WorkspaceStore(path, ws_id, self._notify)
                self._stores[ws_id] = st
            return st

    def ids(self) -> list[str]:
        return sorted(p.stem for p in self.ws_dir.glob("*.sqlite"))

    def list(self) -> list[dict[str, Any]]:
        out = [self._open(i).summary() for i in self.ids()]
        return sorted(out, key=lambda w: (w["name"] or "").lower())

    def get(self, ref: str) -> WorkspaceStore:
        """Find a workspace by id, or by name (case-insensitive)."""
        if not ref:
            raise NotFound("workspace is required; call list_workspaces to see them")
        ids = self.ids()
        if ref in ids:
            return self._open(ref)
        low = ref.strip().lower()
        for i in ids:
            st = self._open(i)
            if st.name.lower() == low:
                return st
        if slugify(ref) in ids:
            return self._open(slugify(ref))
        raise NotFound(f"workspace {ref!r} not found; existing: {', '.join(ids) or 'none'}")

    def create(self, name: str, board_name: str | None = "Main") -> WorkspaceStore:
        base = slugify(name)
        ws_id, n = base, 2
        with self._lock:
            while (self.ws_dir / f"{ws_id}.sqlite").exists():
                ws_id = f"{base}-{n}"
                n += 1
            st = WorkspaceStore(self.ws_dir / f"{ws_id}.sqlite", ws_id, self._notify)
            st.set_meta("name", name.strip() or ws_id)
            st.set_meta("created_at", now())
            st.set_meta("settings", json.dumps(DEFAULT_SETTINGS))
            self._stores[ws_id] = st
        if board_name:
            st.create_board(board_name)
        self._notify("*", "workspaces")
        return st

    def rename(self, ref: str, name: str) -> WorkspaceStore:
        st = self.get(ref)
        st.set_meta("name", name)
        st.changed("workspace")
        self._notify("*", "workspaces")
        return st

    def delete(self, ref: str) -> str:
        """Move the workspace file to the trash folder. Nothing is destroyed."""
        st = self.get(ref)
        with self._lock:
            st.close()
            self._stores.pop(st.id, None)
            self.trash_dir.mkdir(parents=True, exist_ok=True)
            dest = self.trash_dir / f"{st.id}-{int(time.time())}.sqlite"
            shutil.move(str(st.path), dest)
            for suffix in ("-wal", "-shm"):
                side = Path(str(st.path) + suffix)
                if side.exists():
                    side.unlink()
        self._notify("*", "workspaces")
        return str(dest)

    def close(self) -> None:
        with self._lock:
            for st in self._stores.values():
                st.close()
            self._stores.clear()
