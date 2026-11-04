"""Apply a :class:`LogicalMutations` plan to a Polars LazyFrame.

Only edited columns are rewritten; every other column keeps sharing the base data's buffers, so
a scenario costs memory in proportion to what it changes, not to the dataset.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import polars as pl

from callscore_calc.ext import (
    Kind,
    LType,
    compile_expr,
    dec_dtype,
    decimal_places,
    materialize,
    to_polars,
)

from .planner import LogicalMutations, OverrideBatch, ShockOp


def apply_mutations(lf: pl.LazyFrame, plan: LogicalMutations) -> pl.LazyFrame:
    physical = dict(lf.collect_schema()) if plan.ops else {}
    for op in plan.ops:
        if isinstance(op, OverrideBatch):
            lf = _apply_overrides(lf, op, plan, physical)
        else:
            lf = lf.with_columns(_shock(op).alias(op.column))
    for f in plan.formulas:
        lf = lf.with_columns(materialize(f.typed, f.ltype).alias(f.name))
    return lf


def edit_keys_frame(plan: LogicalMutations) -> pl.DataFrame:
    """The distinct keys the overrides touch, typed like the dataset's key columns."""
    keys = plan.key_columns
    data = {k: [key[i] for key in plan.edit_keys] for i, k in enumerate(keys)}
    return pl.DataFrame(data, schema={k: to_polars(plan.base_env[k]) for k in keys})


def _apply_overrides(
    lf: pl.LazyFrame,
    batch: OverrideBatch,
    plan: LogicalMutations,
    physical: Mapping[str, pl.DataType],
) -> pl.LazyFrame:
    keys = plan.key_columns
    key_types = [to_polars(plan.base_env[k]) for k in keys]

    def dtype_of(column: str) -> pl.DataType:
        # A categorical column stays categorical; everything else uses the canonical dtype.
        actual = physical.get(column)
        return actual if isinstance(actual, pl.Categorical) else to_polars(plan.base_env[column])

    if len(keys) == 1:
        # Hash lookup per edited column; no join, and untouched rows keep their values.
        key = keys[0]
        exprs = []
        for column, edits in batch.edits.items():
            dtype = dtype_of(column)
            old = pl.Series([k[0] for k in edits], dtype=key_types[0])
            new = pl.Series(list(edits.values()), dtype=dtype)
            exprs.append(
                pl.col(key)
                .replace_strict(old, new, default=pl.col(column), return_dtype=dtype)
                .alias(column)
            )
        return lf.with_columns(exprs)
    # Composite keys: one wide row per edited key, joined on the keys in the base row order.
    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    for column, edits in batch.edits.items():
        for key_value, value in edits.items():
            rows.setdefault(key_value, {})[column] = value
    edit_columns = list(batch.edits)
    data: dict[str, list[Any]] = {k: [] for k in keys}
    schema: dict[str, pl.DataType] = dict(zip(keys, key_types, strict=True))
    for i, column in enumerate(edit_columns):
        data[f"__v{i}"] = []
        data[f"__s{i}"] = []
        schema[f"__v{i}"] = dtype_of(column)
        schema[f"__s{i}"] = pl.Boolean()
    for key_value, values in rows.items():
        for k, v in zip(keys, key_value, strict=True):
            data[k].append(v)
        for i, column in enumerate(edit_columns):
            present = column in values
            data[f"__v{i}"].append(values.get(column))
            data[f"__s{i}"].append(present)
    edits_frame = pl.DataFrame(data, schema=schema).lazy()
    joined = lf.join(edits_frame, on=list(keys), how="left", maintain_order="left")
    updates = [
        pl.when(pl.col(f"__s{i}").fill_null(False))
        .then(pl.col(f"__v{i}"))
        .otherwise(pl.col(column))
        .alias(column)
        for i, column in enumerate(edit_columns)
    ]
    helpers = [f"__{p}{i}" for i in range(len(edit_columns)) for p in ("v", "s")]
    return joined.with_columns(updates).drop(helpers)


def _shock(op: ShockOp) -> pl.Expr:
    new = shocked_value(pl.col(op.column), op.ltype, op.op, op.amount, op.round)
    if op.where is None:
        return new
    return pl.when(compile_expr(op.where)).then(new).otherwise(pl.col(op.column))


def shocked_value(x: pl.Expr, ltype: LType, op: str, amount: Decimal, round_: bool) -> pl.Expr:
    if ltype.kind is Kind.FLOAT:
        return x + float(amount) if op == "add" else x * float(amount)
    places = decimal_places(amount)
    integral = amount == amount.to_integral_value()
    if ltype.kind is Kind.INT and integral:
        whole = int(amount)
        return x + whole if op == "add" else x * whole
    # Exact decimal arithmetic, then round half-even to the column's scale (0 for integers).
    scale = (ltype.scale or 0) if ltype.kind is Kind.DECIMAL else 0
    y = pl.lit(amount, dtype=dec_dtype(places))
    if op == "add":
        exact = x.cast(dec_dtype(max(scale, places))) + y
    else:
        exact = x.cast(dec_dtype(scale + places)) * y
    rounded = exact.round(scale)
    if ltype.kind is Kind.INT:
        assert round_
        return rounded.cast(pl.Int64)
    return rounded.cast(dec_dtype(scale))
