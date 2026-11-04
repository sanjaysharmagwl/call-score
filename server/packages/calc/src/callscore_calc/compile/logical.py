"""Logical plans: a validated, typed description of what a request computes.

All validation happens here. The Polars executor (:mod:`.query`) and the
pure-Python reference (:mod:`callscore_calc.verify.reference`) both execute these plans, so their
meaning is defined once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from callscore_calc.config import Limits
from callscore_calc.dtypes import (
    BOOL,
    INT,
    NUMERIC,
    ORDERABLE,
    Kind,
    LType,
    NumericConfig,
    coerce_value,
    common_type,
)
from callscore_calc.errors import LimitExceeded, SpecError, join_path
from callscore_calc.spec.expr import Binary, ColRef, Func, IsNull, Logic, Node, columns, walk
from callscore_calc.spec.query import BUILTIN_AGGS, Page, Query

from .validate import Budget, Typed, check, check_predicate

if TYPE_CHECKING:
    from callscore_calc.plugins import AggregateDef, FunctionDef, Registry

ROW_INDEX = "__row"
LEVEL = "__level"

# --- Shared helpers ---------------------------------------------------------------------------


def check_name(name: str, path: str) -> None:
    """Reject names reserved for the engine's internal columns."""
    if name.startswith("__"):
        raise SpecError(
            f"names starting with '__' are reserved: {name}", code="name_conflict", path=path
        )


def materializable(typed: Typed, path: str) -> LType:
    """The type a column computed by ``typed`` gets; always-null expressions have none."""
    ltype = typed.ltype.rigid()
    if ltype.kind is Kind.NULL:
        raise SpecError(
            "this expression is always null; cast it to give the column a type",
            code="type_mismatch",
            path=path,
        )
    return ltype


# --- Queries ----------------------------------------------------------------------------------

HiddenFn = Literal["sum", "count", "min", "max", "count_distinct", "count_rows", "plugin"]


@dataclass(frozen=True)
class DerivePlan:
    name: str
    expr: Typed
    where: Typed | None
    otherwise: Typed | None
    ltype: LType


@dataclass(frozen=True)
class HiddenAgg:
    """One aggregate over rows. ``sum`` of no values is null (SQL semantics)."""

    name: str
    fn: HiddenFn
    arg: Typed | None
    where: Typed | None
    ltype: LType
    impl: Any = None  # the plugin AggregateDef when fn == "plugin"


@dataclass(frozen=True)
class MeasurePlan:
    name: str
    hidden: tuple[HiddenAgg, ...]
    final: Typed  # expression over the hidden aggregate columns
    ltype: LType


@dataclass(frozen=True)
class PostPlan:
    name: str
    expr: Typed
    ltype: LType


@dataclass(frozen=True)
class PivotPlan:
    on: tuple[str, ...]
    on_types: tuple[LType, ...]
    values: tuple[str, ...]
    domain: tuple[tuple[Any, ...], ...] | None
    totals: bool
    separator: str
    null_label: str


@dataclass(frozen=True)
class SortSpec:
    by: str
    desc: bool
    nulls_last: bool


@dataclass
class LogicalQuery:
    aggregated: bool
    filter: Typed | None
    derives: tuple[DerivePlan, ...]
    row_env: dict[str, LType]
    select: tuple[str, ...]
    group_by: tuple[str, ...]
    measures: tuple[MeasurePlan, ...]
    post: tuple[PostPlan, ...]
    having: Typed | None
    rollup: bool
    pivot: PivotPlan | None
    sort: tuple[SortSpec, ...]
    tiebreak: tuple[str, ...]
    page: Page | None
    output: dict[str, LType]
    value_types: dict[str, LType] = field(default_factory=dict)
    totals: tuple[str, ...] = ()  # measures read by total() in post or having

    @property
    def value_names(self) -> tuple[str, ...]:
        return tuple(m.name for m in self.measures) + tuple(p.name for p in self.post)


def plan_query(
    query: Query,
    env: Mapping[str, LType],
    cfg: NumericConfig,
    limits: Limits,
    *,
    key_columns: tuple[str, ...],
    path: str = "/query",
    registry: Registry | None = None,
) -> LogicalQuery:
    """Validate ``query`` against the columns in ``env``; ``registry`` adds plugin functions
    and aggregates."""
    fns = registry.functions if registry is not None else {}
    aggs = registry.aggregates if registry is not None else {}

    def budget() -> Budget:
        return Budget(limits.max_expr_depth, limits.max_expr_nodes)

    row_env = {k: v for k, v in env.items() if k != ROW_INDEX}
    visible = list(row_env)
    flt = None
    if query.filter is not None:
        flt = check_predicate(
            query.filter, row_env, cfg, join_path(path, "filter"), budget(), functions=fns
        )

    derives = []
    for i, d in enumerate(query.derive):
        dpath = join_path(path, "derive", i)
        check_name(d.name, join_path(dpath, "name"))
        if d.name in row_env:
            raise SpecError(
                f"column {d.name} already exists; derived columns need new names",
                code="name_conflict",
                path=join_path(dpath, "name"),
            )
        expr = check(d.expr, row_env, cfg, join_path(dpath, "expr"), budget(), functions=fns)
        where = otherwise = None
        ltype = expr.ltype
        if d.where is not None:
            where = check_predicate(
                d.where, row_env, cfg, join_path(dpath, "where"), budget(), functions=fns
            )
            if d.otherwise is not None:
                otherwise = check(
                    d.otherwise,
                    row_env,
                    cfg,
                    join_path(dpath, "otherwise"),
                    budget(),
                    functions=fns,
                )
                try:
                    ltype = common_type(expr.ltype, otherwise.ltype, "a derive and its otherwise")
                except SpecError as exc:
                    exc.path = dpath
                    raise
        ltype = materializable(Typed(expr.node, ltype), join_path(dpath, "expr"))
        derives.append(DerivePlan(d.name, expr, where, otherwise, ltype))
        row_env[d.name] = ltype
        visible.append(d.name)

    if not query.aggregated:
        select = tuple(visible) if query.select is None else query.select
        for i, name in enumerate(select):
            if name not in row_env:
                raise _unknown(name, join_path(path, "select", i), row_env)
        sort = _plan_sort(query, path, allowed=set(row_env), fixed=None)
        tiebreak = key_columns or (ROW_INDEX,)
        if query.page is not None and query.page.limit > limits.max_page_size:
            raise LimitExceeded(
                f"page limit is above {limits.max_page_size}", path=join_path(path, "page")
            )
        return LogicalQuery(
            aggregated=False,
            filter=flt,
            derives=tuple(derives),
            row_env=row_env,
            select=select,
            group_by=(),
            measures=(),
            post=(),
            having=None,
            rollup=False,
            pivot=None,
            sort=sort,
            tiebreak=tuple(k for k in tiebreak if k not in {s.by for s in sort}),
            page=query.page,
            output={name: row_env[name] for name in select},
        )

    if len(query.group_by) > limits.max_group_by:
        raise LimitExceeded(f"more than {limits.max_group_by} group_by columns")
    if len(query.measures) + len(query.post) > limits.max_measures:
        raise LimitExceeded(f"more than {limits.max_measures} measures")
    if query.page is not None and query.page.limit > limits.max_page_size:
        raise LimitExceeded(
            f"page limit is above {limits.max_page_size}", path=join_path(path, "page")
        )

    output: dict[str, LType] = {}
    for i, name in enumerate(query.group_by):
        gpath = join_path(path, "group_by", i)
        if name not in row_env:
            raise _unknown(name, gpath, row_env)
        if row_env[name].kind is Kind.OTHER:
            raise SpecError(f"cannot group by {name}", code="unsupported_type", path=gpath)
        if name in output:
            raise SpecError(f"{name} is grouped twice", code="name_conflict", path=gpath)
        output[name] = row_env[name]
    if query.rollup:
        output[LEVEL] = INT

    measures = []
    for i, m in enumerate(query.measures):
        mpath = join_path(path, "measures", i)
        check_name(m.name, join_path(mpath, "name"))
        if m.name in output:
            raise SpecError(
                f"{m.name} is already an output column", code="name_conflict", path=mpath
            )
        plan = _plan_measure(m, i, row_env, cfg, mpath, budget, fns, aggs)
        measures.append(plan)
        output[m.name] = plan.ltype

    totals = {m.name: m.ltype for m in measures}
    post = []
    for i, p in enumerate(query.post):
        ppath = join_path(path, "post", i)
        check_name(p.name, join_path(ppath, "name"))
        if p.name in output:
            raise SpecError(
                f"{p.name} is already an output column", code="name_conflict", path=ppath
            )
        typed = check(
            p.expr, output, cfg, join_path(ppath, "expr"), budget(), functions=fns, totals=totals
        )
        ltype = materializable(typed, join_path(ppath, "expr"))
        post.append(PostPlan(p.name, typed, ltype))
        output[p.name] = ltype

    having = None
    if query.having is not None:
        having = check_predicate(
            query.having,
            output,
            cfg,
            join_path(path, "having"),
            budget(),
            functions=fns,
            totals=totals,
        )
    used = _totals_used([*(p.expr for p in query.post), query.having])

    value_types = {m.name: m.ltype for m in measures} | {p.name: p.ltype for p in post}
    pivot = None
    if query.pivot is not None:
        pivot = _plan_pivot(query, row_env, measures, post, join_path(path, "pivot"))
        index = {k: v for k, v in output.items() if k in query.group_by or k == LEVEL}
        if pivot.totals:
            index.update({v: output[v] for v in pivot.values})
        allowed_sort = None  # pivot result fields are only known once the domain is
        output = index
    else:
        allowed_sort = set(output)
    fixed = set(query.group_by) if query.rollup else None
    sort = _plan_sort(query, path, allowed=allowed_sort, fixed=fixed)
    return LogicalQuery(
        aggregated=True,
        filter=flt,
        derives=tuple(derives),
        row_env=row_env,
        select=(),
        group_by=query.group_by,
        measures=tuple(measures),
        post=tuple(post),
        having=having,
        rollup=query.rollup,
        pivot=pivot,
        sort=sort,
        tiebreak=tuple(g for g in query.group_by if g not in {s.by for s in sort}),
        page=query.page,
        output=output,
        value_types=value_types,
        totals=tuple(m.name for m in measures if m.name in used),
    )


def _totals_used(exprs: Sequence[Node | None]) -> set[str]:
    """The measures named by ``total()`` calls (already type-checked)."""
    used = set()
    for expr in exprs:
        if expr is None:
            continue
        for n in walk(expr):
            if isinstance(n, Func) and n.name == "total" and isinstance(n.args[0], ColRef):
                used.add(n.args[0].name)
    return used


def _unknown(name: str, path: str, env: Mapping[str, LType]) -> SpecError:
    return SpecError(
        f"unknown column: {name}",
        code="unknown_column",
        path=path,
        detail={"column": name, "available": sorted(env)[:100]},
    )


def _plan_measure(
    m: Any,
    index: int,
    env: Mapping[str, LType],
    cfg: NumericConfig,
    path: str,
    budget: Any,
    fns: Mapping[str, FunctionDef],
    aggs: Mapping[str, AggregateDef],
) -> MeasurePlan:
    def hidden(j: int) -> str:
        return f"__m{index}_{j}"

    where = None
    if m.where is not None:
        where = check_predicate(
            m.where, env, cfg, join_path(path, "where"), budget(), functions=fns
        )
    if m.fn == "count_rows":
        agg = HiddenAgg(hidden(0), "count_rows", None, where, INT)
        return MeasurePlan(m.name, (agg,), _col(agg.name, INT), INT)

    of_node: Node = m.of
    if not columns(of_node):
        raise SpecError(
            "a measure must read at least one column",
            code="invalid_measure",
            path=join_path(path, "of"),
        )
    if m.fn == "wavg":
        of_node = Binary(op="mul", left=m.of, right=m.weight)
        both = Logic(
            op="and",
            args=(IsNull(arg=m.of, negate=True), IsNull(arg=m.weight, negate=True)),
        )
        guard = check_predicate(both, env, cfg, path, budget(), functions=fns)
        where = guard if where is None else _and(where, guard)
    arg = check(of_node, env, cfg, join_path(path, "of"), budget(), functions=fns)
    arg_type = arg.ltype.rigid()
    fn = m.fn
    if fn not in BUILTIN_AGGS:
        return _plugin_measure(m, hidden(0), arg, where, aggs, path)
    if fn in ("sum", "mean", "wavg") and arg_type.kind not in NUMERIC:
        raise SpecError(f"{fn} needs numbers, got {arg_type}", code="type_mismatch", path=path)
    if fn in ("min", "max") and arg_type.kind not in ORDERABLE:
        raise SpecError(f"{fn} needs orderable values", code="type_mismatch", path=path)

    if fn in ("sum", "min", "max"):
        agg = HiddenAgg(hidden(0), fn, arg, where, arg_type)
        return MeasurePlan(m.name, (agg,), _col(agg.name, arg_type), arg_type)
    if fn in ("count", "count_distinct"):
        agg = HiddenAgg(hidden(0), fn, arg, where, INT)
        return MeasurePlan(m.name, (agg,), _col(agg.name, INT), INT)
    if fn == "mean":
        total = HiddenAgg(hidden(0), "sum", arg, where, arg_type)
        count = HiddenAgg(hidden(1), "count", arg, where, INT)
        return _ratio(m.name, total, count, cfg, path)
    weight = check(m.weight, env, cfg, join_path(path, "weight"), budget(), functions=fns)
    weight_type = weight.ltype.rigid()
    if weight_type.kind not in NUMERIC:
        raise SpecError("wavg weight must be numeric", code="type_mismatch", path=path)
    num = HiddenAgg(hidden(0), "sum", arg, where, arg_type)
    den = HiddenAgg(hidden(1), "sum", weight, where, weight_type)
    return _ratio(m.name, num, den, cfg, path)


def _plugin_measure(
    m: Any,
    name: str,
    arg: Typed,
    where: Typed | None,
    aggs: Mapping[str, AggregateDef],
    path: str,
) -> MeasurePlan:
    adef = aggs.get(m.fn)
    if adef is None:
        raise SpecError(
            f"unknown aggregate: {m.fn}",
            code="unknown_aggregate",
            path=join_path(path, "fn"),
            detail={"aggregate": m.fn, "available": sorted([*BUILTIN_AGGS, *aggs])},
        )
    try:
        ltype = adef.typecheck(arg.ltype.rigid())
    except (TypeError, ValueError) as exc:
        raise SpecError(f"{m.fn}: {exc}", code="type_mismatch", path=path) from None
    if ltype.kind in (Kind.NULL, Kind.OTHER):
        raise SpecError(
            f"{m.fn} must return a concrete type, not {ltype}", code="type_mismatch", path=path
        )
    ltype = ltype.rigid()
    agg = HiddenAgg(name, "plugin", arg, where, ltype, adef)
    return MeasurePlan(m.name, (agg,), _col(agg.name, ltype), ltype)


def _ratio(name: str, num: HiddenAgg, den: HiddenAgg, cfg: NumericConfig, path: str) -> MeasurePlan:
    node = Binary(op="div", left=ColRef(name=num.name), right=ColRef(name=den.name))
    final = check(node, {num.name: num.ltype, den.name: den.ltype}, cfg, path)
    ltype = final.ltype.rigid()
    return MeasurePlan(name, (num, den), final, ltype)


def _col(name: str, ltype: LType) -> Typed:
    return Typed(ColRef(name=name), ltype)


def _and(a: Typed, b: Typed) -> Typed:
    return Typed(Logic(op="and", args=(a.node, b.node)), BOOL, (a, b))


def _plan_pivot(
    query: Query,
    env: Mapping[str, LType],
    measures: Sequence[MeasurePlan],
    post: Sequence[PostPlan],
    path: str,
) -> PivotPlan:
    spec = query.pivot
    assert spec is not None
    on_types = []
    for i, name in enumerate(spec.on):
        if name not in env:
            raise _unknown(name, join_path(path, "on", i), env)
        if name in query.group_by:
            raise SpecError(f"{name} is both grouped and pivoted", code="name_conflict", path=path)
        if env[name].kind is Kind.OTHER:
            raise SpecError(f"cannot pivot on {name}", code="unsupported_type", path=path)
        on_types.append(env[name])
    names = [m.name for m in measures] + [p.name for p in post]
    values = tuple(names) if spec.values is None else spec.values
    for i, value in enumerate(values):
        if value not in names:
            raise SpecError(
                f"pivot value {value} is not a measure",
                code="unknown_column",
                path=join_path(path, "values", i),
            )
    domain = None
    if spec.domain is not None:
        domain = tuple(
            tuple(
                coerce_value(v, t, what="pivot domain value")
                for v, t in zip(combo, on_types, strict=True)
            )
            for combo in spec.domain
        )
    return PivotPlan(
        on=spec.on,
        on_types=tuple(on_types),
        values=values,
        domain=domain,
        totals=spec.totals,
        separator=spec.separator,
        null_label=spec.null_label,
    )


def _plan_sort(
    query: Query, path: str, *, allowed: set[str] | None, fixed: set[str] | None
) -> tuple[SortSpec, ...]:
    seen: set[str] = set()
    out = []
    for i, key in enumerate(query.sort):
        spath = join_path(path, "sort", i, "by")
        if key.by in seen:
            raise SpecError(f"{key.by} is sorted twice", code="invalid_sort", path=spath)
        if allowed is not None and key.by not in allowed:
            raise SpecError(
                f"cannot sort by {key.by}: not a column of this view",
                code="unknown_column",
                path=spath,
            )
        if fixed is not None and key.by not in fixed:
            raise SpecError(
                "with rollup, only group_by columns can be sorted",
                code="invalid_sort",
                path=spath,
            )
        seen.add(key.by)
        out.append(SortSpec(key.by, key.desc, key.nulls_last))
    return tuple(out)
