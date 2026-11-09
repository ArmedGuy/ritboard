import asyncio
import base64

import httpx
from mcp import Client

from test_live import data, server  # noqa: F401  (fixture)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_artifact_lifecycle(server):  # noqa: F811
    async def run():
        async with Client(server + "/mcp") as c:
            ws = data(await c.call_tool("create_workspace", {"name": "Arts"}))["workspace"]["id"]
            board = data(await c.call_tool("get_overview", {"workspace": ws}))["boards"][0]["id"]
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
            assert a["latest"] == 1 and len(a["files"]) == 4 and out["placement"]["x"] == 100
            assert a["url"].endswith(f"/w/{ws}/a/{a['id']}/")

            r = httpx.get(f"{server}/w/{ws}/a/{a['id']}", follow_redirects=False)
            assert r.status_code == 308 and r.headers["location"].endswith("/")
            page = httpx.get(f"{server}/w/{ws}/a/{a['id']}/")
            assert page.status_code == 200 and "app.js" in page.text
            assert "sandbox allow-scripts" in page.headers["content-security-policy"]
            assert "allow-same-origin" not in page.headers["content-security-policy"]
            assert page.headers["content-type"].startswith("text/html")
            js = httpx.get(f"{server}/w/{ws}/a/{a['id']}/app.js")
            assert js.headers["content-type"].startswith("text/javascript")
            assert httpx.get(f"{server}/w/{ws}/a/{a['id']}/img/dot.png").content == PNG
            assert httpx.get(f"{server}/w/{ws}/a/{a['id']}/nope.txt").status_code == 404
            assert httpx.get(f"{server}/w/{ws}/a/{a['id']}/../../api/workspaces").status_code in (404, 400)

            v2 = data(await c.call_tool("update_artifact", {
                "workspace": ws, "artifact_id": a["id"], "html": "<h1>v2</h1>", "remove": ["app.js"], "note": "simpler",
            }))
            assert v2["latest"] == 2 and {f["path"] for f in v2["files"]} == {"index.html", "data/rows.json", "img/dot.png"}
            assert httpx.get(f"{server}/w/{ws}/a/{a['id']}/").text.endswith("<h1>v2</h1>")
            old = httpx.get(f"{server}/w/{ws}/a/{a['id']}@1/app.js")
            assert old.status_code == 200 and "immutable" in old.headers["cache-control"]

            got = data(await c.call_tool("get_artifact", {"workspace": ws, "artifact_id": a["id"], "version": 1}))
            png = next(f for f in got["files"] if f["path"] == "img/dot.png")
            assert png["encoding"] == "base64" and base64.b64decode(png["content"]) == PNG
            assert [v["version"] for v in got["versions"]] == [1, 2]

            bad = await c.call_tool("publish_artifact", {"workspace": ws, "title": "x", "files": [{"path": "../evil", "content": "x"}]})
            assert bad.is_error
            nf = await c.call_tool("publish_artifact", {"workspace": ws, "title": "x", "files": [{"path": "a.html", "content": "x"}]})
            assert nf.is_error  # no index.html

            st = httpx.get(f"{server}/api/w/{ws}/state").json()
            card = next(f for f in st["findings"] if f["kind"] == "artifact")
            assert card["content"] == a["id"] and st["artifacts"][0]["latest"] == 2

            gone = data(await c.call_tool("delete_artifact", {"workspace": ws, "artifact_id": a["id"]}))
            assert gone["cards_removed"] == 1
            assert httpx.get(f"{server}/w/{ws}/a/{a['id']}/").status_code == 404

    asyncio.run(run())


def test_artifact_from_ui_api(server):  # noqa: F811
    ws = httpx.post(server + "/api/workspaces", json={"name": "Arts UI"}).json()["id"]
    out = httpx.post(f"{server}/api/w/{ws}/artifacts", json={"title": "Note", "files": {"index.html": "<p>one</p>"}}).json()
    aid = out["artifact"]["id"]
    assert out["finding"]["kind"] == "artifact"
    v = httpx.post(f"{server}/api/w/{ws}/artifacts/{aid}/versions", json={"files": {"index.html": "<p>two</p>"}}).json()
    assert v["latest"] == 2
    assert httpx.get(f"{server}/w/{ws}/a/{aid}@1/").text.endswith("<p>one</p>")
    pinned = httpx.post(f"{server}/api/w/{ws}/findings", json={"title": "Pinned", "kind": "artifact", "content": f"{aid}@1"})
    assert pinned.status_code == 200
    bad = httpx.post(f"{server}/api/w/{ws}/findings", json={"title": "Bad", "kind": "artifact", "content": f"{aid}@9"})
    assert bad.status_code == 400


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
            pub = data(await c.call_tool("publish_artifact", {"workspace": ws, "title": "p", "html": "<p>hi</p>", "add_card": False}))
            moved = data(await c.call_tool("update_finding", {
                "workspace": ws, "finding_id": ok["id"], "kind": "artifact", "content": pub["artifact"]["id"]}))
            assert moved["kind"] == "artifact"

    asyncio.run(run())


def test_artifact_database(server):  # noqa: F811
    async def run():
        async with Client(server + "/mcp") as c:
            ws = data(await c.call_tool("create_workspace", {"name": "Db"}))["workspace"]["id"]
            aid = data(await c.call_tool("publish_artifact", {"workspace": ws, "title": "p", "html": "<head></head><p>x</p>"}))["artifact"]["id"]
            page = httpx.get(f"{server}/w/{ws}/a/{aid}/").text
            assert f'<script src="/w/{ws}/a/{aid}/_ritboard.js"></script>' in page
            assert "window.claude" in httpx.get(f"{server}/w/{ws}/a/{aid}/_ritboard.js").text

            base = f"{server}/w/{ws}/a/{aid}/_db"
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
            assert httpx.get(f"{server}/w/{ws}/a/{a['id']}/grid.bin").status_code == 404

            bad = httpx.put(url, content=b"nope")
            assert bad.status_code == 400 and "does not match" in bad.json()["detail"]
            ok = httpx.put(url, content=big).json()
            assert sorted(ok["stored"]) == ["copy/grid.bin", "grid.bin"] and ok["still_waiting"] == 0
            assert httpx.get(f"{server}/w/{ws}/a/{a['id']}/copy/grid.bin").content == big

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
