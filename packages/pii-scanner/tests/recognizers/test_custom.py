"""Tests for the BYOD custom-recognizer TOML loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from pleno_pii_scanner.recognizers import (
    CustomRecognizerLoadError,
    CustomRecognizerSchemaError,
    load_custom_recognizers,
)


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


class TestLoading:
    def test_minimal_recognizer(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "INTERNAL_API_TOKEN"
language = "any"

[[recognizer.patterns]]
name = "v1"
regex = "INT-[A-Z0-9]{32}"
score = 0.9
""",
        )
        recognizers, verifiers = load_custom_recognizers(p)
        assert len(recognizers) == 1
        r = recognizers[0]
        assert r.entity == "INTERNAL_API_TOKEN"
        assert r.language == "any"
        assert len(r.patterns) == 1
        assert r.patterns[0].name == "v1"
        assert r.patterns[0].score == 0.9
        assert verifiers == {}

    def test_language_defaults_to_any(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
""",
        )
        recognizers, _ = load_custom_recognizers(p)
        assert recognizers[0].language == "any"

    def test_context_keywords_preserved(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
context = ["api_key", "internal_token"]
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
""",
        )
        recognizers, _ = load_custom_recognizers(p)
        assert recognizers[0].context == ("api_key", "internal_token")

    def test_multiple_recognizers(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "A"
[[recognizer.patterns]]
name = "a"
regex = "A"
score = 0.5

[[recognizer]]
entity = "B"
[[recognizer.patterns]]
name = "b"
regex = "B"
score = 0.6
""",
        )
        recognizers, _ = load_custom_recognizers(p)
        assert [r.entity for r in recognizers] == ["A", "B"]

    def test_path_is_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `~/.config/pleno/custom.toml` style paths must work.
        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "custom.toml"
        _write(
            target,
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
""",
        )
        recognizers, _ = load_custom_recognizers("~/custom.toml")
        assert recognizers[0].entity == "X"


class TestVerifierAttachment:
    def test_regex_check_verifier(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X.*"
score = 0.5
[recognizer.verifier]
type = "regex_check"
extra_pattern = "^X-[A-Z]+$"
""",
        )
        _, verifiers = load_custom_recognizers(p)
        assert "X" in verifiers
        assert verifiers["X"].check("X-PROD") == "passed"
        assert verifiers["X"].check("XYZ") == "failed"

    def test_luhn_verifier(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "CORP_CARD"
[[recognizer.patterns]]
name = "p"
regex = "[0-9]{16}"
score = 0.5
[recognizer.verifier]
type = "luhn"
""",
        )
        _, verifiers = load_custom_recognizers(p)
        assert verifiers["CORP_CARD"].check("4242424242424242") == "passed"
        assert verifiers["CORP_CARD"].check("4242424242424241") == "failed"

    def test_unknown_verifier_type_raises_schema_error(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
[recognizer.verifier]
type = "nonexistent_verifier"
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="unknown verifier"):
            load_custom_recognizers(p)


class TestLoadErrors:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(CustomRecognizerLoadError, match="not found"):
            load_custom_recognizers(tmp_path / "nope.toml")

    def test_path_is_a_directory(self, tmp_path: Path) -> None:
        with pytest.raises(CustomRecognizerLoadError, match="not found"):
            load_custom_recognizers(tmp_path)

    def test_invalid_toml(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "rec.toml", "this is not = = valid toml")
        with pytest.raises(CustomRecognizerLoadError, match="parse"):
            load_custom_recognizers(p)


class TestSchemaErrors:
    def test_missing_recognizer_array(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "rec.toml", "[other]\nx = 1")
        with pytest.raises(CustomRecognizerSchemaError, match="recognizer"):
            load_custom_recognizers(p)

    def test_recognizer_must_be_array(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "rec.toml", "recognizer = 'not an array'")
        with pytest.raises(CustomRecognizerSchemaError, match="array"):
            load_custom_recognizers(p)

    def test_unknown_top_level_keys(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5

[unknown_section]
key = "value"
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="unknown_section"):
            load_custom_recognizers(p)

    def test_recognizer_item_must_be_table(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "rec.toml", 'recognizer = ["not a table"]')
        with pytest.raises(CustomRecognizerSchemaError, match="must be a table"):
            load_custom_recognizers(p)

    def test_missing_required_recognizer_keys(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "rec.toml", "[[recognizer]]\nlanguage = 'ja'")
        with pytest.raises(CustomRecognizerSchemaError) as exc:
            load_custom_recognizers(p)
        msg = str(exc.value)
        assert "entity" in msg
        assert "patterns" in msg

    def test_unknown_recognizer_keys(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
typo = "oops"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="typo"):
            load_custom_recognizers(p)

    def test_empty_entity(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = ""
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="entity"):
            load_custom_recognizers(p)

    def test_non_string_entity(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = 42
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="entity"):
            load_custom_recognizers(p)

    def test_empty_language(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
language = ""
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="language"):
            load_custom_recognizers(p)

    def test_context_must_be_list_of_strings(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
context = [1, 2, 3]
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="context"):
            load_custom_recognizers(p)

    def test_duplicate_entity_in_one_file(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5

[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "q"
regex = "Y"
score = 0.5
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="duplicate entity"):
            load_custom_recognizers(p)


class TestPatternErrors:
    def test_patterns_must_be_non_empty(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
patterns = []
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="non-empty"):
            load_custom_recognizers(p)

    def test_patterns_must_be_array(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
patterns = "not an array"
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="non-empty array"):
            load_custom_recognizers(p)

    def test_pattern_item_must_be_table(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
patterns = ["not a table"]
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="must be a table"):
            load_custom_recognizers(p)

    def test_pattern_missing_keys(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="regex"):
            load_custom_recognizers(p)

    def test_pattern_unknown_keys(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
typo = "oops"
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="typo"):
            load_custom_recognizers(p)

    def test_invalid_regex_raises(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "[unclosed"
score = 0.5
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="regex is invalid"):
            load_custom_recognizers(p)

    def test_score_must_be_in_unit_range(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 1.5
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match=r"\[0,1\]"):
            load_custom_recognizers(p)

    def test_score_must_be_number(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = "high"
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="score"):
            load_custom_recognizers(p)

    def test_score_bool_rejected(self, tmp_path: Path) -> None:
        # Python treats `True` as `int(1)`. Allowing `score = true` would
        # silently set score to 1.0 when the user almost certainly meant
        # something else. Loud failure.
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = true
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="score"):
            load_custom_recognizers(p)

    def test_empty_pattern_name(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = ""
regex = "X"
score = 0.5
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="name"):
            load_custom_recognizers(p)

    def test_empty_regex_string(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = ""
score = 0.5
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="regex"):
            load_custom_recognizers(p)

    def test_duplicate_pattern_name(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
[[recognizer.patterns]]
name = "p"
regex = "Y"
score = 0.5
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="duplicate name"):
            load_custom_recognizers(p)


class TestVerifierSchemaErrors:
    def test_verifier_must_be_table(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
verifier = "not a table"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="verifier"):
            load_custom_recognizers(p)

    def test_verifier_type_required(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
[recognizer.verifier]
extra_pattern = "^X-"
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="verifier.type"):
            load_custom_recognizers(p)

    def test_empty_verifier_type(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "rec.toml",
            """
[[recognizer]]
entity = "X"
[[recognizer.patterns]]
name = "p"
regex = "X"
score = 0.5
[recognizer.verifier]
type = ""
""",
        )
        with pytest.raises(CustomRecognizerSchemaError, match="verifier.type"):
            load_custom_recognizers(p)


class TestPermissionError:
    def test_unreadable_file_surfaces_load_error(self, tmp_path: Path) -> None:
        # A file we can stat but not read (mode 000). Honors the OSError
        # branch in the loader. Skipped on platforms where chmod has no
        # effect (Windows CI primarily).
        import os

        p = tmp_path / "rec.toml"
        p.write_text(
            '[[recognizer]]\nentity="X"\n[[recognizer.patterns]]\nname="p"\nregex="X"\nscore=0.5\n'
        )
        try:
            os.chmod(p, 0o000)
        except (OSError, NotImplementedError):
            pytest.skip("chmod unavailable on this platform")
        try:
            with pytest.raises(CustomRecognizerLoadError, match="could not read"):
                load_custom_recognizers(p)
        finally:
            os.chmod(p, 0o644)
