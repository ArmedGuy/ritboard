"""Command line: `ritbord serve` runs the service, `ritbord app` opens a native window."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from platformdirs import user_data_dir


def default_data_dir() -> Path:
    return Path(os.environ.get("RITBORD_DATA") or user_data_dir("ritbord", appauthor=False))


def _serve_args(p: argparse.ArgumentParser, default_host: str = "127.0.0.1", default_port: int = 8765) -> None:
    p.add_argument("--host", default=os.environ.get("RITBORD_HOST", default_host))
    p.add_argument("--port", type=int, default=int(os.environ.get("RITBORD_PORT", default_port)))
    p.add_argument("--data", type=Path, default=default_data_dir(), help="data folder (default: %(default)s)")
    p.add_argument(
        "--token",
        default=os.environ.get("RITBORD_TOKEN"),
        help="require this token for the API, MCP and UI (use when listening beyond localhost)",
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        dest="allowed_hosts",
        help="Host header allowed for MCP requests, e.g. board.lan:8765 (repeatable)",
    )


def _build(args):
    from .server import Config, create_app

    cfg = Config(
        data_dir=args.data, host=args.host, port=args.port, token=args.token, allowed_hosts=args.allowed_hosts
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
    print(f"Ritbord on http://{args.host}:{args.port}  MCP: http://{args.host}:{args.port}/mcp  data: {args.data}")
    if args.dev:
        # Restart on Python changes; open browsers reload on UI changes.
        os.environ.update(
            RITBORD_DATA=str(args.data),
            RITBORD_HOST=args.host,
            RITBORD_PORT=str(args.port),
            RITBORD_TOKEN=args.token or "",
            RITBORD_ALLOWED_HOSTS=",".join(args.allowed_hosts),
            RITBORD_DEV="1",
        )
        uvicorn.run(
            "ritbord.server:app_from_env",
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
        raise SystemExit("Desktop mode needs pywebview: uv sync --extra desktop  (or: uv tool install 'ritbord[desktop]')")

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
        print(f"Ritbord on {url}  MCP: {url}/mcp")
    _wait_up(url)
    if args.token:
        url += f"/?token={args.token}"
    webview.create_window(
        "Ritbord",
        url,
        width=args.width,
        height=args.height,
        fullscreen=args.fullscreen,
        background_color="#0f141b",
        text_select=True,
    )
    webview.start(private_mode=False)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ritbord", description="Big-screen planning board with an MCP endpoint")
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

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("serve", "app", "-h", "--help"):
        argv.insert(0, "serve")
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
