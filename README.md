# Ritboard

A planning board for big screens. Agents post findings over MCP, sort them into a tree,
and put them on boards at exact pixel positions. You arrange and edit the same board in a
browser or a native window. Changes show up live everywhere.

A workspace is an ordinary folder: findings are Markdown, CSV, HTML and other files, folders
make the tree, and boards are small YAML files. It works like an Obsidian vault. Keep it
in git, edit it in any editor, and the board follows.

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

New workspaces go in `~/Ritboard/<name>` (change with `--workspaces-dir` or `RITBOARD_WORKSPACES`).
Any other folder can be one too:

```sh
uv run ritboard open ~/Projects/gpu-perf     # or "Open folder…" in the workspace menu
uv run ritboard list                         # workspaces and their folders
uv run ritboard forget gpu-perf              # off the list; the folder stays
```

The list of workspaces is `workspaces.yaml` in Ritboard's settings folder
(`%LOCALAPPDATA%\ritboard` on Windows, `~/.local/share/ritboard` on Linux).

## Connect agents

One endpoint for all workspaces. Every data tool takes a `workspace` argument.

```sh
claude mcp add --transport http ritboard http://127.0.0.1:8765/mcp
```

Tools: `list_workspaces`, `current_workspace`, `create_workspace`, `open_workspace`, `update_workspace`,
`get_overview`, `list_findings`, `get_finding`, `add_finding`, `add_findings`,
`update_finding`, `delete_finding`, `create_board`, `update_board`, `delete_board`,
`get_board`, `place_findings`, `remove_from_board`, `publish_artifact`, `update_artifact`,
`get_artifact`, `list_artifacts`, `upload_links`, `fill_from_local_files`, `artifact_data`, `delete_artifact`.

Claude Desktop only starts MCP servers as local commands (stdio). `ritboard mcp` is that
command: it forwards every call to the running Ritboard over its local HTTP socket, so there
is still one server and live updates still reach the board. It finds the server through
`<data>/server.json`, which `ritboard serve` writes on start, so no port goes in the config.
When no server is running, it runs the tools itself, straight on the workspace folders;
boards that are open later show the changes.
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
restart: the bridge tells the app the tools changed.

Agents that can write files (Claude Code in the workspace folder, say) don't need the tools
for content at all: a file they write appears in the tree and on boards within a moment.

claude.ai on the web can't reach a server on your machine. That needs an HTTPS tunnel
and `--token`.

## Model

```
gpu-perf/                       the workspace folder
  .ritboard/
    workspace.yaml              name, default board size, grid, category colours
    boards/overview.yaml        one file per board: size and placed cards
    meta.yaml                   title, category, tags for files that can't hold them
    .gitignore                  keeps local/ out of git
    local/                      this computer only: upload links, rename index, trash
  Profiling/                    folders are the tree
    Allreduce stalls.md         a finding
    Per-node bandwidth.csv
  Sun map/                      a folder with index.html is an artifact
    index.html
    data/grid.bin
    .data/plan.json             the artifact's database
```

- **Workspace**: one folder, anywhere. Hidden files and folders (`.git`, `.obsidian`) and
  `node_modules` are left out; up to 5000 files and folders are shown.
- **Finding**: a file or folder. Its id is its path in the workspace (`Profiling/Allreduce
  stalls.md`), and its kind follows the extension: `.md` Markdown, `.txt` and code or data
  files as text, `.csv`/`.tsv` as a table, `.html` as a sandboxed snippet, `.url` (a Windows
  internet shortcut) as a framed page, images, folders, artifacts, and anything else as a
  file you can open. Its title is the file name unless metadata says otherwise.
- **Metadata**: Markdown keeps it in front matter, which Ritboard reads and writes without
  touching keys it doesn't know:
  ```markdown
  ---
  category: network
  tags: [nccl]
  source: profiler-agent
  ---
  ```
  HTML keeps it in a first-line `<!--ritboard ... -->` comment. Everything else uses
  `.ritboard/meta.yaml`.
- **Board**: a tab with a fixed size in pixels, stored as `.ritboard/boards/<id>.yaml` with one
  line per card: `Profiling/Allreduce stalls.md: [80, 80, 460, 260, 1]` (x, y, w, h, z).
  Change the size in Settings or over MCP at any time. Resizing never moves cards; cards left
  outside are outlined and listed under *On board*. A finding can sit on several boards.
- **Renames**: renaming or moving a finding in Ritboard renames or moves its file and updates
  the boards. When something else does it (Explorer, git, an editor), Ritboard recognises
  the file by its content and does the same, also for renames made while it wasn't running.
  A card whose file is gone shows as missing; Delete takes it off the board.
- **Deleting** a finding moves its file to `.ritboard/local/trash/`.
- **Live**: the server watches every workspace folder. Changes from anywhere reach open
  boards within a moment. Set `RITBOARD_POLL=1` if the folder is on a network drive or in
  WSL, where change events don't arrive.

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

An artifact is a page or small site in a workspace: a folder with `index.html` plus any
scripts, styles, data and images. There are no versions inside Ritboard; the folder's git
history is the history.

- Address: `/w/<workspace>/a/<key>/`, where the key is made from the folder path and a
  secret kept in Ritboard's settings folder, so it can't be guessed. Relative links between
  its files work. Renaming the folder changes the address.
- Placed on a board, an artifact shows the page. When any of its files changes, from Ritboard
  or from an editor, every card showing it reloads. The ↗ in the card header opens it
  full-window.
- Pages run sandboxed with a throwaway origin, in a card and full-window alike: scripts run
  and CDN libraries load, but they can't call Ritboard's API, and cookies and localStorage
  are off. With `--token`, artifact pages are reachable without the token; the key protects
  them.
- Each artifact has its own small database. Pages reach it the same way claude.ai artifacts
  do: `const db = await window.claude.use("db")`, then `db.collection("plan")` with `doc(id)`,
  `set`, `update`, `delete`, `get`, `add` and `onSnapshot`. Ritboard loads a small runtime
  into every artifact page for this. The data is JSON, one file per collection in the
  artifact's `.data` folder. Changes are pushed to open pages as they happen; a page opened
  full-window also reloads itself when its files change.
  Agents read and write the same data with the `artifact_data` tool. Other claude.ai page
  features (calling Claude, user identity, file uploads) don't exist here.
- Agents use `publish_artifact` (with `html` for one page or `files` for several) and
  `update_artifact` (changed files only; the rest stay). In the UI, make one with
  ＋ Finding → kind Artifact, edit its `index.html` from the card, and find them all in the
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
lists what's still waiting, and `renew=true` makes fresh links. Up to 64 MB per artifact.

Agents on the same computer that can't reach the server over HTTP, such as sandboxed
sessions that can only write to a folder, save the file there and pass its absolute path on
this computer as `local_path` in `shadow_files`, or later with `fill_from_local_files`.
Ritboard reads it from disk and checks the hash, so only files whose content the agent
already knows can be read. Allowed folders: your home folder by default when listening on
localhost only, otherwise none; set them with `--allow-files <folder>` (repeatable), or
turn it off with `--no-local-files`. The same folders limit where agents and the browser may
create or open workspaces by path.

To bring in a page from elsewhere, an HTML file or a folder with `index.html` (no server
needed; it copies the files into the workspace):

```sh
uv run ritboard import page.html -w my-workspace --title "My page"
uv run ritboard import site_folder -w my-workspace --board Main
uv run ritboard import page.html -w my-workspace --card "Notes/old card.md" --data docs.json
```

`--card` puts the artifact where that finding sits on the boards and deletes the finding.
`--data` loads database documents: `{"collection": [{"id": "...", "data": {...}}]}`.

Claude artifacts (claude.ai links) are not supported: claude.ai refuses to be shown inside
other pages. Ritboard rejects `url` cards that point at claude.ai or claude.site with a
message telling the agent to publish the HTML as a Ritboard artifact instead.

## From the SQLite version

Earlier versions kept each workspace in one `.sqlite` file in the settings folder. On its
first start, `ritboard serve` turns each of them into a folder under `~/Ritboard` and moves
the old file to `old-sqlite/` in the settings folder. `ritboard migrate` does the same by
hand. Artifacts keep their latest version and their database; a note that had other notes
under it becomes a folder holding the note and the others.

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

Settings via environment: `RITBOARD_DATA`, `RITBOARD_WORKSPACES`, `RITBOARD_HOST`,
`RITBOARD_PORT`, `RITBOARD_TOKEN`, `RITBOARD_POLL`.

## Develop

```sh
uv run pytest
```

The UI is plain JavaScript in `src/ritboard/static/`, no build step.

## License

MIT. See [LICENSE](LICENSE).
