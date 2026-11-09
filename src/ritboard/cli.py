"""Command line: `ritboard serve` runs the service, `ritboard app` opens a native window."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from platformdirs import user_data_dir


# Environment variables from before the rename (RITBORD_*) still work.
for _k, _v in list(os.environ.items()):
    if _k.startswith("RITBORD_"):
        os.environ.setdefault("RITBOARD_" + _k[len("RITBORD_"):], _v)


def default_data_dir() -> Path:
    if os.environ.get("RITBOARD_DATA"):
        return Path(os.environ["RITBOARD_DATA"])
    new = Path(user_data_dir("ritboard", appauthor=False))
    old = Path(user_data_dir("ritbord", appauthor=False))  # name before the rename
    if old.is_dir() and not new.exists():
        try:
            old.rename(new)
            print(f"Moved data from {old} to {new}", file=sys.stderr)
        except OSError as e:  # e.g. an old server still has the files open
            print(f"Using {old}; could not move it to {new} ({e.strerror or e})", file=sys.stderr)
            return old
    return new


def _serve_args(p: argparse.ArgumentParser, default_host: str = "127.0.0.1", default_port: int = 8765) -> None:
    p.add_argument("--host", default=os.environ.get("RITBOARD_HOST", default_host))
    p.add_argument("--port", type=int, default=int(os.environ.get("RITBOARD_PORT", default_port)))
    p.add_argument("--data", type=Path, default=default_data_dir(), help="data folder (default: %(default)s)")
    p.add_argument(
        "--token",
        default=os.environ.get("RITBOARD_TOKEN"),
        help="require this token for the API, MCP and UI (use when listening beyond localhost)",
    )
    p.add_argument(
        "--allow-files",
        action="append",
        type=Path,
        dest="file_roots",
        default=None,
        help="folder agents may fill artifact files from with local_path (repeatable; "
        "default: your home folder when listening on localhost only)",
    )
    p.add_argument("--no-local-files", action="store_true", help="don't let agents fill artifact files from local paths")
    p.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        dest="allowed_hosts",
        help="Host header allowed for MCP requests, e.g. board.lan:8765 (repeatable)",
    )


def _file_roots(args) -> list[Path] | None:
    if args.no_local_files:
        return []
    return args.file_roots  # None means the default


def _build(args):
    from .server import Config, create_app

    cfg = Config(
        data_dir=args.data, host=args.host, port=args.port, token=args.token, allowed_hosts=args.allowed_hosts,
        file_roots=_file_roots(args),
    )
    return cfg, create_app(cfg)


def _check_port(host: str, port: int) -> None:
    """Fail early with a readable message instead of a bare socket error."""
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(fam, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError as e:
            hint = ""
            if sys.platform == "win32" and getattr(e, "winerror", None) == 10013:
                hint = (
                    "\nWindows has reserved this port (often Hyper-V or WSL). See the blocked ranges with:\n"
                    "  netsh interface ipv4 show excludedportrange protocol=tcp\n"
                    "and pick a port outside them, e.g. --port 5170."
                )
            elif getattr(e, "errno", None) in (98, 48) or getattr(e, "winerror", None) == 10048:
                hint = "\nSomething is already listening there. Stop it or pick another --port."
            raise SystemExit(f"Can't listen on {host}:{port}: {e.strerror or e}{hint}")


def cmd_serve(args) -> None:
    import uvicorn

    _check_port(args.host, args.port)

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        print("warning: listening beyond localhost without --token; anyone on the network can edit boards", file=sys.stderr)
    print(f"Ritboard on http://{args.host}:{args.port}  MCP: http://{args.host}:{args.port}/mcp  data: {args.data}")
    if args.dev:
        # Restart on Python changes; open browsers reload on UI changes.
        os.environ.update(
            RITBOARD_DATA=str(args.data),
            RITBOARD_HOST=args.host,
            RITBOARD_PORT=str(args.port),
            RITBOARD_TOKEN=args.token or "",
            RITBOARD_ALLOWED_HOSTS=",".join(args.allowed_hosts),
            RITBOARD_DEV="1",
            RITBOARD_FILE_ROOTS="none" if args.no_local_files else os.pathsep.join(str(p) for p in args.file_roots or []),
        )
        uvicorn.run(
            "ritboard.server:app_from_env",
            factory=True,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            reload=True,
            reload_dirs=[str(Path(__file__).parent)],
        )
        return
    _, app = _build(args)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


def _free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _wait_up(url: str, timeout: float = 15) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url + "/api/info", timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            time.sleep(0.1)
    raise SystemExit(f"server at {url} did not come up")


def cmd_app(args) -> None:
    try:
        import webview  # pywebview
    except ImportError:
        raise SystemExit("Desktop mode needs pywebview: uv sync --extra desktop  (or: uv tool install 'ritboard[desktop]')")

    if not args.url and args.port:
        # A service already running on this port? Use it instead of starting a second one.
        try:
            urllib.request.urlopen(f"http://{args.host}:{args.port}/api/info", timeout=0.5)
            args.url = f"http://{args.host}:{args.port}"
        except urllib.error.HTTPError:  # answered, e.g. 401 behind a token
            args.url = f"http://{args.host}:{args.port}"
        except Exception:
            pass
    if args.url:
        url = args.url.rstrip("/")
    else:
        import uvicorn

        if args.port == 0:
            args.port = _free_port(args.host)
        _, app = _build(args)
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        url = f"http://{args.host}:{args.port}"
        print(f"Ritboard on {url}  MCP: {url}/mcp")
    _wait_up(url)
    if args.token:
        url += f"/?token={args.token}"
    webview.create_window(
        "Ritboard",
        url,
        width=args.width,
        height=args.height,
        fullscreen=args.fullscreen,
        background_color="#0f141b",
        text_select=True,
    )
    webview.start(private_mode=False)


def _locate(args) -> tuple[str, str | None]:
    """The running server's address and token, from --url/--token or <data>/server.json."""
    import json

    if args.url:
        return args.url.rstrip("/"), args.token
    try:
        info = json.loads((args.data_dir / "server.json").read_text())
        return info["url"], args.token or info.get("token")
    except (OSError, ValueError, KeyError):
        raise SystemExit(f"No running Ritboard found (no {args.data_dir / 'server.json'}). Start `ritboard serve`, or pass --url.")


def cmd_import(args) -> None:
    """Publish a local HTML file, or a folder of files, as an artifact on the running server."""
    import base64
    import json
    import urllib.error

    url, token = _locate(args)
    src: Path = args.path
    if not src.exists():
        raise SystemExit(f"{src} does not exist")
    if src.is_dir():
        paths = [p for p in sorted(src.rglob("*")) if p.is_file()]
        rel = {p: p.relative_to(src).as_posix() for p in paths}
        entry = args.entry or "index.html"
    else:
        paths, rel = [src], {src: "index.html"}
        entry = "index.html"
    files, files_b64 = {}, {}
    for p in paths:
        data = p.read_bytes()
        try:
            files[rel[p]] = data.decode("utf-8")
        except UnicodeDecodeError:
            files_b64[rel[p]] = base64.b64encode(data).decode()
    title = args.title or (src.stem if src.is_file() else src.name)

    def call(method, path, body=None):
        req = urllib.request.Request(url + path, method=method, data=None if body is None else json.dumps(body).encode())
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read()
                return json.loads(text) if text else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise SystemExit(f"{method} {path} failed ({e.code}): {detail}")

    ws = args.workspace
    out = call("POST", f"/api/w/{ws}/artifacts", {
        "title": title, "files": files, "files_b64": files_b64, "entry": entry, "source": "import",
        "add_card": not args.card, "board_id": args.board,
    })
    aid = out["artifact"]["id"]
    if args.card:
        call("PATCH", f"/api/w/{ws}/findings/{args.card}", {"kind": "artifact", "content": aid})
    n = 0
    if args.data:
        # {"collection": [{"id": "...", "data": {...}}, ...], ...}
        for coll, docs in json.loads(Path(args.data).read_text(encoding="utf-8")).items():
            for d in docs:
                q = urllib.parse.urlencode({"c": coll, "id": d["id"]})
                call("PUT", f"/w/{ws}/a/{aid}/_db/doc?{q}", d["data"])
                n += 1
    print(f"Published {title!r} as {aid} ({len(paths)} file(s)) in {ws}"
          + (f", card {args.card} now shows it" if args.card else "")
          + (f", {n} database document(s) imported" if n else ""))
    print(f"  {url}/w/{ws}/a/{aid}/")


def cmd_mcp(args) -> None:
    import asyncio
    import json

    from .stdio_bridge import run_bridge

    url, token = args.url, args.token

    def locate():
        # A running server leaves its address in <data>/server.json.
        try:
            info = json.loads((args.data / "server.json").read_text())
            return info["url"], info.get("token")
        except (OSError, ValueError, KeyError):
            return f"http://127.0.0.1:{os.environ.get('RITBOARD_PORT', 8765)}", None

    asyncio.run(run_bridge(url, token, locate=None if url else locate))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ritboard", description="Big-screen planning board with an MCP endpoint")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the server (for a service or a browser)")
    _serve_args(s)
    s.add_argument("--log-level", default="info")
    s.add_argument("--dev", action="store_true", help="restart on code changes and reload open browsers on UI changes")
    s.set_defaults(func=cmd_serve)

    a = sub.add_parser("app", help="open a native window (starts a server unless --url is given)")
    _serve_args(a)
    a.add_argument("--url", help="connect to an already running server instead of starting one")
    a.add_argument("--fullscreen", action="store_true")
    a.add_argument("--width", type=int, default=1600)
    a.add_argument("--height", type=int, default=1000)
    a.set_defaults(func=cmd_app)

    m = sub.add_parser("mcp", help="stdio MCP server that forwards to a running Ritboard (for Claude Desktop)")
    m.add_argument(
        "--url",
        default=os.environ.get("RITBOARD_URL"),
        help="address of the running server (default: found via <data>/server.json)",
    )
    m.add_argument("--token", default=os.environ.get("RITBOARD_TOKEN"))
    m.add_argument("--data", type=Path, default=default_data_dir(), help="data folder of the server to find")
    m.set_defaults(func=cmd_mcp)

    i = sub.add_parser("import", help="publish a local HTML file or folder as an artifact on the running server")
    i.add_argument("path", type=Path, help="an .html file, or a folder with index.html and its other files")
    i.add_argument("--workspace", "-w", required=True)
    i.add_argument("--title")
    i.add_argument("--entry", help="entry page inside a folder (default index.html)")
    i.add_argument("--board", help="also place a new card on this board (id or name)")
    i.add_argument("--card", help="turn this existing card (finding id) into the artifact card instead of adding one")
    i.add_argument("--data", help='JSON file with database documents: {"collection": [{"id": ..., "data": {...}}]}')
    i.add_argument("--url", default=os.environ.get("RITBOARD_URL"))
    i.add_argument("--token", default=os.environ.get("RITBOARD_TOKEN"))
    i.add_argument("--data-dir", dest="data_dir", type=Path, default=default_data_dir())
    i.set_defaults(func=cmd_import)

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("serve", "app", "mcp", "import", "-h", "--help"):
        argv.insert(0, "serve")
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
