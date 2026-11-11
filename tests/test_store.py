import os
import time

import pytest
import yaml

from ritboard.store import NotFound, Registry


@pytest.fixture
def reg(tmp_path):
    r = Registry(tmp_path / "data", default_root=tmp_path / "ws")
    yield r
    r.close()


def test_workspace_lifecycle(reg, tmp_path):
    st = reg.create("Alpha Project")
    assert st.id == "alpha-project" and st.root == (tmp_path / "ws" / "Alpha Project").resolve()
    assert (st.root / ".ritboard" / "workspace.yaml").exists()
    assert "local/" in (st.root / ".ritboard" / ".gitignore").read_text()
    assert reg.get("alpha project").id == st.id  # by name, case-insensitive
    assert len(st.list_boards()) == 1
    st2 = reg.create("Alpha Project")
    assert st2.id == "alpha-project-2" and st2.root.name == "Alpha Project 2"
    reg.rename(st2.id, "Beta")
    assert reg.get("beta").id == "alpha-project-2"
    kept = reg.remove("beta")
    assert os.path.isdir(kept)  # the folder stays
    with pytest.raises(NotFound):
        reg.get("beta")
    with pytest.raises(ValueError):
        reg.create("x", path=st.root)  # already a workspace


def test_open_existing_folder(reg, tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "plan.md").write_text("---\ncategory: plan\naliases: [p]\n---\n# Plan\n")
    (repo / "data.csv").write_text("a,b\n1,2\n")
    (repo / "notes.txt").write_text("hello")
    (repo / "run.py").write_text("print(1)")
    (repo / "pic.png").write_bytes(b"\x89PNG")
    (repo / "blob.bin").write_bytes(b"\0")
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("x")
    (repo / "site").mkdir()
    (repo / "site" / "index.html").write_text("<p>hi</p>")
    st = reg.open(repo)
    fs = {f["id"]: f for f in st.list_findings()}
    assert set(fs) == {"docs", "docs/plan.md", "data.csv", "notes.txt", "run.py", "pic.png", "blob.bin", "site"}
    assert fs["docs/plan.md"]["category"] == "plan" and fs["docs/plan.md"]["title"] == "plan"
    assert "<h1>" in fs["docs/plan.md"]["rendered"]
    assert fs["data.csv"]["kind"] == "csv" and fs["run.py"]["kind"] == "text" and fs["run.py"]["title"] == "run.py"
    assert fs["pic.png"]["kind"] == "image" and fs["pic.png"]["src"].startswith("/api/w/")
    assert fs["blob.bin"]["kind"] == "file" and fs["site"]["kind"] == "artifact"
    # Ritboard's own metadata doesn't disturb keys it doesn't know.
    st.update_finding("docs/plan.md", tags=["x"])
    assert "aliases: [p]" in (repo / "docs" / "plan.md").read_text() or "aliases:\n- p" in (repo / "docs" / "plan.md").read_text()
    assert reg.open(repo).id == st.id  # opening twice is the same workspace


def test_findings_are_files(reg):
    st = reg.create("T")
    root = st.add_finding("Folder", kind="folder")
    assert root["id"] == "Folder" and (st.root / "Folder").is_dir()
    a = st.add_finding("A: table", "| x | y |\n|---|---|\n| 1 | 2 |", parent_id="Folder", category="perf")
    assert a["id"] == "Folder/A - table.md" and a["title"] == "A: table" and "<table>" in a["rendered"]
    text = (st.root / a["id"]).read_text()
    assert text.startswith("---\n") and "category: perf" in text and "A: table" in text
    b = st.add_finding("B", "a,b\n1,2", kind="table", parent_id="Folder")
    assert b["kind"] == "csv" and "<td>1</td>" in b["rendered"]
    x = st.add_finding("X", "<script>alert(1)</script>")
    assert "<script>" not in x["rendered"]
    h = st.add_finding("Chart", "<b>hi</b>", kind="html", category="viz")
    assert (st.root / "Chart.html").read_text().startswith("<!--ritboard\n") and h["content"] == "<b>hi</b>"
    u = st.add_finding("Site", "https://example.com", kind="url")
    assert (st.root / "Site.url").read_text().startswith("[InternetShortcut]") and u["content"] == "https://example.com"
    img = st.add_finding("Dot", "data:image/png;base64,iVBORw0KGgo=", kind="image")
    assert img["id"] == "Dot.png" and (st.root / "Dot.png").read_bytes().startswith(b"\x89PNG")
    assert st.settings()["categories"]["perf"].startswith("#")
    with pytest.raises(ValueError):
        st.add_finding("inside a file", parent_id=a["id"])
    with pytest.raises(ValueError):
        st.update_finding("Folder", parent_id="Folder")  # inside itself

    # Rename, move, convert.
    a2 = st.update_finding(a["id"], title="Renamed")
    assert a2["id"] == "Folder/Renamed.md" and not (st.root / a["id"]).exists()
    assert "title:" not in (st.root / a2["id"]).read_text()  # the name says it now
    moved = st.update_finding(a2["id"], parent_id="")
    assert moved["id"] == "Renamed.md"
    conv = st.update_finding("Renamed.md", kind="text")
    assert conv["id"] == "Renamed.txt" and conv["category"] == "perf"
    assert "Renamed.txt" in yaml.safe_load((st.root / ".ritboard" / "meta.yaml").read_text())

    # Deleting moves to the trash; a folder's contents move up.
    out = st.delete_finding("Folder")
    assert out["deleted"] == 1 and st.get_finding("B.csv")["kind"] == "csv"
    assert ".ritboard/local/trash" in out["trashed_to"].replace("\\", "/")


def test_placements_and_bounds(reg):
    st = reg.create("T")
    board = st.list_boards()[0]
    f1 = st.add_finding("one")
    f2 = st.add_finding("two", kind="html", content="<b>hi</b>")
    a, b = st.place(board["id"], [{"finding_id": f1["id"]}, {"finding_id": f2["id"]}])
    assert not (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])
    st.place(board["id"], [{"finding_id": f1["id"], "x": 3000, "y": 1900}])
    p = next(p for p in st.placements(board["id"]) if p["finding_id"] == f1["id"])
    assert (p["x"], p["y"], p["w"]) == (3000, 1900, 420)  # size kept
    view = st.board_view(board["name"])  # by name
    assert view["out_of_bounds"] == [f1["id"]]
    st.update_board(board["id"], width=8000, height=4000)
    assert st.board_view(board["id"])["out_of_bounds"] == []
    st.unplace(board["id"], [f2["id"]])
    assert len(st.board_view(board["id"])["placements"]) == 1
    saved = (st.root / ".ritboard" / "boards" / "main.yaml").read_text()
    assert "one.md: [3000, 1900, 420, 300," in saved  # one line per card


def test_renames_outside_ritboard_are_followed(reg):
    st = reg.create("T")
    bid = st.list_boards()[0]["id"]
    st.add_finding("Net", kind="folder")
    f = st.add_finding("stalls", "# stalls\nunique text 1", parent_id="Net")
    c = st.add_finding("rows", "a,b\n1,2", kind="csv", parent_id="Net", category="data")
    st.place(bid, [{"finding_id": f["id"], "x": 10, "y": 20}, {"finding_id": c["id"]}])

    os.rename(st.root / "Net", st.root / "Network")  # a folder, as Explorer or git would
    st.mark_dirty()
    ids = {p["finding_id"]: p for p in st.placements(bid)}
    assert set(ids) == {"Network/stalls.md", "Network/rows.csv"} and ids["Network/stalls.md"]["x"] == 10
    assert st.get_finding("Network/rows.csv")["category"] == "data"  # metadata followed too

    os.rename(st.root / "Network" / "stalls.md", st.root / "moved.md")  # a single file
    st.mark_dirty()
    assert "moved.md" in {p["finding_id"] for p in st.placements(bid)}

    # A file deleted outside Ritboard leaves a missing card behind, not an error.
    (st.root / "moved.md").unlink()
    st.mark_dirty()
    view = st.board_view(bid)
    assert view["missing"] == ["moved.md"]


def test_renames_while_closed_are_followed(reg, tmp_path):
    st = reg.create("T")
    bid = st.list_boards()[0]["id"]
    f = st.add_finding("idea", "something only this file says")
    st.place(bid, [{"finding_id": f["id"]}])
    root = st.root
    reg.close()
    os.rename(root / "idea.md", root / "better idea.md")
    reg2 = Registry(tmp_path / "data", default_root=tmp_path / "ws")
    assert [p["finding_id"] for p in reg2.get("T").placements(bid)] == ["better idea.md"]


def test_files_written_by_others_show_up(reg):
    st = reg.create("T")
    (st.root / "new.md").write_text("---\ntitle: From an editor\n---\nhi")
    time.sleep(0.01)
    st.mark_dirty()
    assert st.get_finding("new.md")["title"] == "From an editor"
    (st.root / "Report").mkdir()
    (st.root / "Report" / "index.html").write_text("<p>x</p>")
    st.mark_dirty()
    assert st.get_finding("Report")["kind"] == "artifact"
    assert [a["id"] for a in st.list_artifacts()] == ["Report"]


def test_artifact_writes_stay_inside(reg, tmp_path):
    st = reg.create("T")
    a = st.create_artifact("Page", {"index.html": b"<p>x</p>"})
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, st.root / a["id"] / "data")  # e.g. from a cloned repository
    with pytest.raises(ValueError, match="outside the artifact"):
        st.update_artifact(a["id"], {"data/x.json": b"{}"})
    assert not (outside / "x.json").exists()
    assert st.db_write(a["id"], "plan", "d", {"k": 1})["version"] == 1


def test_artifact_versions_are_old_copies(reg):
    st = reg.create("T")
    bid = st.list_boards()[0]["id"]
    a = st.create_artifact("Chart", {"index.html": b"one", "app.js": b"js", "old.css": b"css"})
    assert a["version"] == 1
    root = st.root / "Chart"

    v2 = st.update_artifact("Chart", {"index.html": b"two", "app.js": b"js"}, remove=["old.css"])
    assert v2["version"] == 2
    assert sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) == [
        "app.js", "index.html", "index@v1.html", "old@v1.css"]  # app.js didn't change: no copy
    assert {f["path"] for f in v2["files"]} == {"index.html", "app.js"}

    st.update_artifact("Chart", {"index.html": b"three", "new.js": b"n"})
    assert st.artifact_file("Chart", "index.html")[0] == b"three"
    assert st.artifact_file("Chart", "index.html", 2)[0] == b"two"
    assert st.artifact_file("Chart", "index.html", 1)[0] == b"one"
    assert st.artifact_file("Chart", "app.js", 1)[0] == b"js"
    assert st.artifact_file("Chart", "old.css", 1)[0] == b"css"
    with pytest.raises(NotFound):
        st.artifact_file("Chart", "old.css", 2)  # removed in version 2
    old = st.get_artifact("Chart", with_content=True, version=1)
    assert {f["path"]: f["content"] for f in old["files"]}["index.html"] == "one" and old["latest"] == 3
    assert st.update_artifact("Chart", {"index.html": b"three"})["version"] == 3  # same content: no new version

    with pytest.raises(ValueError, match="old versions"):
        st.update_artifact("Chart", {"index@v9.html": b"x"})

    # A card can show an older version, and it follows the artifact when it is renamed.
    st.place(bid, [{"finding_id": "Chart@v1", "x": 5, "y": 5}, {"finding_id": "Chart"}])
    st.update_finding("Chart", title="Step chart")
    view = st.board_view(bid)
    assert {p["finding_id"] for p in view["placements"]} == {"Step chart@v1", "Step chart"}
    assert view["missing"] == []
    with pytest.raises(NotFound):
        st.place(bid, [{"finding_id": "Step chart@v9"}])
