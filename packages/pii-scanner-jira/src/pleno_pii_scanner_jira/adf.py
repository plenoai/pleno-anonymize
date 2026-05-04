"""ADF (Atlassian Document Format) -> plain-text converter.

Cloud Jira returns issue body fields (`description`, `comment[].body`)
as ADF — a JSON tree of typed nodes. Detectors downstream operate on
plain text, so we flatten the tree before handing it off.

Why plain text rather than Markdown:

* The Jira PII scan target is the user-visible body content; ADF
  formatting (bold, panels, tables) carries no PII signal but adds
  syntactic noise that confuses regex match positions.
* ADF table cells, panel bodies and code blocks all contain the same
  rich-text leaves; a flat newline-separated stream keeps the detector
  surface uniform across containers.

Defensive contract: an unknown node `type` emits
`<!-- unsupported: {type} -->` and the recursion continues. A future
ADF schema change must never crash a scan. Recursion is bounded at
`MAX_DEPTH=100` — well above Jira's UI cap (~6 levels of nested
panels) but enough to defend against a pathological cycle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


# Hard cap on ADF tree recursion. Jira's UI imposes <10 levels in
# practice; 100 leaves headroom while bounding the worst case if a
# malformed response (or a future API bug) accidentally produces a
# self-referential `content` array. Picked to match the equivalent
# Notion connector cap so operators see one consistent depth budget.
MAX_DEPTH = 100


# Sentinel emitted when recursion is truncated. Picked so an operator
# grepping output can find truncated regions without ambiguity vs. real
# document content.
DEPTH_TRUNCATED_MARKER = "<!-- depth-truncated -->"


# Block-level node types that must produce a trailing newline so
# detectors do not see neighbouring blocks as one run-on sentence.
_BLOCK_TYPES: frozenset[str] = frozenset(
    {
        "paragraph",
        "heading",
        "bulletList",
        "orderedList",
        "listItem",
        "codeBlock",
        "blockquote",
        "panel",
        "table",
        "tableRow",
        "rule",
        "mediaSingle",
        "mediaGroup",
        "expand",
        "nestedExpand",
    }
)


def adf_to_text(node: Any, *, max_depth: int = MAX_DEPTH) -> str:
    """Render an ADF node (or document root) as plain text.

    `node` is the parsed JSON. The document root looks like
    `{"type": "doc", "version": 1, "content": [...]}`; individual nodes
    look like `{"type": "paragraph", "content": [...]}` or
    `{"type": "text", "text": "...", "marks": [...]}`. We accept either
    a root document, an arbitrary node, or a list of nodes — all three
    forms occur in the wild (issue `description` is a doc, comment
    bodies are docs, but tests sometimes hand us a bare paragraph).

    Anything that is not a Mapping or a Sequence (None, int, the wrong
    type) renders as the empty string. Defensive: ADF parsers in
    Atlassian's own SDKs treat malformed payloads the same way.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        # A bare string slipped past the schema; render it verbatim so
        # the operator at least sees the leaked content.
        return node
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        # A list of nodes — common when callers pass `doc["content"]`
        # directly. Render as a sibling sequence.
        return _render_sequence(list(node), depth=0, max_depth=max_depth)
    if not isinstance(node, Mapping):
        return ""
    return _render_node(node, depth=0, max_depth=max_depth)


def _render_node(node: Mapping[str, Any], *, depth: int, max_depth: int) -> str:
    """Dispatch on `node['type']`."""
    if depth >= max_depth:
        return DEPTH_TRUNCATED_MARKER
    node_type = node.get("type")
    if not isinstance(node_type, str):
        return ""
    handler = _HANDLERS.get(node_type)
    if handler is not None:
        return handler(node, depth=depth, max_depth=max_depth)
    # Unknown node type — emit the sentinel and continue rendering any
    # children present. This keeps the scan output forward-compatible
    # with new ADF node types Atlassian rolls out.
    inner = _render_children(node, depth=depth + 1, max_depth=max_depth)
    marker = f"<!-- unsupported: {node_type} -->"
    return f"{marker}\n{inner}".rstrip() if inner else marker


def _render_children(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    """Render `node['content']` as a sibling sequence."""
    children = node.get("content")
    if not isinstance(children, Sequence) or isinstance(children, (str, bytes)):
        return ""
    return _render_sequence(list(children), depth=depth, max_depth=max_depth)


def _render_sequence(
    nodes: list[Any], *, depth: int, max_depth: int
) -> str:
    """Render a list of sibling nodes, joining block-level ones with newlines.

    Inline runs (`text`, `mention`, `emoji`, inline marks) are
    concatenated verbatim. Block-level nodes get a trailing newline so
    detectors see one paragraph per line. We do not collapse multiple
    blank lines because the result is downstream-only — humans never
    read it.
    """
    parts: list[str] = []
    for child in nodes:
        if not isinstance(child, Mapping):
            continue
        rendered = _render_node(child, depth=depth, max_depth=max_depth)
        if not rendered:
            continue
        if child.get("type") in _BLOCK_TYPES:
            parts.append(rendered.rstrip("\n") + "\n")
        else:
            parts.append(rendered)
    return "".join(parts).rstrip("\n")


# ---------------------------------------------------------------------
# leaf handlers
# ---------------------------------------------------------------------


def _handle_text(node: Mapping[str, Any], **_: Any) -> str:
    """`{type: "text", text: "...", marks: [...]}` — the inline leaf.

    Marks (bold, italic, strike, code, underline, link) carry no PII
    signal in plain text, so we drop them. Link marks are an exception:
    the URL itself can contain credentials (`?token=...`) so we surface
    the href next to the visible text.
    """
    text = node.get("text")
    if not isinstance(text, str):
        return ""
    marks = node.get("marks")
    if isinstance(marks, Sequence) and not isinstance(marks, (str, bytes)):
        for mark in marks:
            if not isinstance(mark, Mapping):
                continue
            if mark.get("type") == "link":
                attrs = mark.get("attrs")
                if isinstance(attrs, Mapping):
                    href = attrs.get("href")
                    if isinstance(href, str) and href and href != text:
                        # Render `text (href)` so a detector sees both
                        # the visible label and the embedded URL. This
                        # catches the common phishing pattern where a
                        # benign label masks a token-bearing URL.
                        return f"{text} ({href})"
    return text


def _handle_paragraph(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    return _render_children(node, depth=depth + 1, max_depth=max_depth)


def _handle_heading(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    # Drop the `# ` prefix Markdown renderers would emit; downstream
    # consumers only care about the body text. The level lives in
    # `attrs.level` if a future renderer wants it.
    return _render_children(node, depth=depth + 1, max_depth=max_depth)


def _handle_list(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    """bulletList / orderedList — render each `listItem` on its own line."""
    children = node.get("content")
    if not isinstance(children, Sequence) or isinstance(children, (str, bytes)):
        return ""
    items: list[str] = []
    for child in children:
        if not isinstance(child, Mapping):
            continue
        if child.get("type") != "listItem":
            continue
        rendered = _handle_list_item(child, depth=depth + 1, max_depth=max_depth)
        if rendered:
            items.append(rendered)
    return "\n".join(items)


def _handle_list_item(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    inner = _render_children(node, depth=depth + 1, max_depth=max_depth)
    # Strip the trailing newline added by block children inside the
    # item; the outer list joiner re-adds one between items.
    return inner.rstrip("\n")


def _handle_code_block(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    """codeBlock — flatten to its inner text, dropping the language attr.

    Detectors care about the literal code (a `password = "x"` constant
    is a finding regardless of the language tag); `attrs.language` is
    metadata that adds nothing to PII signal.
    """
    return _render_children(node, depth=depth + 1, max_depth=max_depth)


def _handle_inline_code(node: Mapping[str, Any], **_: Any) -> str:
    """Some ADF dialects emit a top-level `code` node; treat as text."""
    text = node.get("text")
    if isinstance(text, str):
        return text
    return ""


def _handle_mention(node: Mapping[str, Any], **_: Any) -> str:
    """`{type: "mention", attrs: {id, text, accessLevel}}`.

    The `attrs.text` is the user-visible `@Display Name` form; the
    `attrs.id` is the Atlassian Account ID — both are useful: the
    display name often contains a real name (PII), and the id is the
    canonical identifier downstream tools key on.
    """
    attrs = node.get("attrs")
    if not isinstance(attrs, Mapping):
        return ""
    text = attrs.get("text")
    if isinstance(text, str) and text:
        return text
    mention_id = attrs.get("id")
    if isinstance(mention_id, str) and mention_id:
        return f"@{mention_id}"
    return ""


def _handle_emoji(node: Mapping[str, Any], **_: Any) -> str:
    attrs = node.get("attrs")
    if isinstance(attrs, Mapping):
        shortname = attrs.get("shortName")
        if isinstance(shortname, str):
            return shortname
        text = attrs.get("text")
        if isinstance(text, str):
            return text
    return ""


def _handle_hard_break(_node: Mapping[str, Any], **_: Any) -> str:
    return "\n"


def _handle_inline_card(node: Mapping[str, Any], **_: Any) -> str:
    """`inlineCard` — a Smart Link. The URL is the entire payload."""
    attrs = node.get("attrs")
    if not isinstance(attrs, Mapping):
        return ""
    url = attrs.get("url")
    if isinstance(url, str) and url:
        return url
    return ""


def _handle_media_single(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    """mediaSingle — wraps a single `media` node; surface its filename/url."""
    return _render_children(node, depth=depth + 1, max_depth=max_depth)


def _handle_media(node: Mapping[str, Any], **_: Any) -> str:
    attrs = node.get("attrs")
    if not isinstance(attrs, Mapping):
        return ""
    parts: list[str] = []
    for key in ("collection", "id", "url", "alt"):
        v = attrs.get(key)
        if isinstance(v, str) and v:
            parts.append(f"{key}={v}")
    return " ".join(parts)


def _handle_panel(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    """panel — coloured callout box. Render its content verbatim.

    The `attrs.panelType` (info, warning, error, note, success) carries
    no PII signal; we drop it.
    """
    return _render_children(node, depth=depth + 1, max_depth=max_depth)


def _handle_blockquote(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    return _render_children(node, depth=depth + 1, max_depth=max_depth)


def _handle_rule(_node: Mapping[str, Any], **_: Any) -> str:
    # Horizontal rule has no content; emit a single newline so adjacent
    # blocks do not run together when joined.
    return "\n"


def _handle_table(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    """table — newline-separated rows; each row is tab-separated cells."""
    children = node.get("content")
    if not isinstance(children, Sequence) or isinstance(children, (str, bytes)):
        return ""
    rows: list[str] = []
    for row in children:
        if not isinstance(row, Mapping):
            continue
        if row.get("type") != "tableRow":
            continue
        rendered = _handle_table_row(row, depth=depth + 1, max_depth=max_depth)
        if rendered:
            rows.append(rendered)
    return "\n".join(rows)


def _handle_table_row(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    cells = node.get("content")
    if not isinstance(cells, Sequence) or isinstance(cells, (str, bytes)):
        return ""
    cell_texts: list[str] = []
    for cell in cells:
        if not isinstance(cell, Mapping):
            continue
        if cell.get("type") not in ("tableCell", "tableHeader"):
            continue
        text = _render_children(cell, depth=depth + 1, max_depth=max_depth)
        cell_texts.append(text.replace("\n", " ").strip())
    return "\t".join(cell_texts)


def _handle_doc(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    """The document root; just descend into `content`."""
    return _render_children(node, depth=depth, max_depth=max_depth)


def _handle_expand(
    node: Mapping[str, Any], *, depth: int, max_depth: int
) -> str:
    """expand / nestedExpand — collapsible block. Render the body."""
    return _render_children(node, depth=depth + 1, max_depth=max_depth)


# Dispatch table. Defined after the handlers so the references are
# bound. `_HANDLERS.get(t)` returns None for unknown types, which the
# top-level `_render_node` translates into the unsupported sentinel.
_HANDLERS: Mapping[str, Any] = {
    "doc": _handle_doc,
    "text": _handle_text,
    "paragraph": _handle_paragraph,
    "heading": _handle_heading,
    "bulletList": _handle_list,
    "orderedList": _handle_list,
    "listItem": _handle_list_item,
    "codeBlock": _handle_code_block,
    "code": _handle_inline_code,
    "mention": _handle_mention,
    "emoji": _handle_emoji,
    "hardBreak": _handle_hard_break,
    "inlineCard": _handle_inline_card,
    "mediaSingle": _handle_media_single,
    "mediaGroup": _handle_media_single,
    "media": _handle_media,
    "panel": _handle_panel,
    "blockquote": _handle_blockquote,
    "rule": _handle_rule,
    "table": _handle_table,
    "tableRow": _handle_table_row,
    "tableCell": _handle_paragraph,
    "tableHeader": _handle_paragraph,
    "expand": _handle_expand,
    "nestedExpand": _handle_expand,
}


__all__ = [
    "DEPTH_TRUNCATED_MARKER",
    "MAX_DEPTH",
    "adf_to_text",
]
