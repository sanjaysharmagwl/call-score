"""Logical types and the typing rules shared by the validator, the compiler and the reference.

Every column and expression has a logical type (:class:`LType`). Numeric literals are *flexible*:
``1.05`` is an exact decimal that turns into a float when it meets a float column, so
``price * 1.05`` works whether ``price`` is ``Float64`` or ``Decimal``. Two non-flexible operands
of kinds float and decimal never mix silently; the request has to cast one of them.

Decimal rules (precision is always 38):

* ``a + b``, ``a - b``: scale ``max(sa, sb)``, exact.
* ``a * b``: scale ``min(sa + sb, max(max_scale, sa, sb))``, exact unless capped.
* ``a / b``: scale ``max(division_scale, sa, sb)``; a zero divisor gives null.
* ``int / int`` is an exact (flexible) decimal at ``division_scale``, not a float.
* Rounding, wherever it happens, is half-to-even.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

import polars as pl

from callscore_calc.errors import SpecError

MAX_PRECISION = 38
# Enough digits for any intermediate Decimal the engine can produce (two 38-digit operands).
DECIMAL_CONTEXT = Context(prec=100, rounding=ROUND_HALF_EVEN)

BinaryOp = Literal["add", "sub", "mul", "div", "pow"]


class Kind(str, Enum):
    INT = "int"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOL = "bool"
    STR = "str"
    DATE = "date"
    DATETIME = "datetime"
    NULL = "null"
    OTHER = "other"


NUMERIC = frozenset({Kind.INT, Kind.FLOAT, Kind.DECIMAL})
ORDERABLE = frozenset(
    {Kind.INT, Kind.FLOAT, Kind.DECIMAL, Kind.STR, Kind.DATE, Kind.DATETIME, Kind.BOOL}
)


@dataclass(frozen=True)
class NumericConfig:
    """Engine-wide numeric settings; they are part of every result fingerprint."""

    max_scale: int = 18
    division_scale: int = 10

    def __post_init__(self) -> None:
        if not 0 <= self.division_scale <= self.max_scale <= MAX_PRECISION:
            raise ValueError("need 0 <= division_scale <= max_scale <= 38")


@dataclass(frozen=True)
class LType:
    """A logical type. ``scale`` is set for decimals only; ``flex`` marks literal-derived values."""

    kind: Kind
    scale: int | None = None
    flex: bool = False

    @property
    def numeric(self) -> bool:
        return self.kind in NUMERIC

    def rigid(self) -> LType:
        return LType(self.kind, self.scale) if self.flex else self

    def __str__(self) -> str:
        if self.kind is Kind.DECIMAL:
            return f"decimal({self.scale})"
        return str(self.kind.value)


INT = LType(Kind.INT)
FLOAT = LType(Kind.FLOAT)
BOOL = LType(Kind.BOOL)
STR = LType(Kind.STR)
DATE = LType(Kind.DATE)
DATETIME = LType(Kind.DATETIME)
NULL = LType(Kind.NULL, flex=True)


def decimal(scale: int, *, flex: bool = False) -> LType:
    return LType(Kind.DECIMAL, scale, flex)


# --- Polars mapping ---------------------------------------------------------------------------

_INT_DTYPES = (pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32)


def from_polars(dtype: pl.DataType) -> LType:
    """Map a Polars dtype to a logical type (``OTHER`` if the engine can't compute with it)."""
    if isinstance(dtype, pl.Decimal):
        return decimal(dtype.scale)
    if isinstance(dtype, pl.Datetime):
        return DATETIME if dtype.time_zone is None else LType(Kind.OTHER)
    if dtype in _INT_DTYPES:
        return INT
    if dtype in (pl.Float32, pl.Float64):
        return FLOAT
    if dtype == pl.Boolean:
        return BOOL
    if dtype in (pl.String, pl.Categorical) or isinstance(dtype, pl.Categorical | pl.Enum):
        return STR
    if dtype == pl.Date:
        return DATE
    if dtype == pl.Null:
        return NULL
    return LType(Kind.OTHER)


def to_polars(ltype: LType) -> pl.DataType:
    """The canonical Polars dtype the engine uses for a logical type."""
    kind = ltype.kind
    if kind is Kind.INT:
        return pl.Int64()
    if kind is Kind.FLOAT:
        return pl.Float64()
    if kind is Kind.DECIMAL:
        return pl.Decimal(MAX_PRECISION, ltype.scale or 0)
    if kind is Kind.BOOL:
        return pl.Boolean()
    if kind is Kind.STR:
        return pl.String()
    if kind is Kind.DATE:
        return pl.Date()
    if kind is Kind.DATETIME:
        return pl.Datetime("us")
    if kind is Kind.NULL:
        return pl.Null()
    raise SpecError(f"type {ltype} has no Polars equivalent", code="unsupported_type")


def normalized_dtype(dtype: pl.DataType) -> pl.DataType | None:
    """The dtype a catalog stores a column as, or ``None`` to keep it unchanged.

    Integers widen to Int64, floats to Float64, decimals to precision 38 and naive datetimes to
    microseconds, so arithmetic sees one dtype per logical kind. Categoricals stay categorical
    (grouping on them is about twice as fast; they compare and sort by value like strings), while
    enums become strings because they order by declaration, not by value.
    """
    ltype = from_polars(dtype)
    if ltype.kind in (Kind.OTHER, Kind.NULL) or isinstance(dtype, pl.Categorical):
        return None
    target = to_polars(ltype)
    return None if target == dtype else target


# --- Typing rules -----------------------------------------------------------------------------


def _conflict(a: LType, b: LType, what: str) -> SpecError:
    return SpecError(
        f"cannot combine {a} and {b} in {what}; cast one side explicitly",
        code="type_mismatch",
        detail={"left": str(a), "right": str(b)},
    )


def common_type(a: LType, b: LType, what: str = "an expression") -> LType:
    """The type two values are compared, coalesced or chosen between as (no scale growth)."""
    if a.kind is Kind.NULL:
        return b
    if b.kind is Kind.NULL:
        return a
    if a.numeric and b.numeric:
        return _numeric_common(a, b, what)
    if a.kind is not b.kind:
        raise _conflict(a, b, what)
    return LType(a.kind, a.scale, a.flex and b.flex)


def _numeric_common(a: LType, b: LType, what: str) -> LType:
    kinds = {a.kind, b.kind}
    if kinds == {Kind.INT}:
        return LType(Kind.INT, None, a.flex and b.flex)
    # Integers never conflict with anything, so only float/decimal operands decide flexibility.
    flex = all(t.flex for t in (a, b) if t.kind is not Kind.INT)
    if Kind.FLOAT in kinds and Kind.DECIMAL in kinds:
        # A decimal literal may become a float; a float never silently becomes a decimal.
        dec = a if a.kind is Kind.DECIMAL else b
        if dec.flex:
            return LType(Kind.FLOAT, None, flex)
        raise _conflict(a, b, what)
    if Kind.FLOAT in kinds:
        return LType(Kind.FLOAT, None, flex)
    return decimal(max(_scale(a), _scale(b)), flex=flex)


def _scale(t: LType) -> int:
    return t.scale or 0 if t.kind is Kind.DECIMAL else 0


def arith_result(op: BinaryOp, a: LType, b: LType, cfg: NumericConfig) -> LType:
    """Result type of ``a <op> b``. Operands are cast to ``common_type`` first (see compiler)."""
    for side in (a, b):
        if not (side.numeric or side.kind is Kind.NULL):
            raise SpecError(
                f"arithmetic needs numbers, got {side}", code="type_mismatch", detail={"op": op}
            )
    if a.kind is Kind.NULL and b.kind is Kind.NULL:
        return NULL
    common = common_type(a, b, "arithmetic")
    flex = common.flex
    if op == "pow":
        if common.kind is Kind.DECIMAL and not flex:
            raise SpecError(
                "power is not supported for decimals; cast to float first", code="type_mismatch"
            )
        return LType(Kind.FLOAT, None, flex)
    if common.kind is Kind.FLOAT:
        return common
    if common.kind is Kind.INT:
        if op != "div":
            return common
        # int / int is exact division at the division scale; like a literal, the result may
        # still become a float next to a float.
        return decimal(cfg.division_scale, flex=True)
    sa, sb = _scale(a), _scale(b)
    if op in ("add", "sub"):
        return decimal(max(sa, sb), flex=flex)
    if op == "mul":
        return decimal(min(sa + sb, max(cfg.max_scale, sa, sb)), flex=flex)
    return decimal(max(cfg.division_scale, sa, sb), flex=flex)


# --- Values -----------------------------------------------------------------------------------

Scalar = str | int | float | bool | None


def canonical_decimal(value: Decimal) -> str:
    """Shortest plain (non-exponent) text for a decimal; ``-0`` becomes ``0``."""
    if not value.is_finite():
        raise ValueError(f"not a finite decimal: {value}")
    if value == 0:
        return "0"
    text = format(value.normalize(DECIMAL_CONTEXT), "f")
    return text


def decimal_places(value: Decimal) -> int:
    exponent = value.normalize(DECIMAL_CONTEXT).as_tuple().exponent
    assert isinstance(exponent, int)
    return max(0, -exponent)


def quantize(value: Decimal, scale: int) -> Decimal:
    return value.quantize(
        Decimal(1).scaleb(-scale), rounding=ROUND_HALF_EVEN, context=DECIMAL_CONTEXT
    )


def parse_decimal(value: object) -> Decimal:
    """Parse a JSON number or numeric string into an exact decimal."""
    if isinstance(value, bool):
        raise ValueError("expected a number, got a boolean")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("expected a finite number")
        return Decimal(repr(value))
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, str):
        try:
            out = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"not a number: {value!r}") from None
        if not out.is_finite():
            raise ValueError("expected a finite number")
        return out
    raise ValueError(f"expected a number, got {type(value).__name__}")


def coerce_value(value: object, ltype: LType, *, what: str = "value") -> Any:
    """Convert a JSON scalar into the Python value a column of ``ltype`` holds.

    Strings are accepted for every kind, since grid editors send text. A decimal with more places
    than the column scale is rejected rather than rounded.
    """
    if value is None:
        return None
    kind = ltype.kind
    try:
        if kind is Kind.STR:
            if not isinstance(value, str):
                raise ValueError("expected a string")
            return value
        if kind is Kind.BOOL:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower() == "true"
            raise ValueError("expected true or false")
        if kind is Kind.INT:
            number = parse_decimal(value)
            if number != number.to_integral_value():
                raise ValueError("expected a whole number")
            return int(number)
        if kind is Kind.FLOAT:
            return float(parse_decimal(value))
        if kind is Kind.DECIMAL:
            number = parse_decimal(value)
            scale = ltype.scale or 0
            if decimal_places(number) > scale:
                raise ValueError(f"has more than {scale} decimal places")
            return quantize(number, scale)
        if kind is Kind.DATE:
            if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
                return value
            if isinstance(value, str):
                return dt.date.fromisoformat(value.strip()[:10])
            raise ValueError("expected an ISO date string")
        if kind is Kind.DATETIME:
            if isinstance(value, dt.datetime):
                return value
            if isinstance(value, str):
                parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    raise ValueError("expected a datetime without a time zone")
                return parsed
            raise ValueError("expected an ISO datetime string")
    except ValueError as exc:
        raise SpecError(
            f"{what} {value!r} is not a valid {ltype}: {exc}",
            code="invalid_value",
            detail={"value": _json_repr(value), "type": str(ltype)},
        ) from None
    raise SpecError(f"{what} cannot be set on a column of type {ltype}", code="unsupported_type")


def _json_repr(value: object) -> Scalar:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def json_value(value: object, ltype: LType) -> Scalar:
    """Canonical JSON form of a column value (decimals and dates as strings)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(quantize(value, ltype.scale or 0), "f")
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, str | int | float | bool):
        return value
    return str(value)
