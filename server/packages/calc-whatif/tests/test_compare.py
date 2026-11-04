from decimal import Decimal

import pytest

from callscore_calc import CalcEngine
from callscore_calc_whatif import WhatIfPlugin


def shock(column: str, op: str, value: int) -> dict[str, object]:
    return {"kind": "shock", "column": column, "op": op, "value": value}


def test_aggregated_compare(engine: CalcEngine) -> None:
    scenarios = engine.plugin(WhatIfPlugin).scenarios
    scenario = scenarios.create("pos", "tech up")
    scenarios.append(
        scenario.id,
        [
            {
                "kind": "shock",
                "column": "price",
                "op": "pct",
                "value": 10,
                "where": "sector == 'Tech'",
            }
        ],
        expected_version=0,
    )
    result = engine.compare(
        {
            "dataset": "pos",
            "extensions": {"whatif": {"scenario": scenario.id}},
            "query": {
                "group_by": ["sector"],
                "measures": [{"name": "mv", "fn": "sum", "of": "price * qty"}],
                "sort": [{"by": "mv__delta", "desc": True}],
            },
        }
    )
    rows = result.frame.to_dicts()
    assert list(rows[0]) == ["sector", "mv", "mv__base", "mv__delta", "mv__pct"]
    tech = rows[0]
    assert tech["sector"] == "Tech"
    assert tech["mv__base"] == Decimal("2512.50")
    assert tech["mv__delta"] == Decimal("251.30")  # 101.25 -> 111.38 (x10), 50.00 -> 55.00 (x30)
    assert tech["mv__pct"] == pytest.approx(10.0019900497)
    assert all(r["mv__delta"] == 0 for r in rows[1:])


def test_compare_two_what_ifs_with_rollup(engine: CalcEngine) -> None:
    result = engine.compare(
        {
            "dataset": "pos",
            "extensions": {"whatif": {"steps": [shock("qty", "add", 1)]}},
            "base": {"extensions": {"whatif": {"steps": [shock("qty", "mul", 2)]}}},
            "query": {
                "group_by": ["sector"],
                "rollup": True,
                "measures": [{"name": "q", "fn": "sum", "of": "qty"}],
            },
        }
    )
    total = result.frame.to_dicts()[-1]
    assert total == {
        "sector": None,
        "__level": 0,
        "q": 216,
        "q__base": 420,
        "q__delta": -204,
        "q__pct": pytest.approx(-48.5714285714),
    }


def test_row_level_compare_and_zero_base(engine: CalcEngine) -> None:
    # Version 1 shape (what_if / base_what_if), upgraded by the engine.
    result = engine.compare(
        {
            "dataset": "pos",
            "what_if": [
                {"kind": "override", "edits": [{"key": {"id": 2}, "column": "qty", "value": 25}]}
            ],
            "base_what_if": [
                {"kind": "override", "edits": [{"key": {"id": 3}, "column": "qty", "value": 0}]}
            ],
            "query": {
                "select": ["id", "qty"],
                "sort": [{"by": "qty__delta", "desc": True}],
                "page": {"limit": 2},
            },
        }
    )
    assert result.meta.total_rows == 6
    rows = result.frame.to_dicts()
    assert rows[0] == {
        "id": 3,
        "qty": 30,
        "qty__base": 0,
        "qty__delta": 30,
        "qty__pct": None,  # a zero base has no percentage change
    }
    assert rows[1]["id"] == 2 and rows[1]["qty__delta"] == 5


def test_shock_moves_weights_through_total(engine: CalcEngine) -> None:
    """total() sees the shocked rows, so a sector's weight moves as well as its value."""
    result = engine.compare(
        {
            "dataset": "pos",
            "extensions": {
                "whatif": {
                    "steps": [
                        {
                            "kind": "shock",
                            "column": "price",
                            "op": "pct",
                            "value": -50,
                            "where": "sector == 'Energy'",
                        }
                    ]
                }
            },
            "query": {
                "group_by": ["sector"],
                "measures": [{"name": "mv", "fn": "sum", "of": "price * qty"}],
                "post": [{"name": "w", "expr": "float(mv) / float(total(mv))"}],
            },
        }
    )
    rows = {r["sector"]: r for r in result.frame.to_dicts()}
    assert rows["Energy"]["w__base"] == pytest.approx(3004 / 8106.5)
    assert rows["Energy"]["w"] == pytest.approx(1502 / 6604.5)
    assert rows["Tech"]["mv__delta"] == 0 and rows["Tech"]["w__delta"] > 0
    assert sum(r["w"] for r in rows.values()) == pytest.approx(1.0)
