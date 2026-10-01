import pytest

from app.models.identifiers import (
    InvalidCikError,
    InvalidTickerError,
    looks_like_cik,
    normalize_cik,
    normalize_optional_cik,
    normalize_ticker,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (320193, "0000320193"),  # the canonical example, as an int
        ("320193", "0000320193"),  # as a string
        ("0000320193", "0000320193"),  # already padded
        ("  320193 \n", "0000320193"),  # whitespace
        ("\t0000320193\t", "0000320193"),
        (1, "0000000001"),
        (9_999_999_999, "9999999999"),  # largest 10-digit CIK
        ("CIK0000320193", "0000320193"),  # EDGAR display form
        ("cik 320193", "0000320193"),
        ("00000000320193", "0000320193"),  # extra leading zeros, value still fits
    ],
)
def test_normalize_cik_accepts(value, expected):
    assert normalize_cik(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "   ",
        "abc",  # non-numeric
        "12ab34",
        "-320193",  # sign
        "+320193",
        "320,193",  # separators
        "320 193",
        "320193.0",  # float text is ambiguous
        320193.0,  # float
        None,
        True,  # bool is an int subclass but not a CIK
        0,  # zero is not a CIK
        "0000000000",
        -5,
        10_000_000_000,  # 11 digits: too large
        "10000000000",
        "12345678901234",  # too long even without padding
        "²³",  # non-ASCII digits that str.isdigit() would accept
        "٣٢٠١٩٣",  # Arabic-Indic digits
        b"320193",
        [320193],
    ],
)
def test_normalize_cik_rejects(value):
    with pytest.raises(InvalidCikError):
        normalize_cik(value)


def test_invalid_cik_error_is_a_value_error_with_a_clear_message():
    with pytest.raises(ValueError, match="digits"):
        normalize_cik("abc")
    with pytest.raises(ValueError, match="at most 10 digits"):
        normalize_cik(10**10)


def test_normalize_is_idempotent():
    once = normalize_cik(320193)
    assert normalize_cik(once) == once


def test_optional_cik():
    assert normalize_optional_cik(None) is None
    assert normalize_optional_cik(320193) == "0000320193"
    with pytest.raises(InvalidCikError):
        normalize_optional_cik("")  # empty is invalid, not "unknown"


@pytest.mark.parametrize(
    ("text", "expected"), [("320193", True), ("CIK320193", True), ("AAPL", False), ("BRK.B", False)]
)
def test_looks_like_cik(text, expected):
    assert looks_like_cik(text) is expected


@pytest.mark.parametrize(
    ("value", "expected"), [("aapl", "AAPL"), (" brk.b ", "BRK.B"), ("BRK-B", "BRK-B"), ("X", "X")]
)
def test_normalize_ticker(value, expected):
    assert normalize_ticker(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "AAPL; DROP TABLE", "a b", "TOOLONGTICKERSYMBOL", "$AAPL", None, 5])
def test_normalize_ticker_rejects(value):
    with pytest.raises(InvalidTickerError):
        normalize_ticker(value)
