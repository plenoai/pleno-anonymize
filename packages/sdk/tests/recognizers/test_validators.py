from pleno_anonymize.recognizers.validators import (
    luhn,
    my_number,
    corporate_number,
    validate,
)


def test_luhn_valid():
    assert luhn("4242424242424242") is True
    assert luhn("4242-4242-4242-4242") is True
    assert luhn("4111 1111 1111 1111") is True


def test_luhn_invalid():
    assert luhn("4242424242424241") is False
    assert luhn("1234567890123456") is False
    assert luhn("123") is False


def test_my_number_valid():
    # Generated example matching weighted-sum spec.
    # body=12345678901, weights=(6,5,4,3,2,7,6,5,4,3,2)
    # s = 1*6+2*5+3*4+4*3+5*2+6*7+7*6+8*5+9*4+0*3+1*2 = 212
    # r = 212 % 11 = 3, check = 11-3 = 8
    assert my_number("123456789018") is True
    assert my_number("1234-5678-9018") is True


def test_my_number_invalid():
    assert my_number("123456789010") is False
    assert my_number("12345678901") is False  # too short


def test_corporate_number_valid():
    # Real public corporate numbers (NTA registry)
    assert corporate_number("1180301018771") is True  # トヨタ自動車
    assert corporate_number("5010401067252") is True  # ソニーグループ
    assert corporate_number("7000012050002") is True  # 国税庁


def test_corporate_number_invalid():
    assert corporate_number("1123456789012") is False
    assert corporate_number("123456789012") is False  # too short
    assert corporate_number("2123456789012") is False  # wrong check digit


def test_validate_dispatch():
    assert validate("CREDIT_CARD", "4242424242424242") is True
    assert validate("CREDIT_CARD", "4242424242424241") is False
    assert validate("PHONE_NUMBER", "090-1234-5678") is None  # no validator
