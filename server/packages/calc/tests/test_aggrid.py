from typing import Any

import pytest

from callscore_calc import CalcEngine, Measure, SpecError
from callscore_calc.adapters.aggrid import (
    AgGridAdapter,
    ServerSideRequest,
    decode_group_key,
    encode_group_key,
)
from callscore_calc.dtypes import DATE, INT, STR
from callscore_calc.spec.formula import to_formula

ADAPTER = AgGridAdapter()


def ssrm(**fields: Any) -> dict[str, Any]:
    return {"startRow": 0, "endRow": 100, **fields}


def rows(engine: CalcEngine, **fields: Any) -> dict[str, Any]:
    return ADAPTER.rows(engine, ssrm(**fields), dataset="pos").model_dump()


def column(name: str, agg: str | None = None) -> dict[str, Any]:
    return {"id": name, "field": name, "aggFunc": agg}


def test_top_level_groups(engine: CalcEngine) -> None:
    out = rows(
        engine,
        rowGroupCols=[column("desk")],
        valueCols=[column("qty", "sum"), column("price", "avg")],
        sortModel=[{"colId": "ag-Grid-AutoColumn", "sort": "desc"}],
    )
    assert out["rowCount"] == 4
    assert [r["desk"] for r in out["rowData"]] == ["rates", "equity", "credit", None]
    rates = out["rowData"][0]
    assert rates["qty"] == 30 and rates["__group_key"] == '"rates"'
    assert rates["__row_id"] == 'g:["\\"rates\\""]'
    assert out["rowData"][-1]["__group_key"] == "null"


def test_drill_down_with_null_group_key(engine: CalcEngine) -> None:
    out = rows(
        engine,
        rowGroupCols=[column("desk"), column("sector")],
        valueCols=[column("qty", "sum")],
        groupKeys=["null"],
    )
    assert out["rowData"] == [
        {"sector": "Fin", "qty": 60, "__group_key": '"Fin"', "__row_id": 'g:["null", "\\"Fin\\""]'}
    ]


def test_leaf_rows_have_stable_ids(engine: CalcEngine) -> None:
    out = rows(
        engine,
        rowGroupCols=[column("desk")],
        groupKeys=['"rates"'],
        sortModel=[
            {"colId": "qty", "sort": "desc"},
            {"colId": "ag-Grid-AutoColumn", "sort": "asc"},
        ],
    )
    assert [r["id"] for r in out["rowData"]] == [2, 1]
    assert out["rowData"][0]["__row_id"] == "r:[2]"
    assert out["rowData"][0]["price"] == 99.5  # decimals as floats for the grid


def test_paging_blocks(engine: CalcEngine) -> None:
    first = ADAPTER.rows(engine, {"startRow": 0, "endRow": 4}, dataset="pos")
    second = ADAPTER.rows(engine, {"startRow": 4, "endRow": 8}, dataset="pos")
    ids = [r["id"] for r in first.rowData + second.rowData]
    assert ids == [1, 2, 3, 4, 5, 6] and first.rowCount == 6


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ({"desk": {"filterType": "text", "type": "contains", "filter": "RAT"}}, [1, 2]),
        ({"desk": {"filterType": "text", "type": "notContains", "filter": "rat"}}, [3, 4, 5, 6]),
        ({"desk": {"filterType": "text", "type": "blank"}}, [6]),
        ({"desk": {"filterType": "text", "type": "startsWith", "filter": "cr"}}, [3, 4]),
        ({"qty": {"filterType": "number", "type": "greaterThan", "filter": 30}}, [4, 5, 6]),
        (
            {"qty": {"filterType": "number", "type": "inRange", "filter": 20, "filterTo": 50}},
            [3, 4],
        ),
        ({"price": {"filterType": "number", "type": "lessThan", "filter": 60.5}}, [3, 6]),
        ({"price": {"filterType": "number", "type": "blank"}}, [5]),
        (
            {
                "trade_date": {
                    "filterType": "date",
                    "type": "lessThan",
                    "dateFrom": "2026-03-01 00:00:00",
                }
            },
            [1, 2],
        ),
        ({"desk": {"filterType": "set", "values": ["credit", None]}}, [3, 4, 6]),
        ({"desk": {"filterType": "set", "values": []}}, []),
        (
            {
                "qty": {
                    "filterType": "number",
                    "operator": "OR",
                    "conditions": [
                        {"filterType": "number", "type": "lessThan", "filter": 15},
                        {"filterType": "number", "type": "greaterThan", "filter": 55},
                    ],
                }
            },
            [1, 6],
        ),
        (
            {
                "sector": {
                    "filterType": "multi",
                    "filterModels": [None, {"filterType": "set", "values": ["Tech"]}],
                },
                "qty": {"filterType": "number", "type": "notEqual", "filter": 50},
            },
            [1, 3],
        ),
    ],
)
def test_filters(engine: CalcEngine, model: dict[str, Any], expected: list[int]) -> None:
    out = rows(engine, filterModel=model, sortModel=[{"colId": "id", "sort": "asc"}])
    assert [r["id"] for r in out["rowData"]] == expected


def test_unsupported_filters(engine: CalcEngine) -> None:
    with pytest.raises(SpecError):
        rows(engine, filterModel={"filterType": "join", "type": "AND", "conditions": []})
    with pytest.raises(SpecError):
        rows(
            engine,
            filterModel={"qty": {"filterType": "number", "operator": "AND", "condition1": {}}},
        )


def test_pivot_mode(engine: CalcEngine) -> None:
    out = rows(
        engine,
        pivotMode=True,
        rowGroupCols=[column("sector")],
        pivotCols=[column("desk")],
        valueCols=[column("qty", "sum")],
    )
    assert out["pivotResultFields"] == ["credit_qty", "equity_qty", "rates_qty", "(blank)_qty"]
    tech = next(r for r in out["rowData"] if r["sector"] == "Tech")
    assert (tech["credit_qty"], tech["rates_qty"], tech["(blank)_qty"]) == (30, 10, None)
    # Drilling into a group keeps the same pivot columns (domain ignores group keys).
    sub = rows(
        engine,
        pivotMode=True,
        rowGroupCols=[column("sector"), column("id")],
        pivotCols=[column("desk")],
        valueCols=[column("qty", "sum")],
        groupKeys=['"Fin"'],
    )
    assert sub["pivotResultFields"] == out["pivotResultFields"]
    assert [(r["id"], r["rates_qty"], r["(blank)_qty"]) for r in sub["rowData"]] == [
        (2, 20, None),
        (6, None, 60),
    ]


def test_pivot_mode_without_row_groups(engine: CalcEngine) -> None:
    out = rows(
        engine, pivotMode=True, pivotCols=[column("sector")], valueCols=[column("qty", "sum")]
    )
    assert out["rowCount"] == 1
    assert out["rowData"][0]["Tech_qty"] == 90


def test_custom_aggregation(engine: CalcEngine) -> None:
    adapter = AgGridAdapter(
        custom_aggs={"wavg": lambda c: Measure(name=c, fn="wavg", of=c, weight="float(qty)")}
    )
    out = adapter.rows(
        engine,
        ssrm(rowGroupCols=[column("sector")], valueCols=[column("yield", "wavg")]),
        dataset="pos",
    )
    fin = next(r for r in out.rowData if r["sector"] == "Fin")
    assert fin["yield"] == pytest.approx((0.04 * 20 + 0.01 * 60) / 80)
    with pytest.raises(SpecError, match="first"):
        rows(engine, rowGroupCols=[column("sector")], valueCols=[column("qty", "first")])


def test_group_key_round_trip() -> None:
    import datetime as dt

    assert decode_group_key(encode_group_key(dt.date(2026, 1, 5), DATE), DATE) == dt.date(
        2026, 1, 5
    )
    assert decode_group_key(encode_group_key(None, STR), STR) is None
    assert (
        decode_group_key("123", STR) == "123"
    )  # plain keys from grids without getServerSideGroupKey
    assert decode_group_key("7", INT) == 7


def test_filter_translation_is_readable(engine: CalcEngine) -> None:
    schema = engine.schema("pos")
    expr = ADAPTER.filter_expr(
        {"desk": {"filterType": "text", "type": "equals", "filter": "Rates"}}, schema
    )
    assert expr is not None
    assert to_formula(expr) == "lower(coalesce(desk, '')) == 'rates'"


def test_request_model_ignores_unknown_fields() -> None:
    parsed = ServerSideRequest.model_validate(
        {"startRow": 0, "endRow": 10, "rowGroupCols": [], "somethingNew": 1}
    )
    assert parsed.endRow == 10
