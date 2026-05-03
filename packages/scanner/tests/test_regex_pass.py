from pleno_recognizers.ja import ALL_JA_RECOGNIZERS, JA_PHONE, JA_EMAIL

from pleno_scan.regex_pass import compile_patterns, scan_text


def test_scan_text_finds_phone_and_email():
    patterns = compile_patterns([JA_PHONE, JA_EMAIL])
    text = "連絡先: 090-1234-5678\nmail: user@example.com\n"
    findings = scan_text(text, "x.txt", patterns)
    entities = sorted({f.entity for f in findings})
    assert "PHONE_NUMBER" in entities
    assert "EMAIL_ADDRESS" in entities


def test_scan_text_line_numbers():
    patterns = compile_patterns([JA_EMAIL])
    text = "line1\nline2 user@example.com\nline3\n"
    findings = scan_text(text, "x.txt", patterns)
    assert len(findings) == 1
    assert findings[0].line == 2


def test_scan_text_returns_matched_value():
    patterns = compile_patterns([JA_PHONE])
    text = "番号は 090-1234-5678 です"
    findings = scan_text(text, "x.txt", patterns)
    assert any(f.matched == "090-1234-5678" for f in findings)


def test_compile_all_recognizers():
    patterns = compile_patterns(ALL_JA_RECOGNIZERS)
    assert len(patterns) >= 13
