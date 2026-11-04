"""Typed expression tree -> Polars expression.

Casts follow the typing rules in :mod:`callscore_calc.dtypes`. Decimal operands are widened *before*
an operation, because Polars rounds a decimal product or quotient to the larger input scale.
Division by zero and non-finite float results (``sqrt(-1)``, ``log(0)``) become null.
"""

from __future__ import annotations

import datetime as dt
import functools
import operator
from collections.abc import Callable
from decimal import Decimal

import polars as pl

from callscore_calc.dtypes import FLOAT, Kind, LType, decimal, to_polars
from callscore_calc.errors import SpecError
from callscore_calc.spec.expr import (
    Binary,
    Cast,
    ColRef,
    Compare,
    Func,
    IfElse,
    InList,
    IsNull,
    Lit,
    Logic,
    Unary,
)

from .validate import Typed, total_column

_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1

_Op = Callable[[pl.Expr, pl.Expr], pl.Expr]
_CMP: dict[str, _Op] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}
_ARITH: dict[str, _Op] = {"add": operator.add, "sub": operator.sub, "mul": operator.mul}


def dec_dtype(scale: int) -> pl.Decimal:
    return pl.Decimal(38, scale)


def as_type(expr: pl.Expr, source: LType, target: LType) -> pl.Expr:
    """Cast ``expr`` (of logical type ``source``) to ``target``.

    Narrowing a decimal rounds half-to-even.
    """
    if target.kind is Kind.NULL:
        return expr
    if source.kind is Kind.NULL:
        return expr.cast(to_polars(target))
    if target.kind is Kind.DECIMAL:
        scale = target.scale or 0
        if source.kind is Kind.DECIMAL and (source.scale or 0) > scale:
            return expr.round(scale).cast(dec_dtype(scale))
        return expr.cast(dec_dtype(scale))
    if target.kind in (Kind.INT, Kind.FLOAT):
        return expr.cast(to_polars(target))
    if source.kind is target.kind:
        return expr  # strings may be categorical; they compare and sort by value either way
    return expr.cast(to_polars(target))


def finite(expr: pl.Expr) -> pl.Expr:
    """Null out NaN and infinities."""
    return pl.when(expr.is_finite()).then(expr)


def nonzero(expr: pl.Expr) -> pl.Expr:
    return pl.when(expr != 0).then(expr)


def lit_value(node: Lit) -> object:
    """The Python value of a literal (decimals exact, dates parsed)."""
    value = node.value
    if node.type == "num":
        return Decimal(str(value))
    if node.type == "date":
        return dt.date.fromisoformat(str(value))
    if node.type == "datetime":
        return dt.datetime.fromisoformat(str(value))
    return value


def compile_expr(t: Typed) -> pl.Expr:
    node = t.node
    if isinstance(node, ColRef):
        return pl.col(node.name)
    if isinstance(node, Lit):
        return _lit(node, t.ltype)
    if isinstance(node, Binary):
        return _binary(t, node)
    if isinstance(node, Unary):
        arg = compile_expr(t.args[0])
        return -arg if node.op == "neg" else ~arg
    if isinstance(node, Compare):
        left, right = t.args
        op = t.operand
        assert op is not None
        if op.kind is Kind.NULL:
            return pl.lit(None, dtype=pl.Boolean)
        a = as_type(compile_expr(left), left.ltype, op)
        b = as_type(compile_expr(right), right.ltype, op)
        return _CMP[node.op](a, b)
    if isinstance(node, Logic):
        parts = [_bool(a) for a in t.args]
        return functools.reduce(operator.and_ if node.op == "and" else operator.or_, parts)
    if isinstance(node, InList):
        op = t.operand
        assert op is not None
        subject, *values = t.args
        if op.kind is Kind.NULL:
            return pl.lit(None, dtype=pl.Boolean)
        items = []
        for v in values:
            assert isinstance(v.node, Lit)
            items.append(_convert_value(lit_value(v.node), v.ltype, op))
        series = pl.Series(items, dtype=to_polars(op))
        test = as_type(compile_expr(subject), subject.ltype, op).is_in(series.implode())
        return ~test if node.negate else test
    if isinstance(node, IsNull):
        arg = compile_expr(t.args[0])
        return arg.is_not_null() if node.negate else arg.is_null()
    if isinstance(node, IfElse):
        cond, then, *rest = t.args
        target = t.ltype
        branch = pl.when(_bool(cond)).then(as_type(compile_expr(then), then.ltype, target))
        if rest:
            return branch.otherwise(as_type(compile_expr(rest[0]), rest[0].ltype, target))
        return branch
    if isinstance(node, Func):
        return _func(t, node)
    assert isinstance(node, Cast)
    return _cast(t, node)


def materialize(t: Typed, ltype: LType | None = None) -> pl.Expr:
    """Compile and cast to the canonical dtype of the (rigid) result type."""
    target = (ltype or t.ltype).rigid()
    return as_type(compile_expr(t), t.ltype, target)


def _bool(t: Typed) -> pl.Expr:
    expr = compile_expr(t)
    return expr.cast(pl.Boolean) if t.ltype.kind is Kind.NULL else expr


def _lit(node: Lit, ltype: LType) -> pl.Expr:
    value = lit_value(node)
    if node.type == "null":
        return pl.lit(None)
    if node.type == "int":
        assert isinstance(value, int)
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise SpecError(
                f"integer literal {value} does not fit in 64 bits", code="invalid_value"
            )
        return pl.lit(value, dtype=pl.Int64)
    if node.type == "num":
        return pl.lit(value, dtype=dec_dtype(ltype.scale or 0))
    return pl.lit(value, dtype=to_polars(ltype.rigid()))


def _convert_value(value: object, source: LType, target: LType) -> object:
    """Convert a literal's Python value to the Python value of ``target`` (for ``in`` lists)."""
    if value is None or source.kind is target.kind:
        return value
    if target.kind is Kind.FLOAT and isinstance(value, int | Decimal):
        return float(value)
    if target.kind is Kind.DECIMAL and isinstance(value, int | float):
        return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    return value


def _binary(t: Typed, node: Binary) -> pl.Expr:
    left, right = t.args
    result = t.ltype
    if result.kind is Kind.NULL:
        return pl.lit(None)
    a, b = compile_expr(left), compile_expr(right)
    op = node.op
    if result.kind is Kind.DECIMAL:
        rs = result.scale or 0
        if op in ("add", "sub"):
            x, y = as_type(a, left.ltype, result), as_type(b, right.ltype, result)
            return (x + y) if op == "add" else (x - y)
        # Widen the left side to the result scale so Polars keeps every digit (it rounds a
        # decimal product or quotient to the larger input scale).
        x = as_type(a, left.ltype, decimal(rs))
        rscale = (right.ltype.scale or 0) if right.ltype.kind is Kind.DECIMAL else 0
        y = as_type(b, right.ltype, decimal(rscale))
        out = x * y if op == "mul" else x / nonzero(y)
        return out.cast(dec_dtype(rs))
    if result.kind is Kind.INT:
        x, y = as_type(a, left.ltype, result), as_type(b, right.ltype, result)
        return _ARITH[op](x, y)
    x, y = as_type(a, left.ltype, FLOAT), as_type(b, right.ltype, FLOAT)
    if op == "div":
        return finite(x / nonzero(y))
    if op == "pow":
        return finite(x.pow(y))
    return _ARITH[op](x, y)


def _func(t: Typed, node: Func) -> pl.Expr:
    if t.impl is not None:
        return _plugin_func(t)
    if node.name == "total":
        measure = node.args[0]
        assert isinstance(measure, ColRef)
        return pl.col(total_column(measure.name))
    name = node.name
    args = t.args
    first = args[0]
    x = compile_expr(first)
    kind = first.ltype.kind
    if kind is Kind.NULL and name not in ("min", "max", "coalesce"):
        return pl.lit(None)
    if name == "abs":
        return x.abs()
    if name == "round":
        places = int(args[1].node.value) if len(args) == 2 else 0  # type: ignore[union-attr,arg-type]
        if kind is Kind.FLOAT:
            return x.round(places)
        if kind is Kind.DECIMAL:
            return x.round(places).cast(dec_dtype(t.ltype.scale or 0))
        return x
    if name in ("floor", "ceil"):
        if kind is Kind.INT:
            return x
        out = x.floor() if name == "floor" else x.ceil()
        return out.cast(dec_dtype(0)) if kind is Kind.DECIMAL else out
    if name in ("sqrt", "log", "exp"):
        f = as_type(x, first.ltype, FLOAT)
        return finite(f.sqrt() if name == "sqrt" else f.log() if name == "log" else f.exp())
    if name in ("min", "max", "coalesce"):
        target = t.operand
        assert target is not None
        parts = [as_type(compile_expr(a), a.ltype, target) for a in args]
        if name == "coalesce":
            return pl.coalesce(parts)
        return pl.min_horizontal(parts) if name == "min" else pl.max_horizontal(parts)
    x = x.cast(pl.String)  # string functions need String, not Categorical
    if name == "lower":
        return x.str.to_lowercase()
    if name == "upper":
        return x.str.to_uppercase()
    pattern = compile_expr(args[1]).cast(pl.String)
    if name == "contains":
        return x.str.contains(pattern, literal=True)
    if name == "starts_with":
        return x.str.starts_with(pattern)
    return x.str.ends_with(pattern)


def _plugin_func(t: Typed) -> pl.Expr:
    """A plugin function: arguments at their rigid types, result cast to the declared type."""
    args = [materialize(a) for a in t.args]
    out: pl.Expr = t.impl.polars(*args)
    out = out.cast(to_polars(t.ltype))
    if t.ltype.kind is Kind.FLOAT:
        out = finite(out)
    if t.impl.nulls == "propagate" and args:
        out = pl.when(pl.all_horizontal([a.is_not_null() for a in args])).then(out)
    return out


def _cast(t: Typed, node: Cast) -> pl.Expr:
    arg = t.args[0]
    x = compile_expr(arg)
    source = arg.ltype.kind
    if source is Kind.NULL:
        return pl.lit(None).cast(to_polars(t.ltype))
    if node.to == "int":
        return x.cast(pl.Int64, strict=False)
    if node.to == "float":
        return finite(x.cast(pl.Float64, strict=False))
    if node.to == "decimal":
        if source is Kind.DECIMAL:
            return as_type(x, arg.ltype, t.ltype)
        return x.cast(dec_dtype(node.scale or 0), strict=False)
    if node.to == "str":
        return x.cast(pl.String)
    if source is Kind.DATETIME:
        return x.dt.date()
    if source is Kind.STR:
        return x.str.to_date("%Y-%m-%d", strict=False)
    return x
