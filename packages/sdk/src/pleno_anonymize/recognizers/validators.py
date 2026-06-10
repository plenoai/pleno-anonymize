"""Checksum validators for PII identifiers.

Each validator returns True if the value passes the formal checksum.
A failed checksum strongly suggests a false positive.
"""

import re

_DIGITS = re.compile(r"\d")


def _digits(value: str) -> str:
    return "".join(_DIGITS.findall(value))


def luhn(value: str) -> bool:
    """Credit card Luhn check. Accepts dashes/spaces."""
    digits = _digits(value)
    if len(digits) < 12 or len(digits) > 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


_MY_NUMBER_WEIGHTS = (6, 5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


def my_number(value: str) -> bool:
    """Japanese individual My Number (12 digits) check digit.

    Spec: https://www.cas.go.jp/jp/seisaku/bangoseido/pdf/checkdigit.pdf
    """
    digits = _digits(value)
    if len(digits) != 12:
        return False
    body, check = digits[:11], int(digits[11])
    s = sum(int(d) * w for d, w in zip(body, _MY_NUMBER_WEIGHTS))
    r = s % 11
    expected = 0 if r <= 1 else 11 - r
    return expected == check


_CORP_NUMBER_WEIGHTS = (
    2,
    1,
    2,
    1,
    2,
    1,
    2,
    1,
    2,
    1,
    2,
    1,
)  # high→low position left-to-right


def corporate_number(value: str) -> bool:
    """Japanese corporate number (13 digits): check digit is the leading digit.

    Spec: https://www.houjin-bangou.nta.go.jp/setsumei/
    """
    digits = _digits(value)
    if len(digits) != 13:
        return False
    check = int(digits[0])
    body = digits[1:]
    s = sum(int(d) * w for d, w in zip(body, _CORP_NUMBER_WEIGHTS))
    expected = 9 - (s % 9)
    return expected == check


VALIDATORS: dict[str, callable] = {
    "CREDIT_CARD": luhn,
    "MY_NUMBER": my_number,
    "MY_NUMBER_CORPORATE": corporate_number,
}


def validate(entity: str, value: str) -> bool | None:
    """Returns True/False if a validator exists for this entity, else None."""
    fn = VALIDATORS.get(entity)
    if fn is None:
        return None
    return fn(value)
