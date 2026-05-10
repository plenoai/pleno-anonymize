import re

from pleno_anonymize.recognizers.ja import ALL_JA_RECOGNIZERS, JA_PHONE, JA_EMAIL


def test_all_recognizers_have_compilable_regexes():
    for r in ALL_JA_RECOGNIZERS:
        for p in r.patterns:
            re.compile(p.regex)


def test_phone_matches_mobile():
    pat = next(p for p in JA_PHONE.patterns if p.name == "ja_phone_mobile")
    assert re.search(pat.regex, "連絡先は 090-1234-5678 です")
    assert re.search(pat.regex, "tel: 08012345678")


def test_email_matches():
    pat = JA_EMAIL.patterns[0]
    assert re.search(pat.regex, "send to user@example.com please")


def test_recognizer_count():
    # Bumped to 14 in issue #102: PERSON_LATIN recognizer added as a recall
    # booster for Latin-script names in Japanese-mixed text.
    assert len(ALL_JA_RECOGNIZERS) == 14
