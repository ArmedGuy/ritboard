# Ritbord

A planning board for big screens. Agents post findings over MCP, sort them into a tree,
put them on boards at exact pixel positions, and link them. You arrange and edit the same
board in a browser or a native window. Changes show up live everywhere.

## Run

```sh
uv sync
uv run ritbord serve                 # http://127.0.0.1:8765, MCP at /mcp
uv run python scripts/demo_seed.py   # optional: a demo workspace, written over MCP
```

Native window instead of a browser tab (uses the system webview, not a bundled Chromium):

```sh
uv sync --extra desktop
uv run ritbord app                        # starts its own server
uv run ritbord app --url http://127.0.0.1:8765 --fullscreen   # window onto a running service
```

Install as a command: `uv tool install .` (or `uv tool install '.[desktop]'`).

## Connect agents

One endpoint for all workspaces. Every data tool takes a `workspace` argument.

```sh
claude mcp add --transport http ritbord http://127.0.0.1:8765/mcp
```

Tools: `list_workspaces`, `current_workspace`, `create_workspace`, `update_workspace`,
`get_overview`, `list_findings`, `get_finding`, `add_finding`, `add_findings`,
`update_finding`, `delete_finding`, `create_board`, `update_board`, `delete_board`,
`get_board`, `place_findings`, `remove_from_board`, `link_findings`, `update_link`, `unlink`.

claude.ai can't reach a server on your LAN. To connect it there you need an HTTPS tunnel
and `--token`.

## Model

- **Workspace**: one project, one SQLite file in `<data>/workspaces/`. Deleting moves the file to `<data>/trash/`.
- **Finding**: an item in the tree. Kinds: `md`, `text`, `csv` (table), `html` (sandboxed iframe),
  `url` (framed page), `image`, `folder`. Exists whether or not it's on a board.
- **Board**: a tab with a fixed size in pixels. Change it in Settings or over MCP at any time.
  Resizing never moves cards; cards left outside are outlined and listed under *On board*.
- **Placement**: a finding on a board at `x, y, w, h`. A finding can sit on several boards.
- **Link**: an arrow between two findings on one board, with an optional label.

## Screen

The board is drawn at its set size and never reflows when the window changes. Pan and zoom
are per screen and remembered.

With *1 board pixel = 1 device pixel* on (Settings → This screen), OS display scaling is
cancelled out, so a 3840×2160 board fills a 4K panel exactly at 150% or 200% Windows scaling.

| Key | Does |
|---|---|
| drag header | move (snaps to grid, Alt to override) |
| drag corner | resize |
| drag the dot on a card's right edge | link to another card |
| drag in empty space | select several |
| Space + drag, middle drag, wheel | pan |
| Ctrl + wheel, `+` `-` | zoom |
| `F` / `1` | fit / actual size |
| arrows, Shift + arrows | nudge 1 px / one grid step |
| Delete | take selection off the board (or delete the selected link) |
| Enter, double-click header | edit |
| `\` | findings panel |
| `H` | hide bars (move the mouse to the top edge to bring the bar back) |

Drag findings from the tree onto the board to place them, or onto another finding to nest them.

## Claude artifacts

A published Claude artifact can be framed with its embed URL (`kind: url`) if this host is
in the artifact's allowed domains. More reliable: have the agent send the HTML itself
(`kind: html`).

## Service

Linux (systemd, user service):

```sh
uv tool install .
mkdir -p ~/.config/systemd/user && cp deploy/ritbord.service ~/.config/systemd/user/
systemctl --user enable --now ritbord
```

Windows: `uv tool install .`, then run `ritbord serve` from Task Scheduler at logon,
or wrap it with NSSM for a real service.

Listening beyond localhost:

```sh
ritbord serve --host 0.0.0.0 --token "$(openssl rand -hex 16)" --allowed-host board.lan:8765
```

Open the UI once with `http://board.lan:8765/?token=...`; it sets a cookie. Agents send
`Authorization: Bearer ...`.

Settings via environment: `RITBORD_DATA`, `RITBORD_HOST`, `RITBORD_PORT`, `RITBORD_TOKEN`.

## Develop

```sh
uv run pytest
```

The UI is plain JavaScript in `src/ritbord/static/`, no build step.
