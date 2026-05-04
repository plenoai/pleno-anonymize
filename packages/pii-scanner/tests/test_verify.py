from pleno_recognizers.ja import JA_CREDIT_CARD, JA_PHONE

from pleno_pii_scanner.models import Finding
from pleno_pii_scanner.verify import verify


def _f(entity, matched, file="a.txt"):
    return Finding(
        entity=entity, file=file, line=1, col=1, score=0.5,
        snippet=matched, matched=matched, pattern_name="p",
    )


def test_verify_credit_card_passes_luhn():
    out = verify([_f("CREDIT_CARD", "4242424242424242")], [JA_CREDIT_CARD])
    assert out[0].verification == "passed"


def test_verify_credit_card_fails_luhn():
    out = verify([_f("CREDIT_CARD", "4242424242424241")], [JA_CREDIT_CARD])
    assert out[0].verification == "failed"


def test_verify_unverified_when_no_validator():
    out = verify([_f("PHONE_NUMBER", "090-1234-5678")], [JA_PHONE])
    assert out[0].verification == "unverified"


def test_verify_context_promotes_to_passed():
    findings = [_f("PHONE_NUMBER", "090-1234-5678", file="x.txt")]
    text = {"x.txt": "電話: 090-1234-5678"}
    out = verify(findings, [JA_PHONE], file_text_for=text)
    assert out[0].verification == "passed"
    assert out[0].score > 0.5


def test_verify_person_email_proximity_boosts_score():
    """Issue #102: a low-score PERSON regex hit gains a strong bump when an
    email sits in the wider window. PEP-style author lines need this so
    "Guido van Rossum" surfaces above the noise floor."""
    from pleno_recognizers.ja import JA_PERSON_LATIN

    f = Finding(
        entity="PERSON",
        file="pep.rst",
        line=3,
        col=10,
        score=0.3,
        snippet="| Author: Guido van Rossum <guido@python.org>,",
        matched="Guido van Rossum",
        pattern_name="person_latin_multi_word",
    )
    text = {"pep.rst": "| PEP: 8\n| Title: Style\n| Author: Guido van Rossum <guido@python.org>,\n"}
    out = verify([f], [JA_PERSON_LATIN], file_text_for=text)
    assert out[0].verification == "passed"
    assert out[0].score >= 0.7, out[0].score


def test_verify_person_without_email_stays_unverified():
    """No email nearby means no promotion — score floor preserved."""
    from pleno_recognizers.ja import JA_PERSON_LATIN

    f = Finding(
        entity="PERSON",
        file="readme.md",
        line=1,
        col=1,
        score=0.3,
        snippet="See Apache License terms.",
        matched="Apache License",
        pattern_name="person_latin_multi_word",
    )
    text = {"readme.md": "See Apache License terms.\n"}
    out = verify([f], [JA_PERSON_LATIN], file_text_for=text)
    # No email in window — no promotion. (noise_filters drops these later.)
    assert out[0].verification == "unverified"
    assert out[0].score == 0.3
