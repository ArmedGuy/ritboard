import asyncio
import json
import time
import base64
from pathlib import Path

import httpx
from mcp import Client

from test_live import data, server  # noqa: F401  (fixture)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_artifact_lifecycle(server):  # noqa: F811
    async def run():
        async with Client(server + "/mcp") as c:
            ov = data(await c.call_tool("create_workspace", {"name": "Arts"}))
            ws, root = ov["workspace"]["id"], Path(ov["workspace"]["path"])
            board = ov["boards"][0]["id"]
            out = data(await c.call_tool("publish_artifact", {
                "workspace": ws, "title": "Chart", "board_id": board, "x": 100, "y": 100,
                "files": [
                    {"path": "index.html", "content": "<script src='app.js'></script><img src='img/dot.png'>"},
                    {"path": "app.js", "content": "fetch('data/rows.json')"},
                    {"path": "data/rows.json", "content": "[1,2,3]"},
                    {"path": "img/dot.png", "content": base64.b64encode(PNG).decode(), "encoding": "base64"},
                ],
            }))
            a = out["artifact"]
            assert a["id"] == "Chart" and len(a["files"]) == 4 and out["placement"]["x"] == 100
            assert (root / "Chart" / "img" / "dot.png").read_bytes() == PNG
            url = a["url"]
            assert url.startswith(server + f"/w/{ws}/a/") and url.endswith("/")

            r = httpx.get(url.rstrip("/"), follow_redirects=False)
            assert r.status_code == 308 and r.headers["location"].endswith("/")
            page = httpx.get(url)
            assert page.status_code == 200 and "app.js" in page.text
            assert "sandbox allow-scripts" in page.headers["content-security-policy"]
            assert "allow-same-origin" not in page.headers["content-security-policy"]
            assert page.headers["content-type"].startswith("text/html")
            js = httpx.get(url + "app.js")
            assert js.headers["content-type"].startswith("text/javascript")
            assert httpx.get(url + "img/dot.png").content == PNG
            assert httpx.get(url + "nope.txt").status_code == 404
            assert httpx.get(url + "../../api/workspaces").status_code in (404, 400)
            assert httpx.get(url + ".data/x.json").status_code == 404

            v2 = data(await c.call_tool("update_artifact", {
                "workspace": ws, "artifact_id": a["id"], "html": "<h1>v2</h1>", "remove": ["app.js"],
            }))
            assert {f["path"] for f in v2["files"]} == {"index.html", "data/rows.json", "img/dot.png"}
            assert httpx.get(url).text.endswith("<h1>v2</h1>")
            assert not (root / "Chart" / "app.js").exists()

            got = data(await c.call_tool("get_artifact", {"workspace": ws, "artifact_id": a["id"]}))
            png = next(f for f in got["files"] if f["path"] == "img/dot.png")
            assert png["encoding"] == "base64" and base64.b64decode(png["content"]) == PNG

            renamed = data(await c.call_tool("update_artifact", {"workspace": ws, "artifact_id": "Chart", "title": "Step chart"}))
            assert renamed["id"] == "Step chart" and (root / "Step chart" / "index.html").exists()
            view = data(await c.call_tool("get_board", {"workspace": ws, "board_id": board}))
            assert view["placements"][0]["finding_id"] == "Step chart"

            bad = await c.call_tool("publish_artifact", {"workspace": ws, "title": "x", "files": [{"path": "../evil", "content": "x"}]})
            assert bad.is_error
            nf = await c.call_tool("publish_artifact", {"workspace": ws, "title": "x", "files": [{"path": "a.html", "content": "x"}]})
            assert nf.is_error  # no index.html

            st = httpx.get(f"{server}/api/w/{ws}/state").json()
            card = next(f for f in st["findings"] if f["kind"] == "artifact")
            assert card["id"] == "Step chart" and card["url"].startswith(f"/w/{ws}/a/")

            gone = data(await c.call_tool("delete_artifact", {"workspace": ws, "artifact_id": "Step chart"}))
            assert gone["deleted"] == "Step chart" and not (root / "Step chart").exists()
            assert httpx.get(renamed["url"]).status_code == 404

    asyncio.run(run())


def test_artifact_from_ui_api(server):  # noqa: F811
    ws = httpx.post(server + "/api/workspaces", json={"name": "Arts UI"}).json()["id"]
    out = httpx.post(f"{server}/api/w/{ws}/artifacts", json={"title": "Note", "files": {"index.html": "<p>one</p>"}}).json()
    a = out["artifact"]
    v = httpx.post(f"{server}/api/w/{ws}/artifact/files", params={"id": a["id"]}, json={"files": {"index.html": "<p>two</p>"}}).json()
    assert v["files"][0]["path"] == "index.html"
    assert httpx.get(server + a["url"]).text.endswith("<p>two</p>")
    got = httpx.get(f"{server}/api/w/{ws}/artifact", params={"id": a["id"], "content": True}).json()
    assert got["files"][0]["content"] == "<p>two</p>"
    card = httpx.post(f"{server}/api/w/{ws}/findings", json={"title": "Made as a card", "kind": "artifact", "content": "<p>x</p>"})
    assert card.json()["kind"] == "artifact"


def test_claude_links_are_refused(server):  # noqa: F811
    async def run():
        async with Client(server + "/mcp") as c:
            ws = data(await c.call_tool("create_workspace", {"name": "No Claude links"}))["workspace"]["id"]
            for url in ("https://claude.ai/artifact/abc", "https://claude.site/public/artifacts/x/embed"):
                r = await c.call_tool("add_finding", {"workspace": ws, "title": "x", "kind": "url", "content": url})
                assert r.is_error and "publish_artifact" in r.content[0].text
            ok = data(await c.call_tool("add_finding", {"workspace": ws, "title": "x", "kind": "url", "content": "https://example.com"}))
            r = await c.call_tool("update_finding", {"workspace": ws, "finding_id": ok["id"], "content": "https://claude.ai/x"})
            assert r.is_error
            r = await c.call_tool("add_finding", {"workspace": ws, "title": "x", "kind": "image", "content": "https://claude.ai/x.png"})
            assert r.is_error

    asyncio.run(run())


def test_artifact_database(server):  # noqa: F811
    async def run():
        async with Client(server + "/mcp") as c:
            ws = data(await c.call_tool("create_workspace", {"name": "Db"}))["workspace"]["id"]
            a = data(await c.call_tool("publish_artifact", {"workspace": ws, "title": "p", "html": "<head></head><p>x</p>"}))["artifact"]
            aid, url = a["id"], a["url"]
            page = httpx.get(url).text
            key = url.split("/")[-2]
            assert f'<script src="/w/{ws}/a/{key}/_ritboard.js" data-version="' in page
            assert "window.claude" in httpx.get(url + "_ritboard.js").text

            base = url + "_db"
            first = httpx.get(base, params={"c": "plan"}).json()
            assert first == {"rev": 0, "docs": []}
            assert httpx.get(base, params={"c": "plan", "rev": 0}).json()["unchanged"] is True
            r = httpx.put(base + "/doc", params={"c": "plan", "id": "bed1"}, json={"label": "Bed", "pts": [1, 2]})
            assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
            r = httpx.patch(base + "/doc", params={"c": "plan", "id": "bed1"}, json={"note": "kale"})
            assert r.json()["data"] == {"label": "Bed", "pts": [1, 2], "note": "kale"} and r.json()["version"] == 2
            assert httpx.options(base + "/doc").status_code == 204

            got = data(await c.call_tool("artifact_data", {"workspace": ws, "artifact_id": aid, "action": "list", "collection": "plan"}))
            assert got["docs"][0]["data"]["note"] == "kale"
            stale = await c.call_tool("artifact_data", {"workspace": ws, "artifact_id": aid, "action": "update",
                                                          "collection": "plan", "doc_id": "bed1", "data": {"x": 1}, "if_version": 1})
            assert stale.is_error and "version 2" in stale.content[0].text
            data(await c.call_tool("artifact_data", {"workspace": ws, "artifact_id": aid, "action": "set",
                                                     "collection": "plan", "doc_id": "pin", "data": {"x": 1}}))
            cols = data(await c.call_tool("artifact_data", {"workspace": ws, "artifact_id": aid, "action": "collections"}))
            assert cols["collections"] == [{"collection": "plan", "docs": 2}]
            assert httpx.delete(base + "/doc", params={"c": "plan", "id": "bed1"}).status_code == 204
            assert [d["id"] for d in httpx.get(base, params={"c": "plan"}).json()["docs"]] == ["pin"]
            assert httpx.put(base + "/doc", params={"c": "../x", "id": "a"}, json={}).status_code == 400
            assert httpx.put(base + "/doc", params={"c": "a:b", "id": "a"}, json={"k": 1}).status_code == 200  # ':' is fine on Windows too
            wss = data(await c.call_tool("list_workspaces", {}))["workspaces"]
            root = Path(next(w["path"] for w in wss if w["id"] == ws))
            files = sorted(p.name for p in (root / "p" / ".data").iterdir())
            assert "plan.json" in files and "a%3Ab.json" in files

    asyncio.run(run())


def test_shadow_files_and_upload_links(server):  # noqa: F811
    import hashlib

    big = bytes(range(256)) * 2000  # 512 KB, binary
    h = hashlib.sha256(big).hexdigest()

    async def run():
        async with Client(server + "/mcp") as c:
            ws = data(await c.call_tool("create_workspace", {"name": "Uploads"}))["workspace"]["id"]
            out = data(await c.call_tool("publish_artifact", {
                "workspace": ws, "title": "Grid", "html": "<script src=grid.bin></script>",
                "shadow_files": [{"path": "grid.bin", "sha256": h, "size": len(big)},
                                 {"path": "copy/grid.bin", "sha256": h, "size": len(big)}],
            }))
            a = out["artifact"]
            assert a["pending"] == 2
            waiting = a["uploads"]["waiting"]
            assert len(waiting) == 2 and "curl" in waiting[0] and "note" in waiting[1]
            url = waiting[0]["put_url"]
            assert httpx.get(a["url"] + "grid.bin").status_code == 404

            bad = httpx.put(url, content=b"nope")
            assert bad.status_code == 400 and "does not match" in bad.json()["detail"]
            ok = httpx.put(url, content=big).json()
            assert sorted(ok["stored"]) == ["copy/grid.bin", "grid.bin"] and ok["still_waiting"] == 0
            assert httpx.get(a["url"] + "copy/grid.bin").content == big

            # A new publish replaces the links; the same content needs no upload.
            v2 = data(await c.call_tool("update_artifact", {
                "workspace": ws, "artifact_id": a["id"],
                "shadow_files": [{"path": "again.bin", "sha256": h, "size": len(big)},
                                 {"path": "tiny.bin", "sha256": hashlib.sha256(b"t").hexdigest(), "size": 1}],
            }))
            assert v2["pending"] == 1 and [w["path"] for w in v2["uploads"]["waiting"]] == ["tiny.bin"]
            assert httpx.put(url, content=big).status_code == 404  # old link is dead
            new_url = v2["uploads"]["waiting"][0]["put_url"]
            assert httpx.put(new_url, content=b"t").json()["still_waiting"] == 0

            links = data(await c.call_tool("upload_links", {"workspace": ws, "artifact_id": a["id"]}))
            assert links["waiting"] == []
            renewed = data(await c.call_tool("upload_links", {"workspace": ws, "artifact_id": a["id"], "renew": True}))
            assert renewed["waiting"] == []

    asyncio.run(run())


def test_shadow_files_from_local_paths(tmp_path):
    import hashlib

    from ritboard.mcp_tools import build_mcp
    from ritboard.store import Registry

    allowed = tmp_path / "shared"
    allowed.mkdir()
    big = b"\x00\x01" * 100000
    h = hashlib.sha256(big).hexdigest()
    (allowed / "grid.bin").write_bytes(big)
    (tmp_path / "secret.txt").write_text("nope")
    reg = Registry(tmp_path / "data", default_root=tmp_path / "ws")

    async def run():
        async with Client(build_mcp(reg, {}, base_url="http://x", file_roots=[allowed])) as c:
            ws = data(await c.call_tool("create_workspace", {"name": "Local"}))["workspace"]["id"]
            out = data(await c.call_tool("publish_artifact", {
                "workspace": ws, "title": "Grid", "html": "<p>x</p>",
                "shadow_files": [{"path": "grid.bin", "sha256": h, "size": len(big), "local_path": str(allowed / "grid.bin")}],
            }))
            assert out["local_files"][0]["stored"] == ["grid.bin"] and out["artifact"]["pending"] == 0
            aid = out["artifact"]["id"]
            assert reg.get(ws).artifact_file(aid, "grid.bin")[0] == big

            # Later fill, and the guards: outside the allowed folder, wrong hash, relative path.
            sh = hashlib.sha256(b"nope").hexdigest()
            v2 = data(await c.call_tool("update_artifact", {
                "workspace": ws, "artifact_id": aid,
                "shadow_files": [{"path": "s.txt", "sha256": sh, "size": 4}],
            }))
            assert v2["pending"] == 1
            res = data(await c.call_tool("fill_from_local_files", {"workspace": ws, "artifact_id": aid, "files": [
                {"sha256": sh, "local_path": str(tmp_path / "secret.txt")},
                {"sha256": sh, "local_path": "relative/secret.txt"},
                {"sha256": sh, "local_path": str(allowed / "grid.bin")},
            ]}))
            errs = [f.get("error", "") for f in res["local_files"]]
            assert "outside the folders" in errs[0] and "absolute" in errs[1] and "does not match" in errs[2]
            assert res["artifact"]["pending"] == 1

            # Folders an agent may make or open are limited the same way.
            r = await c.call_tool("open_workspace", {"folder": str(tmp_path)})
            assert r.is_error and "outside the folders" in r.content[0].text
            (allowed / "proj").mkdir()
            (allowed / "proj" / "n.md").write_text("x")
            ok = data(await c.call_tool("open_workspace", {"folder": str(allowed / "proj")}))
            assert ok["findings"][0]["id"] == "n.md"

        async with Client(build_mcp(reg, {}, base_url="http://x", file_roots=[])) as c:
            res = data(await c.call_tool("fill_from_local_files", {"workspace": ws, "artifact_id": aid, "files": [
                {"sha256": sh, "local_path": str(allowed / "grid.bin")}]}))
            assert "off on this server" in res["local_files"][0]["error"]

    asyncio.run(run())


def test_artifact_events_are_pushed(server):  # noqa: F811
    import threading

    w = httpx.post(server + "/api/workspaces", json={"name": "Events"}).json()
    ws, root = w["id"], Path(w["path"])
    a = httpx.post(f"{server}/api/w/{ws}/artifacts", json={"title": "E", "files": {"index.html": "<head></head>v1"}}).json()["artifact"]
    url = server + a["url"]
    assert 'data-version="' in httpx.get(url).text

    events = []

    def listen():
        with httpx.stream("GET", url + "_events", timeout=15) as r:
            assert r.headers["access-control-allow-origin"] == "*"
            name = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: ") and name:
                    events.append((name, line[6:]))
                    if name == "deleted":
                        return

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    time.sleep(1)  # let the folder watcher start on the new workspace
    hello = json.loads(events[0][1])["latest"]
    httpx.put(url + "_db/doc", params={"c": "x", "id": "d"}, json={"a": 1})
    httpx.post(f"{server}/api/w/{ws}/artifact/files", params={"id": "E"}, json={"files": {"index.html": "v2"}})
    time.sleep(0.3)
    (root / "E" / "extra.js").write_text("// written by an editor")  # outside Ritboard: the watcher sees it
    for _ in range(60):
        if sum(1 for n, _ in events if n == "version") >= 2:
            break
        time.sleep(0.1)
    httpx.delete(f"{server}/api/w/{ws}/artifact", params={"id": "E"})
    t.join(5)
    names = [n for n, _ in events]
    assert names[0] == "hello" and "db" in names and names.count("version") >= 2 and names[-1] == "deleted"
    assert json.loads(dict(events)["version"])["latest"] != hello
