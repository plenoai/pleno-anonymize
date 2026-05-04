"""Issue #102 regression: Latin-script personal names in Japanese-mixed text.

The legacy ja_ner_ja default returned **zero** PERSON detections for English
author attributions embedded in Japanese release notes (nodejs/nodejs-ja
weekly notes) and translation credits (mumumu/pep8-ja headers). The fix
combines:

  1. A low-score ``PERSON_LATIN`` regex recognizer (recall booster).
  2. ``verify`` promoting the candidate when an email sits in the wider
     window (PEP author lists span continuation lines).
  3. A noise filter dropping Latin-name candidates that lack an
     author-context signal — so "Apache License" / "Pull Request" /
     "Hello World" do not surface as PERSON.

These tests run the full pipeline (NER + verify + noise filter) against
fixtures that mirror the original failing inputs from the issue.
"""

from __future__ import annotations

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


_FIXTURES = Path(__file__).parent / "fixtures" / "latin_names"


def _scan(path: Path):
    """Run the production pipeline on a single file: NER → verify → noise."""
    from pleno_pii_scanner.ner_pass import scan_text
    from pleno_pii_scanner.noise_filters import filter_noise
    from pleno_pii_scanner.verify import verify
    from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

    text = path.read_text(encoding="utf-8")
    file_text = {path.name: text}
    findings = scan_text(text, path.name, language="ja")
    findings = verify(findings, ALL_JA_RECOGNIZERS, file_text_for=file_text)
    findings = filter_noise(findings, file_text_for=file_text)
    return findings


def test_nodejs_ja_weekly_credits_detect_latin_persons():
    """Acceptance: nodejs-ja-style "(Yosuke Furukawa) [#1313]" credits surface."""
    findings = _scan(_FIXTURES / "nodejs_ja_weekly.md")
    persons = {f.matched for f in findings if f.entity == "PERSON"}
    # Issue body explicitly calls out these three.
    assert "Yosuke Furukawa" in persons, persons
    assert "Roman Reiss" in persons, persons
    assert "Evan Lucas" in persons, persons


def test_pep8_ja_translation_credits_detect_latin_persons():
    """Acceptance: PEP-style "Author: Name <email>" credits surface."""
    findings = _scan(_FIXTURES / "pep8_ja_credits.rst")
    persons = {f.matched for f in findings if f.entity == "PERSON"}
    assert "Guido van Rossum" in persons, persons
    assert "Barry Warsaw" in persons, persons
    assert "Alyssa Coghlan" in persons, persons


def test_email_proximity_promotes_person_to_passed():
    """When an email sits in the window, PERSON candidates pass verification."""
    findings = _scan(_FIXTURES / "pep8_ja_credits.rst")
    rossum = [
        f for f in findings if f.entity == "PERSON" and f.matched == "Guido van Rossum"
    ]
    assert rossum, "expected Guido van Rossum to be detected"
    # Score promoted past the noise floor; verification flagged passed.
    assert rossum[0].score >= 0.5
    assert rossum[0].verification == "passed"


def test_normal_prose_does_not_flood_with_latin_pseudo_names():
    """Apache License / Pull Request / Hello World must not surface as PERSON.

    This is the precision guardrail. The PERSON_LATIN regex is intentionally
    permissive; the noise filter is what stops it from flooding any English-
    leaning README with garbage.
    """
    findings = _scan(_FIXTURES / "normal_prose.md")
    person_matches = {f.matched for f in findings if f.entity == "PERSON"}
    assert "Apache License" not in person_matches, person_matches
    assert "Pull Request" not in person_matches, person_matches
    assert "Hello World" not in person_matches, person_matches
    assert "Code of Conduct" not in person_matches, person_matches
    assert "Quick Start" not in person_matches, person_matches
    assert "Table of Contents" not in person_matches, person_matches
