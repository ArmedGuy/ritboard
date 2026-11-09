"""Storage: one SQLite file per workspace.

A finding exists in the workspace tree whether or not it is on a board.
A placement puts a finding on one board at exact pixel coordinates.
"""

from __future__ import annotations

import base64
import json
import mimetypes
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
    "artifact": (800, 600),
}

ARTIFACT_MAX_BYTES = 16 * 1024 * 1024  # per version, all files together

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
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    entry TEXT NOT NULL DEFAULT 'index.html',
    latest INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_versions (
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    source TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (artifact_id, version)
);
CREATE TABLE IF NOT EXISTS artifact_files (
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    path TEXT NOT NULL,
    content BLOB NOT NULL,
    content_type TEXT NOT NULL,
    PRIMARY KEY (artifact_id, version, path)
);
"""


class NotFound(LookupError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str, n: int = 8) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return prefix + "_" + "".join(secrets.choice(alphabet) for _ in range(n))


def clean_path(path: str) -> str:
    """A relative file path inside an artifact: forward slashes, no '..', no leading '/'."""
    p = (path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    parts = p.split("/")
    if not p or p.startswith("/") or len(p) > 240 or any(s in ("", ".", "..") for s in parts):
        raise ValueError(f"bad file path {path!r}; use a relative path like 'index.html' or 'data/rows.json'")
    return p


def content_type(path: str) -> str:
    ct = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if path.endswith((".js", ".mjs")):
        ct = "text/javascript"
    if ct.startswith("text/") or ct in ("application/json", "image/svg+xml"):
        ct += "; charset=utf-8"
    return ct


def parse_artifact_ref(ref: str) -> tuple[str, int | None]:
    """'a_xxx' -> latest; 'a_xxx@3' -> version 3."""
    aid, _, ver = (ref or "").strip().partition("@")
    if ver and not ver.isdigit():
        raise ValueError(f"bad artifact reference {ref!r}; use 'id' or 'id@version'")
    return aid, int(ver) if ver else None


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
        if kind == "artifact":
            content = self._check_artifact_ref(content)
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
        if sets.get("kind", cur["kind"]) == "artifact" and ("kind" in sets or "content" in sets):
            sets["content"] = self._check_artifact_ref(sets.get("content", cur["content"]))
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

    # ---- artifacts ------------------------------------------------------
    # An artifact is a small website kept in this workspace: an entry page plus any other
    # files, stored whole for every version. Cards of kind "artifact" point at one by id.

    def _check_artifact_ref(self, ref: str) -> str:
        aid, ver = parse_artifact_ref(ref)
        a = self.artifact_meta(aid)
        if ver is not None and not 1 <= ver <= a["latest"]:
            raise ValueError(f"artifact {aid} has versions 1 to {a['latest']}")
        return f"{aid}@{ver}" if ver else aid

    def artifact_meta(self, aid: str) -> dict[str, Any]:
        rows = self._q("SELECT * FROM artifacts WHERE id=?", (aid,))
        if not rows:
            raise NotFound(f"artifact {aid} not found")
        return dict(rows[0])

    def list_artifacts(self) -> list[dict[str, Any]]:
        rows = self._q(
            "SELECT a.*, (SELECT COUNT(*) FROM artifact_files f WHERE f.artifact_id=a.id AND f.version=a.latest) files,"
            " (SELECT COALESCE(SUM(LENGTH(content)),0) FROM artifact_files f WHERE f.artifact_id=a.id AND f.version=a.latest) bytes"
            " FROM artifacts a ORDER BY a.updated_at DESC"
        )
        return [dict(r) for r in rows]

    def get_artifact(self, aid: str, version: int | None = None, with_content: bool = False) -> dict[str, Any]:
        a = self.artifact_meta(aid)
        v = version or a["latest"]
        if not 1 <= v <= a["latest"]:
            raise NotFound(f"artifact {aid} has no version {v}")
        files = []
        for r in self._q(
            "SELECT path, content, content_type FROM artifact_files WHERE artifact_id=? AND version=? ORDER BY path", (aid, v)
        ):
            f: dict[str, Any] = {"path": r["path"], "content_type": r["content_type"], "size": len(r["content"])}
            if with_content:
                if r["content_type"].startswith("text/") or "json" in r["content_type"] or "svg" in r["content_type"]:
                    f["content"], f["encoding"] = bytes(r["content"]).decode("utf-8", "replace"), "text"
                else:
                    f["content"], f["encoding"] = base64.b64encode(r["content"]).decode(), "base64"
            files.append(f)
        versions = [dict(r) for r in self._q(
            "SELECT version, note, source, created_at FROM artifact_versions WHERE artifact_id=? ORDER BY version", (aid,)
        )]
        return {**a, "version": v, "files": files, "versions": versions}

    def artifact_file(self, aid: str, version: int | None, path: str) -> tuple[bytes, str]:
        a = self.artifact_meta(aid)
        v = version or a["latest"]
        rows = self._q(
            "SELECT content, content_type FROM artifact_files WHERE artifact_id=? AND version=? AND path=?",
            (aid, v, path or a["entry"]),
        )
        if not rows:
            raise NotFound(f"{path or a['entry']} not in artifact {aid} v{v}")
        return bytes(rows[0]["content"]), rows[0]["content_type"]

    def _write_version(
        self, db, aid: str, version: int, files: dict[str, bytes], note: str, source: str | None
    ) -> None:
        total = sum(len(b) for b in files.values())
        if total > ARTIFACT_MAX_BYTES:
            raise ValueError(f"artifact is {total} bytes; the limit is {ARTIFACT_MAX_BYTES}")
        db.execute(
            "INSERT INTO artifact_versions(artifact_id,version,note,source,created_at) VALUES(?,?,?,?,?)",
            (aid, version, note or "", source, now()),
        )
        db.executemany(
            "INSERT INTO artifact_files(artifact_id,version,path,content,content_type) VALUES(?,?,?,?,?)",
            [(aid, version, path, sqlite3.Binary(data), content_type(path)) for path, data in files.items()],
        )

    def create_artifact(
        self,
        title: str,
        files: dict[str, bytes],
        entry: str = "index.html",
        description: str = "",
        source: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        files = {clean_path(p): b for p, b in files.items()}
        entry = clean_path(entry)
        if entry not in files:
            raise ValueError(f"the entry file {entry} is missing; files given: {', '.join(files) or 'none'}")
        aid = new_id("a", 16)  # long: artifact pages are served without the token
        t = now()
        with self._tx() as db:
            db.execute(
                "INSERT INTO artifacts(id,title,description,entry,latest,source,created_at,updated_at) VALUES(?,?,?,?,1,?,?,?)",
                (aid, title, description or "", entry, source, t, t),
            )
            self._write_version(db, aid, 1, files, note or "first version", source)
        self.changed("artifacts")
        return self.get_artifact(aid)

    def update_artifact(
        self,
        aid: str,
        files: dict[str, bytes] | None = None,
        remove: list[str] | None = None,
        note: str = "",
        title: str | None = None,
        description: str | None = None,
        entry: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """New version = latest files, plus/overwritten by `files`, minus `remove`.
        Changing only title/description doesn't make a new version."""
        a = self.artifact_meta(aid)
        new_files = {clean_path(p): b for p, b in (files or {}).items()}
        drop = {clean_path(p) for p in (remove or [])}
        entry = clean_path(entry) if entry else a["entry"]
        with self._tx() as db:
            sets = {"updated_at": now()}
            if title is not None:
                sets["title"] = title
            if description is not None:
                sets["description"] = description
            if new_files or drop or entry != a["entry"]:
                cur = {
                    r["path"]: bytes(r["content"])
                    for r in db.execute(
                        "SELECT path, content FROM artifact_files WHERE artifact_id=? AND version=?", (aid, a["latest"])
                    )
                }
                missing = drop - set(cur)
                if missing:
                    raise ValueError(f"not in the artifact: {', '.join(sorted(missing))}")
                merged = {p: b for p, b in cur.items() if p not in drop}
                merged.update(new_files)
                if entry not in merged:
                    raise ValueError(f"the entry file {entry} would be missing")
                v = a["latest"] + 1
                self._write_version(db, aid, v, merged, note, source)
                sets.update(latest=v, entry=entry)
            cols = ", ".join(f"{k}=?" for k in sets)
            db.execute(f"UPDATE artifacts SET {cols} WHERE id=?", (*sets.values(), aid))
            # Cards showing this artifact re-render when their finding changes.
            db.execute(
                "UPDATE findings SET updated_at=? WHERE kind='artifact' AND (content=? OR content LIKE ?)",
                (sets["updated_at"], aid, aid + "@%"),
            )
        self.changed("artifacts")
        return self.get_artifact(aid)

    def delete_artifact(self, aid: str) -> dict[str, Any]:
        self.artifact_meta(aid)
        with self._tx() as db:
            n = db.execute(
                "DELETE FROM findings WHERE kind='artifact' AND (content=? OR content LIKE ?)", (aid, aid + "@%")
            ).rowcount
            db.execute("DELETE FROM artifacts WHERE id=?", (aid,))
        self.changed("artifacts")
        return {"deleted": aid, "cards_removed": n}

    # ---- views ----------------------------------------------------------
    def state(self) -> dict[str, Any]:
        """Everything the UI needs for this workspace."""
        return {
            "workspace": self.summary(),
            "settings": self.settings(),
            "findings": self.list_findings(),
            "boards": self.list_boards(),
            "placements": self.placements(),
            "artifacts": self.list_artifacts(),
        }

    def board_view(self, bid: str) -> dict[str, Any]:
        """A board as an agent sees it: placements with titles and bounds problems."""
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
        return {
            "board": board,
            "placements": placed,
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
