"""Columnar / row-oriented data extractors (parquet, orc, avro, csv, jsonl).

Optional extra (``pleno-pii-scanner[columnar]``).

Per ADR-0007 §6: **only string-typed columns are scanned**. Numeric,
boolean, and timestamp columns cannot encode PII without a string cast,
and scanning them would multiply false positives without finding new
secrets. The schema check happens once per file (not per row) so the
overhead is negligible compared to the row scan.

CSV / JSONL get string-only treatment by sniffing each value: anything
that parses as a pure number or boolean is skipped.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator, Iterable

from pleno_pii_scanner.extractors.base import (
    ExtractedFragment,
    ExtractorError,
    doc_payload,
)
from pleno_pii_scanner.extractors.text import decode_bytes
from pleno_pii_scanner.sources.base import Document, DocumentChunk


class ParquetExtractor:
    """Apache Parquet -> per-string-cell fragments."""

    name = "columnar:parquet"
    accepts = frozenset({"application/vnd.apache.parquet"})

    def __init__(self) -> None:
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise ExtractorError(
                "ParquetExtractor requires the [columnar] extra"
            ) from exc
        self._pq = pq

    async def extract(
        self, doc: Document | DocumentChunk
    ) -> AsyncIterator[ExtractedFragment]:
        import pyarrow as pa

        payload = _as_bytes(doc)
        table = self._pq.read_table(io.BytesIO(payload))
        for col_idx, field in enumerate(table.schema):
            if not _is_string_arrow(field.type, pa):
                continue
            column = table.column(col_idx)
            for row_idx, value in enumerate(column.to_pylist()):
                if value is None or not isinstance(value, str):
                    continue
                yield ExtractedFragment(
                    text=value,
                    path_hint=f"column:{field.name}/row:{row_idx}",
                    byte_offset=None,
                    extractor=self.name,
                )


class AvroExtractor:
    """Avro -> per-string-field fragments."""

    name = "columnar:avro"
    accepts = frozenset({"application/vnd.apache.avro"})

    def __init__(self) -> None:
        try:
            import fastavro
        except ImportError as exc:
            raise ExtractorError(
                "AvroExtractor requires the [columnar] extra"
            ) from exc
        self._fastavro = fastavro

    async def extract(
        self, doc: Document | DocumentChunk
    ) -> AsyncIterator[ExtractedFragment]:
        payload = _as_bytes(doc)
        reader = self._fastavro.reader(io.BytesIO(payload))
        for row_idx, record in enumerate(reader):
            for field_name, value in record.items():
                if not isinstance(value, str):
                    continue
                yield ExtractedFragment(
                    text=value,
                    path_hint=f"column:{field_name}/row:{row_idx}",
                    byte_offset=None,
                    extractor=self.name,
                )


class CsvExtractor:
    """CSV -> per-string-cell fragments.

    Header row is treated as cell content (it can carry PII via column
    names like ``customer_email``). Numeric-looking cells are skipped.
    """

    name = "columnar:csv"
    accepts = frozenset({"text/csv"})

    async def extract(
        self, doc: Document | DocumentChunk
    ) -> AsyncIterator[ExtractedFragment]:
        payload = doc_payload(doc)
        if isinstance(payload, bytes):
            text = decode_bytes(payload)
        else:
            text = payload
        reader = csv.reader(io.StringIO(text))
        headers: list[str] | None = None
        for row_idx, row in enumerate(reader):
            if headers is None:
                headers = list(row)
            for col_idx, value in enumerate(row):
                if not value or _looks_numeric_or_bool(value):
                    continue
                col_name = (
                    headers[col_idx]
                    if headers and col_idx < len(headers)
                    else f"col{col_idx}"
                )
                yield ExtractedFragment(
                    text=value,
                    path_hint=f"column:{col_name}/row:{row_idx}",
                    byte_offset=None,
                    extractor=self.name,
                )


class JsonlExtractor:
    """JSON Lines -> per-string-leaf fragments.

    Recurses into nested objects and arrays so a deeply-nested PII field
    is still emitted. Skips non-string leaves.
    """

    name = "columnar:jsonl"
    accepts = frozenset({"application/x-ndjson", "application/jsonl"})

    async def extract(
        self, doc: Document | DocumentChunk
    ) -> AsyncIterator[ExtractedFragment]:
        payload = doc_payload(doc)
        if isinstance(payload, bytes):
            text = decode_bytes(payload)
        else:
            text = payload
        for line_idx, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            for path, value in _walk_json(obj, ""):
                yield ExtractedFragment(
                    text=value,
                    path_hint=f"line:{line_idx}{path}",
                    byte_offset=None,
                    extractor=self.name,
                )


def _walk_json(node: object, prefix: str) -> Iterable[tuple[str, str]]:
    if isinstance(node, str):
        yield (prefix, node)
        return
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_json(v, f"{prefix}/{k}")
        return
    if isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_json(v, f"{prefix}[{i}]")
        return


def _is_string_arrow(arrow_type, pa) -> bool:
    """True for arrow types that can carry free-form text.

    Excludes binary (which cannot reliably regex-scan) and dict-encoded
    columns whose value type is non-string.
    """
    if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return True
    if pa.types.is_dictionary(arrow_type):
        return _is_string_arrow(arrow_type.value_type, pa)
    return False


def _looks_numeric_or_bool(value: str) -> bool:
    """Cheap pre-filter to skip numeric / boolean CSV cells."""
    v = value.strip().lower()
    if v in {"true", "false", "null", "none"}:
        return True
    try:
        float(v)
    except ValueError:
        return False
    return True


def _as_bytes(doc: Document | DocumentChunk) -> bytes:
    payload = doc_payload(doc)
    if isinstance(payload, str):
        raise ExtractorError("Columnar extractors require binary payload")
    return payload
