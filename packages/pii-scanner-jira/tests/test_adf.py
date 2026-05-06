"""Hermetic tests for ADF -> plain-text conversion."""

from __future__ import annotations

from pleno_pii_scanner_jira.adf import (
    DEPTH_TRUNCATED_MARKER,
    MAX_DEPTH,
    adf_to_text,
)
from tests.conftest import adf_doc, adf_text


# --- input shape tolerance ----------------------------------------


class TestInputShape:
    def test_none_returns_empty(self) -> None:
        assert adf_to_text(None) == ""

    def test_int_returns_empty(self) -> None:
        # Defensive against malformed responses where Jira hands back
        # a number (e.g. for legacy custom fields).
        assert adf_to_text(123) == ""

    def test_bare_string_passes_through(self) -> None:
        # When a raw string slips past the schema we still surface the
        # text so the operator sees the leak.
        assert adf_to_text("plain string") == "plain string"

    def test_list_of_nodes(self) -> None:
        # Some test setups pass `doc["content"]` directly.
        out = adf_to_text(
            [
                {"type": "paragraph", "content": [adf_text("hi")]},
                {"type": "paragraph", "content": [adf_text("world")]},
            ]
        )
        assert "hi" in out
        assert "world" in out

    def test_doc_root(self) -> None:
        doc = adf_doc({"type": "paragraph", "content": [adf_text("body")]})
        assert "body" in adf_to_text(doc)


# --- per-node-type rendering --------------------------------------


class TestNodeTypes:
    def test_paragraph(self) -> None:
        doc = adf_doc({"type": "paragraph", "content": [adf_text("hello")]})
        assert adf_to_text(doc).strip() == "hello"

    def test_heading(self) -> None:
        doc = adf_doc(
            {
                "type": "heading",
                "attrs": {"level": 1},
                "content": [adf_text("Title")],
            }
        )
        assert "Title" in adf_to_text(doc)

    def test_bullet_list(self) -> None:
        doc = adf_doc(
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [adf_text("first")],
                            }
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [adf_text("second")],
                            }
                        ],
                    },
                ],
            }
        )
        out = adf_to_text(doc)
        assert "first" in out and "second" in out

    def test_ordered_list(self) -> None:
        doc = adf_doc(
            {
                "type": "orderedList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [adf_text("alpha")],
                            }
                        ],
                    },
                ],
            }
        )
        assert "alpha" in adf_to_text(doc)

    def test_code_block(self) -> None:
        doc = adf_doc(
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [adf_text('password = "leak"')],
            }
        )
        assert 'password = "leak"' in adf_to_text(doc)

    def test_inline_code_node(self) -> None:
        # Some ADF dialects emit a top-level `code` leaf.
        out = adf_to_text({"type": "code", "text": "inline-leak"})
        assert out == "inline-leak"

    def test_inline_code_node_without_text(self) -> None:
        assert adf_to_text({"type": "code"}) == ""

    def test_mention_uses_attrs_text(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {"id": "557058:abc", "text": "@Alice Smith"},
                    }
                ],
            }
        )
        assert "@Alice Smith" in adf_to_text(doc)

    def test_mention_falls_back_to_id(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [{"type": "mention", "attrs": {"id": "557058:abc"}}],
            }
        )
        assert "@557058:abc" in adf_to_text(doc)

    def test_mention_without_attrs_is_empty(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [{"type": "mention"}],
            }
        )
        assert adf_to_text(doc) == ""

    def test_emoji_shortname(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [{"type": "emoji", "attrs": {"shortName": ":+1:"}}],
            }
        )
        assert ":+1:" in adf_to_text(doc)

    def test_emoji_text_fallback(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [{"type": "emoji", "attrs": {"text": "thumbs"}}],
            }
        )
        assert "thumbs" in adf_to_text(doc)

    def test_emoji_without_attrs_empty(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [{"type": "emoji"}],
            }
        )
        assert adf_to_text(doc) == ""

    def test_hard_break_emits_newline(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [
                    adf_text("a"),
                    {"type": "hardBreak"},
                    adf_text("b"),
                ],
            }
        )
        out = adf_to_text(doc)
        assert "a" in out and "b" in out

    def test_inline_card(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "inlineCard",
                        "attrs": {"url": "https://example.com/leak?token=xyz"},
                    }
                ],
            }
        )
        assert "https://example.com/leak?token=xyz" in adf_to_text(doc)

    def test_inline_card_without_attrs_empty(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [{"type": "inlineCard"}],
            }
        )
        assert adf_to_text(doc) == ""

    def test_media_single_with_media_attrs(self) -> None:
        doc = adf_doc(
            {
                "type": "mediaSingle",
                "content": [
                    {
                        "type": "media",
                        "attrs": {
                            "id": "abc",
                            "collection": "MediaServicesSample",
                            "alt": "screenshot.png",
                        },
                    }
                ],
            }
        )
        out = adf_to_text(doc)
        assert "id=abc" in out
        assert "alt=screenshot.png" in out

    def test_media_no_attrs_empty(self) -> None:
        doc = adf_doc({"type": "media"})
        assert adf_to_text(doc) == ""

    def test_panel(self) -> None:
        doc = adf_doc(
            {
                "type": "panel",
                "attrs": {"panelType": "warning"},
                "content": [{"type": "paragraph", "content": [adf_text("be careful")]}],
            }
        )
        assert "be careful" in adf_to_text(doc)

    def test_blockquote(self) -> None:
        doc = adf_doc(
            {
                "type": "blockquote",
                "content": [{"type": "paragraph", "content": [adf_text("quoted")]}],
            }
        )
        assert "quoted" in adf_to_text(doc)

    def test_rule_no_crash(self) -> None:
        doc = adf_doc(
            {"type": "paragraph", "content": [adf_text("a")]},
            {"type": "rule"},
            {"type": "paragraph", "content": [adf_text("b")]},
        )
        out = adf_to_text(doc)
        assert "a" in out and "b" in out

    def test_table(self) -> None:
        doc = adf_doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableHeader",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [adf_text("name")],
                                    }
                                ],
                            },
                            {
                                "type": "tableHeader",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [adf_text("ssn")],
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableCell",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [adf_text("alice")],
                                    }
                                ],
                            },
                            {
                                "type": "tableCell",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [adf_text("123-45-6789")],
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        )
        out = adf_to_text(doc)
        assert "name\tssn" in out
        assert "alice\t123-45-6789" in out

    def test_expand(self) -> None:
        doc = adf_doc(
            {
                "type": "expand",
                "attrs": {"title": "details"},
                "content": [
                    {"type": "paragraph", "content": [adf_text("hidden body")]}
                ],
            }
        )
        assert "hidden body" in adf_to_text(doc)


# --- marks (link in particular) -----------------------------------


class TestMarks:
    def test_link_mark_renders_url(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [
                    adf_text(
                        "click",
                        marks=[
                            {
                                "type": "link",
                                "attrs": {"href": "https://acme/?token=secret"},
                            }
                        ],
                    )
                ],
            }
        )
        out = adf_to_text(doc)
        assert "click" in out
        assert "https://acme/?token=secret" in out

    def test_link_mark_same_text_no_duplicate(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [
                    adf_text(
                        "https://acme",
                        marks=[
                            {
                                "type": "link",
                                "attrs": {"href": "https://acme"},
                            }
                        ],
                    )
                ],
            }
        )
        out = adf_to_text(doc).strip()
        assert out == "https://acme"

    def test_bold_mark_dropped(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [adf_text("bold", marks=[{"type": "strong"}])],
            }
        )
        assert adf_to_text(doc).strip() == "bold"

    def test_text_without_marks_field(self) -> None:
        # No marks at all — most common case.
        assert adf_to_text({"type": "text", "text": "plain"}) == "plain"

    def test_text_with_non_mapping_marks(self) -> None:
        # Defensive: a malformed `marks` field.
        out = adf_to_text({"type": "text", "text": "x", "marks": ["not a mapping"]})
        assert out == "x"

    def test_text_missing_text_field(self) -> None:
        assert adf_to_text({"type": "text"}) == ""

    def test_link_mark_without_attrs(self) -> None:
        doc = adf_doc(
            {
                "type": "paragraph",
                "content": [adf_text("click", marks=[{"type": "link"}])],
            }
        )
        assert adf_to_text(doc).strip() == "click"


# --- unknown nodes -------------------------------------------------


class TestUnsupported:
    def test_unknown_type_emits_marker(self) -> None:
        out = adf_to_text({"type": "futureWidget"})
        assert "<!-- unsupported: futureWidget -->" in out

    def test_unknown_type_with_children_continues(self) -> None:
        out = adf_to_text(
            {
                "type": "futureWidget",
                "content": [{"type": "paragraph", "content": [adf_text("inside")]}],
            }
        )
        assert "<!-- unsupported: futureWidget -->" in out
        assert "inside" in out

    def test_node_without_type_is_empty(self) -> None:
        assert adf_to_text({"text": "no type"}) == ""

    def test_non_mapping_in_sequence_skipped(self) -> None:
        # Sequence with garbage entries is tolerated.
        out = adf_to_text(
            [
                {"type": "paragraph", "content": [adf_text("a")]},
                "string entry",
                None,
                {"type": "paragraph", "content": [adf_text("b")]},
            ]
        )
        assert "a" in out and "b" in out


# --- depth bounding ------------------------------------------------


class TestDepthBound:
    def test_truncates_deeply_nested_input(self) -> None:
        # Build a chain of nested panels exceeding MAX_DEPTH.
        node: dict = {"type": "paragraph", "content": [adf_text("leaf")]}
        for _ in range(MAX_DEPTH + 5):
            node = {"type": "panel", "content": [node]}
        out = adf_to_text(node)
        assert DEPTH_TRUNCATED_MARKER in out

    def test_below_depth_renders_normally(self) -> None:
        node: dict = {"type": "paragraph", "content": [adf_text("leaf")]}
        for _ in range(5):
            node = {"type": "panel", "content": [node]}
        out = adf_to_text(node)
        assert "leaf" in out
        assert DEPTH_TRUNCATED_MARKER not in out

    def test_custom_max_depth(self) -> None:
        node: dict = {"type": "paragraph", "content": [adf_text("leaf")]}
        for _ in range(10):
            node = {"type": "panel", "content": [node]}
        out = adf_to_text(node, max_depth=2)
        assert DEPTH_TRUNCATED_MARKER in out


# --- defensive container handling --------------------------------


class TestDefensive:
    def test_bullet_list_non_sequence_content(self) -> None:
        # `content` field present but not a list — should render empty.
        assert adf_to_text({"type": "bulletList", "content": "oops"}) == ""

    def test_bullet_list_skips_non_mapping_children(self) -> None:
        out = adf_to_text({"type": "bulletList", "content": ["bad", None, 5]})
        assert out == ""

    def test_bullet_list_skips_non_list_item(self) -> None:
        out = adf_to_text(
            {
                "type": "bulletList",
                "content": [{"type": "paragraph", "content": [adf_text("not item")]}],
            }
        )
        assert out == ""

    def test_table_non_sequence_content(self) -> None:
        assert adf_to_text({"type": "table", "content": "oops"}) == ""

    def test_table_row_non_sequence_content(self) -> None:
        assert adf_to_text({"type": "tableRow", "content": "oops"}) == ""

    def test_table_skips_non_table_row(self) -> None:
        out = adf_to_text(
            {
                "type": "table",
                "content": [{"type": "paragraph", "content": [adf_text("not row")]}],
            }
        )
        assert out == ""

    def test_table_skips_non_mapping_rows(self) -> None:
        assert adf_to_text({"type": "table", "content": ["bad"]}) == ""

    def test_table_row_skips_non_cell(self) -> None:
        out = adf_to_text(
            {
                "type": "tableRow",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [adf_text("not cell")],
                    }
                ],
            }
        )
        assert out == ""

    def test_table_row_skips_non_mapping_cell(self) -> None:
        out = adf_to_text({"type": "tableRow", "content": ["bad"]})
        assert out == ""

    def test_emoji_attrs_with_non_string_shortname(self) -> None:
        # shortName is non-string → skip; falls through to text fallback.
        out = adf_to_text({"type": "emoji", "attrs": {"shortName": 5, "text": "ok"}})
        assert out == "ok"

    def test_emoji_attrs_neither_string(self) -> None:
        assert adf_to_text({"type": "emoji", "attrs": {"shortName": 5}}) == ""

    def test_inline_card_non_string_url(self) -> None:
        assert adf_to_text({"type": "inlineCard", "attrs": {"url": 5}}) == ""

    def test_inline_code_non_string_text(self) -> None:
        assert adf_to_text({"type": "code", "text": 5}) == ""

    def test_mention_non_string_text_and_id(self) -> None:
        assert adf_to_text({"type": "mention", "attrs": {"text": 5, "id": 6}}) == ""
