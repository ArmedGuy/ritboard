"""Workspaces from the SQLite version become folders."""

import json
import sqlite3

from ritboard.migrate import migrate_sqlite
from ritboard.store import Registry

OLD_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE findings (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'md',
  content TEXT NOT NULL DEFAULT '', rendered TEXT, category TEXT, tags TEXT NOT NULL DEFAULT '[]', source TEXT,
  sort REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE boards (id TEXT PRIMARY KEY, name TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
  background TEXT, sort REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE placements (board_id TEXT NOT NULL, finding_id TEXT NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
  w INTEGER NOT NULL, h INTEGER NOT NULL, z INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (board_id, finding_id));
CREATE TABLE artifacts (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  entry TEXT NOT NULL DEFAULT 'index.html', latest INTEGER NOT NULL DEFAULT 0, source TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE artifact_files (artifact_id TEXT NOT NULL, version INTEGER NOT NULL, path TEXT NOT NULL,
  content BLOB NOT NULL, content_type TEXT NOT NULL, sha256 TEXT, size INTEGER, pending INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (artifact_id, version, path));
CREATE TABLE artifact_docs (artifact_id TEXT NOT NULL, collection TEXT NOT NULL, doc_id TEXT NOT NULL,
  data TEXT NOT NULL, version INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (artifact_id, collection, doc_id));
"""
T = "2026-11-01T00:00:00+00:00"


def _old_workspace(path):
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)
    db.executemany("INSERT INTO meta VALUES (?,?)", [
        ("name", "Yard plan"),
        ("settings", json.dumps({"board_width": 3840, "board_height": 2160, "grid": 20, "categories": {"sun": "#edae49"}})),
    ])
    rows = [
        ("f_dir", None, "Research", "folder", "", None, "[]", 1),
        ("f_md", "f_dir", "Sun hours", "md", "# Sun\n6 h", "sun", '["june"]', 1),
        ("f_kid", "f_md", "Under a note", "text", "child of a note", None, "[]", 1),
        ("f_claude", None, "Old claude card", "url", "https://claude.ai/artifact/x", None, "[]", 2),
        ("f_art", "f_dir", "Sun map card", "artifact", "a_1@1", "sun", "[]", 2),
        ("f_png", None, "Dot", "image", "data:image/png;base64,iVBORw0KGgo=", None, "[]", 3),
    ]
    db.executemany(
        "INSERT INTO findings(id,parent_id,title,kind,content,category,tags,sort,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(*r, T, T) for r in rows],
    )
    db.execute("INSERT INTO artifacts VALUES ('a_1','Sun map','','index.html',2,'agent',?,?)", (T, T))
    db.executemany("INSERT INTO artifact_files(artifact_id,version,path,content,content_type,pending) VALUES (?,?,?,?,?,?)", [
        ("a_1", 1, "index.html", b"old", "text/html", 0),
        ("a_1", 2, "index.html", b"<p>new</p>", "text/html", 0),
        ("a_1", 2, "grid.bin", b"", "application/octet-stream", 1),
    ])
    db.execute("INSERT INTO artifact_docs VALUES ('a_1','beds','b1',?,3,?)", (json.dumps({"label": "Kale"}), T))
    db.execute("INSERT INTO boards VALUES ('b_1','Yard',7680,2160,NULL,1,?,?)", (T, T))
    db.executemany("INSERT INTO placements VALUES ('b_1',?,?,?,?,?,?)", [
        ("f_md", 10, 20, 400, 300, 1), ("f_art", 500, 20, 800, 600, 2), ("f_png", 0, 0, 50, 50, 3),
    ])
    db.commit()
    db.close()


def test_sqlite_workspace_becomes_a_folder(tmp_path):
    data = tmp_path / "data"
    (data / "workspaces").mkdir(parents=True)
    _old_workspace(data / "workspaces" / "yard-plan.sqlite")
    reg = Registry(data, default_root=tmp_path / "ws")
    log = migrate_sqlite(reg)
    assert any("Converted workspace 'Yard plan'" in line for line in log)
    assert any("grid.bin was never uploaded" in line for line in log)
    assert not (data / "workspaces" / "yard-plan.sqlite").exists() and (data / "old-sqlite" / "yard-plan.sqlite").exists()
    assert migrate_sqlite(reg) == []  # only once

    st = reg.get("Yard plan")
    assert st.root == (tmp_path / "ws" / "Yard plan").resolve()
    assert st.settings()["grid"] == 20 and st.settings()["categories"]["sun"] == "#edae49"
    ids = {f["id"]: f for f in st.list_findings()}
    # A note with children gets a folder of its own, holding the note and the children.
    note = ids["Research/Sun hours/Sun hours.md"]
    assert note["category"] == "sun" and note["tags"] == ["june"]
    assert "Research/Sun hours/Under a note.txt" in ids
    assert ids["Research/Sun map card"]["kind"] == "artifact"
    assert (st.root / "Research" / "Sun map card" / "index.html").read_text() == "<p>new</p>"
    assert (st.root / "Research" / "Sun map card" / "index@v1.html").read_text() == "old"  # history kept
    assert st.get_artifact("Research/Sun map card")["version"] == 2
    assert st.artifact_by_key("a_1") == "Research/Sun map card"  # the old address still works
    assert st.artifact_file("Research/Sun map card", "index.html", 1)[0] == b"old"
    assert st.db_get("Research/Sun map card", "beds", "b1") == {
        "id": "b1", "data": {"label": "Kale"}, "version": 3, "updatedAt": T,
    }
    assert ids["Old claude card.md"]["content"].startswith("Was a url card")
    assert ids["Dot.png"]["kind"] == "image"

    b = st.board_view("Yard")
    assert b["board"]["width"] == 7680
    got = {p["finding_id"]: (p["x"], p["y"], p["w"], p["h"]) for p in b["placements"]}
    assert got == {
        "Research/Sun hours/Sun hours.md": (10, 20, 400, 300),
        "Research/Sun map card@v1": (500, 20, 800, 600),  # it was pinned to version 1, and still is
        "Dot.png": (0, 0, 50, 50),
    }


def test_old_ids_are_found_for_earlier_conversions(tmp_path):
    from ritboard.migrate import link_legacy_ids

    data = tmp_path / "data"
    (data / "workspaces").mkdir(parents=True)
    _old_workspace(data / "workspaces" / "yard-plan.sqlite")
    reg = Registry(data, default_root=tmp_path / "ws")
    migrate_sqlite(reg)
    st = reg.get("Yard plan")
    meta = st._meta_yaml()
    meta["Research/Sun map card"].pop("legacy_id")  # as converted before old ids were kept
    st._save_meta_yaml(meta)
    st.mark_dirty()
    assert link_legacy_ids(reg)
    assert reg.get("Yard plan").artifact_by_key("a_1") == "Research/Sun map card"
    assert link_legacy_ids(reg) == []  # once
