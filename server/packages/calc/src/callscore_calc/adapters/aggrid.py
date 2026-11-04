"""AG Grid Server-Side Row Model (SSRM) adapter. Pure translation; no web framework needed.

Client setup this adapter expects::

    rowModelType: 'serverSide',
    getRowId: p => p.data.__row_id,
    getServerSideGroupKey: d => d.__group_key,   // exact, typed group keys (nulls, dates)
    serverSidePivotResultFieldSeparator: '_',   // must match AgGridAdapter.separator
    readOnlyEdit: true,                           // edits go through onCellEditRequest (a plugin,
                                                  // e.g. what-if, turns them into changes)

``__group_key`` is the JSON encoding of the group value; the adapter decodes it back using the
column type, so null groups, dates and decimals survive the round trip.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from callscore_calc.config import CalcContext
from callscore_calc.dtypes import NUMERIC, Kind, LType, coerce_value, json_value
from callscore_calc.errors import SpecError
from callscore_calc.result import CalcResult, json_safe
from callscore_calc.schema import DatasetSchema
from callscore_calc.spec.expr import (
    Cast,
    ColRef,
    Compare,
    Func,
    InList,
    IsNull,
    Lit,
    Logic,
    Node,
    Unary,
    col,
    lit,
)
from callscore_calc.spec.query import (
    CalcRequest,
    DatasetRef,
    Measure,
    Options,
    Page,
    Pivot,
    Query,
    SortKey,
)

AUTO_GROUP_COLUMN = "ag-Grid-AutoColumn"


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ColumnVO(_Lenient):
    id: str
    displayName: str | None = None
    field: str | None = None
    aggFunc: str | None = None

    @property
    def column(self) -> str:
        return self.field or self.id


class SortModelItem(_Lenient):
    colId: str
    sort: Literal["asc", "desc"]


class ServerSideRequest(_Lenient):
    """``IServerSideGetRowsRequest`` as AG Grid sends it."""

    startRow: int | None = None
    endRow: int | None = None
    rowGroupCols: list[ColumnVO] = []
    valueCols: list[ColumnVO] = []
    pivotCols: list[ColumnVO] = []
    pivotMode: bool = False
    groupKeys: list[str] = []
    filterModel: dict[str, Any] | None = None
    sortModel: list[SortModelItem] = []


class RowsResponse(BaseModel):
    """What the datasource passes to ``params.success(...)``."""

    rowData: list[dict[str, Any]]
    rowCount: int | None = None
    pivotResultFields: list[str] | None = None


@dataclass(frozen=True)
class Shape:
    """How to decorate result rows for the grid."""

    kind: Literal["group", "leaf", "pivot_leaf"]
    group_column: str | None
    group_type: LType | None
    parent_keys: tuple[str, ...]
    key_columns: tuple[str, ...]
    key_types: tuple[LType, ...]


MeasureFactory = Callable[[str], Measure]


@dataclass(frozen=True)
class AgGridAdapter:
    """Translate SSRM requests into engine requests and results back into grid rows.

    ``custom_aggs`` maps an AG Grid ``aggFunc`` name to a function building the measure for a
    column, e.g. ``{"wavg_yield": lambda c: Measure(name=c, fn="wavg", of=c,
    weight="float(price * quantity * fx_rate)")}``. ``in_range_inclusive`` must match the
    grid's filter option.
    """

    separator: str = "_"
    in_range_inclusive: bool = False
    null_label: str = "(blank)"
    decimals: Literal["float", "str"] = "float"
    custom_aggs: Mapping[str, MeasureFactory] = field(default_factory=dict)
    max_pivot_values: int = 500

    # --- Requests ---------------------------------------------------------------------------

    def to_request(
        self,
        ssrm: ServerSideRequest,
        *,
        dataset: str | DatasetRef,
        schema: DatasetSchema,
        extensions: Mapping[str, Any] | None = None,
        options: Options | None = None,
        pivot_domain: Sequence[Sequence[Any]] | None = None,
    ) -> tuple[CalcRequest, Shape]:
        types = schema.ltypes()
        groups = [c.column for c in ssrm.rowGroupCols]
        level = len(ssrm.groupKeys)
        if level > len(groups):
            raise SpecError("more group keys than row groups", code="invalid_request")
        filters: list[Node] = []
        if ssrm.filterModel:
            model_filter = self.filter_expr(ssrm.filterModel, schema)
            if model_filter is not None:
                filters.append(model_filter)
        for name, raw in zip(groups, ssrm.groupKeys, strict=False):
            filters.append(_equals(name, decode_group_key(raw, _type(types, name))))
        where = (
            None
            if not filters
            else filters[0]
            if len(filters) == 1
            else Logic(op="and", args=tuple(filters))
        )
        page = None
        if ssrm.startRow is not None and ssrm.endRow is not None:
            page = Page(offset=ssrm.startRow, limit=max(0, ssrm.endRow - ssrm.startRow))
        keys = schema.key_columns
        key_types = tuple(types[k] for k in keys)
        measures = tuple(self.measure(v, types) for v in ssrm.valueCols)
        pivoting = ssrm.pivotMode and bool(ssrm.pivotCols) and bool(measures)
        if level < len(groups) or (ssrm.pivotMode and not groups):
            group = groups[level] if level < len(groups) else None
            pivot = None
            if pivoting:
                domain = None if pivot_domain is None else tuple(tuple(c) for c in pivot_domain)
                pivot = Pivot(
                    on=tuple(c.column for c in ssrm.pivotCols),
                    domain=domain,
                    separator=self.separator,
                    null_label=self.null_label,
                )
            names = {m.name for m in measures}
            sort = self._sort(ssrm.sortModel, group, names, pivot is not None)
            query = Query(
                filter=where,
                group_by=(group,) if group else (),
                measures=measures,
                pivot=pivot,
                sort=sort,
                page=page,
            )
            shape = Shape(
                "group" if group else "pivot_leaf",
                group,
                _type(types, group) if group else None,
                tuple(ssrm.groupKeys),
                keys,
                key_types,
            )
        else:
            if ssrm.pivotMode:
                # In pivot mode the deepest group level is the leaf; the grid never asks below it.
                query = Query(filter=where, page=Page(limit=0))
            else:
                sort = tuple(
                    SortKey(by=s.colId, desc=s.sort == "desc")
                    for s in ssrm.sortModel
                    if s.colId in types
                )
                query = Query(filter=where, sort=sort, page=page)
            shape = Shape("leaf", None, None, tuple(ssrm.groupKeys), keys, key_types)
        request = CalcRequest(
            dataset=DatasetRef.model_validate(dataset),
            extensions=dict(extensions or {}),
            query=query,
            options=options or Options(),
        )
        return request, shape

    def pivot_domain_request(
        self,
        ssrm: ServerSideRequest,
        *,
        dataset: str | DatasetRef,
        schema: DatasetSchema,
        extensions: Mapping[str, Any] | None = None,
    ) -> CalcRequest | None:
        """The request for the pivot values, filtered by the filter model only (not by group
        keys), so every block of the grid gets the same pivot columns."""
        if not (ssrm.pivotMode and ssrm.pivotCols and ssrm.valueCols):
            return None
        where = self.filter_expr(ssrm.filterModel, schema) if ssrm.filterModel else None
        on = tuple(c.column for c in ssrm.pivotCols)
        return CalcRequest(
            dataset=DatasetRef.model_validate(dataset),
            extensions=dict(extensions or {}),
            query=Query(filter=where, group_by=on, page=Page(limit=self.max_pivot_values + 1)),
        )

    def measure(self, value: ColumnVO, types: Mapping[str, LType]) -> Measure:
        name = value.column
        agg = value.aggFunc or "sum"
        if agg in self.custom_aggs:
            return self.custom_aggs[agg](name)
        mapped = {"sum": "sum", "min": "min", "max": "max", "avg": "mean", "count": "count_rows"}
        fn = mapped.get(agg)
        if fn is None:
            raise SpecError(
                f"aggregation {agg!r} is not supported; register it in custom_aggs",
                code="unsupported_aggregation",
            )
        _type(types, name)
        if fn == "count_rows":
            return Measure(name=name, fn="count_rows")
        return Measure(name=name, fn=fn, of=col(name))

    def _sort(
        self,
        model: Sequence[SortModelItem],
        group: str | None,
        measures: set[str],
        pivoted: bool,
    ) -> tuple[SortKey, ...]:
        out: list[SortKey] = []
        for item in model:
            name = item.colId
            if name in (AUTO_GROUP_COLUMN, group):
                if group is None:
                    continue
                name = group
            elif name not in measures and not (pivoted and self.separator in name):
                continue  # sorting by a column that isn't part of this level
            if pivoted and name in measures:
                continue  # plain measure names are not columns of a pivoted view
            if any(k.by == name for k in out):
                continue
            out.append(SortKey(by=name, desc=item.sort == "desc"))
        return tuple(out)

    # --- Filters ----------------------------------------------------------------------------

    def filter_expr(self, model: Mapping[str, Any] | None, schema: DatasetSchema) -> Node | None:
        """Translate a column filter model into one expression (``None`` if it filters nothing)."""
        if not model:
            return None
        if "filterType" in model and model.get("filterType") == "join":
            raise SpecError(
                "the advanced filter model is not supported yet", code="unsupported_filter"
            )
        types = schema.ltypes()
        parts = []
        for column, spec in model.items():
            if spec is None:
                continue
            parts.append(self._column_filter(column, spec, _type(types, column)))
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else Logic(op="and", args=tuple(parts))

    def _column_filter(self, column: str, spec: Mapping[str, Any], ltype: LType) -> Node:
        if "condition1" in spec:
            raise SpecError(
                "the legacy condition1/condition2 filter format is not supported",
                code="unsupported_filter",
            )
        if "conditions" in spec:
            parts = [self._column_filter(column, c, ltype) for c in spec["conditions"]]
            if len(parts) == 1:
                return parts[0]
            op = "or" if str(spec.get("operator", "AND")).upper() == "OR" else "and"
            return Logic(op=op, args=tuple(parts))
        kind = spec.get("filterType")
        if kind == "multi":
            parts = [
                self._column_filter(column, m, ltype) for m in spec.get("filterModels") or [] if m
            ]
            if not parts:
                return lit(True)
            return parts[0] if len(parts) == 1 else Logic(op="and", args=tuple(parts))
        if kind == "text":
            return self._text(column, spec, ltype)
        if kind == "number":
            return self._compare(
                column, spec, ltype, spec.get("filter"), spec.get("filterTo"), number=True
            )
        if kind == "date":
            return self._compare(
                column,
                spec,
                ltype,
                _date(spec.get("dateFrom")),
                _date(spec.get("dateTo")),
                number=False,
            )
        if kind == "set":
            return self._set(column, spec.get("values") or [], ltype)
        raise SpecError(f"filter type {kind!r} is not supported", code="unsupported_filter")

    def _text(self, column: str, spec: Mapping[str, Any], ltype: LType) -> Node:
        op = spec.get("type", "contains")
        subject: Node = col(column)
        if op == "blank":
            return _blank(subject, ltype)
        if op == "notBlank":
            return Unary(op="not", arg=_blank(subject, ltype))
        if ltype.kind is not Kind.STR:
            if ltype.kind is Kind.FLOAT:
                raise SpecError(
                    "text filters don't apply to float columns", code="unsupported_filter"
                )
            subject = Cast(arg=subject, to="str")
        # Case-insensitive, and a missing value behaves like an empty string (as in the grid).
        text = Func(name="lower", args=(Func(name="coalesce", args=(subject, lit(""))),))
        value = lit(str(spec.get("filter") or "").lower())
        if op == "equals":
            return Compare(op="eq", left=text, right=value)
        if op == "notEqual":
            return Compare(op="ne", left=text, right=value)
        name = {
            "contains": "contains",
            "notContains": "contains",
            "startsWith": "starts_with",
            "endsWith": "ends_with",
        }.get(op)
        if name is None:
            raise SpecError(f"text filter {op!r} is not supported", code="unsupported_filter")
        test = Func(name=name, args=(text, value))
        return Unary(op="not", arg=test) if op == "notContains" else test

    def _compare(
        self,
        column: str,
        spec: Mapping[str, Any],
        ltype: LType,
        value: Any,
        upper: Any,
        *,
        number: bool,
    ) -> Node:
        op = spec.get("type", "equals")
        subject: Node = col(column)
        if op == "blank":
            return IsNull(arg=subject)
        if op == "notBlank":
            return IsNull(arg=subject, negate=True)
        if not number and ltype.kind is Kind.DATETIME:
            subject = Cast(arg=subject, to="date")  # the grid compares calendar dates
        elif number and ltype.kind not in NUMERIC:
            raise SpecError(
                f"number filter on non-numeric column {column}", code="unsupported_filter"
            )
        low = lit(value)
        ops = {
            "equals": "eq",
            "notEqual": "ne",
            "lessThan": "lt",
            "lessThanOrEqual": "le",
            "greaterThan": "gt",
            "greaterThanOrEqual": "ge",
        }
        if op in ops:
            return Compare(op=ops[op], left=subject, right=low)
        if op == "inRange":
            lower_op, upper_op = ("ge", "le") if self.in_range_inclusive else ("gt", "lt")
            return Logic(
                op="and",
                args=(
                    Compare(op=lower_op, left=subject, right=low),
                    Compare(op=upper_op, left=subject, right=lit(upper)),
                ),
            )
        raise SpecError(f"filter {op!r} is not supported", code="unsupported_filter")

    def _set(self, column: str, values: Sequence[Any], ltype: LType) -> Node:
        present = [
            coerce_value(v, ltype, what=f"{column} filter value") for v in values if v is not None
        ]
        parts: list[Node] = []
        if present:
            parts.append(InList(arg=col(column), values=tuple(lit(v) for v in present)))
        if any(v is None for v in values):
            parts.append(IsNull(arg=col(column)))
        if not parts:
            return lit(False)
        return parts[0] if len(parts) == 1 else Logic(op="or", args=tuple(parts))

    # --- Responses --------------------------------------------------------------------------

    def to_response(self, result: CalcResult, shape: Shape) -> RowsResponse:
        rows = []
        for row in result.frame.to_dicts():
            out = {k: json_safe(v, self.decimals) for k, v in row.items()}
            if shape.kind == "group" and shape.group_column is not None:
                assert shape.group_type is not None
                key = encode_group_key(row[shape.group_column], shape.group_type)
                out["__group_key"] = key
                out["__row_id"] = "g:" + json.dumps([*shape.parent_keys, key])
            elif shape.kind == "leaf":
                values = [
                    json_value(row.get(k), t)
                    for k, t in zip(shape.key_columns, shape.key_types, strict=True)
                ]
                out["__row_id"] = "r:" + json.dumps(values)
            else:
                out["__row_id"] = "p:" + json.dumps(list(shape.parent_keys))
            rows.append(out)
        return RowsResponse(
            rowData=rows,
            rowCount=result.meta.total_rows,
            pivotResultFields=result.meta.pivot_fields,
        )

    def rows(
        self,
        engine: Any,
        ssrm: ServerSideRequest | Mapping[str, Any],
        *,
        dataset: str | DatasetRef,
        extensions: Mapping[str, Any] | None = None,
        ctx: CalcContext | None = None,
    ) -> RowsResponse:
        """Answer one SSRM ``getRows`` call end to end (engine: a :class:`CalcEngine`)."""
        request = (
            ssrm if isinstance(ssrm, ServerSideRequest) else ServerSideRequest.model_validate(ssrm)
        )
        ref = DatasetRef.model_validate(dataset)
        schema = engine.schema(ref.id, ref.version, ctx=ctx)
        domain = None
        domain_request = self.pivot_domain_request(
            request, dataset=ref, schema=schema, extensions=extensions
        )
        if domain_request is not None:
            on = domain_request.query.group_by
            found = engine.run(domain_request, ctx).frame
            if found.height > self.max_pivot_values:
                raise SpecError(
                    f"more than {self.max_pivot_values} pivot values; filter first",
                    code="limit_exceeded",
                )
            domain = [tuple(r) for r in found.select(on).iter_rows()]
        calc, shape = self.to_request(
            request,
            dataset=ref,
            schema=schema,
            extensions=extensions,
            pivot_domain=domain,
        )
        return self.to_response(engine.run(calc, ctx), shape)


def encode_group_key(value: Any, ltype: LType) -> str:
    return json.dumps(json_value(value, ltype))


def decode_group_key(raw: str, ltype: LType) -> Any:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = raw
    if ltype.kind is Kind.STR and value is not None and not isinstance(value, str):
        value = raw  # a plain (unencoded) string key that happens to look like JSON
    return coerce_value(value, ltype, what="group key")


def _equals(column: str, value: Any) -> Node:
    if value is None:
        return IsNull(arg=ColRef(name=column))
    return Compare(op="eq", left=ColRef(name=column), right=_literal(value))


def _literal(value: Any) -> Lit:
    return lit(value)


def _blank(subject: Node, ltype: LType) -> Node:
    missing = IsNull(arg=subject)
    if ltype.kind is not Kind.STR:
        return missing
    return Logic(op="or", args=(missing, Compare(op="eq", left=subject, right=lit(""))))


def _date(value: Any) -> dt.date | None:
    if value in (None, ""):
        return None
    return dt.date.fromisoformat(str(value).strip()[:10])


def _type(types: Mapping[str, LType], name: str) -> LType:
    if name not in types:
        raise SpecError(f"unknown column: {name}", code="unknown_column", detail={"column": name})
    return types[name]
