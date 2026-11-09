import pytest

from ritbord.store import NotFound, Registry


@pytest.fixture
def reg(tmp_path):
    r = Registry(tmp_path)
    yield r
    r.close()


def test_workspace_lifecycle(reg):
    st = reg.create("Alpha Project")
    assert st.id == "alpha-project"
    assert reg.get("alpha project").id == st.id  # by name, case-insensitive
    assert len(st.list_boards()) == 1
    st2 = reg.create("Alpha Project")
    assert st2.id == "alpha-project-2"
    reg.rename(st2.id, "Beta")
    assert reg.get("beta").id == "alpha-project-2"
    dest = reg.delete("beta")
    assert dest.endswith(".sqlite")
    with pytest.raises(NotFound):
        reg.get("beta")


def test_findings_tree_and_render(reg):
    st = reg.create("T")
    root = st.add_finding("Folder", kind="folder")
    a = st.add_finding("A", "| x | y |\n|---|---|\n| 1 | 2 |", parent_id=root["id"], category="perf")
    assert "<table>" in a["rendered"]
    b = st.add_finding("B", "a,b\n1,2", kind="table", parent_id=a["id"])
    assert b["kind"] == "csv" and "<td>1</td>" in b["rendered"]
    x = st.add_finding("X", "<script>alert(1)</script>")
    assert "<script>" not in x["rendered"]
    assert st.settings()["categories"]["perf"].startswith("#")
    with pytest.raises(ValueError):
        st.update_finding(root["id"], parent_id=b["id"])  # cycle
    st.delete_finding(a["id"])  # children move up
    assert st.get_finding(b["id"])["parent_id"] == root["id"]
    assert st.delete_finding(root["id"], recursive=True)["deleted"] == 2


def test_placements_and_bounds(reg):
    st = reg.create("T")
    board = st.list_boards()[0]
    f1 = st.add_finding("one")
    f2 = st.add_finding("two", kind="html", content="<b>hi</b>")
    ps = st.place(board["id"], [{"finding_id": f1["id"]}, {"finding_id": f2["id"]}])
    a, b = ps
    assert not (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])
    st.place(board["id"], [{"finding_id": f1["id"], "x": 3000, "y": 1900}])
    p = st.placements(board["id"])[0]
    assert (p["x"], p["y"], p["w"]) == (3000, 1900, 420)  # size kept
    view = st.board_view(board["name"])  # by name
    assert view["out_of_bounds"] == [f1["id"]]
    st.update_board(board["id"], width=8000, height=4000)
    assert st.board_view(board["id"])["out_of_bounds"] == []
    st.unplace(board["id"], [f2["id"]])
    assert len(st.board_view(board["id"])["placements"]) == 1
