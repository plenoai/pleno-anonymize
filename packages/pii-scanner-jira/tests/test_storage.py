"""Hermetic tests for Data Center storage-XHTML -> plain-text conversion."""

from __future__ import annotations

from pleno_pii_scanner_jira.storage import storage_to_text


class TestInputShape:
    def test_none_returns_empty(self) -> None:
        assert storage_to_text(None) == ""

    def test_int_returns_empty(self) -> None:
        assert storage_to_text(123) == ""

    def test_empty_string_returns_empty(self) -> None:
        assert storage_to_text("") == ""

    def test_dict_with_value_field(self) -> None:
        # Custom-field shape: `{ "value": "...", "representation": "..." }`.
        assert storage_to_text({"value": "<p>hi</p>"}) == "hi"

    def test_dict_without_value_returns_empty(self) -> None:
        assert storage_to_text({"representation": "html"}) == ""

    def test_dict_value_non_string_returns_empty(self) -> None:
        assert storage_to_text({"value": 123}) == ""


class TestRendering:
    def test_strips_simple_tags(self) -> None:
        out = storage_to_text("<p>Hello, <b>world</b>!</p>")
        assert out == "Hello, world!"

    def test_paragraphs_separated_by_blank_line(self) -> None:
        out = storage_to_text("<p>one</p><p>two</p>")
        assert "one" in out and "two" in out
        # newline separator preserved
        assert "\n" in out

    def test_lists(self) -> None:
        out = storage_to_text("<ul><li>a</li><li>b</li></ul>")
        assert "a" in out and "b" in out

    def test_br_tag(self) -> None:
        out = storage_to_text("first<br/>second")
        assert "first" in out and "second" in out

    def test_self_closing_block_tag(self) -> None:
        out = storage_to_text("a<hr/>b")
        assert "a" in out and "b" in out

    def test_entities_decoded(self) -> None:
        out = storage_to_text("<p>tom&amp;jerry &lt;3</p>")
        assert "tom&jerry <3" in out

    def test_script_content_dropped(self) -> None:
        out = storage_to_text(
            "<p>visible</p><script>password = 'leak'</script>"
        )
        assert "visible" in out
        assert "password" not in out

    def test_style_content_dropped(self) -> None:
        out = storage_to_text("<style>body{color:red}</style><p>text</p>")
        assert "color" not in out
        assert "text" in out

    def test_collapses_consecutive_blank_lines(self) -> None:
        out = storage_to_text("<p>a</p><p></p><p></p><p>b</p>")
        # At most one blank line between content lines.
        assert "\n\n\n" not in out

    def test_non_block_tags_keep_text(self) -> None:
        out = storage_to_text("<span>inline <em>text</em></span>")
        assert out == "inline text"

    def test_table_rows(self) -> None:
        out = storage_to_text(
            "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>"
        )
        assert "a" in out and "b" in out and "c" in out

    def test_unclosed_tag_does_not_crash(self) -> None:
        # HTMLParser is forgiving — verify we don't crash.
        out = storage_to_text("<p>open paragraph forever")
        assert "open paragraph forever" in out

    def test_table_with_storage_value_wrapper(self) -> None:
        out = storage_to_text({"value": "<table><tr><td>x</td></tr></table>"})
        assert "x" in out

    def test_nested_skip_tag(self) -> None:
        out = storage_to_text(
            "<style><style>x</style></style><p>visible</p>"
        )
        assert "visible" in out
        assert "x" not in out

    def test_parser_exception_falls_back_to_unescape(
        self, monkeypatch
    ) -> None:
        # Force the parser to raise on `feed`; the fallback unescape()
        # branch should still surface the raw body.
        from pleno_pii_scanner_jira import storage as storage_mod

        class BoomParser:
            def feed(self, _: str) -> None:
                raise RuntimeError("boom")

            def close(self) -> None:  # pragma: no cover
                pass

        monkeypatch.setattr(storage_mod, "_StorageStripper", BoomParser)
        out = storage_to_text("<p>tom&amp;jerry</p>")
        # `tom&jerry` because unescape() resolves `&amp;`.
        assert "tom&jerry" in out
