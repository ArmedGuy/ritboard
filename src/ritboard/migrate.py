"""Turn workspaces from the SQLite days (<data>/workspaces/*.sqlite) into folders.

Each one becomes a folder under the default workspace folder (~/Ritboard), and the .sqlite
file moves to <data>/old-sqlite/ so nothing is lost and it isn't converted twice.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import files as F
from .store import Registry, WorkspaceStore, copy_name


def _rows(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _convert(db: sqlite3.Connection, st: WorkspaceStore, log: list[str]) -> None:
    meta = {r["key"]: r["value"] for r in _rows(db, "SELECT key, value FROM meta")}
    try:
        settings = json.loads(meta.get("settings") or "{}")
    except ValueError:
        settings = {}
    st.update_settings({k: v for k, v in settings.items() if k in ("board_width", "board_height", "grid", "categories")})

    findings = [dict(r) for r in _rows(db, "SELECT * FROM findings ORDER BY sort, created_at")]
    children: dict[str | None, list[dict[str, Any]]] = {}
    for f in findings:
        children.setdefault(f["parent_id"], []).append(f)
    arts = {r["id"]: dict(r) for r in _rows(db, "SELECT * FROM artifacts")}
    art_paths: dict[str, str] = {}
    paths: dict[str, str] = {}

    def art_files(aid: str, version: int, quiet: bool = False) -> dict[str, bytes]:
        a = arts[aid]
        files = {}
        for r in _rows(db, "SELECT * FROM artifact_files WHERE artifact_id=? AND version=?", (aid, version)):
            if "pending" in r.keys() and r["pending"]:
                if not quiet:
                    log.append(f"  {a['title']}: {r['path']} was never uploaded; left out")
                continue
            files[r["path"]] = bytes(r["content"])
        entry = a.get("entry") or "index.html"
        if "index.html" not in files and entry in files:
            files["index.html"] = files[entry]
        return files

    def keep_history(aid: str, path: str) -> None:
        """Older versions become name@v<n>.ext copies, the same way new publishes keep them."""
        latest = arts[aid]["latest"]
        versions = {v: art_files(aid, v, quiet=True) for v in range(1, latest)}
        versions[latest] = art_files(aid, latest, quiet=True)
        for v in range(1, latest):
            for rel, data in versions[v].items():
                if versions[v + 1].get(rel) != data:
                    target = st._target(path, copy_name(rel, v))
                    F.write_bytes(target, data)
        meta = st._meta_yaml()
        meta.setdefault(path, {})["version"] = latest
        st._save_meta_yaml(meta)
        st.mark_dirty()

    def write_artifact(aid: str, title: str, parent: str | None, f: dict[str, Any] | None) -> str | None:
        if aid in art_paths:
            return art_paths[aid]
        if aid not in arts:
            return None
        files = art_files(aid, arts[aid]["latest"])
        if "index.html" not in files:
            log.append(f"  {title}: artifact has no page; skipped")
            return None
        a = st.create_artifact(
            title, files, description=arts[aid].get("description") or "", source=(f or {}).get("source") or arts[aid].get("source"),
            parent_id=parent, category=(f or {}).get("category"), tags=json.loads((f or {}).get("tags") or "[]"),
        )
        docs: dict[str, dict[str, Any]] = {}
        for r in _rows(db, "SELECT * FROM artifact_docs WHERE artifact_id=?", (aid,)):
            docs.setdefault(r["collection"], {})[r["doc_id"]] = {
                "data": json.loads(r["data"]), "version": r["version"], "updatedAt": r["updated_at"],
            }
        for coll, d in docs.items():
            st._save_coll(a["id"], coll, d)
        if arts[aid]["latest"] > 1:
            keep_history(aid, a["id"])
        art_paths[aid] = a["id"]
        return a["id"]

    def add(f: dict[str, Any], parent: str | None) -> None:
        kids = children.get(f["id"], [])
        tags = json.loads(f.get("tags") or "[]")
        kind = f["kind"]
        try:
            if kind == "folder":
                out = st.add_finding(f["title"], kind="folder", parent_id=parent, category=f["category"], tags=tags, source=f["source"])
                paths[f["id"]] = out["id"]
                for k in kids:
                    add(k, out["id"])
                return
            where = parent
            if kids:  # old boards let anything have children; now only folders do
                where = st.add_finding(f["title"], kind="folder", parent_id=parent)["id"]
            if kind == "artifact":
                aid, _, pin = (f["content"] or "").partition("@")
                p = write_artifact(aid, f["title"], where, f)
                if p:
                    paths[f["id"]] = f"{p}@v{pin}" if pin and pin.isdigit() and int(pin) < arts[aid]["latest"] else p
            else:
                try:
                    out = st.add_finding(f["title"], f["content"], kind, where, f["category"], tags, f["source"])
                except ValueError:  # e.g. a claude.ai link: keep it as a note
                    out = st.add_finding(f["title"], f"Was a {kind} card: {f['content']}", "md", where, f["category"], tags, f["source"])
                paths[f["id"]] = out["id"]
            for k in kids:
                add(k, where)
        except (ValueError, OSError) as e:
            log.append(f"  {f['title']}: {e}")

    for f in children.get(None, []):
        add(f, None)
    for aid, a in arts.items():
        if aid not in art_paths:
            write_artifact(aid, a["title"], None, None)

    for b in _rows(db, "SELECT * FROM boards ORDER BY sort, created_at"):
        nb = st.create_board(b["name"], b["width"], b["height"], b["background"])
        items = [
            {"finding_id": paths[p["finding_id"]], "x": p["x"], "y": p["y"], "w": p["w"], "h": p["h"], "z": p["z"]}
            for p in _rows(db, "SELECT * FROM placements WHERE board_id=? ORDER BY z", (b["id"],))
            if p["finding_id"] in paths
        ]
        if items:
            st.place(nb["id"], items)


def migrate_sqlite(reg: Registry) -> list[str]:
    """Convert every old .sqlite workspace. Returns what it did, one line each.

    Each is built in a hidden folder first and only renamed into place and added to the
    list when the whole conversion worked, so a failure leaves nothing half done."""
    old = reg.data_dir / "workspaces"
    if not old.is_dir():
        return []
    log: list[str] = []
    for src in sorted(old.glob("*.sqlite")):
        db = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        tmp = reg.default_root / f".converting-{src.stem}"
        try:
            name = (_rows(db, "SELECT value FROM meta WHERE key='name'") or [{"value": src.stem}])[0]["value"] or src.stem
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True)
            st = WorkspaceStore(tmp, "_converting", secret=reg.secret, name=name)
            lines: list[str] = []
            _convert(db, st, lines)
            if not st.list_boards():
                st.create_board("Main")
        except Exception as e:  # keep going; the .sqlite stays where it is
            log.append(f"Could not convert {src.name}: {e}")
            db.close()
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        db.close()
        base = reg.default_root / F.safe_name(name)
        root, n = base, 2
        while root.exists():
            root, n = base.with_name(f"{base.name} {n}"), n + 1
        tmp.rename(root)
        reg.open(root)
        log.append(f"Converted workspace {name!r} to {root}")
        log.extend(lines)
        dest = reg.data_dir / "old-sqlite"
        dest.mkdir(exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(src) + suffix)
            if p.exists():
                shutil.move(str(p), str(dest / p.name))
    if log:
        log.append(f"The old files are in {reg.data_dir / 'old-sqlite'}.")
    return log
