"""Scanner tests — engine is stubbed so these run without spaCy or network."""

from __future__ import annotations

import re
from pathlib import Path

from pleno_anonymize import Finding, scan_paths


class _FakeEngine:
    """Detects emails via a regex — fast and deterministic."""

    pattern = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

    def analyze(self, text, *, language="ja", entities=None):  # noqa: ARG002
        out: list[Finding] = []
        for m in self.pattern.finditer(text):
            out.append(
                Finding(
                    entity_type="EMAIL_ADDRESS",
                    start=m.start(),
                    end=m.end(),
                    score=1.0,
                    text=m.group(0),
                )
            )
        return out

    def redact(self, text, **_):
        from pleno_anonymize import RedactResult

        return RedactResult(text=self.pattern.sub("<EMAIL_ADDRESS>", text))


def test_scan_paths_walks_dirs_and_skips_node_modules(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("Contact john@example.com", encoding="utf-8")
    (tmp_path / "b.md").write_text("no pii here", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skip.md").write_text(
        "ignored@example.com", encoding="utf-8"
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "c.txt").write_text("Email: alice@example.com", encoding="utf-8")

    summary = scan_paths(_FakeEngine(), [str(tmp_path)])
    scanned_paths = [f.path for f in summary.files if not f.skipped]

    assert any(p.endswith("a.md") for p in scanned_paths)
    assert any(p.endswith("c.txt") for p in scanned_paths)
    assert not any("node_modules" in p for p in scanned_paths)
    assert summary.total_findings == 2
    assert summary.by_entity == {"EMAIL_ADDRESS": 2}


def test_scan_skips_binary(tmp_path: Path) -> None:
    bin_path = tmp_path / "blob.txt"
    bin_path.write_bytes(b"hello\x00world@example.com")
    summary = scan_paths(_FakeEngine(), [str(tmp_path)])
    assert summary.scanned_files == 0
    assert summary.skipped_files == 1
    assert summary.files[0].skipped == "binary"


def test_scan_respects_include_extensions(tmp_path: Path) -> None:
    (tmp_path / "keep.md").write_text("a@b.example", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("c@d.example", encoding="utf-8")
    summary = scan_paths(_FakeEngine(), [str(tmp_path)], include_extensions=[".md"])
    assert summary.scanned_files == 1
    assert summary.files[0].path.endswith("keep.md")
