"""Verifies the local NER pass actually loads ja_ner_ja and detects PERSON.

This test loads the spaCy model so it's slow on first run. When the model
isn't installed the test is skipped with a clear message rather than
failing — useful for environments where the wheel can't be downloaded
(air-gapped CI, etc.).
"""

from pathlib import Path

import pytest


def _model_available() -> bool:
    try:
        import spacy

        spacy.load("ja_ner_ja")
        return True
    except (ImportError, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _model_available(),
    reason="ja_ner_ja model not installed in venv",
)


def test_ner_detects_person_in_japanese_text():
    from pleno_scan.ner_pass import scan_text

    findings = scan_text(
        "山田太郎さんに連絡してください。電話: 090-1234-5678",
        "x.txt",
        language="ja",
    )
    entities = {f.entity for f in findings}
    assert "PERSON" in entities, f"expected PERSON in {entities}"
    assert "PHONE_NUMBER" in entities, f"expected PHONE_NUMBER in {entities}"


def test_ner_returns_line_col():
    from pleno_scan.ner_pass import scan_text

    text = "header\n名前: 山田太郎\n"
    findings = scan_text(text, "x.txt", language="ja")
    persons = [f for f in findings if f.entity == "PERSON"]
    assert persons, "expected at least one PERSON finding"
    assert persons[0].line == 2


def test_ner_pass_scan_files_reuses_model(tmp_path: Path):
    from pleno_scan.ner_pass import scan_files

    f1 = tmp_path / "a.txt"
    f1.write_text("山田太郎")
    f2 = tmp_path / "b.txt"
    f2.write_text("佐藤花子")

    files = [(Path(p.name), p) for p in (f1, f2)]
    file_text = {p.name: p.read_text() for p in (f1, f2)}
    findings = scan_files(files, file_text, language="ja")
    assert any(f.entity == "PERSON" and f.file == "a.txt" for f in findings)
    assert any(f.entity == "PERSON" and f.file == "b.txt" for f in findings)
