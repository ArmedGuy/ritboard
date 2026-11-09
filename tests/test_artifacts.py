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
            assert httpx.get(f"{server}/w/{ws}/a/{a['id']}/").text == "<h1>v2</h1>"
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
    assert httpx.get(f"{server}/w/{ws}/a/{aid}@1/").text == "<p>one</p>"
    pinned = httpx.post(f"{server}/api/w/{ws}/findings", json={"title": "Pinned", "kind": "artifact", "content": f"{aid}@1"})
    assert pinned.status_code == 200
    bad = httpx.post(f"{server}/api/w/{ws}/findings", json={"title": "Bad", "kind": "artifact", "content": f"{aid}@9"})
    assert bad.status_code == 400
