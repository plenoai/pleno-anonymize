"""Storage-format XHTML → text conversion tests.

Confluence storage format mixes vanilla HTML with namespaced macros
under the `ac:` and `ri:` prefixes. The converter must:

* Preserve text inside `<p>`, `<ul><li>`, headings, tables, links,
  inline `<code>`.
* Handle `<ac:structured-macro>` by extracting the content of its
  `<ac:rich-text-body>` child while dropping `<ac:parameter>` config.
* Tolerate namespaced elements that lack a top-level xmlns declaration
  (Confluence does not always emit one).
* Fall back gracefully on malformed XHTML — never lose content.
"""

from __future__ import annotations


from pleno_pii_scanner_confluence.storage import storage_to_text


class TestStorageBasicHtml:
    def test_empty_input_returns_empty_string(self) -> None:
        assert storage_to_text("") == ""
        assert storage_to_text(None) == ""

    def test_paragraph_text(self) -> None:
        assert storage_to_text("<p>hello world</p>") == "hello world"

    def test_multiple_paragraphs_separated_by_blank_line(self) -> None:
        out = storage_to_text("<p>first</p><p>second</p>")
        assert "first" in out
        assert "second" in out
        # Blocks must not collapse onto the same line.
        first_idx = out.index("first")
        second_idx = out.index("second")
        assert first_idx < second_idx
        assert "\n" in out[first_idx:second_idx]

    def test_heading_preserved(self) -> None:
        out = storage_to_text("<h1>Title</h1><p>body</p>")
        assert "Title" in out
        assert "body" in out

    def test_unordered_list_items_each_on_own_line(self) -> None:
        out = storage_to_text("<ul><li>alpha</li><li>beta</li></ul>")
        assert "alpha" in out
        assert "beta" in out
        # Each list item is a block, so they should not concatenate
        # without a separator.
        assert "alphabeta" not in out

    def test_table_cells_extracted(self) -> None:
        out = storage_to_text("<table><tr><td>name</td><td>alice</td></tr></table>")
        assert "name" in out
        assert "alice" in out

    def test_link_text_preserved(self) -> None:
        out = storage_to_text(
            '<p>see <a href="https://example.test">docs</a> please</p>'
        )
        assert "see" in out
        assert "docs" in out
        assert "please" in out

    def test_inline_code_preserved(self) -> None:
        out = storage_to_text("<p>use <code>kubectl</code> here</p>")
        assert "kubectl" in out

    def test_nbsp_entity_replaced_with_space(self) -> None:
        out = storage_to_text("<p>hello&nbsp;world</p>")
        # `&nbsp;` becomes a regular space; the two words must stay
        # on the same logical line.
        assert "hello world" in out


class TestStorageMacros:
    def test_panel_macro_rich_text_body_extracted(self) -> None:
        body = (
            '<ac:structured-macro ac:name="info">'
            "<ac:rich-text-body><p>note me</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        assert "note me" in storage_to_text(body)

    def test_macro_parameter_dropped(self) -> None:
        # `<ac:parameter>` carries macro config (`language=java`,
        # `title=...`); it must NOT appear in the surface text.
        body = (
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">java</ac:parameter>'
            "<ac:plain-text-body>print()</ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        out = storage_to_text(body)
        assert "java" not in out
        assert "print()" in out

    def test_nested_macros_extracted(self) -> None:
        body = (
            '<ac:structured-macro ac:name="expand">'
            "<ac:rich-text-body>"
            "<p>outer</p>"
            '<ac:structured-macro ac:name="info">'
            "<ac:rich-text-body><p>inner</p></ac:rich-text-body>"
            "</ac:structured-macro>"
            "</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        out = storage_to_text(body)
        assert "outer" in out
        assert "inner" in out

    def test_namespaced_element_with_unbound_prefix_falls_back(self) -> None:
        # `<future:thing>` is not declared in our wrapper namespaces;
        # ElementTree raises ParseError → tag-strip fallback runs.
        body = "<p>hello</p><future:thing>world</future:thing>"
        out = storage_to_text(body)
        assert "hello" in out
        assert "world" in out

    def test_resource_identifier_attributes_drop_silently(self) -> None:
        # `<ri:user ri:userkey="abc"/>` is a self-closing reference;
        # its attributes are pointers, not surface text. The walker
        # must produce no output for it (and not crash on the prefixed
        # attribute).
        body = '<p>before</p><ri:user ri:userkey="ff8080816a8a8a8a"/><p>after</p>'
        out = storage_to_text(body)
        assert "before" in out
        assert "after" in out
        # The userkey is a token-shaped string; it must not appear in
        # the extracted text or it will cause false-positive PII hits.
        assert "ff8080816a8a8a8a" not in out


class TestStorageMalformed:
    def test_unclosed_tag_falls_back_to_tag_strip(self) -> None:
        # ElementTree rejects unclosed `<br>`; fallback should still
        # surface the prose.
        body = "<p>hello<br>world</p>"
        out = storage_to_text(body)
        assert "hello" in out
        assert "world" in out

    def test_completely_garbage_input_returns_best_effort_text(self) -> None:
        # Bare ampersands trip the strict XML parser; tag-strip falls
        # back. The text content survives.
        body = "<<<not really xml & co"
        out = storage_to_text(body)
        # Tag-strip may eat the leading `<<<` (which looks like a
        # malformed open-tag); the trailing words must survive.
        assert "not really xml" in out
        assert "co" in out

    def test_fallback_drops_nbsp_entity(self) -> None:
        # The fallback path must also normalise `&nbsp;` so the two
        # rendering pipelines produce comparable output.
        body = "<p>oops<br>two&nbsp;words</p>"
        out = storage_to_text(body)
        assert "two words" in out


class TestStorageWhitespace:
    def test_runs_of_blank_lines_collapsed(self) -> None:
        body = "<p>a</p><p></p><p></p><p>b</p>"
        out = storage_to_text(body)
        # No more than one blank line between content blocks.
        assert "\n\n\n" not in out

    def test_inline_whitespace_collapsed(self) -> None:
        body = "<p>hello       world</p>"
        out = storage_to_text(body)
        assert "hello world" in out
        assert "hello       world" not in out

    def test_leading_and_trailing_whitespace_stripped(self) -> None:
        body = "<p>   trim me   </p>"
        out = storage_to_text(body)
        assert out == "trim me"
