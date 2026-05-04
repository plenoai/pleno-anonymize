"""Tests for `markdown` — block tree, rich text, and database property rendering."""

from __future__ import annotations

from typing import Any

from pleno_pii_scanner_notion.markdown import (
    DEPTH_TRUNCATED_MARKER,
    MAX_DEPTH,
    render_blocks,
    render_database_row,
    render_rich_text,
)


def text_element(content: str, *, link: str | None = None, **annotations: bool) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "type": "text",
        "text": {"content": content, "link": {"url": link} if link else None},
        "annotations": annotations,
        "plain_text": content,
    }
    return obj


def block(block_type: str, payload: dict[str, Any], *, archived: bool = False, has_children: bool = False, block_id: str = "b") -> dict[str, Any]:
    return {
        "id": block_id,
        "object": "block",
        "type": block_type,
        block_type: payload,
        "archived": archived,
        "has_children": has_children,
    }


# ---------------------------------------------------------------------
# rich text
# ---------------------------------------------------------------------


class TestRichText:
    def test_empty_returns_empty_string(self) -> None:
        assert render_rich_text(None) == ""
        assert render_rich_text([]) == ""

    def test_plain_text(self) -> None:
        assert render_rich_text([text_element("hello")]) == "hello"

    def test_bold_italic_strikethrough_code(self) -> None:
        out = render_rich_text(
            [
                text_element("a", bold=True),
                text_element("b", italic=True),
                text_element("c", strikethrough=True),
                text_element("d", code=True),
            ]
        )
        assert out == "**a***b*~~c~~`d`"

    def test_combined_annotations_nesting_order(self) -> None:
        # Order applied: code → strikethrough → italic → bold (outermost).
        out = render_rich_text(
            [text_element("x", bold=True, italic=True, strikethrough=True, code=True)]
        )
        assert out == "***~~`x`~~***"

    def test_text_with_link_emits_markdown_link(self) -> None:
        out = render_rich_text([text_element("docs", link="https://example.com")])
        assert out == "[docs](https://example.com)"

    def test_link_preserves_annotations_inside(self) -> None:
        out = render_rich_text([text_element("docs", link="https://x.test", bold=True)])
        assert out == "[**docs**](https://x.test)"

    def test_mention_user(self) -> None:
        element = {
            "type": "mention",
            "mention": {"type": "user", "user": {"id": "u1"}},
            "annotations": {},
            "plain_text": "@alice",
        }
        assert render_rich_text([element]) == "[@alice](notion://user/u1)"

    def test_mention_page(self) -> None:
        element = {
            "type": "mention",
            "mention": {"type": "page", "page": {"id": "p1"}},
            "annotations": {},
            "plain_text": "Roadmap",
        }
        assert render_rich_text([element]) == "[Roadmap](notion://page/p1)"

    def test_mention_database(self) -> None:
        element = {
            "type": "mention",
            "mention": {"type": "database", "database": {"id": "d1"}},
            "annotations": {},
            "plain_text": "Issues",
        }
        assert render_rich_text([element]) == "[Issues](notion://database/d1)"

    def test_mention_date(self) -> None:
        element = {
            "type": "mention",
            "mention": {"type": "date", "date": {"start": "2026-05-04"}},
            "annotations": {},
            "plain_text": "May 4 2026",
        }
        assert render_rich_text([element]) == "[May 4 2026](notion://date/2026-05-04)"

    def test_mention_with_explicit_href_wins(self) -> None:
        element = {
            "type": "mention",
            "mention": {"type": "user", "user": {"id": "u9"}},
            "annotations": {},
            "plain_text": "alice",
            "href": "https://example.com/u/9",
        }
        assert render_rich_text([element]) == "[alice](https://example.com/u/9)"

    def test_mention_unknown_type_falls_back_to_plain_text(self) -> None:
        element = {
            "type": "mention",
            "mention": {"type": "template_mention"},
            "annotations": {},
            "plain_text": "today",
        }
        assert render_rich_text([element]) == "today"

    def test_mention_with_non_mapping_inner_object(self) -> None:
        element = {
            "type": "mention",
            "mention": "broken",
            "annotations": {},
            "plain_text": "x",
        }
        # Falls through the type-narrowing guard and renders plain_text.
        assert render_rich_text([element]) == "x"

    def test_equation_inline(self) -> None:
        element = {
            "type": "equation",
            "equation": {"expression": "E=mc^2"},
            "annotations": {},
            "plain_text": "E=mc^2",
        }
        assert render_rich_text([element]) == "$E=mc^2$"

    def test_equation_without_expression_falls_back_to_plain_text(self) -> None:
        element = {
            "type": "equation",
            "equation": {},
            "annotations": {},
            "plain_text": "fallback",
        }
        assert render_rich_text([element]) == "fallback"

    def test_unknown_rich_text_type_uses_plain_text(self) -> None:
        element = {"type": "future_kind", "annotations": {}, "plain_text": "raw"}
        assert render_rich_text([element]) == "raw"

    def test_non_mapping_element_skipped(self) -> None:
        # A protocol violation (string in array) should be skipped, not crash.
        assert render_rich_text(["not a dict", text_element("x")]) == "x"

    def test_annotations_on_empty_text_passthrough(self) -> None:
        # Empty text → no wrapping (avoid `**`` etc on empty).
        element = {"type": "text", "text": {"content": ""}, "annotations": {"bold": True}, "plain_text": ""}
        assert render_rich_text([element]) == ""

    def test_annotations_non_mapping_returns_text_unchanged(self) -> None:
        element = {"type": "text", "text": {"content": "x"}, "annotations": "nope", "plain_text": "x"}
        # `_render_text_element` reads annotations as `or {}` so non-mapping
        # becomes empty mapping and text passes through.
        assert render_rich_text([element]) == "x"


# ---------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------


class TestBlocks:
    def test_empty_blocks_return_empty(self) -> None:
        assert render_blocks(None) == ""
        assert render_blocks([]) == ""

    def test_paragraph(self) -> None:
        out = render_blocks([block("paragraph", {"rich_text": [text_element("hi")]})])
        assert out == "hi"

    def test_headings(self) -> None:
        blocks = [
            block("heading_1", {"rich_text": [text_element("H1")]}),
            block("heading_2", {"rich_text": [text_element("H2")]}),
            block("heading_3", {"rich_text": [text_element("H3")]}),
        ]
        out = render_blocks(blocks)
        assert "# H1" in out
        assert "## H2" in out
        assert "### H3" in out

    def test_heading_without_text_emits_prefix_only(self) -> None:
        out = render_blocks([block("heading_1", {"rich_text": []})])
        assert out == "#"

    def test_bulleted_and_numbered_list(self) -> None:
        blocks = [
            block("bulleted_list_item", {"rich_text": [text_element("a")]}),
            block("numbered_list_item", {"rich_text": [text_element("first")]}),
            block("numbered_list_item", {"rich_text": [text_element("second")]}),
            block("paragraph", {"rich_text": [text_element("break")]}),
            block("numbered_list_item", {"rich_text": [text_element("restart")]}),
        ]
        out = render_blocks(blocks)
        assert "- a" in out
        assert "1. first" in out
        assert "2. second" in out
        # Counter restarts after a non-list block.
        assert "1. restart" in out

    def test_to_do(self) -> None:
        blocks = [
            block("to_do", {"rich_text": [text_element("done")], "checked": True}),
            block("to_do", {"rich_text": [text_element("todo")], "checked": False}),
        ]
        out = render_blocks(blocks)
        assert "- [x] done" in out
        assert "- [ ] todo" in out

    def test_toggle_renders_details_summary(self) -> None:
        out = render_blocks([block("toggle", {"rich_text": [text_element("more")]})])
        assert out == "<details><summary>more</summary></details>"

    def test_code_with_language_fence(self) -> None:
        out = render_blocks(
            [block("code", {"rich_text": [text_element("x = 1")], "language": "python"})]
        )
        assert out == "```python\nx = 1\n```"

    def test_code_without_language(self) -> None:
        out = render_blocks([block("code", {"rich_text": [text_element("raw")]})])
        # Empty fence header is allowed by Markdown spec.
        assert out.startswith("```\n")

    def test_quote_and_callout(self) -> None:
        out = render_blocks(
            [
                block("quote", {"rich_text": [text_element("wisdom")]}),
                block(
                    "callout",
                    {"rich_text": [text_element("info")], "icon": {"type": "emoji", "emoji": "💡"}},
                ),
            ]
        )
        assert "> wisdom" in out
        assert "> 💡 info" in out

    def test_quote_empty_text(self) -> None:
        out = render_blocks([block("quote", {"rich_text": []})])
        assert out == ">"

    def test_callout_without_emoji(self) -> None:
        out = render_blocks([block("callout", {"rich_text": [text_element("hi")], "icon": None})])
        assert out == "> hi"

    def test_divider(self) -> None:
        assert render_blocks([block("divider", {})]) == "---"

    def test_equation_block(self) -> None:
        out = render_blocks([block("equation", {"expression": "x^2"})])
        assert out == "$$\nx^2\n$$"

    def test_equation_block_without_expression_emits_nothing(self) -> None:
        out = render_blocks([block("equation", {})])
        assert out == ""

    def test_embed_and_bookmark(self) -> None:
        blocks = [
            block("embed", {"url": "https://e.test/x"}),
            block(
                "bookmark",
                {"url": "https://b.test/x", "caption": [text_element("title")]},
            ),
        ]
        out = render_blocks(blocks)
        assert "[embed](https://e.test/x)" in out
        assert "[title](https://b.test/x)" in out

    def test_embed_without_url_emits_nothing(self) -> None:
        out = render_blocks([block("embed", {})])
        assert out == ""

    def test_bookmark_without_caption_falls_back_to_url(self) -> None:
        out = render_blocks([block("bookmark", {"url": "https://b.test/y", "caption": []})])
        assert out == "[https://b.test/y](https://b.test/y)"

    def test_bookmark_without_url_emits_nothing(self) -> None:
        out = render_blocks([block("bookmark", {"caption": [text_element("orphan")]})])
        assert out == ""

    def test_link_to_page(self) -> None:
        out = render_blocks([block("link_to_page", {"type": "page_id", "page_id": "pid-1"})])
        assert out == "[link](notion://page_id/pid-1)"

    def test_link_to_page_with_unknown_target_returns_empty(self) -> None:
        out = render_blocks([block("link_to_page", {"type": "page_id"})])
        assert out == ""

    def test_child_page_and_database(self) -> None:
        blocks = [
            block("child_page", {"title": "Subpage"}),
            block("child_database", {"title": "Issues"}),
            block("child_database", {"title": ""}),
        ]
        out = render_blocks(blocks)
        assert "## Subpage" in out
        assert "## Issues (database)" in out
        assert "## (database)" in out

    def test_child_page_empty_title_emits_nothing(self) -> None:
        # Empty title would emit an empty heading; we suppress it so the
        # output stays valid Markdown.
        out = render_blocks([block("child_page", {"title": ""})])
        assert out == ""

    def test_unsupported_block_type(self) -> None:
        out = render_blocks([block("synced_block", {})])
        assert out == "<!-- unsupported: synced_block -->"

    def test_archived_block_skipped_by_default(self) -> None:
        out = render_blocks(
            [
                block("paragraph", {"rich_text": [text_element("kept")]}),
                block("paragraph", {"rich_text": [text_element("dropped")]}, archived=True),
            ]
        )
        assert "kept" in out
        assert "dropped" not in out

    def test_archived_block_kept_with_flag(self) -> None:
        out = render_blocks(
            [
                block("paragraph", {"rich_text": [text_element("kept")]}, archived=True),
            ],
            include_archived=True,
        )
        assert "kept" in out

    def test_non_mapping_block_skipped(self) -> None:
        out = render_blocks(["broken", block("paragraph", {"rich_text": [text_element("x")]})])
        assert out == "x"

    def test_block_with_non_mapping_payload_renders_via_empty_payload(self) -> None:
        # If `block["paragraph"]` is not a Mapping, the renderer treats it
        # as `{}` and emits no body.
        bad = {
            "id": "x",
            "type": "paragraph",
            "paragraph": "not-a-mapping",
            "archived": False,
            "has_children": False,
        }
        out = render_blocks([bad])
        assert out == ""


class TestNestedBlocks:
    def test_children_indented_under_parent(self) -> None:
        children = {
            "p1": [block("paragraph", {"rich_text": [text_element("child")]})],
        }

        def lookup(bid: Any) -> list[Any]:
            return children.get(bid, [])

        parent = block(
            "bulleted_list_item",
            {"rich_text": [text_element("parent")]},
            has_children=True,
            block_id="p1",
        )
        out = render_blocks([parent], children_for=lookup)
        # Child indented two spaces under the bullet.
        assert "- parent" in out
        assert "  child" in out

    def test_recursion_depth_is_capped(self) -> None:
        # Build a recursive child lookup that always returns one paragraph
        # with `has_children=True`. The cap should kick in before stack
        # overflow.
        def lookup(_: Any) -> list[Any]:
            return [block("paragraph", {"rich_text": [text_element("deep")]}, has_children=True, block_id="recurse")]

        parent = block("paragraph", {"rich_text": [text_element("root")]}, has_children=True, block_id="recurse")
        # Call the renderer at MAX_DEPTH — must short-circuit.
        out = render_blocks([parent], children_for=lookup, depth=MAX_DEPTH)
        assert out == DEPTH_TRUNCATED_MARKER

    def test_children_skipped_when_no_lookup(self) -> None:
        parent = block("paragraph", {"rich_text": [text_element("root")]}, has_children=True)
        # No `children_for` callback → just render the parent body.
        assert render_blocks([parent]) == "root"

    def test_parent_without_body_renders_only_children(self) -> None:
        # `divider` returns no body but if it claimed children we must not
        # emit a stray newline. Use a custom block_type that returns "".
        children = {"p": [block("paragraph", {"rich_text": [text_element("c")]})]}

        def lookup(bid: Any) -> list[Any]:
            return children.get(bid, [])

        parent = block("divider", {}, has_children=True, block_id="p")
        # Divider has children -> we still render but body branch puts
        # children below. Divider text is "---", children are appended.
        out = render_blocks([parent], children_for=lookup)
        assert "---" in out
        assert "c" in out


class TestTable:
    def test_table_with_header_and_rows(self) -> None:
        rows = [
            block("table_row", {"cells": [[text_element("name")], [text_element("email")]]}, block_id="r0"),
            block("table_row", {"cells": [[text_element("alice")], [text_element("a@b.test")]]}, block_id="r1"),
        ]
        children = {"t1": rows}

        def lookup(bid: Any) -> list[Any]:
            return children.get(bid, [])

        table = block(
            "table",
            {"table_width": 2, "has_column_header": True, "has_row_header": False},
            has_children=True,
            block_id="t1",
        )
        out = render_blocks([table], children_for=lookup)
        assert "| name | email |" in out
        assert "| --- | --- |" in out
        assert "| alice | a@b.test |" in out

    def test_table_without_column_header_synthesizes_blank(self) -> None:
        rows = [
            block("table_row", {"cells": [[text_element("alice")], [text_element("bob")]]}, block_id="r0"),
        ]
        children = {"t2": rows}

        def lookup(bid: Any) -> list[Any]:
            return children.get(bid, [])

        table = block(
            "table",
            {"table_width": 2, "has_column_header": False},
            has_children=True,
            block_id="t2",
        )
        out = render_blocks([table], children_for=lookup)
        # Header row is blank cells.
        assert "|  |  |" in out
        assert "| alice | bob |" in out

    def test_table_with_no_children_emits_nothing(self) -> None:
        def lookup(_: Any) -> list[Any]:
            return []

        table = block("table", {"table_width": 0}, has_children=True, block_id="t3")
        out = render_blocks([table], children_for=lookup)
        assert out == ""

    def test_table_row_outside_table(self) -> None:
        # A standalone table_row block (e.g. yielded as a sibling) still
        # renders to a Markdown row.
        out = render_blocks(
            [block("table_row", {"cells": [[text_element("x")], [text_element("y")]]})]
        )
        assert out == "| x | y |"

    def test_render_one_row_with_non_mapping_payload(self) -> None:
        # Defensive guard inside `_render_one_row`.
        rows = [{"id": "r", "type": "table_row", "table_row": "broken", "archived": False, "has_children": False}]
        children = {"t": rows}

        def lookup(bid: Any) -> list[Any]:
            return children.get(bid, [])

        table = block("table", {"table_width": 0, "has_column_header": True}, has_children=True, block_id="t")
        # `_row_width` returns 0 → no separator columns; renderer copes.
        out = render_blocks([table], children_for=lookup)
        # The table renderer still emits the (empty) header & separator rows;
        # the broken body row renders to empty string.
        assert "|" in out

    def test_row_width_with_non_mapping_payload(self) -> None:
        from pleno_pii_scanner_notion.markdown import _row_width

        assert _row_width({"table_row": "bad"}) == 0

    def test_row_width_with_non_sequence_cells(self) -> None:
        from pleno_pii_scanner_notion.markdown import _row_width

        # `cells` is normally a list-of-lists; a malformed Notion payload
        # could in theory hand us an int. The width must come back as 0
        # rather than blowing up the table renderer.
        assert _row_width({"table_row": {"cells": 42}}) == 0


# ---------------------------------------------------------------------
# database row properties
# ---------------------------------------------------------------------


class TestDatabaseRow:
    def test_empty_returns_empty(self) -> None:
        assert render_database_row(None) == ""
        assert render_database_row({}) == ""

    def test_skips_low_signal_metadata(self) -> None:
        props = {
            "Created": {"type": "created_time", "created_time": "2020-01-01T00:00:00Z"},
            "Edited": {"type": "last_edited_time", "last_edited_time": "2020-01-01T00:00:00Z"},
            "By": {"type": "created_by", "created_by": {"id": "u"}},
            "EditBy": {"type": "last_edited_by", "last_edited_by": {"id": "u"}},
            "Name": {"type": "title", "title": [text_element("alice")]},
        }
        out = render_database_row(props)
        assert "Created" not in out
        assert "Edited" not in out
        assert "By:" not in out
        assert "Name: alice" in out

    def test_title_and_rich_text(self) -> None:
        out = render_database_row(
            {
                "Title": {"type": "title", "title": [text_element("Hello")]},
                "Body": {"type": "rich_text", "rich_text": [text_element("World", bold=True)]},
            }
        )
        assert "Title: Hello" in out
        assert "Body: **World**" in out

    def test_number_zero_and_none(self) -> None:
        out = render_database_row(
            {
                "Count": {"type": "number", "number": 0},
                "Missing": {"type": "number", "number": None},
            }
        )
        assert "Count: 0" in out
        assert "Missing: " in out

    def test_select_multi_select_status(self) -> None:
        out = render_database_row(
            {
                "Sel": {"type": "select", "select": {"name": "Open"}},
                "Tags": {"type": "multi_select", "multi_select": [{"name": "a"}, {"name": "b"}]},
                "St": {"type": "status", "status": {"name": "InProgress"}},
            }
        )
        assert "Sel: Open" in out
        assert "Tags: a, b" in out
        assert "St: InProgress" in out

    def test_select_null_returns_empty(self) -> None:
        out = render_database_row({"Sel": {"type": "select", "select": None}})
        assert out == "Sel: "

    def test_status_null_returns_empty(self) -> None:
        out = render_database_row({"St": {"type": "status", "status": None}})
        assert out == "St: "

    def test_date_with_and_without_end(self) -> None:
        out = render_database_row(
            {
                "Day": {"type": "date", "date": {"start": "2026-05-04"}},
                "Range": {"type": "date", "date": {"start": "2026-05-04", "end": "2026-05-10"}},
                "Empty": {"type": "date", "date": None},
            }
        )
        assert "Day: 2026-05-04" in out
        assert "Range: 2026-05-04 → 2026-05-10" in out
        assert "Empty: " in out

    def test_email_phone_url(self) -> None:
        out = render_database_row(
            {
                "E": {"type": "email", "email": "a@b.test"},
                "P": {"type": "phone_number", "phone_number": "+81-90-1234-5678"},
                "U": {"type": "url", "url": "https://x.test"},
            }
        )
        assert "E: a@b.test" in out
        assert "P: +81-90-1234-5678" in out
        assert "U: https://x.test" in out

    def test_email_phone_url_null(self) -> None:
        out = render_database_row(
            {
                "E": {"type": "email", "email": None},
                "P": {"type": "phone_number", "phone_number": None},
                "U": {"type": "url", "url": None},
            }
        )
        assert "E: " in out
        assert "P: " in out
        assert "U: " in out

    def test_people_with_and_without_email(self) -> None:
        out = render_database_row(
            {
                "Owners": {
                    "type": "people",
                    "people": [
                        {"id": "u1", "name": "Alice", "person": {"email": "alice@x.test"}},
                        {"id": "u2", "name": "Bob"},
                        {"id": "u3", "person": {"email": "anon@x.test"}},
                        {"id": "u4"},
                    ],
                }
            }
        )
        assert "Owners: Alice <alice@x.test>, Bob, anon@x.test, u4" in out

    def test_people_skips_non_mapping_entries(self) -> None:
        out = render_database_row(
            {"Owners": {"type": "people", "people": ["broken", {"id": "u1", "name": "Alice"}]}}
        )
        assert out == "Owners: Alice"

    def test_files(self) -> None:
        out = render_database_row(
            {
                "Files": {
                    "type": "files",
                    "files": [
                        {"name": "a.png", "type": "file", "file": {"url": "https://x.test/a"}},
                        {"name": "b.png", "type": "external", "external": {"url": "https://x.test/b"}},
                        {"name": "c"},
                    ],
                }
            }
        )
        assert "Files: [a.png](https://x.test/a), [b.png](https://x.test/b), c" in out

    def test_files_skips_non_mapping_entries(self) -> None:
        out = render_database_row(
            {"Files": {"type": "files", "files": ["bad", {"name": "ok"}]}}
        )
        assert out == "Files: ok"

    def test_checkbox(self) -> None:
        out = render_database_row(
            {
                "Y": {"type": "checkbox", "checkbox": True},
                "N": {"type": "checkbox", "checkbox": False},
            }
        )
        assert "Y: true" in out
        assert "N: false" in out

    def test_relation(self) -> None:
        out = render_database_row(
            {"Rel": {"type": "relation", "relation": [{"id": "p1"}, {"id": "p2"}, "broken", {}]}}
        )
        assert out == "Rel: p1, p2"

    def test_formula_string_number_boolean_date(self) -> None:
        cases = [
            ({"type": "string", "string": "hello"}, "hello"),
            ({"type": "number", "number": 42}, "42"),
            ({"type": "boolean", "boolean": True}, "true"),
            ({"type": "boolean", "boolean": False}, "false"),
            ({"type": "date", "date": {"start": "2026-05-04"}}, "2026-05-04"),
        ]
        for formula, expected in cases:
            out = render_database_row({"F": {"type": "formula", "formula": formula}})
            assert out == f"F: {expected}", (formula, out)

    def test_formula_invalid(self) -> None:
        out = render_database_row({"F": {"type": "formula", "formula": None}})
        assert out == "F: "
        out2 = render_database_row({"F": {"type": "formula", "formula": {"type": "string", "string": None}}})
        assert out2 == "F: "
        # Mapping value that isn't a date object renders empty.
        out3 = render_database_row({"F": {"type": "formula", "formula": {"type": "weird", "weird": {"k": 1}}}})
        assert out3 == "F: "

    def test_rollup_array_and_scalar(self) -> None:
        out = render_database_row(
            {
                "R": {
                    "type": "rollup",
                    "rollup": {
                        "type": "array",
                        "array": [
                            {"type": "title", "title": [text_element("X")]},
                            {"type": "number", "number": 7},
                        ],
                    },
                }
            }
        )
        assert "R: X, 7" in out

    def test_rollup_number(self) -> None:
        out = render_database_row(
            {"R": {"type": "rollup", "rollup": {"type": "number", "number": 99}}}
        )
        assert "R: 99" in out

    def test_rollup_date_shape(self) -> None:
        out = render_database_row(
            {
                "R": {
                    "type": "rollup",
                    "rollup": {"type": "date", "date": {"start": "2026-05-04"}},
                }
            }
        )
        assert "R: 2026-05-04" in out

    def test_rollup_invalid(self) -> None:
        out = render_database_row({"R": {"type": "rollup", "rollup": None}})
        assert out == "R: "
        out2 = render_database_row({"R": {"type": "rollup", "rollup": {"type": "number", "number": None}}})
        assert out2 == "R: "

    def test_unknown_property_type_emits_marker(self) -> None:
        out = render_database_row(
            {"Mystery": {"type": "future_kind", "future_kind": {"foo": "bar"}}}
        )
        assert "Mystery: <!-- unsupported property: future_kind -->" in out

    def test_non_mapping_property_skipped(self) -> None:
        out = render_database_row({"Bad": "not a mapping", "Title": {"type": "title", "title": [text_element("ok")]}})
        assert out == "Title: ok"
