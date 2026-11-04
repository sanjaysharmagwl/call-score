"""Build Polars plans for a :class:`LogicalQuery`, and finish them eagerly.

Row views stay lazy end to end (filter, sort and slice are pushed into one plan, so a page of a
large table never materializes the rest). Aggregated views collect the (small) aggregate once;
pivot, sort and paging then run eagerly on it, and the engine caches it for later pages.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import polars as pl

from callscore_calc.config import Limits
from callscore_calc.dtypes import Kind, LType, quantize, to_polars
from callscore_calc.errors import LimitExceeded, SpecError

from .exprs import compile_expr, materialize
from .logical import LEVEL, HiddenAgg, LogicalQuery, SortSpec
from .validate import total_column

TOTAL = "__total"
PIDX = "__pidx"


def rows_frame(lf: pl.LazyFrame, plan: LogicalQuery) -> pl.LazyFrame:
    """The filtered rows with derived columns: the input of every aggregate."""
    if plan.filter is not None:
        lf = lf.filter(compile_expr(plan.filter))
    for d in plan.derives:
        value = materialize(d.expr, d.ltype)
        if d.where is not None:
            other = None if d.otherwise is None else materialize(d.otherwise, d.ltype)
            branch = pl.when(compile_expr(d.where)).then(value)
            value = branch.otherwise(other) if other is not None else branch
            value = value.cast(to_polars(d.ltype))
        lf = lf.with_columns(value.alias(d.name))
    return lf


def leaf_frame(
    lf: pl.LazyFrame, plan: LogicalQuery, limits: Limits, *, row_limit: int | None = None
) -> pl.LazyFrame:
    """Row view: one lazy plan that filters, sorts and slices, with the total row count."""
    rows = rows_frame(lf, plan).with_columns(pl.len().alias(TOTAL))
    keys = [*plan.sort, *(SortSpec(k, False, True) for k in plan.tiebreak)]
    rows = _sort_lazy(rows, keys)
    if plan.page is not None:
        rows = rows.slice(plan.page.offset, plan.page.limit)
    else:
        rows = rows.head(row_limit if row_limit is not None else limits.max_unpaged_rows + 1)
    return rows.select([*plan.select, TOTAL])


def _sort_lazy(lf: pl.LazyFrame, keys: Sequence[SortSpec]) -> pl.LazyFrame:
    if not keys:
        return lf
    return lf.sort(
        [k.by for k in keys],
        descending=[k.desc for k in keys],
        nulls_last=[k.nulls_last for k in keys],
        maintain_order=True,
    )


def count_frame(lf: pl.LazyFrame, plan: LogicalQuery) -> pl.LazyFrame:
    return rows_frame(lf, plan).select(pl.len().alias(TOTAL))


def agg_frame(lf: pl.LazyFrame, plan: LogicalQuery, *, deterministic: bool) -> pl.LazyFrame:
    """Aggregate at the group level (and every rollup level), including pivot columns."""
    rows = rows_frame(lf, plan)
    extra = plan.pivot.on if plan.pivot is not None else ()
    return _levels(rows, plan, extra, deterministic=deterministic, having=True)


def totals_frame(lf: pl.LazyFrame, plan: LogicalQuery, *, deterministic: bool) -> pl.LazyFrame:
    """Pivot totals: the same measures without splitting by the pivot columns."""
    return _levels(rows_frame(lf, plan), plan, (), deterministic=deterministic, having=False)


def domain_frame(lf: pl.LazyFrame, plan: LogicalQuery) -> pl.LazyFrame:
    assert plan.pivot is not None
    on = list(plan.pivot.on)
    return rows_frame(lf, plan).select(on).unique().sort(on, nulls_last=True)


def _levels(
    rows: pl.LazyFrame,
    plan: LogicalQuery,
    extra: Sequence[str],
    *,
    deterministic: bool,
    having: bool,
) -> pl.LazyFrame:
    groups = list(plan.group_by)
    depths = range(len(groups), -1, -1) if plan.rollup else [len(groups)]
    # Compute every aggregate's (masked) input once; each level then aggregates plain columns,
    # which keeps Polars on its fast group-by path and shares the work across rollup levels.
    rows = rows.with_columns(_inputs(plan))
    grand = _grand_totals(rows, plan, deterministic) if plan.totals else None
    frames = []
    for depth in depths:
        frame = _aggregate(rows, [*groups[:depth], *extra], plan, deterministic, grand)
        if plan.rollup:
            frame = frame.with_columns(pl.lit(depth, dtype=pl.Int64).alias(LEVEL))
        frames.append(frame)
    out = frames[0] if len(frames) == 1 else pl.concat(frames, how="diagonal_relaxed")
    if having and plan.having is not None:
        out = out.filter(compile_expr(plan.having))
    columns = [*groups, *([LEVEL] if plan.rollup else []), *extra, *plan.value_names]
    return out.select(columns)


def _input_name(h: HiddenAgg) -> str:
    return f"{h.name}_in"


def _inputs(plan: LogicalQuery) -> list[pl.Expr]:
    """One column per aggregate: its argument, null wherever the measure's ``where`` fails."""
    exprs = []
    for m in plan.measures:
        for h in m.hidden:
            if h.fn == "count_rows":
                if h.where is None:
                    continue
                value = compile_expr(h.where).fill_null(False).cast(pl.Int64)
            else:
                assert h.arg is not None
                value = materialize(h.arg)
                if h.where is not None:
                    value = pl.when(compile_expr(h.where)).then(value)
            exprs.append(value.alias(_input_name(h)))
    return exprs


def _grand_totals(rows: pl.LazyFrame, plan: LogicalQuery, deterministic: bool) -> pl.LazyFrame:
    """One row with what ``total(m)`` reads: each measure it names, over all the rows."""
    needed = [m for m in plan.measures if m.name in plan.totals]
    hidden = [_hidden(h, deterministic).alias(h.name) for m in needed for h in m.hidden]
    return rows.select(hidden).select(
        [materialize(m.final, m.ltype).alias(total_column(m.name)) for m in needed]
    )


def _aggregate(
    rows: pl.LazyFrame,
    keys: list[str],
    plan: LogicalQuery,
    deterministic: bool,
    grand: pl.LazyFrame | None,
) -> pl.LazyFrame:
    """One level of groups; with ``grand``, its columns stay on every row for post and having."""
    hidden = [_hidden(h, deterministic).alias(h.name) for m in plan.measures for h in m.hidden]
    grouped = rows.group_by(keys).agg(hidden) if keys else rows.select(hidden)
    grouped = grouped.with_columns(
        [materialize(m.final, m.ltype).alias(m.name) for m in plan.measures]
    )
    if grand is not None:
        grouped = grouped.join(grand, how="cross")
    for p in plan.post:
        grouped = grouped.with_columns(materialize(p.expr, p.ltype).alias(p.name))
    return grouped.drop([h.name for m in plan.measures for h in m.hidden])


def _hidden(h: HiddenAgg, deterministic: bool) -> pl.Expr:
    if h.fn == "count_rows":
        if h.where is None:
            return pl.len().cast(pl.Int64)
        return pl.col(_input_name(h)).sum().cast(pl.Int64)
    x = pl.col(_input_name(h))
    if h.fn == "plugin":
        # Plugin aggregates see only the present values; a group without any is null.
        value = h.impl.polars(x.drop_nulls()).cast(to_polars(h.ltype))
        present = x.count() > 0
        if h.ltype.kind is Kind.FLOAT:
            present = present & value.is_finite()
        return pl.when(present).then(value)
    if h.fn == "sum":
        # Summing sorted values makes float totals independent of how threads split the rows.
        total = x.sort().sum() if deterministic and h.ltype.kind is Kind.FLOAT else x.sum()
        return pl.when(x.count() > 0).then(total).cast(to_polars(h.ltype))
    if h.fn == "count":
        return x.count().cast(pl.Int64)
    if h.fn == "count_distinct":
        return x.drop_nulls().n_unique().cast(pl.Int64)
    return x.min() if h.fn == "min" else x.max()


# --- Eager finishing --------------------------------------------------------------------------


@dataclass
class Finished:
    frame: pl.DataFrame
    total_rows: int
    pivot_fields: list[str] | None
    columns: dict[str, LType]


def finish_aggregate(
    agg: pl.DataFrame,
    plan: LogicalQuery,
    limits: Limits,
    *,
    domain: pl.DataFrame | None = None,
    totals: pl.DataFrame | None = None,
) -> Finished:
    if agg.height > limits.max_groups:
        raise LimitExceeded(f"the result has more than {limits.max_groups} groups")
    columns = dict(plan.output)
    fields = None
    frame = agg
    if plan.pivot is not None:
        frame, fields, pivot_types = _pivot(agg, plan, limits, domain=domain, totals=totals)
        index_cols = [c for c in plan.output if c in plan.group_by or c == LEVEL]
        totals_cols = [c for c in plan.output if c not in index_cols]
        columns = {c: plan.output[c] for c in index_cols}
        columns.update(pivot_types)
        columns.update({c: plan.output[c] for c in totals_cols})
        for spec in plan.sort:
            if spec.by not in columns:
                raise SpecError(
                    f"cannot sort by {spec.by}: not a column of this view",
                    code="unknown_column",
                    path="/query/sort",
                )
    frame = sort_frame(frame, plan)
    return Finished(frame, frame.height, fields, columns)


def sort_frame(frame: pl.DataFrame, plan: LogicalQuery) -> pl.DataFrame:
    if frame.height == 0:
        return frame
    if plan.rollup:
        return _hierarchical_sort(frame, plan)
    keys = [*plan.sort, *(SortSpec(k, False, True) for k in plan.tiebreak)]
    if not keys:
        return frame
    return frame.sort(
        [k.by for k in keys],
        descending=[k.desc for k in keys],
        nulls_last=[k.nulls_last for k in keys],
        maintain_order=True,
    )


def _hierarchical_sort(frame: pl.DataFrame, plan: LogicalQuery) -> pl.DataFrame:
    """Subtotal rows directly after their details; the grand total last."""
    user = {s.by: s for s in plan.sort}
    by: list[str] = []
    desc: list[bool] = []
    nulls: list[bool] = []
    helpers = []
    for i, g in enumerate(plan.group_by, start=1):
        flag = f"__sub{i}"
        helpers.append((pl.col(LEVEL) < i).alias(flag))
        spec = user.get(g, SortSpec(g, False, True))
        by += [flag, g]
        desc += [False, spec.desc]
        nulls += [True, spec.nulls_last]
    out = frame.with_columns(helpers).sort(
        by, descending=desc, nulls_last=nulls, maintain_order=True
    )
    return out.drop([f"__sub{i}" for i in range(1, len(plan.group_by) + 1)])


def pivot_label(value: Any, ltype: LType, null_label: str) -> str:
    if value is None:
        return null_label
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(quantize(value, ltype.scale or 0), "f")
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    return str(value)


def _pivot(
    agg: pl.DataFrame,
    plan: LogicalQuery,
    limits: Limits,
    *,
    domain: pl.DataFrame | None,
    totals: pl.DataFrame | None,
) -> tuple[pl.DataFrame, list[str], dict[str, LType]]:
    spec = plan.pivot
    assert spec is not None
    on = list(spec.on)
    combos: list[tuple[Any, ...]]
    if spec.domain is not None:
        combos = [tuple(c) for c in spec.domain]
    else:
        assert domain is not None
        combos = [tuple(row) for row in domain.select(on).iter_rows()]
    n_columns = len(combos) * len(spec.values)
    if n_columns > limits.max_pivot_columns:
        raise LimitExceeded(
            f"the pivot would create {n_columns} columns (limit {limits.max_pivot_columns})",
            path="/query/pivot",
        )
    labels = []
    for combo in combos:
        parts = [
            pivot_label(v, t, spec.null_label) for v, t in zip(combo, spec.on_types, strict=True)
        ]
        for part in parts:
            if spec.separator in part:
                raise SpecError(
                    f"pivot value {part!r} contains the separator {spec.separator!r}",
                    code="pivot_separator_conflict",
                    path="/query/pivot/separator",
                )
        labels.append(spec.separator.join(parts))
    index = [*plan.group_by, *([LEVEL] if plan.rollup else [])]
    out_index = index or ["__one"]
    domain_df = pl.DataFrame(
        {**{c: [combo[i] for combo in combos] for i, c in enumerate(on)}, PIDX: range(len(combos))},
        schema={
            # Match the aggregate's dtypes exactly (a pivot column may be categorical).
            **{c: agg.schema[c] for c in on},
            PIDX: pl.Int64,
        },
    )
    tagged = agg.join(domain_df, on=on, how="inner", nulls_equal=True)
    if not index:
        tagged = tagged.with_columns(pl.lit(0).alias("__one"))
    names: dict[str, str] = {}
    types: dict[str, LType] = {}
    ordered: list[str] = []
    value_types = {name: plan.value_types[name] for name in spec.values}
    for pidx, label in enumerate(labels):
        for value in spec.values:
            final = f"{label}{spec.separator}{value}"
            names[f"{value}\x1f{pidx}"] = final
            types[final] = value_types[value]
            ordered.append(final)
    if tagged.height:
        wide = tagged.pivot(
            on=PIDX,
            index=out_index,
            values=list(spec.values),
            aggregate_function=None,
            separator="\x1f",
        )
        if len(spec.values) == 1:
            wide = wide.rename(
                {c: f"{spec.values[0]}\x1f{c}" for c in wide.columns if c not in out_index}
            )
        wide = wide.rename({c: names[c] for c in wide.columns if c in names})
    elif index:
        wide = pl.DataFrame(schema={c: tagged.schema[c] for c in out_index})
    else:
        # Without group columns there is exactly one (grand total) row, even with no data.
        wide = pl.DataFrame({"__one": [0]})
    missing = [c for c in ordered if c not in wide.columns]
    if missing:
        wide = wide.with_columns([pl.lit(None).cast(to_polars(types[c])).alias(c) for c in missing])
    wide = wide.select([*out_index, *ordered])
    if spec.totals and totals is not None:
        totals_cols = [*index, *spec.values]
        if index:
            wide = wide.join(
                totals.select(totals_cols), on=index, how="full", coalesce=True, nulls_equal=True
            )
        else:
            wide = wide.hstack(totals.select(totals_cols))
    if not index:
        wide = wide.drop("__one")
    return wide, ordered, types
