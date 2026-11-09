"""Turn finding content into safe HTML for cards.

Markdown is rendered with raw HTML disabled, so it can go straight into the
page. Kinds that carry their own HTML (html, url) are shown in sandboxed
iframes by the client and are not rendered here.
"""

from __future__ import annotations

import csv
import html
import io

from markdown_it import MarkdownIt

KINDS = ("md", "text", "csv", "html", "url", "image", "folder")
KIND_ALIASES = {
    "markdown": "md",
    "table": "csv",
    "txt": "text",
    "plain": "text",
    "iframe": "url",
    "link": "url",
    "artifact": "url",
    "img": "image",
    "group": "folder",
    "dir": "folder",
}

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(
    ["table", "strikethrough"]
)


def normalize_kind(kind: str | None) -> str:
    k = (kind or "md").strip().lower()
    k = KIND_ALIASES.get(k, k)
    if k not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; use one of {', '.join(KINDS)}")
    return k


def render(kind: str, content: str) -> str | None:
    """Return card HTML for text-like kinds, or None for kinds the client frames."""
    if kind == "md":
        return _md.render(content or "")
    if kind == "text":
        return f"<pre>{html.escape(content or '')}</pre>"
    if kind == "csv":
        return _csv_table(content or "")
    return None


def _csv_table(content: str) -> str:
    text = content.strip("\n")
    if not text:
        return "<table></table>"
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return "<table></table>"
    head, body = rows[0], rows[1:]
    out = ["<table><thead><tr>"]
    out += [f"<th>{html.escape(c)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)
