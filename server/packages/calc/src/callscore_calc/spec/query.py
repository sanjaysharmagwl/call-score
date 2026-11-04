"""Queries (views) and the top-level requests.

A query runs its stages in a fixed order, whatever order the fields are written in:

1. ``filter`` (rows)          5. ``having`` (groups)
2. ``derive`` (new columns)   6. ``rollup`` (subtotals)
3. ``group_by`` + ``measures``  7. ``pivot`` (split by column values)
4. ``post`` (ratios, etc.)    8. ``sort`` (always a total order) and ``page``

Without ``group_by`` or ``measures`` the query returns rows (a leaf view), optionally narrowed to
``select`` columns.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import Field, model_validator

from callscore_calc.dtypes import Scalar
from callscore_calc.spec.base import Model
from callscore_calc.spec.expr import Expr

AggFn = Literal["sum", "mean", "min", "max", "count", "count_rows", "count_distinct", "wavg"]
BUILTIN_AGGS: frozenset[str] = frozenset(get_args(AggFn))
AGG_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class Derive(Model):
    """A new column for this query only. With ``where``, rows that don't match get ``otherwise``."""

    name: str = Field(min_length=1)
    expr: Expr
    where: Expr | None = None
    otherwise: Expr | None = None

    @model_validator(mode="after")
    def _otherwise_needs_where(self) -> Derive:
        if self.otherwise is not None and self.where is None:
            raise ValueError("'otherwise' only applies together with 'where'")
        return self


class Measure(Model):
    """An aggregate per group.

    ``where`` works like SQL ``FILTER (WHERE ...)``: it narrows the rows this measure sees without
    changing which groups exist. ``wavg`` is ``sum(of * weight) / sum(weight)`` over rows where
    both are present, recomputed at every level (never an average of averages). ``sum`` of no
    values is null, as in SQL. Any other ``fn`` must be an aggregate registered by a plugin; it
    takes ``of`` and sees the non-null values of each group.
    """

    name: str = Field(min_length=1)
    fn: str = Field(pattern=AGG_NAME_PATTERN)
    of: Expr | None = None
    weight: Expr | None = None
    where: Expr | None = None

    @model_validator(mode="after")
    def _args(self) -> Measure:
        if self.fn not in BUILTIN_AGGS:
            if self.of is None:
                raise ValueError(f"{self.fn} needs 'of'")
            if self.weight is not None:
                raise ValueError("'weight' is only allowed for wavg")
            return self
        if self.fn == "count_rows":
            if self.of is not None:
                raise ValueError("count_rows counts rows and takes no 'of'")
        elif self.of is None:
            raise ValueError(f"{self.fn} needs 'of'")
        if (self.fn == "wavg") != (self.weight is not None):
            raise ValueError("'weight' is required for wavg and only allowed there")
        return self


class PostAgg(Model):
    """A value computed from measures (and group columns) after aggregation, e.g. a ratio."""

    name: str = Field(min_length=1)
    expr: Expr


class Pivot(Model):
    """Split measures into one column per combination of ``on`` values.

    Result columns are named ``"{v1}{sep}{v2}{sep}{measure}"``. ``domain`` fixes the combinations
    (and their order); without it they are the sorted distinct values of the filtered rows.
    ``totals`` adds the measures across all pivot columns under their plain names.
    """

    on: tuple[str, ...] = Field(min_length=1)
    values: tuple[str, ...] | None = None
    domain: tuple[tuple[Scalar, ...], ...] | None = None
    totals: bool = False
    separator: str = Field(default="_", min_length=1)
    null_label: str = "(blank)"

    @model_validator(mode="after")
    def _domain_width(self) -> Pivot:
        if self.domain is not None and any(len(combo) != len(self.on) for combo in self.domain):
            raise ValueError("every domain entry needs one value per 'on' column")
        return self


class SortKey(Model):
    by: str
    desc: bool = False
    nulls_last: bool = True


class Page(Model):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(ge=0)


class Query(Model):
    filter: Expr | None = None
    derive: tuple[Derive, ...] = ()
    select: tuple[str, ...] | None = None
    group_by: tuple[str, ...] = ()
    measures: tuple[Measure, ...] = ()
    post: tuple[PostAgg, ...] = ()
    having: Expr | None = None
    rollup: bool = False
    pivot: Pivot | None = None
    sort: tuple[SortKey, ...] = ()
    page: Page | None = None

    @property
    def aggregated(self) -> bool:
        return bool(self.group_by or self.measures)

    @model_validator(mode="after")
    def _shape(self) -> Query:
        if not self.aggregated:
            for name in ("post", "having", "rollup", "pivot"):
                if getattr(self, name):
                    raise ValueError(f"'{name}' needs group_by or measures")
        elif self.select is not None:
            raise ValueError(
                "'select' applies to row views; aggregated views return their measures"
            )
        if self.rollup and not self.group_by:
            raise ValueError("'rollup' needs group_by")
        if self.pivot is not None and self.having is not None:
            raise ValueError("'having' cannot be combined with 'pivot' yet")
        return self


class DatasetRef(Model):
    """A dataset and, optionally, a pinned version (default: the latest registered)."""

    id: str = Field(min_length=1)
    version: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, data: Any) -> Any:
        return {"id": data} if isinstance(data, str) else data


class Options(Model):
    """Execution options.

    ``deterministic`` makes float sums independent of thread scheduling (values are summed in
    sorted order per group; slower). ``audit`` records row counts per stage in the result
    metadata (one extra pass per stage).
    """

    deterministic: bool = False
    engine: Literal["auto", "in-memory", "streaming"] = "auto"
    timeout_s: float | None = Field(default=None, gt=0)
    audit: bool = False


Extensions = dict[str, Any]
"""Plugin transform blocks by transform name, e.g. ``{"whatif": {"steps": [...]}}``."""


class CalcRequest(Model):
    """Run ``query`` over ``dataset``, after the plugin transforms named in ``extensions``."""

    spec_version: Literal[2] = 2
    dataset: DatasetRef
    extensions: Extensions = Field(default_factory=dict)
    query: Query = Query()
    options: Options = Options()


class Side(Model):
    """The base side of a comparison: its own ``extensions`` and, optionally, another version
    of the dataset."""

    version: str | None = None
    extensions: Extensions = Field(default_factory=dict)


class CompareRequest(Model):
    """Run the same query on two sides and join them: ``m``, ``m__base``, ``m__delta``, ``m__pct``.

    The target side is ``dataset`` with ``extensions``; the base side is ``base`` (by default the
    unmodified dataset). Aggregated queries join on the group columns, row views on the dataset
    key columns.
    """

    spec_version: Literal[2] = 2
    dataset: DatasetRef
    extensions: Extensions = Field(default_factory=dict)
    base: Side = Side()
    query: Query = Query()
    options: Options = Options()

    @model_validator(mode="after")
    def _no_pivot(self) -> CompareRequest:
        if self.query.pivot is not None:
            raise ValueError("compare does not support pivot yet")
        return self
