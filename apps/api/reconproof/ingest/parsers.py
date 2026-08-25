"""Tolerant parsers for messy source-file values.

These parsers are deliberately strict about *ambiguity* and tolerant about
*formatting*. A value written in an unusual way should be read correctly; a
value whose meaning is genuinely unclear should raise, so it lands in the
rejected-rows report instead of entering the ledger as a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from reconproof.domain.entities import PaymentMethod, RecordStatus
from reconproof.domain.money import Money, MoneyError

#: Indian financial exports are day-first. ``05/07/2026`` is 5 July, not 7 May.
#: This assumption is recorded on every parsed row so a reviewer can see it was
#: a decision rather than an accident.
DAY_FIRST: Final[bool] = True

IST_OFFSET: Final[timedelta] = timedelta(hours=5, minutes=30)

_DATE_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d %b %Y %I:%M %p",
    "%d %b %Y %H:%M",
    "%d %b %Y",
    "%d-%b-%Y",
    "%b %d %Y",
)

_MONTH_FIRST_FORMATS: Final[tuple[str, ...]] = (
    "%m-%d-%Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
)

_TRUE_VALUES: Final[frozenset[str]] = frozenset({"true", "yes", "y", "1", "t"})
_NUMERIC_RE: Final[re.Pattern[str]] = re.compile(r"^-?\d+$")


class ParseError(ValueError):
    """Raised when a cell cannot be interpreted unambiguously."""


#: Formats that carry no time component. A value read with one of these is only
#: accurate to the day, which downstream ordering checks must respect: asserting
#: that a midnight-defaulted settlement preceded an 11:30 payment would be
#: asserting something the data never said.
_DATE_ONLY_FORMATS: Final[frozenset[str]] = frozenset(
    {"%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%b-%Y", "%b %d %Y", "%m/%d/%Y"}
)


@dataclass(frozen=True, slots=True)
class ParsedTimestamp:
    """A parsed timestamp plus what had to be assumed to read it."""

    value: datetime
    assumed_day_first: bool = False
    assumed_utc: bool = False
    #: True when the source gave a date with no time. The stored value is
    #: midnight, but any instant that day is equally consistent with the source.
    date_only: bool = False
    original: str = ""


def parse_money(raw: str | None, currency: str, *, allow_blank: bool = False) -> Money | None:
    """Parse an amount cell.

    Handles Indian lakh grouping, currency symbols, accounting parentheses for
    negatives, and stray whitespace. Delegates the exactness guarantee to
    :meth:`Money.parse`, which refuses any value with more precision than the
    currency supports.
    """
    if raw is None or not raw.strip():
        if allow_blank:
            return None
        raise ParseError("amount is required but blank")
    try:
        return Money.parse(raw, currency)
    except MoneyError as exc:
        raise ParseError(str(exc)) from exc


def parse_timestamp(raw: str | None, *, allow_blank: bool = False) -> ParsedTimestamp | None:
    """Parse a timestamp cell across the formats these exports actually use.

    A value carrying no timezone is treated as UTC and flagged. That flag is the
    honest way to represent the ambiguity: settlement reports are frequently
    IST-naive, and a five-and-a-half-hour error can move a row across a
    settlement window boundary. The timezone-shift detector downstream relies on
    knowing the assumption was made.
    """
    if raw is None or not raw.strip():
        if allow_blank:
            return None
        raise ParseError("timestamp is required but blank")
    text = raw.strip()

    # ``fromisoformat`` covers the well-formed majority, including offsets.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        # ``fromisoformat`` accepts a bare date, which lands on midnight.
        date_only = len(text) <= 10 and ":" not in text
        if parsed.tzinfo is None:
            return ParsedTimestamp(
                parsed.replace(tzinfo=UTC),
                assumed_utc=True,
                date_only=date_only,
                original=text,
            )
        return ParsedTimestamp(parsed.astimezone(UTC), date_only=date_only, original=text)

    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        ambiguous = "%d" in fmt and fmt.index("%d") < fmt.index("%m") if "%m" in fmt else False
        date_only = fmt in _DATE_ONLY_FORMATS
        if parsed.tzinfo is None:
            return ParsedTimestamp(
                parsed.replace(tzinfo=UTC),
                assumed_day_first=ambiguous,
                assumed_utc=True,
                date_only=date_only,
                original=text,
            )
        return ParsedTimestamp(
            parsed.astimezone(UTC),
            assumed_day_first=ambiguous,
            date_only=date_only,
            original=text,
        )

    # Month-first is tried last and only when day-first could not apply, so a
    # genuinely ambiguous value is read using the documented convention rather
    # than whichever format happened to be listed first.
    for fmt in _MONTH_FIRST_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return ParsedTimestamp(
            parsed.replace(tzinfo=UTC),
            assumed_day_first=False,
            assumed_utc=True,
            date_only=fmt in _DATE_ONLY_FORMATS,
            original=text,
        )

    raise ParseError(f"unrecognized timestamp format: {raw!r}")


def parse_int(raw: str | None, *, allow_blank: bool = True) -> int | None:
    if raw is None or not raw.strip():
        if allow_blank:
            return None
        raise ParseError("integer is required but blank")
    text = raw.strip().replace(",", "")
    if not _NUMERIC_RE.match(text):
        raise ParseError(f"not an integer: {raw!r}")
    return int(text)


def parse_bool(raw: str | None) -> bool:
    return bool(raw) and raw.strip().lower() in _TRUE_VALUES


def parse_currency(raw: str | None, default: str) -> str:
    if raw is None or not raw.strip():
        return default
    text = raw.strip().upper()
    aliases = {"RS": "INR", "RS.": "INR", "₹": "INR", "$": "USD", "INR.": "INR"}
    return aliases.get(text, text)


def parse_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def parse_payment_method(raw: str | None) -> PaymentMethod:
    if not raw:
        return PaymentMethod.UNKNOWN
    text = raw.strip().lower()
    aliases = {
        "upi": PaymentMethod.UPI,
        "card": PaymentMethod.CARD,
        "credit_card": PaymentMethod.CARD,
        "debit_card": PaymentMethod.CARD,
        "netbanking": PaymentMethod.NETBANKING,
        "net_banking": PaymentMethod.NETBANKING,
        "nb": PaymentMethod.NETBANKING,
        "wallet": PaymentMethod.WALLET,
        "emi": PaymentMethod.EMI,
    }
    return aliases.get(text, PaymentMethod.UNKNOWN)


def parse_status(raw: str | None) -> RecordStatus:
    if not raw:
        return RecordStatus.UNKNOWN
    text = raw.strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return RecordStatus(text)
    except ValueError:
        return RecordStatus.UNKNOWN


def sniff_delimiter(header_line: str) -> str:
    """Pick the delimiter a header line most likely uses."""
    counts = {candidate: header_line.count(candidate) for candidate in (",", ";", "\t", "|")}
    best = max(counts, key=lambda key: counts[key])
    return best if counts[best] > 0 else ","


def looks_like_formula_injection(value: str) -> bool:
    """True when a cell would execute as a formula if opened in a spreadsheet.

    Exported reports are opened in Excel by the people who use them, so a cell
    beginning with one of these characters is neutralized on export rather than
    passed through.
    """
    return value.lstrip().startswith(("=", "+", "-@", "@", "\t=", "\r="))
