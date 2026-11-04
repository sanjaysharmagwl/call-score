"""``total(m)``: a measure over all the rows a query sees, for weights and shares in post/having."""

from decimal import Decimal
from typing import Any

import polars as pl
import pytest

from callscore_calc import CalcEngine, Catalog, SpecError, parse_formula, to_formula
from callscore_calc.verify import verify

MV = {"name": "mv", "fn": "sum", "of": "price * qty"}
WEIGHT = {"name": "w", "expr": "float(mv) / float(total(mv))"}


def run(engine: CalcEngine, **query: Any) -> list[dict[str, Any]]:
    request = {"dataset": "pos", "query": query}
    report = verify(engine, request)
    assert report.ok, report.problems
    return engine.run(request).frame.to_dicts()


def test_weights_sum_to_one_at_every_rollup_level(engine: CalcEngine) -> None:
    rows = run(engine, group_by=["sector", "desk"], measures=[MV], post=[WEIGHT], rollup=True)
    details = [r for r in rows if r["__level"] == 2]
    sectors = [r for r in rows if r["__level"] == 1]
    (grand,) = [r for r in rows if r["__level"] == 0]
    assert sum(r["w"] or 0.0 for r in details) == pytest.approx(1.0)  # equity has no price
    assert sum(r["w"] for r in sectors) == pytest.approx(1.0)
    assert grand["w"] == pytest.approx(1.0)
    tech = next(r for r in sectors if r["sector"] == "Tech")
    assert tech["w"] == pytest.approx(2512.5 / float(grand["mv"]))  # a share of the whole book


def test_total_keeps_its_type(engine: CalcEngine) -> None:
    rows = run(
        engine,
        group_by=["sector"],
        measures=[MV, {"name": "q", "fn": "sum", "of": "qty"}],
        post=[{"name": "book", "expr": "total(mv)"}, {"name": "rest", "expr": "total(q) - q"}],
    )
    assert {r["book"] for r in rows} == {Decimal("8106.50")}
    fin = next(r for r in rows if r["sector"] == "Fin")
    assert fin["rest"] == 210 - 80


def test_having_does_not_change_the_denominator(engine: CalcEngine) -> None:
    rows = run(engine, group_by=["sector"], measures=[MV], post=[WEIGHT], having="w < 0.35")
    assert [r["sector"] for r in rows] == ["Fin", "Tech"]
    assert sum(r["w"] for r in rows) < 1.0  # Energy is filtered out, but still in the total


def test_total_in_having(engine: CalcEngine) -> None:
    rows = run(engine, group_by=["sector"], measures=[MV], having="mv > total(mv) / 3")
    assert [r["sector"] for r in rows] == ["Energy"]


def test_total_follows_the_filter(engine: CalcEngine) -> None:
    rows = run(engine, filter="desk == 'rates'", group_by=["sector"], measures=[MV], post=[WEIGHT])
    assert sum(r["w"] for r in rows) == pytest.approx(1.0)


def test_total_with_filtered_measures(engine: CalcEngine) -> None:
    """Active weight: two portfolios in one dataset, each with its own denominator."""
    rows = run(
        engine,
        group_by=["sector"],
        measures=[
            {"name": "a", "fn": "sum", "of": "qty", "where": "desk == 'rates'"},
            {"name": "b", "fn": "sum", "of": "qty", "where": "desk != 'rates'"},
        ],
        post=[
            {"name": "wa", "expr": "coalesce(float(a), 0.0) / float(total(a))"},
            {"name": "wb", "expr": "coalesce(float(b), 0.0) / float(total(b))"},
            {"name": "active", "expr": "wa - wb"},
        ],
    )
    assert sum(r["active"] for r in rows) == pytest.approx(0.0)
    tech = next(r for r in rows if r["sector"] == "Tech")
    assert tech["wa"] == pytest.approx(10 / 30)
    assert tech["wb"] == pytest.approx(80 / 120)


def test_pivot_cells_share_one_denominator(engine: CalcEngine) -> None:
    rows = run(
        engine,
        group_by=["sector"],
        measures=[{"name": "q", "fn": "sum", "of": "qty"}],
        post=[{"name": "w", "expr": "float(q) / float(total(q))"}],
        pivot={"on": ["desk"], "values": ["w"], "totals": True},
    )
    cells = [v for r in rows for k, v in r.items() if k.endswith("_w") and v is not None]
    assert sum(cells) == pytest.approx(1.0)
    assert sum(r["w"] for r in rows) == pytest.approx(1.0)


def test_zero_or_null_total_gives_null(engine: CalcEngine) -> None:
    rows = run(
        engine,
        group_by=["sector"],
        measures=[
            {"name": "z", "fn": "sum", "of": "qty * 0"},
            {"name": "none", "fn": "sum", "of": "qty", "where": "qty > 1000"},
        ],
        post=[{"name": "wz", "expr": "z / total(z)"}, {"name": "wn", "expr": "none / total(none)"}],
    )
    assert all(r["wz"] is None and r["wn"] is None for r in rows)


def test_each_compare_side_has_its_own_total(catalog: Catalog, frame: pl.DataFrame) -> None:
    catalog.register_frame(
        "pos",
        frame.with_columns(
            pl.when(pl.col("id") == 2).then(220).otherwise(pl.col("qty")).alias("qty")
        ),
        key_columns=["id"],
        version="v2",
    )
    result = CalcEngine(catalog).compare(
        {
            "dataset": {"id": "pos", "version": "v2"},
            "base": {"version": "v1"},
            "query": {
                "group_by": ["sector"],
                "measures": [{"name": "q", "fn": "sum", "of": "qty"}],
                "post": [{"name": "w", "expr": "float(q) / float(total(q))"}],
            },
        }
    )
    rows = {r["sector"]: r for r in result.frame.to_dicts()}
    assert rows["Fin"]["w__base"] == pytest.approx(80 / 210)
    assert rows["Fin"]["w"] == pytest.approx(280 / 410)
    assert rows["Tech"]["w__delta"] < 0  # unchanged holdings, bigger book: a smaller weight


@pytest.mark.parametrize(
    ("query", "path"),
    [
        ({"filter": "total(qty) > 0"}, "/query/filter"),
        ({"derive": [{"name": "x", "expr": "total(qty)"}]}, "/query/derive/0/expr"),
        (
            {"group_by": ["sector"], "measures": [{"name": "m", "fn": "sum", "of": "total(qty)"}]},
            "/query/measures/0",
        ),
        (
            {
                "group_by": ["sector"],
                "measures": [MV],
                "post": [{"name": "w", "expr": "total(qty)"}],
            },
            "/query/post/0/expr",
        ),
        (
            {
                "group_by": ["sector"],
                "measures": [MV],
                "post": [{"name": "p", "expr": "mv * 2"}, {"name": "w", "expr": "total(p)"}],
            },
            "/query/post/1/expr",
        ),
        (
            {
                "group_by": ["sector"],
                "measures": [MV],
                "post": [{"name": "w", "expr": "total(mv * 2)"}],
            },
            "/query/post/0/expr",
        ),
    ],
)
def test_total_needs_a_measure_after_aggregation(
    engine: CalcEngine, query: dict[str, Any], path: str
) -> None:
    with pytest.raises(SpecError) as info:
        engine.run({"dataset": "pos", "query": query})
    assert info.value.code == "invalid_total"
    assert (info.value.path or "").startswith(path)


def test_total_round_trips_as_a_formula() -> None:
    assert to_formula(parse_formula("mv / total(mv)")) == "mv / total(mv)"
