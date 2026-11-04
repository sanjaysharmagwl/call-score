import threading
import time
from decimal import Decimal
from typing import Any

import polars as pl
import pytest

from callscore_calc import (
    CalcContext,
    CalcEngine,
    Catalog,
    EngineBusy,
    EngineConfig,
    LimitExceeded,
    Limits,
    SpecError,
)
from callscore_calc.verify import check_pages, check_rollup_totals, verify


def run(engine: CalcEngine, **query: Any) -> list[dict[str, Any]]:
    return engine.run({"dataset": "pos", "query": query}).frame.to_dicts()


def value(engine: CalcEngine, expr: str) -> Any:
    """Evaluate one expression on the row with id 1."""
    rows = run(engine, filter="id == 1", derive=[{"name": "v", "expr": expr}], select=["v"])
    return rows[0]["v"]


# --- Arithmetic and types -----------------------------------------------------------------


def test_decimal_arithmetic_is_exact(engine: CalcEngine) -> None:
    assert value(engine, "price * 1.05") == Decimal("106.3125")  # 101.25 * 1.05, not rounded
    assert value(engine, "price * qty") == Decimal("1012.50")
    assert value(engine, "price + 0.001") == Decimal("101.251")
    assert value(engine, "price / 3") == Decimal("33.7500000000")


def test_decimal_division_rounds_half_even(engine: CalcEngine) -> None:
    assert value(engine, "decimal(1, 0) / 8") == Decimal("0.1250000000")
    assert value(engine, "round(decimal(0.125, 3), 2)") == Decimal("0.12")
    assert value(engine, "round(decimal(0.375, 3), 2)") == Decimal("0.38")


def test_integer_division_is_exact_and_flexible(engine: CalcEngine) -> None:
    assert value(engine, "qty / 4") == Decimal("2.5000000000")
    assert value(engine, "price + qty / 4") == Decimal("103.7500000000")
    assert value(engine, "yield + qty / 4") == pytest.approx(2.55)


def test_literals_adapt_but_columns_do_not_mix(engine: CalcEngine) -> None:
    assert value(engine, "yield * 1.5") == pytest.approx(0.075)
    with pytest.raises(SpecError) as info:
        value(engine, "price * yield")
    assert info.value.code == "type_mismatch"
    assert value(engine, "float(price) * yield") == pytest.approx(5.0625)


def test_division_by_zero_and_invalid_math_give_null(engine: CalcEngine) -> None:
    assert value(engine, "price / 0") is None
    assert value(engine, "yield / 0") is None
    assert value(engine, "qty / (qty - 10)") is None
    assert value(engine, "sqrt(-yield)") is None
    assert value(engine, "log(0.0)") is None


def test_null_logic_is_three_valued(engine: CalcEngine) -> None:
    ids = [r["id"] for r in run(engine, filter="price > 60 or desk == 'x'", sort=[{"by": "id"}])]
    assert ids == [1, 2, 4]  # row 5 has no price: null or false -> dropped
    ids = [r["id"] for r in run(engine, filter="not (desk == 'rates')")]
    assert 6 not in ids  # desk is null, so the condition is null


def test_string_and_date_functions(engine: CalcEngine) -> None:
    assert value(engine, "upper(sector)") == "TECH"
    assert value(engine, "contains(lower(desk), 'at')") is True
    assert value(engine, "trade_date >= date('2026-01-01')") is True
    assert value(engine, "str(price)") == "101.25"


def test_unknown_columns_report_a_path(engine: CalcEngine) -> None:
    with pytest.raises(SpecError) as info:
        run(engine, measures=[{"name": "x", "fn": "sum", "of": "nope"}])
    assert info.value.code == "unknown_column"
    assert info.value.path == "/query/measures/0/of"


def test_bad_formula_reports_a_path(engine: CalcEngine) -> None:
    with pytest.raises(SpecError) as info:
        run(engine, filter="price >")
    assert info.value.code == "formula_syntax"
    assert info.value.path == "/query/filter"


# --- Row views ------------------------------------------------------------------------------


def test_row_view_select_sort_and_page(engine: CalcEngine) -> None:
    result = engine.run(
        {
            "dataset": "pos",
            "query": {
                "select": ["id", "qty"],
                "sort": [{"by": "qty", "desc": True}],
                "page": {"offset": 1, "limit": 2},
            },
        }
    )
    assert result.frame.to_dicts() == [{"id": 5, "qty": 50}, {"id": 4, "qty": 40}]
    assert result.meta.total_rows == 6


def test_empty_page_past_the_end_still_counts(engine: CalcEngine) -> None:
    result = engine.run({"dataset": "pos", "query": {"page": {"offset": 100, "limit": 5}}})
    assert result.meta.rows == 0 and result.meta.total_rows == 6


def test_pages_partition_the_view(engine: CalcEngine) -> None:
    request = {"dataset": "pos", "query": {"sort": [{"by": "sector"}]}}
    assert check_pages(engine, request, 4) == []
    agg = {
        "dataset": "pos",
        "query": {"group_by": ["sector"], "measures": [{"name": "n", "fn": "count_rows"}]},
    }
    assert check_pages(engine, agg, 2) == []


def test_derive_with_where(engine: CalcEngine) -> None:
    rows = run(
        engine,
        derive=[{"name": "big", "expr": "qty * 2", "where": "qty >= 40", "otherwise": "0"}],
        select=["id", "big"],
        sort=[{"by": "id"}],
    )
    assert [r["big"] for r in rows] == [0, 0, 0, 80, 100, 120]


def test_unpaged_row_limit(catalog: Catalog) -> None:
    engine = CalcEngine(catalog, config=EngineConfig(limits=Limits(max_unpaged_rows=3)))
    with pytest.raises(LimitExceeded):
        engine.run({"dataset": "pos"})
    assert engine.run({"dataset": "pos", "query": {"page": {"limit": 3}}}).meta.rows == 3


# --- Aggregation ----------------------------------------------------------------------------


def test_measures(engine: CalcEngine) -> None:
    rows = run(
        engine,
        group_by=["sector"],
        measures=[
            {"name": "mv", "fn": "sum", "of": "price * qty"},
            {"name": "px", "fn": "mean", "of": "price"},
            {"name": "n", "fn": "count_rows"},
            {"name": "priced", "fn": "count", "of": "price"},
            {"name": "desks", "fn": "count_distinct", "of": "desk"},
            {"name": "y", "fn": "wavg", "of": "yield", "weight": "float(qty)"},
            {"name": "tech_emea", "fn": "sum", "of": "qty", "where": "desk == 'rates'"},
        ],
    )
    tech = next(r for r in rows if r["sector"] == "Tech")
    assert tech["mv"] == Decimal("2512.50")  # 101.25*10 + 50*30; row 5 has no price
    assert tech["px"] == Decimal("75.6250000000")
    assert (tech["n"], tech["priced"], tech["desks"]) == (3, 2, 3)
    assert tech["y"] == pytest.approx((0.05 * 10 + 0.02 * 50) / 60)
    assert tech["tech_emea"] == 10
    energy = next(r for r in rows if r["sector"] == "Energy")
    assert energy["tech_emea"] is None  # SQL: a sum over no rows is null


def test_post_aggregation_ratio_and_having(engine: CalcEngine) -> None:
    rows = run(
        engine,
        group_by=["sector"],
        measures=[
            {"name": "mv", "fn": "sum", "of": "price * qty"},
            {"name": "q", "fn": "sum", "of": "qty"},
        ],
        post=[{"name": "avg_px", "expr": "mv / q"}],
        having="q > 50",
        sort=[{"by": "q", "desc": True}],
    )
    assert [r["sector"] for r in rows] == ["Tech", "Fin"]
    fin = rows[1]
    assert fin["avg_px"] == Decimal("2590.00") / 80  # ratio of sums, not a mean of ratios


def test_grand_total_without_group_by(engine: CalcEngine) -> None:
    rows = run(engine, measures=[{"name": "q", "fn": "sum", "of": "qty"}])
    assert rows == [{"q": 210}]
    rows = run(
        engine,
        filter="qty > 1000",
        measures=[{"name": "q", "fn": "sum", "of": "qty"}, {"name": "n", "fn": "count_rows"}],
    )
    assert rows == [{"q": None, "n": 0}]


def test_rollup_orders_subtotals_after_details(engine: CalcEngine) -> None:
    result = engine.run(
        {
            "dataset": "pos",
            "query": {
                "group_by": ["sector", "desk"],
                "measures": [{"name": "q", "fn": "sum", "of": "qty"}],
                "rollup": True,
                "sort": [{"by": "sector", "desc": True}],
            },
        }
    )
    rows = result.frame.to_dicts()
    assert [(r["sector"], r["desk"], r["__level"]) for r in rows[:3]] == [
        ("Tech", "credit", 2),
        ("Tech", "equity", 2),
        ("Tech", "rates", 2),
    ]
    assert rows[3] == {"sector": "Tech", "desk": None, "__level": 1, "q": 90}
    assert rows[-1] == {"sector": None, "desk": None, "__level": 0, "q": 210}
    assert check_rollup_totals(result, ["sector", "desk"], ["q"]) == []


def test_rollup_rejects_measure_sort(engine: CalcEngine) -> None:
    with pytest.raises(SpecError, match="rollup"):
        run(
            engine,
            group_by=["sector"],
            measures=[{"name": "q", "fn": "sum", "of": "qty"}],
            rollup=True,
            sort=[{"by": "q"}],
        )


def test_pivot(engine: CalcEngine) -> None:
    result = engine.run(
        {
            "dataset": "pos",
            "query": {
                "group_by": ["sector"],
                "measures": [{"name": "q", "fn": "sum", "of": "qty"}],
                "pivot": {"on": ["desk"], "totals": True},
            },
        }
    )
    assert result.meta.pivot_fields == ["credit_q", "equity_q", "rates_q", "(blank)_q"]
    rows = {r["sector"]: r for r in result.frame.to_dicts()}
    assert rows["Tech"]["credit_q"] == 30 and rows["Tech"]["q"] == 90
    assert rows["Fin"]["(blank)_q"] == 60 and rows["Fin"]["credit_q"] is None


def test_pivot_with_explicit_domain_and_limits(catalog: Catalog) -> None:
    engine = CalcEngine(catalog, config=EngineConfig(limits=Limits(max_pivot_columns=2)))
    query = {"group_by": ["sector"], "measures": [{"name": "q", "fn": "sum", "of": "qty"}]}
    with pytest.raises(LimitExceeded):
        engine.run({"dataset": "pos", "query": {**query, "pivot": {"on": ["desk"]}}})
    result = engine.run(
        {"dataset": "pos", "query": {**query, "pivot": {"on": ["desk"], "domain": [["rates"]]}}}
    )
    assert result.meta.pivot_fields == ["rates_q"]


def test_pivot_separator_conflict(engine: CalcEngine) -> None:
    with pytest.raises(SpecError) as info:
        run(
            engine,
            measures=[{"name": "q", "fn": "sum", "of": "qty"}],
            pivot={"on": ["desk"], "separator": "e"},
        )
    assert info.value.code == "pivot_separator_conflict"


def test_group_limit(catalog: Catalog) -> None:
    engine = CalcEngine(catalog, config=EngineConfig(limits=Limits(max_groups=2)))
    with pytest.raises(LimitExceeded):
        engine.run({"dataset": "pos", "query": {"group_by": ["id"]}})


def test_distinct_values(engine: CalcEngine) -> None:
    assert engine.distinct_values("pos", "desk") == ["credit", "equity", "rates", None]
    assert engine.distinct_values("pos", "desk", filter="qty > 25", limit=2) == ["credit", "equity"]


def test_deterministic_float_sums(engine: CalcEngine) -> None:
    request = {
        "dataset": "pos",
        "query": {"group_by": ["sector"], "measures": [{"name": "y", "fn": "sum", "of": "yield"}]},
        "options": {"deterministic": True},
    }
    assert engine.run(request).frame.equals(engine.run(request).frame)
    assert verify(engine, request).ok


# --- Caching, metadata, context -------------------------------------------------------------


def test_results_are_cached_and_fingerprinted(engine: CalcEngine) -> None:
    request = {"dataset": "pos", "query": {"group_by": ["sector"], "page": {"limit": 1}}}
    first = engine.run(request)
    second = engine.run(
        {**request, "query": {**request["query"], "page": {"offset": 1, "limit": 1}}}
    )
    assert not first.meta.cached and second.meta.cached  # pages share one aggregate
    assert first.meta.fingerprint != second.meta.fingerprint
    assert engine.run(request).meta.fingerprint == first.meta.fingerprint
    assert first.meta.dataset.version == "v1"
    assert first.meta.versions["polars"] == pl.__version__


def test_audit_counts_stages(engine: CalcEngine) -> None:
    result = engine.run(
        {
            "dataset": "pos",
            "query": {"filter": "qty > 25", "group_by": ["sector"]},
            "options": {"audit": True},
        }
    )
    assert result.meta.stage_rows == {"dataset": 6, "filtered": 4, "result": 3}


def test_context_row_filter_and_columns(engine: CalcEngine) -> None:
    ctx = CalcContext(
        principal="ana", row_filter="desk == 'rates'", allowed_columns=frozenset({"qty", "desk"})
    )
    rows = engine.run({"dataset": "pos", "query": {"sort": [{"by": "id"}]}}, ctx).frame.to_dicts()
    assert rows == [{"id": 1, "desk": "rates", "qty": 10}, {"id": 2, "desk": "rates", "qty": 20}]
    with pytest.raises(SpecError) as info:
        engine.run({"dataset": "pos", "query": {"filter": "price > 1"}}, ctx)
    assert info.value.code == "unknown_column"
    # A different context never sees the first one's cached rows.
    assert engine.run({"dataset": "pos", "query": {"sort": [{"by": "id"}]}}).meta.total_rows == 6


def test_on_result_hook(catalog: Catalog) -> None:
    seen = []
    engine = CalcEngine(
        catalog,
        config=EngineConfig(
            on_result=lambda meta, ctx: seen.append((meta.fingerprint, ctx.principal))
        ),
    )
    result = engine.run({"dataset": "pos"}, CalcContext(principal="bob"))
    assert seen == [(result.meta.fingerprint, "bob")]


def test_engine_busy(catalog: Catalog) -> None:
    engine = CalcEngine(catalog, config=EngineConfig(max_concurrent=1, queue_timeout_s=0.05))
    release = threading.Event()
    holding = threading.Event()

    def hold() -> None:
        with engine.kernel.slot():
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(5)
    try:
        started = time.monotonic()
        with pytest.raises(EngineBusy):
            engine.run({"dataset": "pos"})
        assert time.monotonic() - started < 2
    finally:
        release.set()
        thread.join()


def test_explain(engine: CalcEngine) -> None:
    info = engine.explain(
        {
            "dataset": "pos",
            "query": {"derive": [{"name": "d", "expr": "price * 2"}], "filter": "qty > 10"},
        }
    )
    assert info["lineage"]["derived"] == {"d": "price * 2"}
    assert info["columns"]["d"] == "decimal(2)"
    assert info["steps"] == [] and info["extensions"] == {}
    assert "FILTER" in info["plan"]
