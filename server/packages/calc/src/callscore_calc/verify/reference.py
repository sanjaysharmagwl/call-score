"""Pure-Python reference evaluator for logical plans.

Row at a time, exact ``Decimal`` arithmetic, no Polars. It executes the same logical plans as the
engine but implements every operation independently, so it states what the engine *should*
compute. Tests compare the two on random data; :func:`callscore_calc.verify.verify` runs the same
comparison on a sample of real data.

Floats are summed exactly (``math.fsum``); the engine's float sums may differ in the last bits,
so float results are compared with a tolerance.
"""

from __future__ import annotations

import datetime as dt
import functools
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal, InvalidOperation
from typing import Any

from callscore_calc.dtypes import Kind, LType, quantize
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

from ..compile.compare import ComparePlan
from ..compile.logical import LEVEL, HiddenAgg, LogicalQuery, MeasurePlan, SortSpec
from ..compile.query import pivot_label
from ..compile.validate import Typed, total_column

Row = dict[str, Any]
_WIDE = Context(prec=400)
_INT_TEXT = re.compile(r"^[+-]?\d+$")


# --- Values -----------------------------------------------------------------------------------


def convert(value: Any, source: LType, target: LType) -> Any:
    """Convert a value between logical types exactly as the typing rules say."""
    if value is None or target.kind is Kind.NULL or source.kind is Kind.NULL:
        return value
    if target.kind is Kind.DECIMAL:
        return quantize(Decimal(value), target.scale or 0)
    if target.kind is Kind.FLOAT:
        return float(value)
    if target.kind is Kind.INT:
        return int(value)
    return value


def lit_value(node: Lit) -> Any:
    value = node.value
    if node.type == "num":
        return Decimal(str(value))
    if node.type == "date":
        return dt.date.fromisoformat(str(value))
    if node.type == "datetime":
        return dt.datetime.fromisoformat(str(value))
    return value


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _float_round(value: float, places: int) -> float:
    # Polars rounds floats on their shortest decimal representation, half to even.
    exact = Decimal(repr(value))
    rounded = exact.quantize(Decimal(1).scaleb(-places), rounding="ROUND_HALF_EVEN", context=_WIDE)
    return float(rounded)


def evaluate(t: Typed, row: Mapping[str, Any]) -> Any:
    node = t.node
    if isinstance(node, ColRef):
        return row[node.name]
    if isinstance(node, Lit):
        return lit_value(node)
    if isinstance(node, Binary):
        return _binary(t, node, row)
    if isinstance(node, Unary):
        value = evaluate(t.args[0], row)
        if value is None:
            return None
        return -value if node.op == "neg" else not value
    if isinstance(node, Compare):
        left, right = t.args
        a, b = evaluate(left, row), evaluate(right, row)
        if a is None or b is None or t.operand is None:
            return None
        a, b = convert(a, left.ltype, t.operand), convert(b, right.ltype, t.operand)
        return {
            "eq": a == b,
            "ne": a != b,
            "lt": a < b,
            "le": a <= b,
            "gt": a > b,
            "ge": a >= b,
        }[node.op]
    if isinstance(node, Logic):
        values = [evaluate(a, row) for a in t.args]
        if node.op == "and":
            if any(v is False for v in values):
                return False
            return None if any(v is None for v in values) else True
        if any(v is True for v in values):
            return True
        return None if any(v is None for v in values) else False
    if isinstance(node, InList):
        subject, *items = t.args
        value = evaluate(subject, row)
        if value is None or t.operand is None:
            return None
        value = convert(value, subject.ltype, t.operand)
        options = {convert(evaluate(i, row), i.ltype, t.operand) for i in items}
        found = value in options
        return not found if node.negate else found
    if isinstance(node, IsNull):
        missing = evaluate(t.args[0], row) is None
        return not missing if node.negate else missing
    if isinstance(node, IfElse):
        cond, then, *rest = t.args
        if evaluate(cond, row) is True:
            return convert(evaluate(then, row), then.ltype, t.ltype)
        if rest:
            return convert(evaluate(rest[0], row), rest[0].ltype, t.ltype)
        return None
    if isinstance(node, Func):
        return _func(t, node, row)
    assert isinstance(node, Cast)
    return _cast(t, node, row)


def _binary(t: Typed, node: Binary, row: Mapping[str, Any]) -> Any:
    left, right = t.args
    a, b = evaluate(left, row), evaluate(right, row)
    result = t.ltype
    if a is None or b is None or result.kind is Kind.NULL:
        return None
    op = node.op
    if result.kind is Kind.DECIMAL:
        scale = result.scale or 0
        x, y = Decimal(a), Decimal(b)
        if op == "add":
            exact = x + y
        elif op == "sub":
            exact = x - y
        elif op == "mul":
            exact = x * y
        else:
            if y == 0:
                return None
            exact = _WIDE.divide(x, y)
        return quantize(exact, scale)
    if result.kind is Kind.INT:
        return {"add": a + b, "sub": a - b, "mul": a * b}[op]
    fa, fb = float(a), float(b)
    if op == "div":
        return None if fb == 0 else _finite(fa / fb)
    if op == "pow":
        try:
            power = fa**fb
        except (OverflowError, ZeroDivisionError):
            return None
        return None if isinstance(power, complex) else _finite(power)
    return _finite({"add": fa + fb, "sub": fa - fb, "mul": fa * fb}[op])


def _func(t: Typed, node: Func, row: Mapping[str, Any]) -> Any:
    if t.impl is not None:
        return _plugin_func(t, row)
    if node.name == "total":
        measure = node.args[0]
        assert isinstance(measure, ColRef)
        return row[total_column(measure.name)]
    name = node.name
    args = t.args
    if name in ("min", "max", "coalesce"):
        assert t.operand is not None
        values = [convert(evaluate(a, row), a.ltype, t.operand) for a in args]
        present = [v for v in values if v is not None]
        if not present:
            return None
        if name == "coalesce":
            return present[0]
        return min(present) if name == "min" else max(present)
    value = evaluate(args[0], row)
    if value is None:
        return None
    kind = args[0].ltype.kind
    if name == "abs":
        return abs(value)
    if name == "round":
        places = int(args[1].node.value) if len(args) == 2 else 0  # type: ignore[union-attr,arg-type]
        if kind is Kind.FLOAT:
            return _float_round(value, places)
        if kind is Kind.DECIMAL:
            return quantize(quantize(value, places), t.ltype.scale or 0)
        return value
    if name in ("floor", "ceil"):
        if kind is Kind.FLOAT:
            return float(math.floor(value) if name == "floor" else math.ceil(value))
        if kind is Kind.DECIMAL:
            mode = ROUND_FLOOR if name == "floor" else ROUND_CEILING
            return value.to_integral_value(rounding=mode)
        return value
    if name in ("sqrt", "log", "exp"):
        x = float(value)
        try:
            if name == "sqrt":
                return None if x < 0 else math.sqrt(x)
            if name == "log":
                return None if x <= 0 else _finite(math.log(x))
            return _finite(math.exp(x))
        except OverflowError:
            return None
    if name == "lower":
        return str(value).lower()
    if name == "upper":
        return str(value).upper()
    other = evaluate(args[1], row)
    if other is None:
        return None
    if name == "contains":
        return other in value
    if name == "starts_with":
        return value.startswith(other)
    return value.endswith(other)


def _plugin_func(t: Typed, row: Mapping[str, Any]) -> Any:
    values = [convert(evaluate(a, row), a.ltype, a.ltype.rigid()) for a in t.args]
    if t.impl.nulls == "propagate" and any(v is None for v in values):
        return None
    return plugin_value(t.impl.reference(*values), t.ltype)


def plugin_value(value: Any, ltype: LType) -> Any:
    """Coerce a plugin's reference result to its declared type, as the Polars cast does."""
    if value is None:
        return None
    if ltype.kind is Kind.FLOAT:
        return _finite(float(value))
    return convert(value, ltype, ltype)


def _cast(t: Typed, node: Cast, row: Mapping[str, Any]) -> Any:
    value = evaluate(t.args[0], row)
    if value is None:
        return None
    source = t.args[0].ltype
    kind = source.kind
    if node.to == "int":
        if kind is Kind.STR:
            return int(value) if _INT_TEXT.match(value) else None
        if kind is Kind.FLOAT:
            if not math.isfinite(value) or abs(value) >= 2**63:
                return None
            return int(value)
        return int(value)
    if node.to == "float":
        if kind is Kind.STR:
            try:
                parsed = float(value)
            except ValueError:
                return None
            return _finite(parsed)
        return float(value)
    if node.to == "decimal":
        scale = node.scale or 0
        if kind is Kind.FLOAT:
            return quantize(Decimal(repr(value)), scale)
        if kind is Kind.STR:
            try:
                return quantize(Decimal(value), scale)
            except InvalidOperation:
                return None
        return quantize(Decimal(value), scale)
    if node.to == "str":
        if kind is Kind.DECIMAL:
            return format(quantize(value, source.scale or 0), "f")
        if kind is Kind.BOOL:
            return "true" if value else "false"
        if kind is Kind.DATE:
            return value.isoformat()
        return str(value)
    if kind is Kind.DATETIME:
        return value.date()
    if kind is Kind.STR:
        try:
            return dt.date.fromisoformat(value) if len(value) == 10 else None
        except ValueError:
            return None
    return value


# --- Queries ----------------------------------------------------------------------------------


@dataclass
class ReferenceResult:
    rows: list[Row]
    total_rows: int
    columns: dict[str, LType]
    pivot_fields: list[str] | None


def rows_stage(rows: Iterable[Mapping[str, Any]], plan: LogicalQuery) -> list[Row]:
    out = []
    for source in rows:
        if plan.filter is not None and evaluate(plan.filter, source) is not True:
            continue
        row = dict(source)
        for d in plan.derives:
            value = convert(evaluate(d.expr, row), d.expr.ltype, d.ltype)
            if d.where is not None and evaluate(d.where, row) is not True:
                other = d.otherwise
                value = (
                    None if other is None else convert(evaluate(other, row), other.ltype, d.ltype)
                )
            row[d.name] = value
        out.append(row)
    return out


def run_query(rows: Iterable[Mapping[str, Any]], plan: LogicalQuery) -> ReferenceResult:
    """Evaluate a query plan; returns every row (sorted, unpaged) plus the page slice rules."""
    filtered = rows_stage(rows, plan)
    if not plan.aggregated:
        ordered = sort_rows(
            filtered, [*plan.sort, *(SortSpec(k, False, True) for k in plan.tiebreak)]
        )
        projected = [{c: r[c] for c in plan.select} for r in ordered]
        return ReferenceResult(projected, len(projected), dict(plan.output), None)
    extra = plan.pivot.on if plan.pivot is not None else ()
    agg = aggregate_levels(filtered, plan, extra, having=True)
    columns = dict(plan.output)
    fields = None
    if plan.pivot is not None:
        totals = aggregate_levels(filtered, plan, (), having=False) if plan.pivot.totals else None
        agg, fields, types = pivot(agg, filtered, plan, totals)
        index = [c for c in plan.output if c in plan.group_by or c == LEVEL]
        columns = {c: plan.output[c] for c in index}
        columns.update(types)
        columns.update({c: plan.output[c] for c in plan.output if c not in index})
    ordered = sort_aggregate(agg, plan)
    return ReferenceResult(ordered, len(ordered), columns, fields)


def aggregate_levels(
    rows: Sequence[Row], plan: LogicalQuery, extra: Sequence[str], *, having: bool
) -> list[Row]:
    groups = list(plan.group_by)
    depths = range(len(groups), -1, -1) if plan.rollup else [len(groups)]
    grand = grand_totals(rows, plan)
    out: list[Row] = []
    for depth in depths:
        keys = [*groups[:depth], *extra]
        buckets: dict[tuple[Any, ...], list[Row]] = {}
        for r in rows:
            buckets.setdefault(tuple(r[k] for k in keys), []).append(r)
        if not keys and not buckets:
            buckets[()] = []
        for key, members in buckets.items():
            result: Row = dict.fromkeys(groups)
            result.update(zip(keys, key, strict=True))
            for m in plan.measures:
                result[m.name] = measure_value(m, members)
            result.update(grand)
            for p in plan.post:
                result[p.name] = convert(evaluate(p.expr, result), p.expr.ltype, p.ltype)
            if plan.rollup:
                result[LEVEL] = depth
            if having and plan.having is not None and evaluate(plan.having, result) is not True:
                continue
            columns = [*groups, *([LEVEL] if plan.rollup else []), *extra, *plan.value_names]
            out.append({c: result[c] for c in columns})
    return out


def measure_value(m: MeasurePlan, rows: Sequence[Row]) -> Any:
    hidden = {h.name: hidden_agg(h, rows) for h in m.hidden}
    return convert(evaluate(m.final, hidden), m.final.ltype, m.ltype)


def grand_totals(rows: Sequence[Row], plan: LogicalQuery) -> Row:
    """What ``total(m)`` reads: each measure it names, over every row the query sees."""
    needed = [m for m in plan.measures if m.name in plan.totals]
    return {total_column(m.name): measure_value(m, rows) for m in needed}


def hidden_agg(h: HiddenAgg, rows: Sequence[Row]) -> Any:
    selected = [r for r in rows if h.where is None or evaluate(h.where, r) is True]
    if h.fn == "count_rows":
        return len(selected)
    assert h.arg is not None
    arg = h.arg
    rigid = arg.ltype.rigid()
    values = [convert(evaluate(arg, r), arg.ltype, rigid) for r in selected]
    present = [v for v in values if v is not None]
    if h.fn == "count":
        return len(present)
    if h.fn == "count_distinct":
        return len(set(present))
    if not present:
        return None
    if h.fn == "plugin":
        return plugin_value(h.impl.reference(present), h.ltype)
    if h.fn == "sum":
        if h.ltype.kind is Kind.FLOAT:
            return math.fsum(present)
        return sum(present[1:], present[0])
    return min(present) if h.fn == "min" else max(present)


def pivot(
    agg: list[Row], filtered: Sequence[Row], plan: LogicalQuery, totals: list[Row] | None
) -> tuple[list[Row], list[str], dict[str, LType]]:
    spec = plan.pivot
    assert spec is not None
    on = list(spec.on)
    if spec.domain is not None:
        combos = [tuple(c) for c in spec.domain]
    else:
        distinct = {tuple(r[c] for c in on) for r in filtered}
        combos = sorted(distinct, key=lambda combo: tuple((v is None, v) for v in combo))
    labels = [
        spec.separator.join(
            pivot_label(v, t, spec.null_label) for v, t in zip(combo, spec.on_types, strict=True)
        )
        for combo in combos
    ]
    position = {combo: i for i, combo in enumerate(combos)}
    index = [*plan.group_by, *([LEVEL] if plan.rollup else [])]
    fields = [f"{label}{spec.separator}{value}" for label in labels for value in spec.values]
    types = {
        f"{label}{spec.separator}{value}": plan.value_types[value]
        for label in labels
        for value in spec.values
    }
    wide: dict[tuple[Any, ...], Row] = {}
    for r in agg:
        combo = tuple(r[c] for c in on)
        if combo not in position:
            continue
        key = tuple(r[c] for c in index)
        target = wide.setdefault(
            key, {**dict(zip(index, key, strict=True)), **dict.fromkeys(fields)}
        )
        for value in spec.values:
            target[f"{labels[position[combo]]}{spec.separator}{value}"] = r[value]
    if not index and not wide and fields:
        # One grand-total row; with no pivot columns at all there is nothing to show.
        wide[()] = dict.fromkeys(fields)
    if totals is not None:
        for t in totals:
            key = tuple(t[c] for c in index)
            target = wide.setdefault(
                key, {**dict(zip(index, key, strict=True)), **dict.fromkeys(fields)}
            )
            for value in spec.values:
                target[value] = t[value]
        for target in wide.values():
            for value in spec.values:
                target.setdefault(value, None)
    return list(wide.values()), fields, types


def sort_aggregate(rows: list[Row], plan: LogicalQuery) -> list[Row]:
    if plan.rollup:
        user = {s.by: s for s in plan.sort}
        keys: list[SortSpec] = []
        for i, g in enumerate(plan.group_by, start=1):
            flag = f"__sub{i}"
            for r in rows:
                r[flag] = r[LEVEL] < i
            spec = user.get(g, SortSpec(g, False, True))
            keys += [SortSpec(flag, False, True), spec]
        ordered = sort_rows(rows, keys)
        for r in ordered:
            for i in range(1, len(plan.group_by) + 1):
                r.pop(f"__sub{i}", None)
        return ordered
    return sort_rows(rows, [*plan.sort, *(SortSpec(k, False, True) for k in plan.tiebreak)])


def sort_rows(rows: Iterable[Row], keys: Sequence[SortSpec]) -> list[Row]:
    def compare(a: Row, b: Row) -> int:
        for key in keys:
            x, y = a[key.by], b[key.by]
            if x is None and y is None:
                continue
            if x is None:
                return 1 if key.nulls_last else -1
            if y is None:
                return -1 if key.nulls_last else 1
            order = int(x > y) - int(x < y)
            if key.desc:
                order = -order
            if order:
                return order
        return 0

    return sorted(rows, key=functools.cmp_to_key(compare))


def compare_rows(target: list[Row], base: list[Row], plan: ComparePlan) -> list[Row]:
    """Full outer join on the plan's keys (nulls match), then the typed delta columns."""
    keys = list(plan.keys)
    joined: dict[tuple[Any, ...], Row] = {}
    for r in target:
        key = tuple(r[k] for k in keys)
        joined[key] = {**dict(zip(keys, key, strict=True)), **{v: r[v] for v in plan.values}}
    for r in base:
        key = tuple(r[k] for k in keys)
        row = joined.setdefault(
            key, {**dict(zip(keys, key, strict=True)), **dict.fromkeys(plan.values)}
        )
        for v in plan.values:
            row[v + "__base"] = r[v]
    out = []
    for row in joined.values():
        for v in plan.values:
            row.setdefault(v + "__base", None)
        for name, typed, ltype in plan.derived:
            row[name] = convert(evaluate(typed, row), typed.ltype, ltype)
        out.append({c: row[c] for c in plan.output})
    return out


# --- Comparing results ------------------------------------------------------------------------

Matcher = Callable[[Any, Any], bool]


def values_match(
    a: Any, b: Any, ltype: LType, *, rel_tol: float = 1e-9, abs_tol: float = 1e-9
) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if ltype.kind is Kind.FLOAT:
        return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=abs_tol)
    if ltype.kind is Kind.DECIMAL:
        return Decimal(a) == Decimal(b)
    return bool(a == b)


def diff_rows(
    actual: Sequence[Mapping[str, Any]],
    expected: Sequence[Mapping[str, Any]],
    columns: Mapping[str, LType],
    *,
    rel_tol: float = 1e-9,
    abs_tol: float = 1e-9,
    limit: int = 20,
) -> list[str]:
    """Human-readable differences between two row lists (in order), at most ``limit``."""
    problems: list[str] = []
    if len(actual) != len(expected):
        problems.append(f"row count differs: engine {len(actual)}, reference {len(expected)}")
    for i, (a, e) in enumerate(zip(actual, expected, strict=False)):
        for name, ltype in columns.items():
            if not values_match(a.get(name), e.get(name), ltype, rel_tol=rel_tol, abs_tol=abs_tol):
                problems.append(
                    f"row {i}, {name}: engine {a.get(name)!r}, reference {e.get(name)!r}"
                )
                if len(problems) >= limit:
                    return problems
    return problems
