"""Custom column types: exact money and UTC-only timestamps.

``Money``
    Stores a :class:`~decimal.Decimal` as a signed 64-bit integer count of micro-units
    (``10**-6``). Writes accept ``Decimal`` and ``int`` exactly; a ``Decimal`` with more than
    six decimal places is rejected (never silently rounded). ``float`` is accepted for
    interoperability with Polars ``Float64`` frames: it is converted through its shortest
    round-trip ``repr`` and then rounded half-to-even to six places (floats are inexact by
    nature, so rounding is the explicit rule for them). ``bool``, NaN, infinities and values
    outside the int64 range are rejected. Reads return a ``Decimal`` quantized to six places,
    so ``Decimal("123.456789")`` round-trips exactly.

``UTCDateTime``
    Writes require timezone-aware datetimes (naive ones are rejected) and store them as
    naive UTC in SQLite's fixed-width ``YYYY-MM-DD HH:MM:SS.ffffff`` text form, which sorts
    and compares correctly. Reads return aware datetimes in ``UTC``.
"""

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

MONEY_SCALE_DIGITS = 6
MONEY_SCALE = 10**MONEY_SCALE_DIGITS
MONEY_QUANTUM = Decimal(1).scaleb(-MONEY_SCALE_DIGITS)
_INT64_MAX = 2**63 - 1
_INT64_MIN = -(2**63)
_MAX_ADJUSTED = 18  # int64 micro-units cap money at ~9.2e12; reject far larger early


class MoneyError(ValueError):
    """A value cannot be stored exactly as ``Money``."""


def to_micros(value: Decimal | int | float) -> int:
    """Convert a money amount to integer micro-units, applying the rules in the module doc."""
    micros: int
    if isinstance(value, bool):
        raise MoneyError("Money does not accept booleans")
    if isinstance(value, int):
        micros = value * MONEY_SCALE
    elif isinstance(value, float):
        dec = Decimal(repr(value))
        if not dec.is_finite():
            raise MoneyError(f"Money must be finite, got {value!r}")
        micros = int(dec.scaleb(MONEY_SCALE_DIGITS).to_integral_value(rounding=ROUND_HALF_EVEN))
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise MoneyError(f"Money must be finite, got {value!r}")
        if value.adjusted() > _MAX_ADJUSTED:
            raise MoneyError(f"Money value out of range: {value!r}")
        # Pure integer arithmetic: independent of the caller's decimal context, never rounds.
        sign, digits, exponent = value.as_tuple()
        assert isinstance(exponent, int)  # finite decimals have an int exponent
        coefficient = int("".join(map(str, digits)) or "0")
        shift = exponent + MONEY_SCALE_DIGITS
        if shift >= 0:
            micros = coefficient * 10**shift
        else:
            micros, remainder = divmod(coefficient, 10**-shift)
            if remainder:
                raise MoneyError(
                    f"Money supports at most {MONEY_SCALE_DIGITS} decimal places, got {value}"
                )
        if sign:
            micros = -micros
    else:
        raise MoneyError(f"Money expects Decimal, int or float, got {type(value).__name__}")
    if not _INT64_MIN <= micros <= _INT64_MAX:
        raise MoneyError(f"Money value out of range: {value!r}")
    return micros


def from_micros(micros: int) -> Decimal:
    """Convert stored micro-units back to a ``Decimal`` with exactly six decimal places."""
    return Decimal(int(micros)).scaleb(-MONEY_SCALE_DIGITS).quantize(MONEY_QUANTUM)


class Money(TypeDecorator[Decimal]):
    """Exact decimal money stored as int64 micro-units (see module docstring)."""

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> int | None:
        if value is None:
            return None
        return to_micros(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return from_micros(value)

    def process_literal_param(self, value: Any, dialect: Dialect) -> str:
        if value is None:
            return "NULL"
        return str(to_micros(value))


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime stored as naive UTC; naive input is rejected."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UTCDateTime expects a datetime, got {type(value).__name__}")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"Naive datetime rejected (timestamps must be tz-aware): {value}")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):  # pragma: no cover - DateTime impl parses
            raise TypeError(f"Unexpected stored timestamp {value!r}")
        if value.tzinfo is not None:
            return value.astimezone(UTC)
        return value.replace(tzinfo=UTC)
