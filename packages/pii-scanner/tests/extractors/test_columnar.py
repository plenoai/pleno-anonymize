"""Tests for columnar extractors (skipped when [columnar] not installed).

CSV / JSONL extractors have no extra deps so they always run; the
parquet / avro tests skip when pyarrow / fastavro are absent.
"""

from __future__ import annotations

import io

import pytest

from pleno_pii_scanner.extractors import collect
from pleno_pii_scanner.extractors.columnar import (
    CsvExtractor,
    JsonlExtractor,
)
from pleno_pii_scanner.sources.base import Document, DocumentRef


def _ref() -> DocumentRef:
    return DocumentRef(source_id="s", source_kind="t", path="p")


class TestCsvExtractor:
    @pytest.mark.asyncio
    async def test_string_cells_extracted(self) -> None:
        ex = CsvExtractor()
        text = "name,age\nalice,30\nbob,25\n"
        frags = await collect(ex, Document(ref=_ref(), text=text))
        texts = [f.text for f in frags]
        assert "alice" in texts
        assert "bob" in texts
        # numeric cells skipped
        assert "30" not in texts
        assert "25" not in texts

    @pytest.mark.asyncio
    async def test_header_carried_in_path_hint(self) -> None:
        ex = CsvExtractor()
        text = "email,phone\nfoo@bar.com,090-0000-0000\n"
        frags = await collect(ex, Document(ref=_ref(), text=text))
        emails = [f for f in frags if f.text == "foo@bar.com"]
        assert emails
        assert "column:email" in emails[0].path_hint

    @pytest.mark.asyncio
    async def test_boolean_cells_skipped(self) -> None:
        ex = CsvExtractor()
        text = "active,name\ntrue,alice\nFalse,bob\n"
        frags = await collect(ex, Document(ref=_ref(), text=text))
        texts = [f.text for f in frags]
        assert "true" not in texts
        assert "False" not in texts

    @pytest.mark.asyncio
    async def test_binary_input_decoded(self) -> None:
        ex = CsvExtractor()
        frags = await collect(ex, Document(ref=_ref(), binary=b"name\nfoo\nbar\n"))
        texts = [f.text for f in frags]
        assert "foo" in texts
        assert "bar" in texts


class TestJsonlExtractor:
    @pytest.mark.asyncio
    async def test_top_level_strings_yielded(self) -> None:
        ex = JsonlExtractor()
        text = '{"name":"alice","age":30}\n{"name":"bob","age":25}\n'
        frags = await collect(ex, Document(ref=_ref(), text=text))
        texts = [f.text for f in frags]
        assert "alice" in texts
        assert "bob" in texts
        assert 30 not in texts
        assert "30" not in texts

    @pytest.mark.asyncio
    async def test_nested_objects_walked(self) -> None:
        ex = JsonlExtractor()
        text = '{"user":{"contact":{"email":"a@b.c"}}}\n'
        frags = await collect(ex, Document(ref=_ref(), text=text))
        assert any(
            f.text == "a@b.c" and "/user/contact/email" in f.path_hint for f in frags
        )

    @pytest.mark.asyncio
    async def test_arrays_indexed(self) -> None:
        ex = JsonlExtractor()
        text = '{"tags":["alpha","beta"]}\n'
        frags = await collect(ex, Document(ref=_ref(), text=text))
        paths = [f.path_hint for f in frags]
        assert any("[0]" in p for p in paths)
        assert any("[1]" in p for p in paths)

    @pytest.mark.asyncio
    async def test_invalid_lines_skipped(self) -> None:
        ex = JsonlExtractor()
        text = '{"ok":"yes"}\nthis is not json\n{"second":"row"}\n'
        frags = await collect(ex, Document(ref=_ref(), text=text))
        texts = [f.text for f in frags]
        assert "yes" in texts
        assert "row" in texts

    @pytest.mark.asyncio
    async def test_blank_lines_skipped(self) -> None:
        ex = JsonlExtractor()
        text = '{"a":"b"}\n\n{"c":"d"}\n'
        frags = await collect(ex, Document(ref=_ref(), text=text))
        texts = [f.text for f in frags]
        assert texts == ["b", "d"]


class TestParquet:
    pyarrow = pytest.importorskip("pyarrow")

    @pytest.mark.asyncio
    async def test_string_columns_only(self) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        from pleno_pii_scanner.extractors.columnar import ParquetExtractor

        table = pa.table(
            {
                "name": ["alice", "bob"],
                "age": [30, 25],
                "active": [True, False],
            }
        )
        buf = io.BytesIO()
        pq.write_table(table, buf)
        ex = ParquetExtractor()
        frags = await collect(ex, Document(ref=_ref(), binary=buf.getvalue()))
        texts = [f.text for f in frags]
        assert "alice" in texts
        assert "bob" in texts
        # numeric / boolean columns skipped at the schema level
        assert "30" not in texts
        assert "True" not in texts


class TestAvro:
    fastavro = pytest.importorskip("fastavro")

    @pytest.mark.asyncio
    async def test_string_fields_only(self) -> None:
        import fastavro

        from pleno_pii_scanner.extractors.columnar import AvroExtractor

        schema = {
            "type": "record",
            "name": "User",
            "fields": [
                {"name": "name", "type": "string"},
                {"name": "age", "type": "int"},
            ],
        }
        records = [{"name": "alice", "age": 30}, {"name": "bob", "age": 25}]
        buf = io.BytesIO()
        fastavro.writer(buf, schema, records)
        ex = AvroExtractor()
        frags = await collect(ex, Document(ref=_ref(), binary=buf.getvalue()))
        texts = [f.text for f in frags]
        assert "alice" in texts
        assert "bob" in texts
