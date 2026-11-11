"""Start a real server and drive it over HTTP, WebSocket and MCP."""

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
import websockets
from mcp import Client

from ritboard.server import Config, create_app


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config(tmp, **kw):
    return Config(data_dir=tmp / "data", workspaces_dir=tmp / "ws", file_roots=[tmp], **kw)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _port()
    app = create_app(_config(tmp_path_factory.mktemp("srv"), port=port))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/api/info")
            break
        except Exception:
            time.sleep(0.05)
    yield url
    srv.should_exit = True
    t.join(5)


def data(res):
    assert not res.is_error, res.content
    if res.structured_content is not None:
        sc = res.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


def test_mcp_flow_with_live_push(server):
    async def run():
        async with websockets.connect(server.replace("http", "ws") + "/ws") as sock:
            async with Client(server + "/mcp") as c:
                names = {t.name for t in (await c.list_tools()).tools}
                assert {"add_finding", "place_findings", "create_board"} <= names
                assert not {"link_findings", "unlink", "update_link"} & names

                ws = data(await c.call_tool("create_workspace", {"name": "GPU perf"}))
                wid = ws["workspace"]["id"]
                await sock.send(json.dumps({"subscribe": wid}))
                await asyncio.sleep(0.1)

                board = data(await c.call_tool("create_board", {"workspace": "gpu perf", "name": "Wall", "width": 7680, "height": 2160}))
                f1 = data(await c.call_tool("add_finding", {
                    "workspace": wid, "title": "NCCL stalls", "content": "# Stalls\n- allreduce", "category": "network",
                    "source": "agent-a", "board_id": board["id"], "x": 100, "y": 120,
                }))
                assert f1["placement"]["x"] == 100

                for _ in range(10):
                    msg = json.loads(await asyncio.wait_for(sock.recv(), 2))
                    if msg["workspace"] == wid:
                        break
                assert msg["workspace"] == wid and msg["type"] == "changed"

                added = data(await c.call_tool("add_findings", {
                    "workspace": wid, "board_id": board["id"],
                    "findings": [{"title": "Profile", "kind": "csv", "content": "k,v\nsm,80"}, {"title": "Doc", "kind": "html", "content": "<h1>x</h1>"}],
                }))["added"]
                view = data(await c.call_tool("get_board", {"workspace": wid, "board_id": "Wall"}))
                assert len(view["placements"]) == 3

                view = data(await c.call_tool("update_board", {"workspace": wid, "board_id": board["id"], "width": 300}))
                assert len(view["out_of_bounds"]) >= 2

                bad = await c.call_tool("get_board", {"workspace": "nope", "board_id": "x"})
                assert bad.is_error

                ov = data(await c.call_tool("get_overview", {"workspace": wid}))
                assert len(ov["findings"]) == 3 and len(ov["boards"]) == 2

        st = httpx.get(f"{server}/api/w/{wid}/state").json()
        assert len(st["placements"]) == 3 and st["settings"]["categories"]["network"]

    asyncio.run(run())


def test_rest_and_ui_assets(server):
    r = httpx.get(server + "/")
    assert r.status_code == 200 and "Ritboard" in r.text
    assert httpx.get(server + "/static/app.js").status_code == 200
    ws = httpx.post(server + "/api/workspaces", json={"name": "Rest"}).json()
    f = httpx.post(f"{server}/api/w/{ws['id']}/findings", json={"title": "t", "content": "**b**"}).json()
    assert "<strong>" in f["rendered"]
    st = httpx.get(f"{server}/api/w/{ws['id']}/state").json()
    bid = st["boards"][0]["id"]
    p = httpx.put(f"{server}/api/w/{ws['id']}/boards/{bid}/placements", json=[{"finding_id": f["id"], "x": 5, "y": 6, "w": 300, "h": 200}]).json()
    assert p[0]["x"] == 5
    assert httpx.post(f"{server}/api/w/{ws['id']}/boards/{bid}/unplace", json=[f["id"]]).json()["removed"] == 1
    assert httpx.get(f"{server}/api/w/{ws['id']}/finding", params={"id": "zzz"}).status_code == 404
    assert httpx.patch(f"{server}/api/w/{ws['id']}/finding", params={"id": "zzz"}, json={"title": "x"}).status_code == 404
    r = httpx.patch(f"{server}/api/w/{ws['id']}/finding", params={"id": f["id"]}, json={"title": "renamed"})
    assert r.json()["id"] == "renamed.md"
    assert httpx.get(f"{server}/api/w/{ws['id']}/raw", params={"path": "../secret"}).status_code == 400
    assert httpx.get(f"{server}/api/w/{ws['id']}/raw", params={"path": ".ritboard/workspace.yaml"}).status_code == 400
    raw = httpx.get(f"{server}/api/w/{ws['id']}/raw", params={"path": "renamed.md"})
    assert raw.status_code == 200 and raw.headers["content-security-policy"] == "sandbox"
    httpx.post(server + "/api/ui", json={"workspace": ws["id"], "board": bid})

    async def cur():
        async with Client(server + "/mcp") as c:
            return data(await c.call_tool("current_workspace", {}))

    assert asyncio.run(cur())["workspace"] == ws["id"]


def test_token_gate(tmp_path):
    port = _port()
    app = create_app(_config(tmp_path, host="127.0.0.1", port=port, token="s3cret"))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/api/info")
            break
        except Exception:
            time.sleep(0.05)
    try:
        assert httpx.get(url + "/api/workspaces").status_code == 401
        assert httpx.get(url + "/api/workspaces", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        r = httpx.get(url + "/?token=s3cret")
        assert r.status_code == 200 and "ritboard_token" in r.headers.get("set-cookie", "")
    finally:
        srv.should_exit = True
        t.join(5)


def test_token_gate_lets_artifact_pages_through(tmp_path):
    port = _port()
    app = create_app(_config(tmp_path, host="127.0.0.1", port=port, token="s3cret"))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}"
    auth = {"Authorization": "Bearer s3cret"}
    for _ in range(100):
        try:
            httpx.get(url + "/api/info")
            break
        except Exception:
            time.sleep(0.05)
    try:
        ws = httpx.post(url + "/api/workspaces", json={"name": "T"}, headers=auth).json()["id"]
        page = httpx.post(f"{url}/api/w/{ws}/artifacts", json={"title": "P", "files": {"index.html": "ok"}}, headers=auth).json()["artifact"]["url"]
        assert len(page.split("/")[4]) == 24  # an unguessable key, not the folder name
        assert httpx.get(url + page).text.endswith("ok")
        assert httpx.put(url + page + "_db/doc", params={"c": "p", "id": "d"}, json={"a": 1}).status_code == 200
        assert httpx.get(f"{url}/w/{ws}/a/{'0' * 24}/").status_code == 404
        assert httpx.get(f"{url}/api/w/{ws}/artifacts").status_code == 401
        assert httpx.put(f"{url}/w/{ws}/u/notatoken/{'0' * 64}", content=b"x").status_code == 404  # reached the app, not 401
    finally:
        srv.should_exit = True
        t.join(5)


def test_host_guard(server):
    # A page on another site, pointed at 127.0.0.1 by DNS rebinding, sends its own Host.
    r = httpx.get(server + "/api/workspaces", headers={"Host": "evil.example"})
    assert r.status_code == 421
    assert httpx.get(server + "/api/workspaces", headers={"Host": "localhost:1234"}).status_code == 200

    async def ws_from(origin):
        try:
            async with websockets.connect(server.replace("http", "ws") + "/ws", origin=origin) as s:
                await s.send(json.dumps({"subscribe": "x"}))
                await asyncio.wait_for(s.recv(), 0.5)
        except asyncio.TimeoutError:
            return "open"
        except Exception as e:
            return type(e).__name__
        return "open"

    assert asyncio.run(ws_from("http://evil.example")) != "open"
    assert asyncio.run(ws_from(server)) == "open"


def _wait_msg(sock, ws, timeout=6):
    async def go():
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(await asyncio.wait_for(sock.recv(), end - time.time()))
            if msg.get("workspace") == ws:
                return msg
    return go()


def test_folder_changes_are_pushed(server):
    ws = httpx.post(server + "/api/workspaces", json={"name": "Watched"}).json()
    root = __import__("pathlib").Path(ws["path"])
    bid = httpx.get(f"{server}/api/w/{ws['id']}/state").json()["boards"][0]["id"]

    async def run():
        async with websockets.connect(server.replace("http", "ws") + "/ws") as sock:
            await sock.send(json.dumps({"subscribe": ws["id"]}))
            await asyncio.sleep(1)  # let the folder watcher start on the new workspace
            (root / "from-editor.md").write_text("# written outside Ritboard\nonly here")
            msg = await _wait_msg(sock, ws["id"])
            assert msg and msg["type"] == "changed"
            st = httpx.get(f"{server}/api/w/{ws['id']}/state").json()
            assert "from-editor.md" in {f["id"] for f in st["findings"]}

            httpx.put(f"{server}/api/w/{ws['id']}/boards/{bid}/placements", json=[{"finding_id": "from-editor.md", "x": 5, "y": 6}])
            (root / "Moved").mkdir()
            (root / "from-editor.md").rename(root / "Moved" / "from-editor.md")
            for _ in range(40):
                st = httpx.get(f"{server}/api/w/{ws['id']}/state").json()
                if st["placements"] and st["placements"][0]["finding_id"] == "Moved/from-editor.md":
                    break
                await asyncio.sleep(0.1)
            assert st["placements"][0]["finding_id"] == "Moved/from-editor.md" and st["placements"][0]["x"] == 5

    asyncio.run(run())


def test_open_folder_over_rest(server, tmp_path_factory):
    folder = tmp_path_factory.mktemp("srv-open")
    (folder / "a.md").write_text("hi")
    r = httpx.post(server + "/api/workspaces/open", json={"path": str(folder)})
    assert r.status_code == 400 and "outside the folders" in r.json()["detail"]
    data_root = httpx.get(server + "/api/workspaces").json()[0]["path"]
    inside = __import__("pathlib").Path(data_root).parent / "project"
    inside.mkdir()
    (inside / "a.md").write_text("hi")
    w = httpx.post(server + "/api/workspaces/open", json={"path": str(inside)}).json()
    assert w["findings"] == 1
    assert httpx.delete(f"{server}/api/workspaces/{w['id']}").json()["folder_kept"] == str(inside.resolve())
    assert (inside / "a.md").exists()
