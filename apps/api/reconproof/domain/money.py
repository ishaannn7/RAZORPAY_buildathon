"""Integer-subunit money arithmetic.

Every monetary value in ReconProof is an integer count of the currency's minor
unit (paise for INR). Floating point never touches a financial amount: the
reconciliation guarantees in ``accounting.invariants`` are exact-equality
assertions, and IEEE-754 rounding would make them unprovable.

Rate arithmetic (fees, tax) is the one place a fraction is unavoidable. It is
performed in :class:`decimal.Decimal` with an explicit rounding mode and
immediately collapsed back to an integer subunit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, Self

SUBUNIT_EXPONENT: Final[dict[str, int]] = {
    "INR": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "AED": 2,
    "SGD": 2,
    "JPY": 0,
    "KWD": 3,
    "BHD": 3,
}

DEFAULT_CURRENCY: Final[str] = "INR"

# Amounts above this (in subunits) are rejected as parse errors rather than
# stored. 10^15 paise is ~10 trillion rupees; any real merchant row below it.
MAX_SUBUNITS: Final[int] = 10**15

_AMOUNT_CLEAN_RE: Final[re.Pattern[str]] = re.compile(
    r"[,\s\u00a0_]|(?:INR|Rs\.?|₹|USD|\$)", re.IGNORECASE
)
_PARENS_NEGATIVE_RE: Final[re.Pattern[str]] = re.compile(r"^\((.+)\)$")


class MoneyError(ValueError):
    """Raised when a monetary value cannot be represented exactly."""


class CurrencyMismatchError(MoneyError):
    """Raised when an operation combines two different currencies."""


def subunit_exponent(currency: str) -> int:
    """Return the number of decimal places in *currency*'s minor unit."""
    try:
        return SUBUNIT_EXPONENT[currency.upper()]
    except KeyError as exc:
        raise MoneyError(f"unsupported currency: {currency!r}") from exc


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """An exact monetary amount held as integer minor units.

    ``Money(12345, "INR")`` is ₹123.45. Construction never accepts a float.
    """

    subunits: int
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        if not isinstance(self.subunits, int) or isinstance(self.subunits, bool):
            raise MoneyError(f"subunits must be int, got {type(self.subunits).__name__}")
        if abs(self.subunits) > MAX_SUBUNITS:
            raise MoneyError(f"amount out of representable range: {self.subunits}")
        normalized = self.currency.upper()
        if normalized not in SUBUNIT_EXPONENT:
            raise MoneyError(f"unsupported currency: {self.currency!r}")
        object.__setattr__(self, "currency", normalized)

    # ---- constructors -----------------------------------------------------

    @classmethod
    def zero(cls, currency: str = DEFAULT_CURRENCY) -> Self:
        return cls(0, currency)

    @classmethod
    def from_major(cls, major: str | int | Decimal, currency: str = DEFAULT_CURRENCY) -> Self:
        """Build from a major-unit value (rupees). Rejects inexact conversions.

        A float is refused outright: ``0.07`` is not representable in binary
        floating point, and silently accepting it would seed rounding drift
        into the ledger.
        """
        if isinstance(major, float):
            raise MoneyError("refusing to build Money from float; pass str or Decimal")
        exponent = subunit_exponent(currency)
        try:
            value = Decimal(major)
        except (InvalidOperation, TypeError) as exc:
            raise MoneyError(f"cannot parse amount: {major!r}") from exc
        scaled = value.scaleb(exponent)
        if scaled != scaled.to_integral_value():
            raise MoneyError(
                f"amount {major!r} has more precision than {currency} supports "
                f"({exponent} decimal places)"
            )
        return cls(int(scaled), currency)

    @classmethod
    def parse(cls, raw: str | int | Decimal, currency: str = DEFAULT_CURRENCY) -> Self:
        """Parse a messy source-file amount such as ``"1,23,456.78"`` or ``"(450.00)"``.

        Accounting parentheses denote a negative amount. Currency symbols,
        thousands separators (including Indian lakh grouping) and non-breaking
        spaces are stripped. Anything left that is not a clean decimal raises.
        """
        if isinstance(raw, int | Decimal):
            return cls.from_major(raw, currency)
        text = raw.strip()
        if not text:
            raise MoneyError("empty amount")
        negative = False
        parens = _PARENS_NEGATIVE_RE.match(text)
        if parens:
            negative = True
            text = parens.group(1).strip()
        text = _AMOUNT_CLEAN_RE.sub("", text)
        if text.startswith("-"):
            negative = not negative
            text = text[1:]
        elif text.startswith("+"):
            text = text[1:]
        if not text or not re.fullmatch(r"\d*(?:\.\d*)?", text):
            raise MoneyError(f"cannot parse amount: {raw!r}")
        money = cls.from_major(Decimal(text or "0"), currency)
        return cls(-money.subunits, currency) if negative else money

    # ---- arithmetic -------------------------------------------------------

    def _require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"cannot combine {self.currency} with {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.subunits + other.subunits, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.subunits - other.subunits, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.subunits, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.subunits), self.currency)

    def __mul__(self, count: int) -> Money:
        if not isinstance(count, int) or isinstance(count, bool):
            raise MoneyError("Money may only be multiplied by an int")
        return Money(self.subunits * count, self.currency)

    def apply_rate(self, rate: Decimal | str, rounding: str = ROUND_HALF_UP) -> Money:
        """Apply a fractional rate (e.g. a 2% fee) and round to whole subunits.

        The rounding mode is explicit because fee and tax lines in a settlement
        report are rounded by the provider, and reconciliation must reproduce
        that choice rather than guess it.
        """
        factor = Decimal(rate)
        product = (Decimal(self.subunits) * factor).quantize(Decimal(1), rounding=rounding)
        return Money(int(product), self.currency)

    # ---- comparison -------------------------------------------------------

    def __lt__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.subunits < other.subunits

    def __le__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.subunits <= other.subunits

    def __gt__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.subunits > other.subunits

    def __ge__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.subunits >= other.subunits

    # ---- presentation -----------------------------------------------------

    @property
    def is_zero(self) -> bool:
        return self.subunits == 0

    @property
    def major(self) -> Decimal:
        """The amount in major units, exact. For display and export only."""
        return Decimal(self.subunits).scaleb(-subunit_exponent(self.currency))

    def format(self) -> str:
        exponent = subunit_exponent(self.currency)
        sign = "-" if self.subunits < 0 else ""
        digits = str(abs(self.subunits)).rjust(exponent + 1, "0")
        if exponent == 0:
            return f"{sign}{digits}"
        return f"{sign}{digits[:-exponent]}.{digits[-exponent:]}"

    def __str__(self) -> str:
        return f"{self.currency} {self.format()}"

    def __repr__(self) -> str:
        return f"Money({self.subunits}, {self.currency!r})"


def total(amounts: object, currency: str = DEFAULT_CURRENCY) -> Money:
    """Sum an iterable of :class:`Money`, returning zero for an empty input.

    The explicit *currency* argument makes the empty case well-defined instead
    of raising, which matters when summing an empty allocation set.
    """
    result = Money.zero(currency)
    for amount in amounts:  # type: ignore[attr-defined]
        if not isinstance(amount, Money):
            raise MoneyError(f"expected Money, got {type(amount).__name__}")
        result = result + amount
    return result


def looks_like_unit_confusion(a: Money, b: Money) -> bool:
    """True when *a* and *b* differ by exactly a factor of 100.

    A rupees-column read as paise (or the reverse) is one of the most common
    real ingestion faults. Detecting it lets the validator report the true
    cause instead of emitting an unexplained mismatch.
    """
    if a.currency != b.currency or a.is_zero or b.is_zero:
        return False
    high, low = (
        (a.subunits, b.subunits) if abs(a.subunits) > abs(b.subunits) else (b.subunits, a.subunits)
    )
    return low != 0 and high == low * 100
