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
