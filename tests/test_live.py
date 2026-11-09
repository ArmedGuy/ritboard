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

from ritbord.server import Config, create_app


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _port()
    app = create_app(Config(data_dir=tmp_path_factory.mktemp("data"), port=port))
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
                assert {"add_finding", "place_findings", "link_findings", "create_board"} <= names

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
                link = data(await c.call_tool("link_findings", {"workspace": wid, "board_id": "Wall", "from_id": f1["id"], "to_id": added[0]["id"], "label": "explains"}))
                view = data(await c.call_tool("get_board", {"workspace": wid, "board_id": board["id"]}))
                assert len(view["placements"]) == 3 and view["links"][0]["id"] == link["id"]

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
    assert r.status_code == 200 and "Ritbord" in r.text
    assert httpx.get(server + "/static/app.js").status_code == 200
    ws = httpx.post(server + "/api/workspaces", json={"name": "Rest"}).json()
    f = httpx.post(f"{server}/api/w/{ws['id']}/findings", json={"title": "t", "content": "**b**"}).json()
    assert "<strong>" in f["rendered"]
    st = httpx.get(f"{server}/api/w/{ws['id']}/state").json()
    bid = st["boards"][0]["id"]
    p = httpx.put(f"{server}/api/w/{ws['id']}/boards/{bid}/placements", json=[{"finding_id": f["id"], "x": 5, "y": 6, "w": 300, "h": 200}]).json()
    assert p[0]["x"] == 5
    assert httpx.post(f"{server}/api/w/{ws['id']}/boards/{bid}/unplace", json=[f["id"]]).json()["removed"] == 1
    assert httpx.get(f"{server}/api/w/{ws['id']}/findings/zzz").status_code in (404, 405)
    assert httpx.patch(f"{server}/api/w/{ws['id']}/findings/zzz", json={"title": "x"}).status_code == 404
    httpx.post(server + "/api/ui", json={"workspace": ws["id"], "board": bid})

    async def cur():
        async with Client(server + "/mcp") as c:
            return data(await c.call_tool("current_workspace", {}))

    assert asyncio.run(cur())["workspace"] == ws["id"]


def test_token_gate(tmp_path):
    port = _port()
    app = create_app(Config(data_dir=tmp_path, host="127.0.0.1", port=port, token="s3cret"))
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
        assert r.status_code == 200 and "ritbord_token" in r.headers.get("set-cookie", "")
    finally:
        srv.should_exit = True
        t.join(5)
