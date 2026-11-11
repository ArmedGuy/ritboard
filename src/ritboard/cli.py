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


def default_workspaces_dir() -> Path:
    return Path(os.environ["RITBOARD_WORKSPACES"]) if os.environ.get("RITBOARD_WORKSPACES") else Path.home() / "Ritboard"


def _data_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data", type=Path, default=default_data_dir(), help="settings folder (default: %(default)s)")
    p.add_argument(
        "--workspaces-dir", type=Path, default=default_workspaces_dir(),
        help="where new workspaces go when no folder is given (default: %(default)s)",
    )


def _serve_args(p: argparse.ArgumentParser, default_host: str = "127.0.0.1", default_port: int = 8765) -> None:
    p.add_argument("--host", default=os.environ.get("RITBOARD_HOST", default_host))
    p.add_argument("--port", type=int, default=int(os.environ.get("RITBOARD_PORT", default_port)))
    _data_args(p)
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
        file_roots=_file_roots(args), workspaces_dir=args.workspaces_dir,
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
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allowed_hosts:
        print("note: pass --allowed-host <name:port> for each name the board is reached by, so other sites "
              "can't reach it through DNS rebinding", file=sys.stderr)
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
            RITBOARD_WORKSPACES=str(args.workspaces_dir),
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


def _registry(args):
    from .store import Registry

    return Registry(args.data, default_root=args.workspaces_dir)


def cmd_open(args) -> None:
    """Add a folder to the workspace list. Works with or without the server running."""
    st = _registry(args).open(args.folder.expanduser().resolve(), args.name)
    print(f"Workspace {st.name!r} ({st.id}) is {st.root}")


def cmd_list(args) -> None:
    for w in _registry(args).list():
        flag = "  (folder missing)" if w.get("missing") else ""
        print(f"{w['id']:24} {w['name']:28} {w['path']}{flag}")


def cmd_forget(args) -> None:
    path = _registry(args).remove(args.workspace)
    print(f"Removed from the list. The folder stays: {path}")


def cmd_migrate(args) -> None:
    from .migrate import migrate_sqlite

    lines = migrate_sqlite(_registry(args))
    print("\n".join(lines) if lines else f"Nothing to convert in {args.data / 'workspaces'}.")


def cmd_import(args) -> None:
    """Copy a local HTML file, or a folder of files, into a workspace as an artifact."""
    import json

    src: Path = args.path
    if not src.exists():
        raise SystemExit(f"{src} does not exist")
    if src.is_dir():
        paths = [p for p in sorted(src.rglob("*")) if p.is_file() and not any(s.startswith(".") for s in p.relative_to(src).parts)]
        files = {p.relative_to(src).as_posix(): p.read_bytes() for p in paths}
        if args.entry and args.entry != "index.html" and "index.html" not in files:
            files["index.html"] = files[args.entry]
    else:
        files = {"index.html": src.read_bytes()}
    title = args.title or (src.stem if src.is_file() else src.name)
    st = _registry(args).get(args.workspace)
    a = st.create_artifact(title, files, source="import", parent_id=args.folder)
    aid = a["id"]
    note = ""
    if args.card:
        old = st.get_finding(args.card)["id"]
        moved = [p for p in st.placements() if p["finding_id"] == old]
        for p in moved:
            st.place(p["board_id"], [{"finding_id": aid, **{k: p[k] for k in ("x", "y", "w", "h", "z")}}])
        st.delete_finding(old)
        note = f", in place of {old} on {len(moved)} board(s)"
    elif args.board:
        st.place(args.board, [{"finding_id": aid}])
    n = 0
    if args.data_file:
        # {"collection": [{"id": "...", "data": {...}}, ...], ...}
        for coll, docs in json.loads(Path(args.data_file).read_text(encoding="utf-8")).items():
            for d in docs:
                st.db_write(aid, coll, d["id"], d["data"])
                n += 1
    print(f"Copied {title!r} to {st.root / aid} ({len(files)} file(s)){note}"
          + (f", {n} database document(s)" if n else ""))


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

    asyncio.run(run_bridge(url, token, locate=None if url else locate, data_dir=args.data, workspaces_dir=args.workspaces_dir))


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

    m = sub.add_parser("mcp", help="stdio MCP server for Claude Desktop: forwards to a running Ritboard, or works on the files")
    m.add_argument(
        "--url",
        default=os.environ.get("RITBOARD_URL"),
        help="address of the running server (default: found via <data>/server.json)",
    )
    m.add_argument("--token", default=os.environ.get("RITBOARD_TOKEN"))
    _data_args(m)
    m.set_defaults(func=cmd_mcp)

    o = sub.add_parser("open", help="use a folder as a workspace (adds it to the list)")
    o.add_argument("folder", type=Path)
    o.add_argument("--name")
    _data_args(o)
    o.set_defaults(func=cmd_open)

    ls = sub.add_parser("list", help="list workspaces and their folders")
    _data_args(ls)
    ls.set_defaults(func=cmd_list)

    fg = sub.add_parser("forget", help="remove a workspace from the list; its folder stays")
    fg.add_argument("workspace")
    _data_args(fg)
    fg.set_defaults(func=cmd_forget)

    mg = sub.add_parser("migrate", help="convert workspaces from older versions (SQLite) into folders")
    _data_args(mg)
    mg.set_defaults(func=cmd_migrate)

    i = sub.add_parser("import", help="copy a local HTML file or folder into a workspace as an artifact")
    i.add_argument("path", type=Path, help="an .html file, or a folder with index.html and its other files")
    i.add_argument("--workspace", "-w", required=True)
    i.add_argument("--title")
    i.add_argument("--folder", help="workspace folder to put it in (a path inside the workspace)")
    i.add_argument("--entry", help="entry page inside a folder, copied to index.html if that is missing")
    i.add_argument("--board", help="also place it on this board (id or name)")
    i.add_argument("--card", help="put it where this finding is on the boards, and delete that finding")
    i.add_argument("--data", dest="data_file", help='JSON file with database documents: {"collection": [{"id": ..., "data": {...}}]}')
    i.add_argument("--data-dir", dest="data", type=Path, default=default_data_dir())
    i.add_argument("--workspaces-dir", type=Path, default=default_workspaces_dir())
    i.set_defaults(func=cmd_import)

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("serve", "app", "mcp", "open", "list", "forget", "migrate", "import", "-h", "--help"):
        argv.insert(0, "serve")
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
