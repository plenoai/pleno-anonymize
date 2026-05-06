"""Notion block tree + database property → Markdown converters.

Detectors (regex + NER) downstream operate on plain text, so we
flatten the Notion document model into Markdown before handing it
off. Markdown is preferred over a custom AST because:

* It preserves enough structure (lists, headings, code fences) that a
  human reading a finding's context can still recognise the original
  page.
* Mention objects (`@user`, `@page`, `@date`) are rendered as
  Markdown links so detectors can still match emails / dates that
  appear inside the mention text.
* Code fences keep language tags so a Python `password = "x"` inside
  a `code` block stays distinguishable from the same string in prose.

Defensive contract: an unknown block type emits
`<!-- unsupported: {type} -->` and the recursion continues. A future
Notion API change must never crash a scan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


# Hard cap on block-tree recursion depth. Notion's UI imposes ~50 levels
# in practice; 100 leaves headroom while bounding the worst case if a
# malformed response (or a future API bug) accidentally produces a cycle.
MAX_DEPTH = 100

# Sentinel emitted when recursion is truncated. Picked so an operator
# grepping output can find truncated regions without ambiguity vs. real
# document content.
DEPTH_TRUNCATED_MARKER = "<!-- depth-truncated -->"

# Property keys that carry no PII signal but are present on every database
# row; serializing them would dilute the detector input with noise.
_LOW_SIGNAL_PROPERTY_TYPES = frozenset(
    {"created_time", "last_edited_time", "created_by", "last_edited_by"}
)


# ---------------------------------------------------------------------
# rich text
# ---------------------------------------------------------------------


def render_rich_text(rich_text: Sequence[Mapping[str, Any]] | None) -> str:
    """Concatenate a Notion rich-text array into Markdown.

    Each rich-text element is one of:

    * `{type: "text", text: {content, link}, annotations: {...}}`
    * `{type: "mention", mention: {type: ...}, plain_text, href}`
    * `{type: "equation", equation: {expression}, plain_text}`

    Annotations (bold, italic, strikethrough, code) are wrapped with
    Markdown markers in the order Markdown parsers accept them.
    """
    if not rich_text:
        return ""
    parts: list[str] = []
    for element in rich_text:
        if not isinstance(element, Mapping):
            continue
        rendered = _render_rich_text_element(element)
        if rendered:
            parts.append(rendered)
    return "".join(parts)


def _render_rich_text_element(element: Mapping[str, Any]) -> str:
    el_type = element.get("type")
    if el_type == "text":
        return _render_text_element(element)
    if el_type == "mention":
        return _render_mention_element(element)
    if el_type == "equation":
        # Inline equations are wrapped in `$...$` so a downstream Markdown
        # renderer still treats them as math, while detectors that read
        # raw text see the LaTeX source verbatim.
        equation = element.get("equation")
        expression = (
            equation.get("expression", "") if isinstance(equation, Mapping) else ""
        )
        return f"${expression}$" if expression else _plain(element)
    # Unknown rich-text type: fall back to the `plain_text` field that
    # Notion includes on every element for accessibility. This keeps the
    # raw user-visible text reachable even if the type is one we haven't
    # mapped.
    return _plain(element)


def _render_text_element(element: Mapping[str, Any]) -> str:
    text_obj = element.get("text") or {}
    content = text_obj.get("content", "") if isinstance(text_obj, Mapping) else ""
    link = text_obj.get("link") if isinstance(text_obj, Mapping) else None
    href = link.get("url") if isinstance(link, Mapping) else None
    rendered = _apply_annotations(content, element.get("annotations") or {})
    if href:
        # Markdown link: link text already carries annotations.
        return f"[{rendered}]({href})"
    return rendered


def _render_mention_element(element: Mapping[str, Any]) -> str:
    mention = element.get("mention") or {}
    plain_text = element.get("plain_text") or ""
    href = element.get("href")
    if not isinstance(mention, Mapping):
        return _apply_annotations(plain_text, element.get("annotations") or {})
    mention_type = mention.get("type")
    # Build a `notion://` URL for mention types that don't carry an
    # explicit href so detectors still see structured pointers (and so
    # downstream finding rendering can resolve them later).
    if not href:
        if mention_type == "user":
            user = mention.get("user") or {}
            href = (
                f"notion://user/{user.get('id', 'unknown')}"
                if isinstance(user, Mapping)
                else None
            )
        elif mention_type == "page":
            page = mention.get("page") or {}
            href = (
                f"notion://page/{page.get('id', 'unknown')}"
                if isinstance(page, Mapping)
                else None
            )
        elif mention_type == "database":
            db = mention.get("database") or {}
            href = (
                f"notion://database/{db.get('id', 'unknown')}"
                if isinstance(db, Mapping)
                else None
            )
        elif mention_type == "date":
            date = mention.get("date") or {}
            start = date.get("start", "") if isinstance(date, Mapping) else ""
            href = f"notion://date/{start}"
    rendered = _apply_annotations(plain_text, element.get("annotations") or {})
    if href:
        return f"[{rendered}]({href})"
    return rendered


def _apply_annotations(text: str, annotations: Mapping[str, Any]) -> str:
    r"""Wrap `text` with Markdown markers for each truthy annotation.

    Order: code (innermost) → strikethrough → italic → bold → output.
    Wrapping `code` first keeps inline code legible inside other
    decorations (`**\`x\`**`), and `bold` last is the convention every
    Markdown parser tolerates.
    """
    if not text:
        return text
    if not isinstance(annotations, Mapping):
        return text
    if annotations.get("code"):
        text = f"`{text}`"
    if annotations.get("strikethrough"):
        text = f"~~{text}~~"
    if annotations.get("italic"):
        text = f"*{text}*"
    if annotations.get("bold"):
        text = f"**{text}**"
    return text


def _plain(element: Mapping[str, Any]) -> str:
    """Best-effort fallback to the `plain_text` Notion attaches everywhere."""
    plain = element.get("plain_text")
    return plain if isinstance(plain, str) else ""


# ---------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------


def render_blocks(
    blocks: Sequence[Mapping[str, Any]] | None,
    *,
    children_for: "ChildrenLookup | None" = None,
    include_archived: bool = False,
    depth: int = 0,
) -> str:
    """Render a flat list of sibling blocks into Markdown.

    `children_for(block_id) -> list[block]` is the recursion seam: the
    connector's discover/fetch path hydrates child block lists with
    real HTTP calls, while tests inject a dict-backed lookup so the
    converter is fully exercised in isolation.

    `depth` is the current recursion depth. Beyond `MAX_DEPTH` we emit a
    sentinel and stop recursing — Notion's response model can't directly
    express a cycle but a future API change or a malformed mock could.
    """
    if not blocks:
        return ""
    if depth >= MAX_DEPTH:
        return DEPTH_TRUNCATED_MARKER
    out: list[str] = []
    # Numbered-list rendering needs a running counter scoped to the
    # current sibling group, restarted whenever a non-list block breaks
    # the run. Notion stores no list index — clients compute it.
    numbered_counter = 0
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        if not include_archived and block.get("archived"):
            continue
        block_type = block.get("type")
        if block_type == "numbered_list_item":
            numbered_counter += 1
        else:
            numbered_counter = 0
        rendered = _render_block(
            block,
            children_for=children_for,
            include_archived=include_archived,
            depth=depth,
            numbered_index=numbered_counter,
        )
        if rendered:
            out.append(rendered)
    return "\n\n".join(p for p in out if p)


# Type alias — kept as a string-quoted forward reference in the public
# signature to avoid circular imports with the connector module.
ChildrenLookup = Any


def _render_block(
    block: Mapping[str, Any],
    *,
    children_for: ChildrenLookup | None,
    include_archived: bool,
    depth: int,
    numbered_index: int,
) -> str:
    block_type = block.get("type")
    payload = block.get(block_type) if isinstance(block_type, str) else None
    if not isinstance(payload, Mapping):
        payload = {}
    has_children = bool(block.get("has_children"))

    handler = _BLOCK_HANDLERS.get(block_type or "")
    if handler is None:
        # Unknown / unsupported block type. Emit a comment so an operator
        # reading raw output sees what was skipped, but never crash.
        return f"<!-- unsupported: {block_type} -->"

    body = handler(payload, numbered_index=numbered_index)

    if has_children and children_for is not None and block_type not in {"table"}:
        # `table` handles its own children (rows) inline; everything else
        # appends nested children below the parent block, indented two
        # spaces per nesting level so a Markdown renderer treats them as
        # sub-list items where applicable.
        children = children_for(block.get("id"))
        if children:
            child_md = render_blocks(
                children,
                children_for=children_for,
                include_archived=include_archived,
                depth=depth + 1,
            )
            if child_md:
                body = f"{body}\n{_indent(child_md, '  ')}" if body else child_md
    elif block_type == "table" and children_for is not None:
        # Tables: children are `table_row` blocks. Render them inline
        # using the table-specific formatter so the result is a single
        # Markdown table block rather than a header followed by indented
        # rows.
        rows = children_for(block.get("id")) or []
        body = _render_table(payload, rows)
    return body


def _render_paragraph(payload: Mapping[str, Any], **_: Any) -> str:
    return render_rich_text(payload.get("rich_text"))


def _render_heading(level: int):
    prefix = "#" * level

    def _h(payload: Mapping[str, Any], **_: Any) -> str:
        text = render_rich_text(payload.get("rich_text"))
        return f"{prefix} {text}" if text else prefix

    return _h


def _render_bulleted(payload: Mapping[str, Any], **_: Any) -> str:
    return f"- {render_rich_text(payload.get('rich_text'))}"


def _render_numbered(
    payload: Mapping[str, Any], *, numbered_index: int = 1, **_: Any
) -> str:
    return f"{numbered_index}. {render_rich_text(payload.get('rich_text'))}"


def _render_to_do(payload: Mapping[str, Any], **_: Any) -> str:
    box = "[x]" if payload.get("checked") else "[ ]"
    return f"- {box} {render_rich_text(payload.get('rich_text'))}"


def _render_toggle(payload: Mapping[str, Any], **_: Any) -> str:
    # Notion toggles render as a `<details>` summary in their HTML
    # export; we use the same so detectors still see the toggle title
    # alongside the body when a renderer collapses the children.
    return f"<details><summary>{render_rich_text(payload.get('rich_text'))}</summary></details>"


def _render_code(payload: Mapping[str, Any], **_: Any) -> str:
    language = payload.get("language") or ""
    body = render_rich_text(payload.get("rich_text"))
    return f"```{language}\n{body}\n```"


def _render_quote(payload: Mapping[str, Any], **_: Any) -> str:
    text = render_rich_text(payload.get("rich_text"))
    return f"> {text}" if text else ">"


def _render_callout(payload: Mapping[str, Any], **_: Any) -> str:
    icon = payload.get("icon") or {}
    emoji = icon.get("emoji") if isinstance(icon, Mapping) else None
    body = render_rich_text(payload.get("rich_text"))
    prefix = f"{emoji} " if emoji else ""
    return f"> {prefix}{body}"


def _render_divider(_: Mapping[str, Any], **__: Any) -> str:
    return "---"


def _render_table_placeholder(_: Mapping[str, Any], **__: Any) -> str:
    # Real rendering happens in `_render_table`; without children we have
    # nothing to emit.
    return ""


def _render_table_row(payload: Mapping[str, Any], **_: Any) -> str:
    cells = payload.get("cells") or []
    rendered_cells = [
        render_rich_text(cell) for cell in cells if isinstance(cell, Sequence)
    ]
    return "| " + " | ".join(rendered_cells) + " |"


def _render_equation(payload: Mapping[str, Any], **_: Any) -> str:
    expression = payload.get("expression", "")
    return f"$$\n{expression}\n$$" if expression else ""


def _render_embed(payload: Mapping[str, Any], **_: Any) -> str:
    url = payload.get("url", "")
    return f"[embed]({url})" if url else ""


def _render_bookmark(payload: Mapping[str, Any], **_: Any) -> str:
    url = payload.get("url", "")
    caption = render_rich_text(payload.get("caption"))
    if not url:
        return ""
    label = caption or url
    return f"[{label}]({url})"


def _render_link_to_page(payload: Mapping[str, Any], **_: Any) -> str:
    target_type = payload.get("type")
    target_id = payload.get(target_type) if isinstance(target_type, str) else None
    if isinstance(target_id, str):
        return f"[link]({_notion_uri(target_type, target_id)})"
    return ""


def _render_child_page(payload: Mapping[str, Any], **_: Any) -> str:
    title = payload.get("title", "")
    return f"## {title}" if title else ""


def _render_child_database(payload: Mapping[str, Any], **_: Any) -> str:
    title = payload.get("title", "")
    return f"## {title} (database)" if title else "## (database)"


def _render_table(payload: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    """Materialize a Notion `table` block + its `table_row` children.

    Notion stores `has_column_header` / `has_row_header` on the table
    payload so we can emit the standard Markdown header separator only
    when the source explicitly declared a header row. Without a header
    row, every row is data — we still emit a separator row so the
    output remains valid Markdown table syntax (some parsers reject
    tables without a header).
    """
    if not rows:
        return ""
    table_width = int(payload.get("table_width") or 0) or _row_width(rows[0])
    header_row, *body_rows = (
        rows
        if payload.get("has_column_header")
        else (
            [
                {
                    "type": "table_row",
                    "table_row": {"cells": [[] for _ in range(table_width)]},
                }
            ]
            + list(rows)
        )
    )
    parts = [
        _render_one_row(header_row),
        "| " + " | ".join(["---"] * table_width) + " |",
    ]
    for r in body_rows:
        parts.append(_render_one_row(r))
    return "\n".join(parts)


def _render_one_row(row: Mapping[str, Any]) -> str:
    payload = row.get("table_row") or {}
    if not isinstance(payload, Mapping):
        return ""
    return _render_table_row(payload)


def _row_width(row: Mapping[str, Any]) -> int:
    payload = row.get("table_row") or {}
    if not isinstance(payload, Mapping):
        return 0
    cells = payload.get("cells") or []
    return len(cells) if isinstance(cells, Sequence) else 0


def _notion_uri(kind: str | None, ident: str) -> str:
    return f"notion://{kind or 'object'}/{ident}"


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


# Dispatch table — keyed by Notion `type` discriminator. Lookup miss →
# unsupported-comment fallback in `_render_block`.
_BLOCK_HANDLERS: dict[str, Any] = {
    "paragraph": _render_paragraph,
    "heading_1": _render_heading(1),
    "heading_2": _render_heading(2),
    "heading_3": _render_heading(3),
    "bulleted_list_item": _render_bulleted,
    "numbered_list_item": _render_numbered,
    "to_do": _render_to_do,
    "toggle": _render_toggle,
    "code": _render_code,
    "quote": _render_quote,
    "callout": _render_callout,
    "divider": _render_divider,
    "table": _render_table_placeholder,
    "table_row": _render_table_row,
    "equation": _render_equation,
    "embed": _render_embed,
    "bookmark": _render_bookmark,
    "link_to_page": _render_link_to_page,
    "child_page": _render_child_page,
    "child_database": _render_child_database,
}


# ---------------------------------------------------------------------
# database properties
# ---------------------------------------------------------------------


def render_database_row(properties: Mapping[str, Any] | None) -> str:
    """Serialize a database row's `properties` map to `key: value` lines.

    Properties are emitted in the order Notion returned them so the
    output mirrors the column ordering the workspace owner configured.
    `created_time` / `last_edited_time` / `created_by` / `last_edited_by`
    are skipped — they're high-volume metadata with no PII signal and
    would dilute the detector input.
    """
    if not properties:
        return ""
    lines: list[str] = []
    for name, prop in properties.items():
        if not isinstance(prop, Mapping):
            continue
        prop_type = prop.get("type")
        if prop_type in _LOW_SIGNAL_PROPERTY_TYPES:
            continue
        rendered = _render_property(prop, prop_type)
        if rendered is None:
            # Unknown property type — emit the name with a marker so an
            # operator can spot which column dropped out without crashing.
            lines.append(f"{name}: <!-- unsupported property: {prop_type} -->")
            continue
        lines.append(f"{name}: {rendered}")
    return "\n".join(lines)


def _render_property(prop: Mapping[str, Any], prop_type: str | None) -> str | None:
    handler = _PROPERTY_HANDLERS.get(prop_type or "")
    if handler is None:
        return None
    return handler(prop)


def _prop_title(prop: Mapping[str, Any]) -> str:
    return render_rich_text(prop.get("title"))


def _prop_rich_text(prop: Mapping[str, Any]) -> str:
    return render_rich_text(prop.get("rich_text"))


def _prop_number(prop: Mapping[str, Any]) -> str:
    value = prop.get("number")
    return "" if value is None else str(value)


def _prop_select(prop: Mapping[str, Any]) -> str:
    select = prop.get("select")
    if not isinstance(select, Mapping):
        return ""
    return str(select.get("name", ""))


def _prop_multi_select(prop: Mapping[str, Any]) -> str:
    items = prop.get("multi_select") or []
    names = [str(item.get("name", "")) for item in items if isinstance(item, Mapping)]
    return ", ".join(n for n in names if n)


def _prop_status(prop: Mapping[str, Any]) -> str:
    status = prop.get("status")
    if not isinstance(status, Mapping):
        return ""
    return str(status.get("name", ""))


def _prop_date(prop: Mapping[str, Any]) -> str:
    date = prop.get("date")
    if not isinstance(date, Mapping):
        return ""
    start = date.get("start", "")
    end = date.get("end")
    return f"{start} → {end}" if end else str(start or "")


def _prop_email(prop: Mapping[str, Any]) -> str:
    return str(prop.get("email") or "")


def _prop_phone(prop: Mapping[str, Any]) -> str:
    return str(prop.get("phone_number") or "")


def _prop_url(prop: Mapping[str, Any]) -> str:
    return str(prop.get("url") or "")


def _prop_people(prop: Mapping[str, Any]) -> str:
    people = prop.get("people") or []
    rendered: list[str] = []
    for person in people:
        if not isinstance(person, Mapping):
            continue
        name = person.get("name")
        person_obj = person.get("person")
        email = person_obj.get("email") if isinstance(person_obj, Mapping) else None
        if name and email:
            rendered.append(f"{name} <{email}>")
        elif name:
            rendered.append(str(name))
        elif email:
            rendered.append(str(email))
        else:
            rendered.append(str(person.get("id", "")))
    return ", ".join(r for r in rendered if r)


def _prop_files(prop: Mapping[str, Any]) -> str:
    files = prop.get("files") or []
    rendered: list[str] = []
    for f in files:
        if not isinstance(f, Mapping):
            continue
        name = f.get("name", "")
        ftype = f.get("type")
        body = f.get(ftype) if isinstance(ftype, str) else None
        url = body.get("url", "") if isinstance(body, Mapping) else ""
        rendered.append(f"[{name}]({url})" if url else str(name))
    return ", ".join(r for r in rendered if r)


def _prop_checkbox(prop: Mapping[str, Any]) -> str:
    return "true" if prop.get("checkbox") else "false"


def _prop_relation(prop: Mapping[str, Any]) -> str:
    rels = prop.get("relation") or []
    ids = [r.get("id") for r in rels if isinstance(r, Mapping) and r.get("id")]
    return ", ".join(str(i) for i in ids)


def _prop_formula(prop: Mapping[str, Any]) -> str:
    formula = prop.get("formula")
    if not isinstance(formula, Mapping):
        return ""
    f_type = formula.get("type")
    value = formula.get(f_type) if isinstance(f_type, str) else None
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Mapping):
        # Date-typed formula has the same shape as a date property.
        if "start" in value:
            return _prop_date({"date": value})
        return ""
    return str(value)


def _prop_rollup(prop: Mapping[str, Any]) -> str:
    rollup = prop.get("rollup")
    if not isinstance(rollup, Mapping):
        return ""
    r_type = rollup.get("type")
    value = rollup.get(r_type) if isinstance(r_type, str) else None
    if value is None:
        return ""
    if r_type == "array" and isinstance(value, list):
        # Each array element is itself a typed property; recurse.
        rendered = [
            _render_property(item, item.get("type"))
            for item in value
            if isinstance(item, Mapping)
        ]
        return ", ".join(r for r in rendered if r)
    if isinstance(value, Mapping) and "start" in value:
        return _prop_date({"date": value})
    return str(value)


_PROPERTY_HANDLERS: dict[str, Any] = {
    "title": _prop_title,
    "rich_text": _prop_rich_text,
    "number": _prop_number,
    "select": _prop_select,
    "multi_select": _prop_multi_select,
    "status": _prop_status,
    "date": _prop_date,
    "email": _prop_email,
    "phone_number": _prop_phone,
    "url": _prop_url,
    "people": _prop_people,
    "files": _prop_files,
    "checkbox": _prop_checkbox,
    "relation": _prop_relation,
    "formula": _prop_formula,
    "rollup": _prop_rollup,
}


__all__ = [
    "DEPTH_TRUNCATED_MARKER",
    "MAX_DEPTH",
    "render_blocks",
    "render_database_row",
    "render_rich_text",
]
