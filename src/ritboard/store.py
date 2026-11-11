"""Storage: a workspace is a folder of ordinary files.

    my-workspace/                   any folder, can be a git repository
      .ritboard/
        workspace.yaml              name, defaults, category colours
        boards/<id>.yaml            board size and placements (path -> [x, y, w, h, z])
        meta.yaml                   metadata for files that can't hold their own
        .gitignore                  ignores local/
        local/                      this computer only: upload links, rename index, trash
      Notes/                        folders are the tree
        idea.md                     a finding; title, category, tags in front matter
        rows.csv
      Report/                       a folder with index.html is an artifact
        index.html
        .data/<collection>.json     the artifact's database

Findings are identified by their path inside the workspace, e.g. "Notes/idea.md". Boards
point at those paths. When Ritboard renames or moves a file it updates the boards; when
something else does, it recognises the file by its content and does the same.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import files as F
from .render import normalize_kind, render

Notify = Callable[[str, str], None]

DEFAULT_SETTINGS: dict[str, Any] = {
    "board_width": 3840,
    "board_height": 2160,
    "grid": 10,
    "categories": {},  # name -> color
}

DEFAULT_SIZES = {
    "md": (420, 300),
    "text": (420, 300),
    "csv": (520, 300),
    "html": (640, 480),
    "url": (640, 480),
    "image": (400, 300),
    "folder": (300, 220),
    "artifact": (800, 600),
    "file": (300, 160),
}

ARTIFACT_MAX_BYTES = 64 * 1024 * 1024  # one artifact, all files together
UPLOAD_TTL_SECONDS = 4 * 3600  # how long upload links work after a publish
DOC_MAX_BYTES = 1024 * 1024  # one artifact database document
MAX_ENTRIES = 5000  # files and folders shown per workspace
MAX_SHOWN_BYTES = 2 * 1024 * 1024  # bigger text files are listed but not shown in cards
SCAN_EVERY_S = 0.5
_SEGMENT = re.compile(r"^[A-Za-z0-9_\-.~:@+]{1,200}$")
TEXT_KINDS = ("md", "text", "csv", "html", "url")

PALETTE = ["#d1495b", "#edae49", "#00798c", "#30638e", "#6a994e", "#8d6a9f", "#c97b63", "#4f6d7a"]


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


_UNSET: Any = object()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat(timespec="milliseconds")


def check_collection(path: str) -> str:
    segs = (path or "").split("/")
    if not 1 <= len(segs) <= 15 or len(segs) % 2 == 0 or not all(_SEGMENT.match(s) and s not in (".", "..") for s in segs):
        raise ValueError(
            f"bad collection {path!r}: 1 to 15 '/'-separated parts (an odd number), letters, digits and _-.~:@+"
        )
    return path


def check_doc_id(doc_id: str) -> str:
    if not _SEGMENT.match(doc_id or "") or doc_id in (".", ".."):
        raise ValueError(f"bad document id {doc_id!r}")
    return doc_id


def merge_doc(old: Any, patch: Any) -> Any:
    """Deep merge for 'update': nested objects merge; {"__delete__": true} removes a field."""
    if not isinstance(old, dict) or not isinstance(patch, dict):
        return patch
    out = dict(old)
    for k, v in patch.items():
        if isinstance(v, dict) and v.get("__delete__") is True and len(v) == 1:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_doc(out[k], v)
        else:
            out[k] = v
    return out


def content_type(path: str) -> str:
    ct = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if path.endswith((".js", ".mjs")):
        ct = "text/javascript"
    if ct.startswith("text/") or ct in ("application/json", "image/svg+xml"):
        ct += "; charset=utf-8"
    return ct


CLAUDE_LINK = re.compile(r"^https?://([^/]*\.)?(claude\.ai|claude\.site)(:\d+)?(/|$)", re.I)
CLAUDE_LINK_ERROR = (
    "Ritboard does not show claude.ai artifacts: claude.ai refuses to be framed, so the card stays blank. "
    "Publish the artifact's HTML here instead with publish_artifact (html=... for one page, or files=[...]); "
    "it becomes a folder in the workspace, and every card showing it reloads when you update it."
)


def check_not_claude_link(kind: str, content: str) -> None:
    if kind in ("url", "image") and CLAUDE_LINK.match((content or "").strip()):
        raise ValueError(CLAUDE_LINK_ERROR)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s[:48] or "workspace"


def clean_art_path(path: str) -> str:
    """A file path inside an artifact folder."""
    p = F.clean_rel(path)
    if p.split("/")[0] in ("_db", "_events", "_ritboard.js"):
        raise ValueError(f"{p.split('/')[0]} is reserved for Ritboard; pick another file name")
    return p


_OLD_COPY = re.compile(r"^(?P<stem>[^/]+)@v(?P<n>\d+)(?P<ext>\.[A-Za-z0-9_-]+)?$")
_PINNED = re.compile(r"^(?P<base>.+)@v(?P<n>\d+)$")


def copy_name(rel: str, version: int) -> str:
    """index.html as it was in version 3: index@v3.html (same folder)."""
    folder, _, name = rel.rpartition("/")
    stem, ext = (name.rsplit(".", 1)[0], "." + name.rsplit(".", 1)[1]) if "." in name.lstrip(".") else (name, "")
    return f"{folder}/{stem}@v{version}{ext}" if folder else f"{stem}@v{version}{ext}"


def old_copy(rel: str) -> tuple[str, int] | None:
    """index@v3.html -> ("index.html", 3); None for an ordinary name."""
    folder, _, name = rel.rpartition("/")
    m = _OLD_COPY.match(name)
    if not m:
        return None
    orig = m["stem"] + (m["ext"] or "")
    return (f"{folder}/{orig}" if folder else orig), int(m["n"])


def follows(key: str, old: str) -> bool:
    """Does a board card or metadata key go with `old` when it moves? (itself, what is in
    it, and cards pinned to a version of it)"""
    return F.is_under(key, old) or (key.startswith(old + "@v") and key[len(old) + 2:].isdigit())


def _stamp(items) -> int:
    """A number that changes when any of the (name, size, mtime) items change. 0 for none."""
    items = sorted(items)
    if not items:
        return 0
    h = hashlib.sha1(json.dumps(items).encode()).hexdigest()
    return int(h[:13], 16)  # fits a JavaScript number


def check_workspace_folder(path: str | Path, roots: list[Path] | None) -> Path:
    """A folder an agent or the browser may make or open as a workspace."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError(f"give the folder as an absolute path on the computer running Ritboard: {path}")
    p = p.resolve()
    if any(part.startswith(".") for part in p.parts[1:]):
        raise ValueError(f"{p} is inside a hidden folder; pick another folder")
    if p == Path.home().resolve() or p.parent == p:
        raise ValueError(f"{p} is too broad for a workspace; pick a project folder")
    if roots is not None:
        if not roots:
            raise ValueError("this server doesn't open folders by path; start it with --allow-files <folder>")
        if not any(p.is_relative_to(r.expanduser().resolve()) for r in roots):
            raise ValueError(f"{p} is outside the folders Ritboard may use ({', '.join(str(r) for r in roots)})")
    return p


@dataclass
class Entry:
    path: str
    kind: str
    is_dir: bool
    mtime_ns: int
    size: int
    meta: dict[str, Any] = field(default_factory=dict)
    body: str | None = None  # text content, for text kinds
    too_big: bool = False


class WorkspaceStore:
    def __init__(self, root: Path, ws_id: str, notify: Notify | None = None, secret: bytes = b"", name: str | None = None):
        self.root = Path(root).resolve()
        self.id = ws_id
        self.rb = self.root / ".ritboard"
        self.local = self.rb / "local"
        self._notify = notify or (lambda ws, what: None)
        self._secret = secret
        self._lock = threading.RLock()
        self._entries: dict[str, Entry] = {}
        self._cache: dict[str, dict[str, Any]] = {}  # path -> parsed file, by (mtime, size)
        self._yaml_cache: dict[str, tuple[tuple[int, int], Any]] = {}
        self._fp_cache: dict[str, tuple[tuple[int, int], list]] = {}
        self._fps: dict[str, list] = {}  # fingerprints of paths that boards or meta point at
        self._known: set[str] | None = None  # paths seen in the previous scan
        self._scanned = 0.0
        self._dirty = True
        self.truncated = False
        self.problems: list[str] = []
        self.ensure_layout(name)

    # ---- layout ---------------------------------------------------------
    def ensure_layout(self, name: str | None = None) -> None:
        (self.rb / "boards").mkdir(parents=True, exist_ok=True)
        gi = self.rb / ".gitignore"
        if not gi.exists():
            F.write_text(gi, "# Only on this computer: upload links, rename index, deleted files.\nlocal/\n")
        if not (self.rb / "workspace.yaml").exists():
            self._write_ws_yaml({"name": name or self.root.name, **{k: v for k, v in DEFAULT_SETTINGS.items()}})

    def close(self) -> None:
        pass

    def changed(self, what: str) -> None:
        # Scan now, so a file that was just placed is recognised if it's renamed next.
        try:
            self._fresh(force=True)
        except OSError:
            self._dirty = True
        self._notify(self.id, what)

    def _art_event(self, aid: str, kind: str) -> None:
        """Tell open artifact pages: kind is version (files changed), db or deleted."""
        self._notify(self.id, f"artifact:{kind}:{aid}")

    def mark_dirty(self) -> None:
        self._dirty = True

    # ---- YAML files -------------------------------------------------------
    def _yaml(self, p: Path, default: Any) -> Any:
        try:
            st = p.stat()
        except FileNotFoundError:
            return copy.deepcopy(default)
        sig = (st.st_mtime_ns, st.st_size)
        c = self._yaml_cache.get(str(p))
        if c and c[0] == sig:
            return copy.deepcopy(c[1])
        data = F.load_yaml(p)
        if data is None:
            data = copy.deepcopy(default)
        self._yaml_cache[str(p)] = (sig, data)
        return copy.deepcopy(data)

    def _write_yaml(self, p: Path, data: Any, flow_lists: bool = False) -> None:
        F.write_text(p, F.dump_yaml(data, flow_lists))
        self._yaml_cache.pop(str(p), None)

    def _ws_yaml(self) -> dict[str, Any]:
        raw = self._yaml(self.rb / "workspace.yaml", {})
        return raw if isinstance(raw, dict) else {}

    def _write_ws_yaml(self, raw: dict[str, Any]) -> None:
        self._write_yaml(self.rb / "workspace.yaml", raw)

    def _meta_yaml(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self._yaml(self.rb / "meta.yaml", {})
        except ValueError as e:
            self._problem(str(e))
            return {}
        return {str(k): v for k, v in (raw or {}).items() if isinstance(v, dict)} if isinstance(raw, dict) else {}

    def _save_meta_yaml(self, data: dict[str, dict[str, Any]]) -> None:
        clean = {k: {mk: mv for mk, mv in v.items() if mv not in (None, "", [])} for k, v in sorted(data.items())}
        clean = {k: v for k, v in clean.items() if v}
        p = self.rb / "meta.yaml"
        if not clean and not p.exists():
            return
        self._write_yaml(p, clean)

    def _local_json(self, name: str, default: Any) -> Any:
        try:
            return json.loads((self.local / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return copy.deepcopy(default)

    def _save_local_json(self, name: str, data: Any) -> None:
        self.local.mkdir(parents=True, exist_ok=True)
        F.write_text(self.local / name, json.dumps(data, indent=1, sort_keys=True))

    def _problem(self, msg: str) -> None:
        if msg not in self.problems:
            self.problems.append(msg)

    # ---- workspace settings ----------------------------------------------
    @property
    def name(self) -> str:
        return str(self._ws_yaml().get("name") or self.root.name)

    def set_name(self, name: str) -> None:
        raw = self._ws_yaml()
        raw["name"] = name.strip() or self.root.name
        self._write_ws_yaml(raw)
        self.changed("workspace")

    def settings(self) -> dict[str, Any]:
        raw = self._ws_yaml()
        s = dict(DEFAULT_SETTINGS)
        for k in ("board_width", "board_height", "grid"):
            try:
                s[k] = max(1, int(raw.get(k, s[k])))
            except (TypeError, ValueError):
                pass
        cats = raw.get("categories")
        s["categories"] = {str(k): str(v) for k, v in cats.items()} if isinstance(cats, dict) else {}
        return s

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        raw = self._ws_yaml()
        for k, v in patch.items():
            if k == "categories" and isinstance(v, dict):
                cats = dict(raw.get("categories") or {})
                for name, color in v.items():
                    if color is None:
                        cats.pop(name, None)
                    else:
                        cats[name] = color
                raw["categories"] = cats
            elif k in ("board_width", "board_height", "grid"):
                raw[k] = max(1, int(v))
        self._write_ws_yaml(raw)
        self.changed("settings")
        return self.settings()

    def _ensure_category(self, category: str | None) -> None:
        if not category:
            return
        raw = self._ws_yaml()
        cats = dict(raw.get("categories") or {})
        if category not in cats:
            cats[category] = PALETTE[len(cats) % len(PALETTE)]
            raw["categories"] = cats
            self._write_ws_yaml(raw)

    def summary(self) -> dict[str, Any]:
        self._fresh()
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.root),
            "findings": len(self._entries),
            "boards": len(self._boards()),
            "truncated": self.truncated,
        }

    # ---- scanning ---------------------------------------------------------
    def _fresh(self, force: bool = False) -> None:
        with self._lock:
            if force or self._dirty or time.monotonic() - self._scanned > SCAN_EVERY_S:
                self._scan()

    def _scan(self) -> None:
        entries: dict[str, Entry] = {}
        count = 0
        truncated = False

        def walk(folder: Path, rel: str) -> None:
            nonlocal count, truncated
            try:
                items = sorted(os.scandir(folder), key=lambda d: d.name.lower())
            except OSError:
                return
            for de in items:
                if F.skipped(de.name):
                    continue
                if count >= MAX_ENTRIES:
                    truncated = True
                    return
                try:
                    if de.is_symlink():
                        continue
                    is_dir = de.is_dir(follow_symlinks=False)
                    st = de.stat(follow_symlinks=False)
                except OSError:
                    continue
                path = f"{rel}/{de.name}" if rel else de.name
                count += 1
                if is_dir:
                    art = os.path.isfile(os.path.join(de.path, "index.html"))
                    entries[path] = Entry(path, "artifact" if art else "folder", True, st.st_mtime_ns, 0)
                    if not art:
                        walk(Path(de.path), path)
                else:
                    entries[path] = self._file_entry(path, Path(de.path), st)

        self.problems = []
        walk(self.root, "")
        self._follow_moves(entries)
        meta = self._meta_yaml()
        for e in entries.values():
            if self._meta_src(e) == "yaml":
                e.meta = dict(meta.get(e.path) or {})
            if e.kind == "url" and e.meta.get("kind") == "image":
                e.kind = "image"
        self._entries = entries
        self.truncated = truncated
        if truncated:
            self._problem(f"Only the first {MAX_ENTRIES} files and folders are shown.")
        self._cache = {k: v for k, v in self._cache.items() if k in entries}
        self._scanned = time.monotonic()
        self._dirty = False

    def _file_entry(self, path: str, p: Path, st: os.stat_result) -> Entry:
        kind = F.kind_for(p.name)
        e = Entry(path, kind, False, st.st_mtime_ns, st.st_size)
        if kind not in TEXT_KINDS:
            return e
        sig = (st.st_mtime_ns, st.st_size)
        c = self._cache.get(path)
        if not c or c["sig"] != sig:
            c = {"sig": sig, "meta": {}, "body": None, "too_big": False, "rendered": None}
            if st.st_size > MAX_SHOWN_BYTES:
                c["too_big"] = True
            else:
                try:
                    text, _ = F.read_text(p)
                except OSError:
                    text = ""
                if kind == "md":
                    c["meta"], c["body"] = F.split_front_matter(text)
                elif kind == "html":
                    c["meta"], c["body"] = F.split_html_meta(text)
                elif kind == "url":
                    c["body"] = F.parse_url_file(text)
                else:
                    c["body"] = text
            self._cache[path] = c
        e.meta = dict(c["meta"])
        e.body = c["body"]
        e.too_big = c["too_big"]
        return e

    @staticmethod
    def _meta_src(e: Entry) -> str:
        if not e.is_dir and e.kind == "md":
            return "front"
        if not e.is_dir and e.kind == "html":
            return "html"
        return "yaml"

    # ---- following renames made outside Ritboard ----------------------------
    def _tracked(self) -> set[str]:
        out: set[str] = set()
        for b in self._boards().values():
            for k in b["cards"]:
                m = _PINNED.match(k)
                out.add(m["base"] if m else k)
        out.update(self._meta_yaml())
        out.update(self._local_json("uploads.json", {}))
        return out

    def _fingerprint(self, e: Entry) -> list:
        """What a file or folder looks like, to recognise it after a rename."""
        sig = (e.mtime_ns, e.size)
        c = self._fp_cache.get(e.path)
        if c and c[0] == sig and not e.is_dir:
            return c[1]
        p = self.root / e.path
        try:
            if e.is_dir:
                names = sorted(n for n in os.listdir(p) if not F.skipped(n))
                fp = ["d", e.kind, hashlib.sha1("\n".join(names).encode()).hexdigest()]
            else:
                h = hashlib.sha1()
                with open(p, "rb") as fh:
                    h.update(fh.read(1024 * 1024))
                fp = ["f", e.size, h.hexdigest()]
        except OSError:
            return ["?"]
        self._fp_cache[e.path] = (sig, fp)
        return fp

    def _follow_moves(self, entries: dict[str, Entry]) -> None:
        if self._known is None:
            saved = self._local_json("index.json", {})
            self._known = set(saved.get("known", [])) if saved else None
            self._fps = dict(saved.get("fingerprints", {})) if saved else {}
        tracked = self._tracked()
        vanished = [p for p in tracked if p not in entries]
        if vanished and self._known is not None:
            fresh = [p for p in entries if p not in self._known]
            for old in sorted(vanished, key=lambda p: p.count("/")):
                fp = self._fps.get(old)
                if not fp or old not in self._tracked():
                    continue
                cands = []
                for q in fresh:
                    e = entries[q]
                    if (fp[0] == "f") == e.is_dir or (fp[0] == "f" and fp[1] != e.size):
                        continue
                    if self._fingerprint(e) == fp:
                        cands.append(q)
                if len(cands) == 1:
                    self._repoint(old, cands[0])
            tracked = self._tracked()
        fps = {p: self._fingerprint(entries[p]) for p in tracked if p in entries}
        for p, fp in self._fps.items():  # keep what we knew about paths that are gone for now
            if p in tracked and p not in fps:
                fps[p] = fp
        known = set(entries)
        if fps != self._fps or known != self._known:
            self._fps, self._known = fps, known
            try:
                self._save_local_json("index.json", {"known": sorted(known), "fingerprints": fps})
            except OSError:
                pass

    def _repoint(self, old: str, new: str) -> None:
        """A file or folder moved from old to new: boards, metadata and uploads follow."""

        def move(k: str) -> str:
            return new + k[len(old):] if follows(k, old) else k

        for b in self._boards().values():
            if any(follows(k, old) for k in b["cards"]):
                b["cards"] = {move(k): v for k, v in b["cards"].items()}
                self._save_board(b)
        meta = self._meta_yaml()
        if any(F.is_under(k, old) for k in meta):
            self._save_meta_yaml({move(k): v for k, v in meta.items()})
        ups = self._local_json("uploads.json", {})
        if any(F.is_under(k, old) for k in ups):
            self._save_local_json("uploads.json", {move(k): v for k, v in ups.items()})
        self._fps = {move(k): v for k, v in self._fps.items()}

    # ---- findings -------------------------------------------------------
    def _entry(self, fid: str) -> Entry:
        self._fresh()
        key = (fid or "").replace("\\", "/").strip().strip("/")
        e = self._entries.get(key)
        if e is None:
            self._fresh(force=True)
            e = self._entries.get(key)
        if e is None:
            raise NotFound(f"finding {fid!r} not found; ids are paths inside the workspace, like 'Notes/idea.md'")
        return e

    def _title(self, e: Entry) -> str:
        t = e.meta.get("title")
        return str(t) if t else self._display_name(e.path, e.is_dir)

    def raw_url(self, path: str) -> str:
        return f"/api/w/{urllib.parse.quote(self.id)}/raw?path={urllib.parse.quote(path)}"

    def _finding(self, e: Entry, with_content: bool = True) -> dict[str, Any]:
        m = e.meta
        tags = m.get("tags") or []
        d: dict[str, Any] = {
            "id": e.path,
            "parent_id": F.parent_of(e.path),
            "title": self._title(e),
            "kind": e.kind,
            "category": str(m["category"]) if m.get("category") else None,
            "tags": [str(t) for t in tags] if isinstance(tags, list) else [str(tags)],
            "source": str(m["source"]) if m.get("source") else None,
            "updated_at": iso(e.mtime_ns),
        }
        if e.kind == "artifact":
            d.update(self._art_summary(e))
        elif e.kind in ("image", "file"):
            d["src"] = e.body if e.path.lower().endswith(".url") else self.raw_url(e.path)
            d["size"] = e.size
        if with_content:
            if e.too_big:
                d["content"], d["too_big"] = "", True
                d["rendered"] = f"<p><em>Too large to show here ({e.size / 1048576:.1f} MB).</em></p>"
            elif e.kind in TEXT_KINDS or (e.kind == "image" and e.body):
                d["content"] = e.body or ""
                c = self._cache.get(e.path)
                if c is not None and c.get("rendered") is None:
                    c["rendered"] = render(e.kind, e.body or "") or ""
                d["rendered"] = (c or {}).get("rendered") or render(e.kind, e.body or "")
            else:
                d["content"], d["rendered"] = "", None
        return d

    def list_findings(
        self,
        *,
        with_content: bool = True,
        category: str | None = None,
        parent_id: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        self._fresh()
        out = []
        q = (query or "").lower()
        for e in self._entries.values():
            if parent_id is not None and (F.parent_of(e.path) or "") != parent_id.strip("/"):
                continue
            f = self._finding(e, with_content or bool(q))
            if category is not None and f["category"] != (category or None):
                continue
            if q and not (
                q in f["title"].lower() or q in (f.get("content") or "").lower() or any(q in t.lower() for t in f["tags"])
            ):
                continue
            if not with_content:
                f.pop("content", None)
                f.pop("rendered", None)
            out.append(f)
        return out

    def get_finding(self, fid: str) -> dict[str, Any]:
        return self._finding(self._entry(fid))

    def _folder(self, parent_id: str | None) -> str | None:
        """Check that parent_id names a folder (None is the workspace itself)."""
        if not parent_id:
            return None
        e = self._entry(parent_id)
        if e.kind != "folder":
            raise ValueError(f"{e.path} is a {e.kind}, not a folder; findings can only go inside folders")
        return e.path

    def _free_path(self, parent: str | None, base: str, ext: str, taken: str | None = None) -> str:
        """parent/base+ext, or with " 2", " 3"... added if that name is in use. `taken` is the
        finding being renamed: its own name counts as free."""
        for n in range(1, 1000):
            name = f"{base}{ext}" if n == 1 else f"{base} {n}{ext}"
            path = f"{parent}/{name}" if parent else name
            if (taken and path.lower() == taken.lower()) or not (self.root / path).exists():
                return path
        raise ValueError(f"too many files called {base}")

    def _new_path(self, parent: str | None, title: str, ext: str) -> str:
        return self._free_path(parent, F.safe_name(title), ext)

    def _display_name(self, path: str, is_dir: bool) -> str:
        """The title a file or folder gets from its name alone."""
        name = path.rsplit("/", 1)[-1]
        if is_dir:
            return name
        ext = Path(name).suffix.lower()
        return Path(name).stem if ext in F.KIND_BY_EXT or ext in F.IMAGE_EXTS else name

    def _title_meta(self, title: str, path: str, is_dir: bool) -> str | None:
        """Keep the title in metadata only when the name can't say it."""
        return None if self._display_name(path, is_dir) == title else title

    def _write_file(self, path: str, kind: str, meta: dict[str, Any], body: str | None) -> None:
        """Write a text finding with its metadata, wherever that format keeps it. body None
        keeps the text that is in the file now."""
        p = self.root / path
        nl = "\n"
        if p.exists():
            text, nl = F.read_text(p)
            if body is None:
                body = (F.split_front_matter(text)[1] if kind == "md" else F.split_html_meta(text)[1] if kind == "html"
                        else F.parse_url_file(text) if path.lower().endswith(".url") else text)
        body = body or ""
        all_meta = self._meta_yaml()
        if kind == "md":
            F.write_text(p, F.join_front_matter(meta, body), nl)
            all_meta.pop(path, None)
        elif kind == "html":
            F.write_text(p, F.join_html_meta(meta, body), nl)
            all_meta.pop(path, None)
        else:
            F.write_text(p, F.url_file(body) if path.lower().endswith(".url") else body, nl)
            all_meta[path] = meta
        self._save_meta_yaml(all_meta)
        self._cache.pop(path, None)

    def _set_yaml_meta(self, path: str, meta: dict[str, Any]) -> None:
        all_meta = self._meta_yaml()
        all_meta[path] = meta
        self._save_meta_yaml(all_meta)

    def add_finding(
        self,
        title: str,
        content: str = "",
        kind: str = "md",
        parent_id: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        kind = normalize_kind(kind)
        content = content or ""
        check_not_claude_link(kind, content)
        title = (title or "").strip() or "Untitled"
        if kind == "artifact":
            a = self.create_artifact(
                title, {"index.html": content.encode("utf-8")}, source=source, parent_id=parent_id, category=category, tags=tags
            )
            return self.get_finding(a["id"])
        with self._lock:
            self._fresh(force=True)
            parent = self._folder(parent_id)
            meta: dict[str, Any] = {"category": category or None, "tags": list(tags or []) or None, "source": source or None}
            if kind == "folder":
                path = self._new_path(parent, title, "")
                (self.root / path).mkdir(parents=True)
                self._set_yaml_meta(path, {"title": self._title_meta(title, path, True), **meta})
            elif kind == "image":
                path = self._add_image(parent, title, content, meta)
            elif kind == "file":
                raise ValueError("to add a file of another type, copy it into the workspace folder")
            else:
                if kind == "url" and not re.match(r"^[a-z][a-z0-9+.-]*:", content.strip(), re.I):
                    raise ValueError("a url finding needs a URL as its content, like https://example.com")
                path = self._new_path(parent, title, F.EXT_BY_KIND[kind])
                self._write_file(path, kind, {"title": self._title_meta(title, path, False), **meta}, content)
            self._ensure_category(category)
        self.changed("findings")
        return self.get_finding(path)

    def _add_image(self, parent: str | None, title: str, content: str, meta: dict[str, Any]) -> str:
        c = content.strip()
        m = re.match(r"^data:([\w/+.-]+)(;base64)?,(.*)$", c, re.S)
        if m:
            ext = F.IMAGE_EXT_BY_MIME.get(m.group(1).lower())
            if not ext:
                raise ValueError(f"unsupported image type {m.group(1)}")
            data = base64.b64decode(m.group(3)) if m.group(2) else urllib.parse.unquote_to_bytes(m.group(3))
            path = self._new_path(parent, title, ext)
            F.write_bytes(self.root / path, data)
            self._set_yaml_meta(path, {"title": self._title_meta(title, path, False), **meta})
            return path
        if re.match(r"^https?://", c, re.I):
            path = self._new_path(parent, title, ".url")
            self._write_file(path, "url", {"title": self._title_meta(title, path, False), "kind": "image", **meta}, c)
            return path
        raise ValueError("an image finding needs an https:// URL or a data: URI as its content")

    def update_finding(
        self,
        fid: str,
        *,
        title: str | None = None,
        content: str | None = None,
        kind: str | None = None,
        category: Any = _UNSET,
        tags: list[str] | None = None,
        parent_id: Any = _UNSET,
        sort: float | None = None,  # kept for old clients; the tree is sorted by name
        source: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._fresh(force=True)
            e = self._entry(fid)
            path = e.path
            new_kind = normalize_kind(kind) if kind else e.kind
            if new_kind != e.kind and not (e.kind in TEXT_KINDS and new_kind in TEXT_KINDS):
                raise ValueError(
                    f"a {e.kind} can't become a {new_kind}; only md, text, csv, html and url convert into each other"
                )
            if content is not None or new_kind != e.kind:
                check_not_claude_link(new_kind, content if content is not None else (e.body or ""))
            if content is not None and new_kind == "url" and not re.match(r"^[a-z][a-z0-9+.-]*:", content.strip(), re.I):
                raise ValueError("a url finding needs a URL as its content")
            if content is not None and e.kind in ("folder", "artifact", "file") or (content is not None and e.kind == "image" and not e.body):
                if content:
                    raise ValueError(f"a {e.kind} has no text content to change")
                content = None

            meta = dict(e.meta)
            meta_changed = False
            if category is not _UNSET:
                meta["category"] = category or None
                meta_changed = True
            if tags is not None:
                meta["tags"] = list(tags) or None
                meta_changed = True
            if source is not None:
                meta["source"] = source or None
                meta_changed = True

            cur_title = self._title(e)
            new_title = (title or "").strip() or cur_title
            rename = title is not None and new_title != cur_title
            move = parent_id is not _UNSET and (parent_id or None) != F.parent_of(path)
            convert = new_kind != e.kind
            target = path
            if rename or move or convert:
                parent = self._folder(parent_id) if move else F.parent_of(path)
                if move and e.is_dir and parent and F.is_under(parent, path):
                    raise ValueError("cannot move a folder inside itself")
                name = path.rsplit("/", 1)[-1]
                old_ext = "" if e.is_dir else Path(name).suffix
                ext = F.EXT_BY_KIND[new_kind] if convert else old_ext
                if rename:
                    t = new_title
                    if not e.is_dir and old_ext and t.lower().endswith(old_ext.lower()):
                        t = t[: -len(old_ext)]  # titles of .py, .json... files include the extension
                    base = F.safe_name(t)
                else:
                    base = name if e.is_dir else Path(name).stem
                target = self._free_path(parent, base, ext, taken=path)
            if rename or target != path:
                keep = new_title if rename else cur_title
                meta["title"] = self._title_meta(keep, target, e.is_dir)
                meta_changed = True

            if convert:
                body = content if content is not None else e.body
                if body is None:
                    body = F.read_text(self.root / path)[0]
                self._repoint(path, target)
                self._write_file(target, new_kind, meta, body)
                (self.root / path).unlink()
                self._cache.pop(path, None)
            else:
                if target != path:
                    os.rename(self.root / path, self.root / target)
                    self._repoint(path, target)
                    self._cache.pop(path, None)
                if content is not None or meta_changed:
                    if self._meta_src(e) == "yaml" and content is None:
                        self._set_yaml_meta(target, meta)
                    else:
                        self._write_file(target, "url" if e.kind == "image" else e.kind, meta, content)
            if meta.get("category"):
                self._ensure_category(meta["category"])
            self._dirty = True
        self.changed("findings")
        return self.get_finding(target)

    def _trash(self, path: str) -> Path:
        dest = self.local / "trash" / time.strftime("%Y%m%d-%H%M%S") / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.root / path), str(dest))
        return dest

    def _forget(self, path: str) -> None:
        """Take a removed path (and everything under it) off the boards and out of the metadata."""
        for b in self._boards().values():
            if any(follows(k, path) for k in b["cards"]):
                b["cards"] = {k: v for k, v in b["cards"].items() if not follows(k, path)}
                self._save_board(b)
        meta = self._meta_yaml()
        if any(F.is_under(k, path) for k in meta):
            self._save_meta_yaml({k: v for k, v in meta.items() if not F.is_under(k, path)})
        ups = self._local_json("uploads.json", {})
        if any(F.is_under(k, path) for k in ups):
            self._save_local_json("uploads.json", {k: v for k, v in ups.items() if not F.is_under(k, path)})

    def delete_finding(self, fid: str, recursive: bool = False) -> dict[str, Any]:
        """Move a finding's file or folder to .ritboard/local/trash. A folder's contents move up
        a level first unless recursive is true."""
        with self._lock:
            self._fresh(force=True)
            e = self._entry(fid)
            parent = F.parent_of(e.path)
            if e.kind == "folder" and not recursive:
                for child in [c for c in self._entries.values() if F.parent_of(c.path) == e.path]:
                    name = child.path.rsplit("/", 1)[-1]
                    target = f"{parent}/{name}" if parent else name
                    if (self.root / target).exists():
                        stem, ext = (name, "") if child.is_dir else (Path(name).stem, Path(name).suffix)
                        target = self._new_path(parent, stem, ext)
                    os.rename(self.root / child.path, self.root / target)
                    self._repoint(child.path, target)
                n = 1
            else:
                n = sum(1 for p in self._entries if F.is_under(p, e.path))
            self._forget(e.path)
            dest = self._trash(e.path)
            self._dirty = True
        self.changed("findings")
        if e.kind == "artifact":
            self._art_event(e.path, "deleted")
        return {"deleted": n, "trashed_to": str(dest)}

    # ---- boards ---------------------------------------------------------
    def _boards(self) -> dict[str, dict[str, Any]]:
        s = self.settings()
        out: dict[str, dict[str, Any]] = {}
        for p in sorted((self.rb / "boards").glob("*.yaml")):
            try:
                raw = self._yaml(p, {})
            except ValueError as e:
                self._problem(f"Board file {p.name} can't be read: {e}")
                continue
            if not isinstance(raw, dict):
                continue
            cards: dict[str, list[int]] = {}
            for k, v in (raw.get("cards") or {}).items():
                try:
                    vals = [int(x) for x in v][:5]
                except (TypeError, ValueError):
                    continue
                if len(vals) >= 4:
                    cards[str(k)] = vals + [0] * (5 - len(vals))
            out[p.stem] = {
                "id": p.stem,
                "name": str(raw.get("name") or p.stem),
                "width": int(raw.get("width") or s["board_width"]),
                "height": int(raw.get("height") or s["board_height"]),
                "background": raw.get("background") or None,
                "order": float(raw.get("order") or 0),
                "cards": cards,
            }
        return out

    def _save_board(self, b: dict[str, Any]) -> None:
        data: dict[str, Any] = {"name": b["name"], "width": b["width"], "height": b["height"]}
        if b.get("background"):
            data["background"] = b["background"]
        data["order"] = int(b["order"]) if float(b["order"]).is_integer() else b["order"]
        data["cards"] = {k: list(v) for k, v in sorted(b["cards"].items())}
        self._write_yaml(self.rb / "boards" / f"{b['id']}.yaml", data, flow_lists=True)

    @staticmethod
    def _board_out(b: dict[str, Any]) -> dict[str, Any]:
        return {k: b[k] for k in ("id", "name", "width", "height", "background", "order")}

    def list_boards(self) -> list[dict[str, Any]]:
        return [self._board_out(b) for b in sorted(self._boards().values(), key=lambda b: (b["order"], b["name"].lower()))]

    def _board(self, bid: str) -> dict[str, Any]:
        boards = self._boards()
        if bid in boards:
            return boards[bid]
        for b in boards.values():  # agents tend to use the name
            if b["name"].lower() == (bid or "").strip().lower():
                return b
        raise NotFound(f"board {bid!r} not found; boards: {', '.join(b['name'] for b in boards.values()) or 'none'}")

    def get_board_row(self, bid: str) -> dict[str, Any]:
        return self._board_out(self._board(bid))

    def create_board(
        self, name: str, width: int | None = None, height: int | None = None, background: str | None = None
    ) -> dict[str, Any]:
        s = self.settings()
        boards = self._boards()
        base = slugify(name) if slugify(name) != "workspace" else "board"
        bid, n = base, 2
        while bid in boards or (self.rb / "boards" / f"{bid}.yaml").exists():
            bid, n = f"{base}-{n}", n + 1
        order = max([b["order"] for b in boards.values()], default=0) + 1
        self._save_board({
            "id": bid, "name": name.strip() or bid, "width": int(width or s["board_width"]),
            "height": int(height or s["board_height"]), "background": background, "order": order, "cards": {},
        })
        self.changed("boards")
        return self.get_board_row(bid)

    def update_board(
        self,
        bid: str,
        *,
        name: str | None = None,
        width: int | None = None,
        height: int | None = None,
        background: Any = _UNSET,
        sort: float | None = None,
    ) -> dict[str, Any]:
        b = self._board(bid)
        if name is not None:
            b["name"] = name.strip() or b["name"]
        if width is not None:
            b["width"] = max(100, int(width))
        if height is not None:
            b["height"] = max(100, int(height))
        if background is not _UNSET:
            b["background"] = background or None
        if sort is not None:
            b["order"] = float(sort)
        self._save_board(b)
        self.changed("boards")
        return self._board_out(b)

    def delete_board(self, bid: str) -> None:
        b = self._board(bid)
        p = self.rb / "boards" / f"{b['id']}.yaml"
        dest = self.local / "trash" / time.strftime("%Y%m%d-%H%M%S") / ".ritboard" / "boards" / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
        self.changed("boards")

    # ---- placements -----------------------------------------------------
    def _card(self, fid: str) -> tuple[Entry, int | None] | None:
        """What a card shows: a finding, or a version of an artifact ("Report@v3")."""
        e = self._entries.get(fid)
        if e:
            return e, None
        m = _PINNED.match(fid)
        if m:
            b = self._entries.get(m["base"])
            if b and b.kind == "artifact" and 1 <= int(m["n"]) <= self._art_version(b):
                return b, int(m["n"])
        return None

    def placements(self, bid: str | None = None) -> list[dict[str, Any]]:
        self._fresh()
        boards = [self._board(bid)] if bid else list(self._boards().values())
        out = []
        for b in boards:
            for fid, (x, y, w, h, z) in sorted(b["cards"].items(), key=lambda kv: kv[1][4]):
                p = {"board_id": b["id"], "finding_id": fid, "x": x, "y": y, "w": w, "h": h, "z": z}
                if self._card(fid) is None:
                    p["missing"] = True
                out.append(p)
        return out

    @staticmethod
    def _free_spot(board: dict[str, Any], w: int, h: int, taken: list[list[int]]) -> tuple[int, int]:
        gap, step = 20, 20
        for y in range(40, max(41, board["height"] - h), step):
            for x in range(40, max(41, board["width"] - w), step):
                if all(
                    x + w + gap <= p[0] or p[0] + p[2] + gap <= x or y + h + gap <= p[1] or p[1] + p[3] + gap <= y
                    for p in taken
                ):
                    return x, y
        n = len(taken)
        return 40 + (n * 30) % 400, 40 + (n * 30) % 400

    def place(self, bid: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Upsert placements. Each item: finding_id, and optional x, y, w, h, z.

        Missing x/y on a new placement means "find a free spot".
        Missing fields on an existing placement keep their value.
        """
        with self._lock:
            self._fresh()
            b = self._board(bid)
            out = []
            zmax = max([v[4] for v in b["cards"].values()], default=0)
            for it in items:
                fid = str(it["finding_id"]).replace("\\", "/").strip("/")
                cur = b["cards"].get(fid)
                card = self._card(fid)
                if card is None and cur is None:
                    self._fresh(force=True)
                    card = self._card(fid)
                    if card is None:
                        self._entry(fid)  # raises the usual not-found message
                        raise NotFound(f"{fid}: no such version")
                kind = card[0].kind if card else "md"
                dw, dh = DEFAULT_SIZES.get(kind, (420, 300))
                w = int(it.get("w") or (cur[2] if cur else dw))
                h = int(it.get("h") or (cur[3] if cur else dh))
                w, h = max(40, w), max(30, h)
                if it.get("x") is not None and it.get("y") is not None:
                    x, y = int(it["x"]), int(it["y"])
                elif cur:
                    x = int(it["x"]) if it.get("x") is not None else cur[0]
                    y = int(it["y"]) if it.get("y") is not None else cur[1]
                else:
                    taken = [v for k, v in b["cards"].items() if k != fid]
                    x, y = self._free_spot(b, w, h, taken)
                if it.get("z") is not None:
                    z = int(it["z"])
                elif cur:
                    z = cur[4]
                else:
                    zmax += 1
                    z = zmax
                b["cards"][fid] = [x, y, w, h, z]
                out.append({"board_id": b["id"], "finding_id": fid, "x": x, "y": y, "w": w, "h": h, "z": z})
            self._save_board(b)
        self.changed("placements")
        return out

    def unplace(self, bid: str, finding_ids: list[str]) -> int:
        b = self._board(bid)
        n = 0
        for fid in finding_ids:
            if b["cards"].pop(str(fid).strip("/"), None) is not None:
                n += 1
        self._save_board(b)
        self.changed("placements")
        return n

    # ---- artifacts ------------------------------------------------------
    # An artifact is a folder with index.html and any other files the page uses. Its id is
    # its path in the workspace. It is served at /w/<workspace>/a/<key>/, where the key is
    # derived from a secret on this computer, so the address can't be guessed.
    #
    # Big files can be registered as shadow files: path, SHA-256 and size, no content. The
    # artifact is published at once; each shadow file gets a PUT link and is written when its
    # bytes arrive (checked against the hash). The links carry an upload secret that is
    # replaced on every publish, so only links from the latest publish work.

    def art_key(self, aid: str) -> str:
        return hmac.new(self._secret, f"{self.id}\0{aid}".encode(), hashlib.sha256).hexdigest()[:24]

    def art_url(self, aid: str) -> str:
        return f"/w/{urllib.parse.quote(self.id)}/a/{self.art_key(aid)}/"

    def artifact_by_key(self, key: str) -> str:
        self._fresh()
        for e in self._entries.values():
            if e.kind == "artifact" and hmac.compare_digest(self.art_key(e.path), key):
                return e.path
        self._fresh(force=True)
        for e in self._entries.values():
            if e.kind == "artifact" and hmac.compare_digest(self.art_key(e.path), key):
                return e.path
        raise NotFound("no such artifact (it may have been renamed or deleted)")

    def _art(self, aid: str) -> Entry:
        e = self._entry(aid)
        if e.kind != "artifact":
            raise ValueError(f"{e.path} is a {e.kind}, not an artifact (a folder with index.html)")
        return e

    def _art_all(self, aid: str) -> list[tuple[str, Path, os.stat_result]]:
        """Every file in the artifact folder, old copies included."""
        base = self.root / aid
        out = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not F.skipped(d) and not os.path.islink(os.path.join(dirpath, d)))
            for fn in sorted(filenames):
                if F.skipped(fn):
                    continue
                p = Path(dirpath) / fn
                if p.is_symlink():
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append((p.relative_to(base).as_posix(), p, st))
        return out

    def _art_files(self, aid: str) -> list[tuple[str, Path, os.stat_result]]:
        """The artifact's current files."""
        return [f for f in self._art_all(aid) if not old_copy(f[0])]

    def _art_copies(self, aid: str) -> dict[str, list[tuple[int, str]]]:
        """Old copies by the file they were: {"index.html": [(3, "index@v3.html")]}, oldest first."""
        out: dict[str, list[tuple[int, str]]] = {}
        for rel, _, _ in self._art_all(aid):
            oc = old_copy(rel)
            if oc:
                out.setdefault(oc[0], []).append((oc[1], rel))
        for v in out.values():
            v.sort()
        return out

    def _art_version(self, e: Entry, copies: dict[str, list[tuple[int, str]]] | None = None) -> int:
        """The current version: what meta.yaml says, but never behind the old copies."""
        if copies is None:
            copies = self._art_copies(e.path)
        newest_copy = max((n for v in copies.values() for n, _ in v), default=0)
        try:
            said = int(e.meta.get("version") or 1)
        except (TypeError, ValueError):
            said = 1
        return max(said, newest_copy + 1, 1)

    def _target(self, aid: str, rel: str) -> Path:
        """Where to write a file of an artifact: never through a link to somewhere outside it."""
        base = (self.root / aid).resolve()
        p = base / rel
        if not p.parent.resolve().is_relative_to(base) or p.is_symlink():
            raise ValueError(f"{rel} leads outside the artifact folder (a link?); not writing it")
        return p

    def _pending(self, aid: str) -> dict[str, list]:
        return dict((self._local_json("uploads.json", {}).get(aid) or {}).get("pending") or {})

    def _art_summary(self, e: Entry) -> dict[str, Any]:
        everything = self._art_all(e.path)
        files = [f for f in everything if not old_copy(f[0])]
        pending = self._pending(e.path)
        latest = max([st.st_mtime_ns for _, _, st in files], default=e.mtime_ns)
        return {
            "url": self.art_url(e.path),
            "version": self._art_version(e),
            "files": len(files) + sum(1 for p in pending if not any(r == p for r, _, _ in files)),
            "bytes": sum(st.st_size for r, _, st in files if r not in pending) + sum(int(v[1]) for v in pending.values()),
            "pending": len(pending),
            "stamp": _stamp([[r, st.st_size, st.st_mtime_ns] for r, _, st in everything] + [[f"pending:{p}", 0, 0] for p in pending]),
            "updated_at": iso(latest),
        }

    def artifact_meta(self, aid: str) -> dict[str, Any]:
        e = self._art(aid)
        return {"id": e.path, "title": self._title(e), "description": e.meta.get("description") or "", **self._art_summary(e)}

    def stamp(self, aid: str) -> int:
        return self._art_summary(self._art(aid))["stamp"]

    def list_artifacts(self) -> list[dict[str, Any]]:
        self._fresh()
        arts = [self.artifact_meta(e.path) for e in self._entries.values() if e.kind == "artifact"]
        return sorted(arts, key=lambda a: a["updated_at"], reverse=True)

    def _resolve(self, aid: str, rel: str, version: int | None, copies, current: set[str]) -> str | None:
        """Which file holds `rel` as it was in `version` (None: now)."""
        if version is not None:
            later = [c for n, c in copies.get(rel, []) if n >= version]
            if later:
                return later[0]
        return rel if rel in current else None

    def get_artifact(self, aid: str, with_content: bool = False, version: int | None = None) -> dict[str, Any]:
        a = self.artifact_meta(aid)
        e = self._art(aid)
        copies = self._art_copies(a["id"])
        latest = self._art_version(e, copies)
        if version is not None and not 1 <= version <= latest:
            raise NotFound(f"artifact {a['id']} has versions 1 to {latest}")
        if version == latest:
            version = None
        pending = self._pending(a["id"]) if version is None else {}
        current = {rel: (p, st) for rel, p, st in self._art_files(a["id"])}
        files = []
        for rel in sorted(set(current) | set(copies)):
            if rel in pending:
                continue
            src = self._resolve(a["id"], rel, version, copies, set(current))
            if src is None:
                continue
            p = self.root / a["id"] / src
            f: dict[str, Any] = {"path": rel, "content_type": content_type(rel), "size": p.stat().st_size}
            if src != rel:
                f["stored_as"] = src
            if with_content:
                data = p.read_bytes()
                ct = f["content_type"]
                if ct.startswith("text/") or "json" in ct or "svg" in ct or "javascript" in ct:
                    f["content"], f["encoding"] = data.decode("utf-8", "replace"), "text"
                else:
                    f["content"], f["encoding"] = base64.b64encode(data).decode(), "base64"
            files.append(f)
        for rel, (h, size) in sorted(pending.items()):
            files.append({"path": rel, "content_type": content_type(rel), "size": size, "pending": True, "sha256": h})
        files.sort(key=lambda f: f["path"])
        return {**a, "version": version or latest, "latest": latest, "entry": "index.html", "files": files}

    def artifact_file(self, aid: str, path: str, version: int | None = None) -> tuple[bytes, str]:
        rel = clean_art_path(path or "index.html")
        e = self._art(aid)
        copies = self._art_copies(e.path)
        if version is not None:
            latest = self._art_version(e, copies)
            if not 1 <= version <= latest:
                raise NotFound(f"artifact {e.path} has versions 1 to {latest}")
            if version == latest:
                version = None
        if version is None and rel in self._pending(e.path):
            raise NotFound(f"{rel} has not been uploaded yet")
        base = (self.root / e.path).resolve()
        src = self._resolve(e.path, rel, version, copies, {rel} if (base / rel).is_file() else set())
        p = (base / src).resolve() if src else None
        if p is None or not p.is_relative_to(base) or not p.is_file():
            raise NotFound(f"{rel} not in artifact {aid}" + (f" version {version}" if version else ""))
        return p.read_bytes(), content_type(rel)

    @staticmethod
    def _shadow(item: Any) -> tuple[str, int]:
        h = str(item.get("sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", h):
            raise ValueError("a shadow file needs sha256 as 64 hex characters")
        size = int(item.get("size", -1))
        if size < 0:
            raise ValueError("a shadow file needs its size in bytes")
        return h, size

    def _set_uploads(self, aid: str, pending: dict[str, list], rotate: bool) -> None:
        ups = self._local_json("uploads.json", {})
        cur = ups.get(aid) or {}
        if rotate or not cur.get("token"):
            cur["token"] = secrets.token_urlsafe(24)
            cur["expires"] = time.time() + UPLOAD_TTL_SECONDS
        cur["pending"] = pending
        if pending or cur.get("token"):
            ups[aid] = cur
        self._save_local_json("uploads.json", ups)

    def _fill_from_existing(self, aid: str, pending: dict[str, list]) -> None:
        """Shadow files whose content the artifact already has (old copies too) are filled at once."""
        if not pending:
            return
        by_size: dict[int, list[Path]] = {}
        for rel, p, st in self._art_all(aid):
            if rel not in pending:
                by_size.setdefault(st.st_size, []).append(p)
        for rel, (h, size) in list(pending.items()):
            for p in by_size.get(int(size), []):
                data = p.read_bytes()
                if hashlib.sha256(data).hexdigest() == h:
                    F.write_bytes(self._target(aid, rel), data)
                    pending.pop(rel)
                    break

    def _check_size(self, aid: str | None, files: dict[str, bytes], shadow: dict[str, tuple[str, int]], drop: set[str]) -> None:
        """The limit is on the current files; old copies don't count."""
        total = sum(len(b) for b in files.values()) + sum(s for _, s in shadow.values())
        if aid:
            total += sum(st.st_size for rel, _, st in self._art_files(aid) if rel not in files and rel not in shadow and rel not in drop)
        if total > ARTIFACT_MAX_BYTES:
            raise ValueError(f"artifact would be {total} bytes; the limit is {ARTIFACT_MAX_BYTES}")

    @staticmethod
    def _publishable(rel: str) -> str:
        rel = clean_art_path(rel)
        if old_copy(rel):
            raise ValueError(f"{rel}: names like name@v3.ext are where Ritboard keeps old versions; pick another name")
        return rel

    def create_artifact(
        self,
        title: str,
        files: dict[str, bytes],
        entry: str = "index.html",
        description: str = "",
        source: str | None = None,
        note: str = "",
        shadows: dict[str, Any] | None = None,
        parent_id: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        files = {self._publishable(p): b for p, b in files.items()}
        shadow = {self._publishable(p): self._shadow(s) for p, s in (shadows or {}).items()}
        if set(files) & set(shadow):
            raise ValueError(f"given both with content and as a shadow file: {', '.join(sorted(set(files) & set(shadow)))}")
        if (entry or "index.html") != "index.html":
            raise ValueError("an artifact's page must be index.html; name the entry page that")
        if "index.html" not in files and "index.html" not in shadow:
            raise ValueError(f"index.html is missing; files given: {', '.join([*files, *shadow]) or 'none'}")
        self._check_size(None, files, shadow, set())
        title = (title or "").strip() or "Untitled"
        with self._lock:
            self._fresh(force=True)
            parent = self._folder(parent_id)
            path = self._new_path(parent, title, "")
            base = self.root / path
            base.mkdir(parents=True)
            for rel, data in files.items():
                F.write_bytes(self._target(path, rel), data)
            if "index.html" in shadow:  # marks the folder as an artifact until the real page arrives
                F.write_text(self._target(path, "index.html"), "<!doctype html><p>Waiting for index.html to be uploaded.</p>")
            self._set_yaml_meta(path, {
                "title": self._title_meta(title, path, True), "description": description or None,
                "category": category or None, "tags": list(tags or []) or None, "source": source or None,
                "version": 1,
            })
            pending = {p: [h, s] for p, (h, s) in shadow.items()}
            self._fill_from_existing(path, pending)
            self._set_uploads(path, pending, rotate=True)
            self._ensure_category(category)
            self._dirty = True
        self.changed("artifacts")
        return self.get_artifact(path)

    def _keep_old(self, aid: str, rel: str, version: int) -> None:
        """Before a file is replaced or removed, keep it as name@v<version>.ext."""
        src = self._target(aid, rel)
        if src.is_file():
            os.replace(src, self._target(aid, copy_name(rel, version)))

    def update_artifact(
        self,
        aid: str,
        files: dict[str, bytes] | None = None,
        remove: list[str] | None = None,
        note: str = "",
        title: str | None = None,
        description: str | None = None,
        entry: str | None = None,
        source: str | None = None,
        shadows: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a new version: write the given files, register shadow files, delete the paths
        in `remove`. Files that change or go are kept as name@v<old version>.ext first, so
        older versions stay readable. Every change to files replaces the upload secret."""
        if entry and entry != "index.html":
            raise ValueError("an artifact's page must be index.html")
        new_files = {self._publishable(p): b for p, b in (files or {}).items()}
        new_shadow = {self._publishable(p): self._shadow(s) for p, s in (shadows or {}).items()}
        if set(new_files) & set(new_shadow):
            raise ValueError("a path is given both with content and as a shadow file")
        drop = {self._publishable(p) for p in (remove or [])}
        if "index.html" in drop and "index.html" not in new_files and "index.html" not in new_shadow:
            raise ValueError("index.html can't be removed; it is what makes the folder an artifact")
        with self._lock:
            e = self._art(aid)
            aid = e.path
            pending = self._pending(aid)
            have = {rel for rel, _, _ in self._art_files(aid)} | set(pending)
            missing = drop - have
            if missing:
                raise ValueError(f"not in the artifact: {', '.join(sorted(missing))}")
            self._check_size(aid, new_files, new_shadow, drop)
            cur = self._art_version(e)
            changed = False
            for rel in drop:
                pending.pop(rel, None)
                self._keep_old(aid, rel, cur)
                changed = True
            for rel, data in new_files.items():
                p = self._target(aid, rel)
                if rel not in pending and p.is_file() and p.stat().st_size == len(data) and p.read_bytes() == data:
                    continue  # unchanged
                self._keep_old(aid, rel, cur)
                F.write_bytes(p, data)
                pending.pop(rel, None)
                changed = True
            for rel, (h, s) in new_shadow.items():
                p = self._target(aid, rel)
                if rel not in pending and p.is_file() and p.stat().st_size == s and hashlib.sha256(p.read_bytes()).hexdigest() == h:
                    continue  # already this content
                self._keep_old(aid, rel, cur)
                pending[rel] = [h, s]
                changed = True
            meta = dict(e.meta)
            if changed:
                self._fill_from_existing(aid, pending)
                self._set_uploads(aid, pending, rotate=True)
                meta["version"] = cur + 1
            if description is not None:
                meta["description"] = description or None
            if source is not None:
                meta["source"] = source or None
            if meta != e.meta:
                self._set_yaml_meta(aid, meta)
            self._dirty = True
        if title is not None and title.strip() and title.strip() != self._title(e):
            aid = self.update_finding(aid, title=title)["id"]
        self.changed("artifacts")
        if changed:
            self._art_event(aid, "version")
        return self.get_artifact(aid)

    def upload_links(self, aid: str, rotate: bool = False) -> dict[str, Any]:
        """The current upload secret and the files still waiting for it. rotate=True replaces
        the secret, which also renews an expired one."""
        aid = self._art(aid).path
        cur = self._local_json("uploads.json", {}).get(aid) or {}
        if rotate or not cur.get("token") or cur.get("expires", 0) < time.time():
            self._set_uploads(aid, self._pending(aid), rotate=True)
            cur = self._local_json("uploads.json", {}).get(aid) or {}
        pending = cur.get("pending") or {}
        return {
            "token": cur["token"],
            "expires_in_minutes": max(0, int((cur["expires"] - time.time()) / 60)),
            "pending": [{"path": p, "sha256": h, "size": s} for p, (h, s) in sorted(pending.items())],
        }

    def receive_upload(self, token: str, digest: str, data: bytes) -> dict[str, Any]:
        """Fill shadow files that have this hash, via an upload link."""
        with self._lock:
            ups = self._local_json("uploads.json", {})
            aid = next((a for a, u in ups.items() if u.get("token") and hmac.compare_digest(u["token"], token)), None)
            if aid is None:
                raise NotFound("this upload link is not valid; a newer publish replaced it, ask for fresh links")
            if ups[aid].get("expires", 0) < time.time():
                raise NotFound("this upload link has expired; ask for fresh links (upload_links)")
            out = self._fill(aid, digest, data)
        self.changed("artifacts")
        self._art_event(aid, "version")
        return out

    def fill_shadow(self, aid: str, digest: str, data: bytes) -> dict[str, Any]:
        """Fill shadow files directly, e.g. from a file on this computer."""
        aid = self._art(aid).path
        with self._lock:
            out = self._fill(aid, digest, data)
        self.changed("artifacts")
        self._art_event(aid, "version")
        return out

    def _fill(self, aid: str, digest: str, data: bytes) -> dict[str, Any]:
        digest = digest.lower()
        pending = self._pending(aid)
        targets = [p for p, (h, _) in pending.items() if h == digest]
        if not targets:
            raise NotFound("no file waiting for upload has that hash (it may have arrived already)")
        got = hashlib.sha256(data).hexdigest()
        if got != digest:
            raise ValueError(f"content does not match the hash: expected {digest}, got {got}")
        if len(data) != int(pending[targets[0]][1]):
            raise ValueError(f"expected {pending[targets[0]][1]} bytes, got {len(data)}")
        for t in targets:
            F.write_bytes(self._target(aid, t), data)
            pending.pop(t)
        ups = self._local_json("uploads.json", {})
        ups.setdefault(aid, {})["pending"] = pending
        self._save_local_json("uploads.json", ups)
        self._dirty = True
        return {"artifact_id": aid, "stored": sorted(targets), "still_waiting": len(pending)}

    def delete_artifact(self, aid: str) -> dict[str, Any]:
        aid = self._art(aid).path
        out = self.delete_finding(aid, recursive=True)
        return {"deleted": aid, "trashed_to": out["trashed_to"]}

    # ---- artifact database --------------------------------------------
    # Each artifact gets a small document store its pages use through window.claude.use("db").
    # A collection is a JSON file under the artifact's .data folder.

    def _coll_file(self, aid: str, collection: str) -> Path:
        segs = [urllib.parse.quote(s, safe="-_.~@+") for s in check_collection(collection).split("/")]
        return self._target(aid, "/".join([".data", *segs[:-1], f"{segs[-1]}.json"]))

    def _coll(self, aid: str, collection: str) -> dict[str, Any]:
        p = self._coll_file(aid, collection)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as e:
            raise ValueError(f"{p.name} in the artifact's .data folder is not valid JSON: {e}") from e
        return data if isinstance(data, dict) else {}

    def db_rev(self, aid: str) -> int:
        base = self.root / aid / ".data"
        items = []
        for dirpath, _, filenames in os.walk(base):
            for fn in filenames:
                if fn.endswith(".json"):
                    try:
                        st = os.stat(os.path.join(dirpath, fn))
                    except OSError:
                        continue
                    items.append([os.path.join(dirpath, fn), st.st_size, st.st_mtime_ns])
        return _stamp(items)

    @staticmethod
    def _doc_out(doc_id: str, d: dict[str, Any]) -> dict[str, Any]:
        return {"id": doc_id, "data": d.get("data"), "version": d.get("version", 1), "updatedAt": d.get("updatedAt")}

    def db_list(self, aid: str, collection: str) -> list[dict[str, Any]]:
        aid = self._art(aid).path
        return [self._doc_out(k, v) for k, v in sorted(self._coll(aid, collection).items()) if isinstance(v, dict)]

    def db_collections(self, aid: str) -> list[dict[str, Any]]:
        aid = self._art(aid).path
        base = self.root / aid / ".data"
        out = []
        for p in sorted(base.rglob("*.json")) if base.is_dir() else []:
            rel = p.relative_to(base).as_posix()[: -len(".json")]
            name = "/".join(urllib.parse.unquote(s) for s in rel.split("/"))
            try:
                out.append({"collection": name, "docs": len(self._coll(aid, name))})
            except ValueError:
                continue
        return out

    def db_get(self, aid: str, collection: str, doc_id: str) -> dict[str, Any]:
        aid = self._art(aid).path
        d = self._coll(aid, collection).get(check_doc_id(doc_id))
        if not isinstance(d, dict):
            raise NotFound(f"no document {collection}/{doc_id}")
        return self._doc_out(doc_id, d)

    def _save_coll(self, aid: str, collection: str, docs: dict[str, Any]) -> None:
        F.write_text(self._coll_file(aid, collection), json.dumps(docs, indent=1, ensure_ascii=False, sort_keys=True) + "\n")

    def db_write(
        self, aid: str, collection: str, doc_id: str, data: Any, merge: bool = False, if_version: int | None = None
    ) -> dict[str, Any]:
        aid = self._art(aid).path
        check_doc_id(doc_id)
        if not isinstance(data, dict):
            raise ValueError("a document must be a JSON object")
        with self._lock:
            docs = self._coll(aid, collection)
            cur = docs.get(doc_id) if isinstance(docs.get(doc_id), dict) else None
            cur_v = cur.get("version", 1) if cur else 0
            if if_version is not None and cur_v != if_version:
                raise Conflict(f"{collection}/{doc_id} is at version {cur_v}, not {if_version}")
            new = merge_doc(cur.get("data"), data) if (merge and cur) else (merge_doc({}, data) if merge else data)
            if len(json.dumps(new, ensure_ascii=False).encode()) > DOC_MAX_BYTES:
                raise ValueError(f"document is larger than {DOC_MAX_BYTES} bytes")
            t = now()
            docs[doc_id] = {"data": new, "version": cur_v + 1, "updatedAt": t}
            self._save_coll(aid, collection, docs)
        self._art_event(aid, "db")
        return {"id": doc_id, "data": new, "version": cur_v + 1, "updatedAt": t}

    def db_delete(self, aid: str, collection: str, doc_id: str, if_version: int | None = None) -> None:
        aid = self._art(aid).path
        check_doc_id(doc_id)
        with self._lock:
            docs = self._coll(aid, collection)
            cur = docs.get(doc_id) if isinstance(docs.get(doc_id), dict) else None
            cur_v = cur.get("version", 1) if cur else 0
            if if_version is not None and cur_v != if_version:
                raise Conflict(f"{collection}/{doc_id} is at version {cur_v}, not {if_version}")
            if cur is not None:
                docs.pop(doc_id)
                self._save_coll(aid, collection, docs)
        self._art_event(aid, "db")

    # ---- raw files --------------------------------------------------------
    def raw_file(self, path: str) -> Path:
        """A file in the workspace, for images and downloads. Nothing hidden, nothing outside."""
        rel = F.clean_rel(path)
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root) or not p.is_file():
            raise NotFound(f"{rel} not found")
        return p

    # ---- views ----------------------------------------------------------
    def state(self) -> dict[str, Any]:
        """Everything the UI needs for this workspace."""
        self._fresh()
        return {
            "workspace": self.summary(),
            "settings": self.settings(),
            "findings": self.list_findings(),
            "boards": self.list_boards(),
            "placements": self.placements(),
            "artifacts": self.list_artifacts(),
            "problems": list(self.problems),
        }

    def board_view(self, bid: str) -> dict[str, Any]:
        """A board as an agent sees it: placements with titles and bounds problems."""
        self._fresh()
        board = self.get_board_row(bid)
        placed = []
        for p in self.placements(board["id"]):
            card = self._card(p["finding_id"])
            e, pin = card if card else (None, None)
            out = p["x"] < 0 or p["y"] < 0 or p["x"] + p["w"] > board["width"] or p["y"] + p["h"] > board["height"]
            item = {
                "finding_id": p["finding_id"],
                "title": (self._title(e) + (f" (version {pin})" if pin else "")) if e else p["finding_id"],
                "kind": e.kind if e else "missing",
                "category": (e.meta.get("category") or None) if e else None,
                **{k: p[k] for k in ("x", "y", "w", "h", "z")},
                "out_of_bounds": out,
            }
            if not e:
                item["missing"] = True
            placed.append(item)
        return {
            "board": board,
            "placements": placed,
            "out_of_bounds": [p["finding_id"] for p in placed if p["out_of_bounds"]],
            "missing": [p["finding_id"] for p in placed if p.get("missing")],
        }


class Registry:
    """The workspaces this computer knows about: a list of folders in <data>/workspaces.yaml."""

    def __init__(self, data_dir: Path, notify: Notify | None = None, default_root: Path | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.list_file = self.data_dir / "workspaces.yaml"
        self.default_root = Path(default_root).expanduser() if default_root else Path.home() / "Ritboard"
        self.notify: Notify = notify or (lambda ws, what: None)
        self.secret = self._load_secret()
        self._stores: dict[str, WorkspaceStore] = {}
        self._items: list[dict[str, str]] = []
        self._sig: tuple[int, int] | None = None
        self._lock = threading.RLock()

    def _load_secret(self) -> bytes:
        p = self.data_dir / "secret"
        try:
            return p.read_text().strip().encode()
        except FileNotFoundError:
            s = secrets.token_hex(32)
            F.write_text(p, s + "\n")
            return s.encode()

    def _notify(self, ws: str, what: str) -> None:
        self.notify(ws, what)

    def _load(self) -> list[dict[str, str]]:
        with self._lock:
            try:
                st = self.list_file.stat()
                sig = (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                sig = None
            if sig != self._sig:
                raw = F.load_yaml(self.list_file) if sig else None
                items = (raw or {}).get("workspaces") if isinstance(raw, dict) else None
                self._items = [
                    {"id": str(i["id"]), "path": str(i["path"])}
                    for i in (items or []) if isinstance(i, dict) and i.get("id") and i.get("path")
                ]
                self._sig = sig
                for wid in list(self._stores):
                    if not any(i["id"] == wid and Path(i["path"]).resolve() == self._stores[wid].root for i in self._items):
                        self._stores.pop(wid)
            return list(self._items)

    def _save(self) -> None:
        F.write_text(self.list_file, F.dump_yaml({"workspaces": self._items}))
        self._sig = None

    def ids(self) -> list[str]:
        return [i["id"] for i in self._load()]

    def _open(self, item: dict[str, str]) -> WorkspaceStore:
        with self._lock:
            st = self._stores.get(item["id"])
            if st is None:
                root = Path(item["path"])
                if not root.is_dir():
                    raise NotFound(f"workspace {item['id']}: its folder {root} is missing")
                st = WorkspaceStore(root, item["id"], self._notify, self.secret)
                self._stores[item["id"]] = st
            return st

    def open_stores(self) -> list[WorkspaceStore]:
        out = []
        for i in self._load():
            try:
                out.append(self._open(i))
            except NotFound:
                pass
        return out

    def list(self) -> list[dict[str, Any]]:
        out = []
        for i in self._load():
            try:
                out.append(self._open(i).summary())
            except NotFound:
                out.append({"id": i["id"], "name": i["id"], "path": i["path"], "missing": True})
        return sorted(out, key=lambda w: (w["name"] or "").lower())

    def get(self, ref: str) -> WorkspaceStore:
        """Find a workspace by id, name (case-insensitive) or folder."""
        if not ref:
            raise NotFound("workspace is required; call list_workspaces to see them")
        items = self._load()
        for i in items:
            if i["id"] == ref:
                return self._open(i)
        low = ref.strip().lower()
        for i in items:
            try:
                st = self._open(i)
            except NotFound:
                continue
            if st.name.lower() == low or str(st.root).lower() == low.rstrip("/\\"):
                return st
        for i in items:
            if i["id"] == slugify(ref):
                return self._open(i)
        raise NotFound(f"workspace {ref!r} not found; existing: {', '.join(i['id'] for i in items) or 'none'}")

    def _register(self, root: Path, name: str) -> WorkspaceStore:
        with self._lock:
            items = self._load()
            for i in items:
                if Path(i["path"]).resolve() == root.resolve():
                    return self._open(i)
            base = slugify(name)
            wid, n = base, 2
            while any(i["id"] == wid for i in items):
                wid, n = f"{base}-{n}", n + 1
            self._items = items + [{"id": wid, "path": str(root)}]
            self._save()
            st = self._open({"id": wid, "path": str(root)})
        self._notify("*", "workspaces")
        return st

    def create(self, name: str, board_name: str | None = "Main", path: str | Path | None = None) -> WorkspaceStore:
        """Make a new workspace folder: at `path`, or under the default folder by name."""
        name = name.strip() or "Workspace"
        if path:
            root = Path(path).expanduser().resolve()
            if (root / ".ritboard").exists():
                raise ValueError(f"{root} is already a workspace; open it with open_workspace")
            if root.exists() and any(root.iterdir()):
                raise ValueError(f"{root} is not empty; use open_workspace to use its files as they are")
        else:
            base = F.safe_name(name)
            root = self.default_root / base
            n = 2
            while root.exists():
                root, n = self.default_root / f"{base} {n}", n + 1
        root.mkdir(parents=True, exist_ok=True)
        WorkspaceStore(root, "_new", name=name)
        st = self._register(root, name)
        if board_name and not st.list_boards():
            st.create_board(board_name)
        return st

    def open(self, path: str | Path, name: str | None = None) -> WorkspaceStore:
        """Use an existing folder as a workspace. Its files stay as they are; Ritboard adds .ritboard/."""
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"no such folder: {root}")
        st = self._register(root, WorkspaceStore(root, "_new", name=name or root.name).name)
        if not st.list_boards():
            st.create_board("Main")
        return st

    def rename(self, ref: str, name: str) -> WorkspaceStore:
        st = self.get(ref)
        st.set_name(name)
        self._notify("*", "workspaces")
        return st

    def remove(self, ref: str) -> str:
        """Forget a workspace. Its folder and files stay where they are."""
        with self._lock:
            try:
                st = self.get(ref)
                wid, path = st.id, str(st.root)
            except NotFound:
                item = next((i for i in self._load() if i["id"] == ref), None)
                if not item:
                    raise
                wid, path = item["id"], item["path"]
            self._items = [i for i in self._load() if i["id"] != wid]
            self._save()
            self._stores.pop(wid, None)
        self._notify("*", "workspaces")
        return path

    delete = remove

    def close(self) -> None:
        with self._lock:
            self._stores.clear()
