"""Add (or update) the Ritboard entry in Claude Desktop's MCP config.

    uv run python scripts/add_to_claude_desktop.py

Keeps every other server in the file and saves a .bak copy first. Restart Claude Desktop
afterwards (quit it fully, not just close the window).
"""

import json
import os
import shutil
import sys
from pathlib import Path


def config_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def main() -> None:
    project = Path(__file__).resolve().parent.parent
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv not found on PATH")
    entry = {"command": uv, "args": ["--directory", str(project), "run", "ritboard", "mcp"]}

    path = config_path()
    cfg = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        cfg = json.loads(text) if text.strip() else {}
        shutil.copyfile(path, path.with_name(path.name + ".bak"))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)

    servers = cfg.setdefault("mcpServers", {})
    old = servers.get("ritboard")
    servers["ritboard"] = entry
    if servers.pop("ritbord", None) is not None:  # the entry from before the rename
        print("Removed the old 'ritbord' entry")
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    print(("Updated" if old else "Added") + f" 'ritboard' in {path}")
    print(f"  command: {uv}")
    print(f"  args:    {' '.join(entry['args'])}")
    others = [k for k in servers if k != "ritboard"]
    if others:
        print(f"Kept: {', '.join(others)}")
    print("Quit Claude Desktop fully and start it again to load it.")


if __name__ == "__main__":
    main()
