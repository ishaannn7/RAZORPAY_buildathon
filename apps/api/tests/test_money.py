"""Money arithmetic must be exact. These are the properties everything else assumes."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reconproof.domain.money import (
    CurrencyMismatchError,
    Money,
    MoneyError,
    looks_like_unit_confusion,
    total,
)

subunits = st.integers(min_value=-(10**12), max_value=10**12)


class TestExactness:
    def test_refuses_float_construction(self) -> None:
        # 0.07 is not representable in binary floating point. Accepting it would
        # seed rounding drift into every downstream balance assertion.
        with pytest.raises(MoneyError):
            Money.from_major(0.07)

    def test_refuses_excess_precision(self) -> None:
        with pytest.raises(MoneyError):
            Money.from_major(Decimal("1.005"), "INR")

    def test_accepts_exact_precision(self) -> None:
        assert Money.from_major(Decimal("1.00"), "INR").subunits == 100

    @given(subunits)
    def test_major_roundtrip(self, value: int) -> None:
        money = Money(value, "INR")
        assert Money.from_major(money.major, "INR") == money

    def test_zero_decimal_currency(self) -> None:
        assert Money.from_major(500, "JPY").subunits == 500
        assert Money(500, "JPY").format() == "500"

    def test_three_decimal_currency(self) -> None:
        assert Money.from_major(Decimal("1.234"), "KWD").subunits == 1234


class TestParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1234.56", 123456),
            ("1,23,456.78", 12345678),  # Indian lakh grouping
            ("₹ 3,241.00", 324100),
            ("INR 428.00", 42800),
            (" 12330.49 ", 1233049),
            ("(450.00)", -45000),  # accounting parentheses
            ("-450.00", -45000),
            ("0.00", 0),
            ("99", 9900),
        ],
    )
    def test_parses_messy_formats(self, raw: str, expected: int) -> None:
        assert Money.parse(raw, "INR").subunits == expected

    def test_double_negative_parentheses_and_sign(self) -> None:
        # "(-450.00)" is a negated negative, which is positive.
        assert Money.parse("(-450.00)", "INR").subunits == 45000

    @pytest.mark.parametrize("raw", ["", "  ", "abc", "12.34.56", "1,2,3.4.5", "--5"])
    def test_rejects_unparseable(self, raw: str) -> None:
        with pytest.raises(MoneyError):
            Money.parse(raw, "INR")


class TestArithmetic:
    @given(subunits, subunits)
    def test_addition_is_exact(self, left: int, right: int) -> None:
        assert (Money(left) + Money(right)).subunits == left + right

    @given(subunits, subunits)
    def test_subtraction_inverts_addition(self, left: int, right: int) -> None:
        money = Money(left)
        assert (money + Money(right)) - Money(right) == money

    @given(st.lists(subunits, max_size=40))
    def test_total_matches_python_sum(self, values: list[int]) -> None:
        assert total([Money(value) for value in values]).subunits == sum(values)

    def test_empty_total_is_zero(self) -> None:
        assert total([], "INR") == Money.zero("INR")

    def test_currency_mismatch_refused(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            Money(100, "INR") + Money(100, "USD")

    def test_rate_rounding_is_explicit(self) -> None:
        # 2% of 619.25 is 12.385, which must round half-up to 12.39 to match the
        # provider's own fee line.
        assert Money.parse("619.25").apply_rate(Decimal("0.02")).format() == "12.39"

    @given(subunits)
    def test_multiplication_by_int(self, value: int) -> None:
        assert (Money(value) * 3).subunits == value * 3

    def test_multiplication_by_float_refused(self) -> None:
        with pytest.raises(MoneyError):
            Money(100) * 1.5  # type: ignore[operator]


class TestUnitConfusion:
    def test_detects_exact_hundred_multiple(self) -> None:
        assert looks_like_unit_confusion(Money(50000), Money(500))

    def test_ignores_other_ratios(self) -> None:
        assert not looks_like_unit_confusion(Money(50000), Money(501))
        assert not looks_like_unit_confusion(Money(50000), Money(5000))  # 10x
        assert not looks_like_unit_confusion(Money(50000), Money(50))  # 1000x

    def test_detection_is_symmetric(self) -> None:
        assert looks_like_unit_confusion(Money(1000), Money(10))
        assert looks_like_unit_confusion(Money(10), Money(1000))

    def test_zero_is_not_confusion(self) -> None:
        assert not looks_like_unit_confusion(Money(0), Money(0))

    def test_different_currencies_not_compared(self) -> None:
        assert not looks_like_unit_confusion(Money(50000, "INR"), Money(500, "USD"))


class TestFormatting:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, "0.00"), (5, "0.05"), (100, "1.00"), (-45000, "-450.00"), (12345678, "123456.78")],
    )
    def test_format(self, value: int, expected: str) -> None:
        assert Money(value, "INR").format() == expected
