# Ritboard

A planning board for big screens. Agents post findings over MCP, sort them into a tree,
and put them on boards at exact pixel positions. You arrange and edit the same board in a
browser or a native window. Changes show up live everywhere.

## Run

```sh
uv sync
uv run ritboard serve                 # http://127.0.0.1:8765, MCP at /mcp
uv run python scripts/demo_seed.py   # optional: a demo workspace, written over MCP
```

While working on Ritboard itself, `uv run ritboard serve --dev` restarts on Python changes and
reloads open browsers when the UI files change.

On Windows, error 10013 on start means the port is in a range Hyper-V or WSL reserved.
`netsh interface ipv4 show excludedportrange protocol=tcp` lists them; pick another `--port`
(or set `RITBOARD_PORT`).

Native window instead of a browser tab (uses the system webview, not a bundled Chromium):

```sh
uv sync --extra desktop
uv run ritboard app                        # starts its own server
uv run ritboard app --url http://127.0.0.1:8765 --fullscreen   # window onto a running service
```

Install as a command: `uv tool install .` (or `uv tool install '.[desktop]'`).

## Connect agents

One endpoint for all workspaces. Every data tool takes a `workspace` argument.

```sh
claude mcp add --transport http ritboard http://127.0.0.1:8765/mcp
```

Tools: `list_workspaces`, `current_workspace`, `create_workspace`, `update_workspace`,
`get_overview`, `list_findings`, `get_finding`, `add_finding`, `add_findings`,
`update_finding`, `delete_finding`, `create_board`, `update_board`, `delete_board`,
`get_board`, `place_findings`, `remove_from_board`, `publish_artifact`, `update_artifact`,
`get_artifact`, `list_artifacts`, `upload_links`, `fill_from_local_files`, `artifact_data`, `delete_artifact`.

Claude Desktop only starts MCP servers as local commands (stdio). `ritboard mcp` is that
command: it forwards every call to the running Ritboard over its local HTTP socket, so there
is still one server and live updates still reach the board. It finds the server through
`<data>/server.json`, which `ritboard serve` writes on start, so no port goes in the config.
Run `uv run python scripts/add_to_claude_desktop.py` to add it, or add it to
`%APPDATA%\Claude\claude_desktop_config.json` yourself, then restart the app:

```json
{
  "mcpServers": {
    "ritboard": {
      "command": "uv",
      "args": ["--directory", "C:\\path\\to\\ritboard", "run", "ritboard", "mcp"]
    }
  }
}
```

If the app can't find `uv`, give its full path (`where uv` shows it). The bridge takes its
tool list from the running server, so after updating Ritboard only the server needs a
restart: the bridge tells the app the tools changed. When the server is down the tools
still list; calls then return a message saying to start it.

claude.ai on the web can't reach a server on your machine. That needs an HTTPS tunnel
and `--token`.

## Model

- **Workspace**: one project, one SQLite file in `<data>/workspaces/`. Deleting moves the file to `<data>/trash/`.
- **Finding**: an item in the tree. Kinds: `md`, `text`, `csv` (table), `html` (a one-off
  snippet in a sandboxed frame), `artifact` (shows an artifact), `url` (framed page), `image`,
  `folder`. Exists whether or not it's on a board.
- **Artifact**: a small website kept in the workspace, see below.
- **Board**: a tab with a fixed size in pixels. Change it in Settings or over MCP at any time.
  Resizing never moves cards; cards left outside are outlined and listed under *On board*.
- **Placement**: a finding on a board at `x, y, w, h`. A finding can sit on several boards.

## Screen

The board is drawn at its set size and never reflows when the window changes. A board larger
than the window pans; you can pan a little past each edge, where the darker hatched area
shows you're off the board. Pan and zoom are per screen and remembered.

Themes (Settings → This screen): dark blue-grey (default), black and light. The board has a
cutting-mat grid: fine lines every 20 px, heavier ones every 100 px, and pixel numbers along
the top and left edges. A board can still have its own background colour.

Settings → This screen also sets the size of the bars and panels (small, medium, large,
extra large) without changing the board.

With *1 board pixel = 1 device pixel* on (Settings → This screen), OS display scaling is
cancelled out, so a 3840×2160 board fills a 4K panel exactly at 150% or 200% Windows scaling.

| Key | Does |
|---|---|
| drag header | move (snaps to grid, Alt to override) |
| drag corner | resize |
| drag empty space, middle drag, Space + drag, wheel | pan |
| Shift + drag empty space | select several |
| Ctrl + wheel, `+` `-` | zoom |
| `F` / `1` | fit / actual size |
| arrows, Shift + arrows | nudge 1 px / one grid step |
| Delete | take selection off the board |
| Enter, double-click header | edit |
| `\` | findings panel |
| `H` | hide bars (move the mouse to the top edge to bring the bar back) |

Drag findings from the tree onto the board to place them, or onto another finding to nest them.

## Artifacts

An artifact is a page or small site that belongs to a workspace: `index.html` plus any
scripts, styles, data and images. Every publish is a numbered version, kept whole.

- Address: `/w/<workspace>/a/<id>/` for the latest version, `/w/<workspace>/a/<id>@3/` for
  version 3. Relative links between its files work.
- A card of kind `artifact` shows one, latest by default or pinned with `id@3`. When a new
  version is published, every card showing it reloads. The ↗ in the card header opens it
  full-window.
- Pages run sandboxed with a throwaway origin, in a card and full-window alike: scripts run
  and CDN libraries load, but they can't call Ritboard's API, and cookies and localStorage
  are off. With `--token`, artifact pages are reachable without the token; their ids are
  long and random.
- Each artifact has its own small database. Pages reach it the same way claude.ai artifacts
  do: `const db = await window.claude.use("db")`, then `db.collection("plan")` with `doc(id)`,
  `set`, `update`, `delete`, `get`, `add` and `onSnapshot`. Ritboard loads a small runtime
  into every artifact page for this. Changes are pushed to open pages as they happen; a
  page opened full-window also reloads itself when a new version is published.
  Agents read and write the same data with the `artifact_data` tool. Other claude.ai page
  features (calling Claude, user identity, file uploads) don't exist here.
- Agents use `publish_artifact` (with `html` for one page or `files` for several) and
  `update_artifact` (changed files only; the rest carry over). In the UI, make one with
  ＋ Finding → kind Artifact, edit its `index.html` from the card, and manage them in the
  Artifacts tab, where you can drag one onto the board.

Big files don't have to pass through an MCP call. An agent lists them in `shadow_files`
with their path, SHA-256 and size. The artifact is published at once, and the reply holds
one PUT link per waiting file:

```sh
curl -sS -T data/grid.bin "http://127.0.0.1:8765/w/<ws>/u/<secret>/<sha256>"
```

Ritboard checks the hash and size, and the file appears in the page as soon as it arrives.
Until then it answers 404. Files the artifact already has with the same hash are filled
in without an upload. The secret in the links is replaced on every publish of that
artifact, so only the latest links work; they also expire after 4 hours. `upload_links`
lists what's still waiting, and `renew=true` makes fresh links. Up to 64 MB per version.

Agents on the same computer that can't reach the server over HTTP, such as sandboxed
sessions that can only write to a folder, save the file there and pass its absolute path on
this computer as `local_path` in `shadow_files`, or later with `fill_from_local_files`.
Ritboard reads it from disk and checks the hash, so only files whose content the agent
already knows can be read. Allowed folders: your home folder by default when listening on
localhost only, otherwise none; set them with `--allow-files <folder>` (repeatable), or
turn it off with `--no-local-files`.

To bring in a page from elsewhere, an HTML file or a folder with `index.html`:

```sh
uv run ritboard import page.html -w my-workspace --title "My page"
uv run ritboard import site_folder -w my-workspace --board Main
uv run ritboard import page.html -w my-workspace --card f_xxxxxxxx --data docs.json
```

`--card` turns an existing card into the artifact card, keeping its place on the boards.
`--data` loads database documents: `{"collection": [{"id": "...", "data": {...}}]}`.

Claude artifacts (claude.ai links) are not supported: claude.ai refuses to be shown inside
other pages. Ritboard rejects `url` cards that point at claude.ai or claude.site with a
message telling the agent to publish the HTML as a Ritboard artifact instead. Older cards
like that show a note and an Open link.

## Service

Linux (systemd, user service):

```sh
uv tool install .
mkdir -p ~/.config/systemd/user && cp deploy/ritboard.service ~/.config/systemd/user/
systemctl --user enable --now ritboard
```

Windows: `uv tool install .`, then run `ritboard serve` from Task Scheduler at logon,
or wrap it with NSSM for a real service.

Listening beyond localhost:

```sh
ritboard serve --host 0.0.0.0 --token "$(openssl rand -hex 16)" --allowed-host board.lan:8765
```

Open the UI once with `http://board.lan:8765/?token=...`; it sets a cookie. Agents send
`Authorization: Bearer ...`.

Settings via environment: `RITBOARD_DATA`, `RITBOARD_HOST`, `RITBOARD_PORT`, `RITBOARD_TOKEN`.

## Develop

```sh
uv run pytest
```

The UI is plain JavaScript in `src/ritboard/static/`, no build step.

## License

MIT. See [LICENSE](LICENSE).
