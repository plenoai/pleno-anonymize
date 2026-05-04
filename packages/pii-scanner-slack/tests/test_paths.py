"""Tests for slack:// path + cursor JSON helpers."""

from __future__ import annotations

import pytest

from pleno_pii_scanner_slack import _paths


class TestMessagePath:
    def test_message_path_format(self) -> None:
        assert (
            _paths.message_path("T01", "C02", "1700000000.000100")
            == "slack://T01/C02/1700000000.000100"
        )

    def test_file_path_format(self) -> None:
        assert (
            _paths.file_path("T01", "C02", "1700000000.000100", "F03")
            == "slack://T01/C02/1700000000.000100/files/F03"
        )


class TestCursorRoundTrip:
    def test_round_trip(self) -> None:
        state = {"C01": "1700000000.000100", "C02": "1700000001.000200"}
        blob = _paths.dump_cursor(state)
        assert _paths.load_cursor(blob) == state

    def test_load_none_is_empty(self) -> None:
        assert _paths.load_cursor(None) == {}

    def test_load_empty_is_empty(self) -> None:
        assert _paths.load_cursor("") == {}

    def test_dump_is_sorted(self) -> None:
        # Determinism: feeding the same logical state twice must produce
        # the same string regardless of dict insertion order.
        a = _paths.dump_cursor({"C02": "2", "C01": "1"})
        b = _paths.dump_cursor({"C01": "1", "C02": "2"})
        assert a == b
        assert a == '{"C01":"1","C02":"2"}'

    def test_load_rejects_non_object(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            _paths.load_cursor("[1, 2, 3]")

    def test_load_rejects_non_string_values(self) -> None:
        with pytest.raises(ValueError, match="string"):
            _paths.load_cursor('{"C01": 12345}')

    def test_load_rejects_non_string_keys(self) -> None:
        # JSON requires string keys, but a hand-edited checkpoint that
        # somehow ended up with numeric keys after a transform must fail
        # loudly. Constructing this case requires going through dump.
        # `json.dumps({1: "x"})` raises; we have to hand-craft the blob.
        with pytest.raises(ValueError, match="string"):
            _paths.load_cursor('{"C01": [1]}')
