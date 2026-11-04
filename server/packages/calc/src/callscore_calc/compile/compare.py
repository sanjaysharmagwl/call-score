"""Two-way compare: join a target and a base result, with deltas typed like any expression.

For every compared value ``m`` the output has ``m`` (target), ``m__base``, and for numbers
``m__delta = m - m__base`` and ``m__pct = float(m__delta) / float(m__base) * 100`` (null when the
base is zero or missing).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import polars as pl

from callscore_calc.dtypes import NUMERIC, LType, NumericConfig
from callscore_calc.errors import SpecError
from callscore_calc.spec.expr import Binary, Cast, ColRef, Lit

from .exprs import materialize
from .validate import Typed, check

BASE, DELTA, PCT = "__base", "__delta", "__pct"


@dataclass(frozen=True)
class ComparePlan:
    keys: tuple[str, ...]
    values: tuple[str, ...]
    derived: tuple[tuple[str, Typed, LType], ...]
    output: dict[str, LType]


def plan_compare(
    keys: tuple[str, ...],
    target: Mapping[str, LType],
    base: Mapping[str, LType],
    values: tuple[str, ...],
    cfg: NumericConfig,
) -> ComparePlan:
    output: dict[str, LType] = {}
    for key in keys:
        if key not in base or target[key].kind is not base[key].kind:
            raise SpecError(f"{key} differs between the two sides", code="compare_mismatch")
        output[key] = target[key]
    derived = []
    for name in values:
        if name not in base:
            raise SpecError(f"{name} only exists on one side", code="compare_mismatch")
        t, b = target[name], base[name]
        output[name] = t
        output[name + BASE] = b
        if t.kind in NUMERIC and b.kind in NUMERIC:
            env = {name: t, name + BASE: b}
            delta = check(
                Binary(op="sub", left=ColRef(name=name), right=ColRef(name=name + BASE)), env, cfg
            )
            env[name + DELTA] = delta.ltype.rigid()
            pct_node = Binary(
                op="mul",
                left=Binary(
                    op="div",
                    left=Cast(arg=ColRef(name=name + DELTA), to="float"),
                    right=Cast(arg=ColRef(name=name + BASE), to="float"),
                ),
                right=Lit(type="int", value=100),
            )
            pct = check(pct_node, env, cfg)
            derived.append((name + DELTA, delta, delta.ltype.rigid()))
            derived.append((name + PCT, pct, pct.ltype.rigid()))
            output[name + DELTA] = delta.ltype.rigid()
            output[name + PCT] = pct.ltype.rigid()
        elif t.kind is not b.kind:
            raise SpecError(f"{name} has different types on the two sides", code="compare_mismatch")
    return ComparePlan(keys, values, tuple(derived), output)


def compare_frame(target: pl.LazyFrame, base: pl.LazyFrame, plan: ComparePlan) -> pl.LazyFrame:
    keys = list(plan.keys)
    right = base.select([*keys, *(pl.col(v).alias(v + BASE) for v in plan.values)])
    left = target.select([*keys, *plan.values])
    if keys:
        joined = left.join(right, on=keys, how="full", coalesce=True, nulls_equal=True)
    else:
        joined = left.join(right, how="cross")  # both sides are one grand-total row
    for name, typed, ltype in plan.derived:
        joined = joined.with_columns(materialize(typed, ltype).alias(name))
    return joined.select(list(plan.output))
