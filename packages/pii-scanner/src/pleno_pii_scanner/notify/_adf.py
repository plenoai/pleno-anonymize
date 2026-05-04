"""Atlassian Document Format (ADF) builder for Jira issue bodies.

Kept private (underscore prefix) — only the Jira transport consumes it.
We hand-roll the document instead of pulling atlassian-python-api so the
notifier core stays single-dep (httpx).
"""

from __future__ import annotations

from typing import Mapping, Sequence

from pleno_pii_scanner.models import Finding
from pleno_pii_scanner.notify.base import excerpt, severity_for


def build_issue_adf(
    *,
    scan_id: str,
    findings: Sequence[Finding],
    severity_summary: Mapping[str, int],
    metadata: Mapping[str, str],
) -> dict:
    """Return an ADF document describing the batch."""
    summary_text = ", ".join(
        f"{sev}: {n}" for sev, n in sorted(severity_summary.items())
    ) or "no findings"
    metadata_pairs = ", ".join(f"{k}={v}" for k, v in sorted(metadata.items()))

    rows: list[dict] = [_table_row_header()]
    for f in findings:
        rows.append(
            _table_row_cells(
                [
                    severity_for(f),
                    f.entity,
                    f.verification,
                    f"{f.file}:{f.line}",
                    excerpt(f),
                ]
            )
        )

    return {
        "version": 1,
        "type": "doc",
        "content": [
            _heading(f"PII scan {scan_id}"),
            _paragraph(f"Severity summary: {summary_text}."),
            _paragraph(f"Metadata: {metadata_pairs}" if metadata_pairs else "Metadata: -"),
            {"type": "table", "content": rows},
        ],
    }


def _heading(text: str, level: int = 2) -> dict:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def _paragraph(text: str) -> dict:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text}],
    }


def _table_row_header() -> dict:
    return {
        "type": "tableRow",
        "content": [
            _header_cell(t)
            for t in ("severity", "entity", "verification", "location", "excerpt")
        ],
    }


def _table_row_cells(values: Sequence[str]) -> dict:
    return {
        "type": "tableRow",
        "content": [_data_cell(v) for v in values],
    }


def _header_cell(text: str) -> dict:
    return {
        "type": "tableHeader",
        "attrs": {},
        "content": [_paragraph(text)],
    }


def _data_cell(text: str) -> dict:
    return {
        "type": "tableCell",
        "attrs": {},
        "content": [_paragraph(text)],
    }


def comment_adf(text: str) -> dict:
    """Plain ADF document for `POST /issue/{key}/comment` bodies."""
    return {
        "version": 1,
        "type": "doc",
        "content": [_paragraph(text)],
    }
