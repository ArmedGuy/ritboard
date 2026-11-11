"""Small helpers for keeping findings as ordinary files.

A finding's kind comes from its extension. Metadata (title, category, tags, source) lives
in the file when the format has room for it: YAML front matter in Markdown, a leading
`<!--ritboard ... -->` comment in HTML. Other files keep theirs in `.ritboard/meta.yaml`.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any

import yaml

KIND_BY_EXT = {
    ".md": "md",
    ".markdown": "md",
    ".txt": "text",
    ".csv": "csv",
    ".tsv": "csv",
    ".html": "html",
    ".htm": "html",
    ".url": "url",
}
# Shown as plain text.
TEXT_EXTS = {
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log", ".xml", ".sql",
    ".py", ".js", ".mjs", ".ts", ".sh", ".ps1", ".bat", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".cu", ".cuh", ".rs", ".go", ".java", ".rb", ".lua", ".diff", ".patch",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp", ".ico"}
EXT_BY_KIND = {"md": ".md", "text": ".txt", "csv": ".csv", "html": ".html", "url": ".url"}
IMAGE_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
    "image/svg+xml": ".svg", "image/avif": ".avif", "image/bmp": ".bmp",
}
SKIP_NAMES = {"node_modules", "__pycache__", "Thumbs.db", "desktop.ini"}
META_KEYS = ("title", "category", "tags", "source", "description", "kind")


def kind_for(name: str, is_dir: bool = False) -> str:
    if is_dir:
        return "folder"
    ext = Path(name).suffix.lower()
    if ext in KIND_BY_EXT:
        return KIND_BY_EXT[ext]
    if ext in TEXT_EXTS:
        return "text"
    if ext in IMAGE_EXTS:
        return "image"
    return "file"


def skipped(name: str) -> bool:
    """Hidden files and folders (.git, .ritboard, .data, editor temp files) and a few others."""
    return name.startswith(".") or name.startswith("~$") or name in SKIP_NAMES


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_name(title: str) -> str:
    """A file or folder name that works on Windows, macOS and Linux."""
    s = re.sub(r"\s*:\s+", " - ", (title or "").strip())  # "Stalls: step 4k" -> "Stalls - step 4k"
    s = _BAD_CHARS.sub("-", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ").lstrip(".")
    s = s[:120].rstrip(". ")
    if not s:
        s = "Untitled"
    if s.lower() in _RESERVED:
        s += "_"
    return s


def clean_rel(path: str) -> str:
    """A relative path inside a workspace: forward slashes, no '..', no leading '/', nothing hidden."""
    p = (path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    parts = p.split("/")
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or any(s in ("", ".", "..") for s in parts):
        raise ValueError(f"bad path {path!r}; use a path inside the workspace like 'Notes/idea.md'")
    if any(s.startswith(".") for s in parts):
        raise ValueError(f"{path!r}: names starting with '.' are reserved")
    return p


def parent_of(path: str) -> str | None:
    return path.rsplit("/", 1)[0] if "/" in path else None


def is_under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "/")


# ---- text with remembered line endings ------------------------------------
def read_text(p: Path) -> tuple[str, str]:
    raw = p.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    nl = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode("utf-8", "replace").replace("\r\n", "\n"), nl


def write_bytes(p: Path, data: bytes) -> None:
    """Write via a temporary file and rename, so readers never see half a file."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f".~{p.name}.{secrets.token_hex(4)}.tmp"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_text(p: Path, text: str, nl: str = "\n") -> None:
    write_bytes(p, text.replace("\r\n", "\n").replace("\n", nl).encode("utf-8"))


# ---- YAML -------------------------------------------------------------------
def load_yaml(p: Path) -> Any:
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except yaml.YAMLError as e:
        raise ValueError(f"{p.name} is not valid YAML: {e}") from e


def dump_yaml(data: Any, flow_lists: bool = False) -> str:
    return yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, width=1000, default_flow_style=None if flow_lists else False
    )


# ---- metadata inside files ---------------------------------------------------
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)?---[ \t]*(?:\n|\Z)", re.S)
_HTML_META = re.compile(r"\A\s*<!--ritboard[ \t]*\n(.*?)-->[ \t]*\n?", re.S)


def _as_meta(raw: Any) -> dict[str, Any] | None:
    return raw if isinstance(raw, dict) else None


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    m = _FM.match(text)
    if not m:
        return {}, text
    try:
        meta = _as_meta(yaml.safe_load(m.group(1) or "") or {})
    except yaml.YAMLError:
        meta = None
    if meta is None:
        return {}, text
    return meta, text[m.end():]


def join_front_matter(meta: dict[str, Any], body: str) -> str:
    meta = {k: v for k, v in meta.items() if v not in (None, "", [])}
    if not meta:
        return body
    return "---\n" + dump_yaml(meta) + "---\n" + body


def split_html_meta(text: str) -> tuple[dict[str, Any], str]:
    m = _HTML_META.match(text)
    if not m:
        return {}, text
    try:
        meta = _as_meta(yaml.safe_load(m.group(1)) or {})
    except yaml.YAMLError:
        meta = None
    return (meta, text[m.end():]) if meta is not None else ({}, text)


def join_html_meta(meta: dict[str, Any], body: str) -> str:
    meta = {k: v for k, v in meta.items() if v not in (None, "", [])}
    if not meta:
        return body
    return "<!--ritboard\n" + dump_yaml(meta) + "-->\n" + body


def parse_url_file(text: str) -> str:
    """Windows .url shortcut ([InternetShortcut] URL=...) or just a URL on the first line."""
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("URL="):
            return line[4:].strip()
    for line in text.splitlines():
        if line.strip() and not line.strip().startswith("["):
            return line.strip()
    return ""


def url_file(url: str) -> str:
    return f"[InternetShortcut]\nURL={url.strip()}\n"
