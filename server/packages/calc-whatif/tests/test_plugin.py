"""The what-if plugin through the plugin API: request shape, composition, discovery, boundary."""

import ast
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from callscore_calc import (
    Bound,
    CalcEngine,
    CalcError,
    Catalog,
    FunctionDef,
    LimitExceeded,
    PlanContext,
    Plugin,
    PluginError,
    Registry,
    SpecError,
    TransformDef,
    TransformPlan,
    discover_plugins,
)
from callscore_calc.ext import FLOAT, LType, Model
from callscore_calc.verify import verify
from callscore_calc_whatif import InMemoryScenarioStore, WhatIfLimits, WhatIfPlugin

SHOCK = {"kind": "shock", "column": "price", "op": "pct", "value": 10, "where": "sector == 'Tech'"}
FORMULA = {"kind": "formula", "name": "mv", "expr": "price * qty"}
QUERY = {"group_by": ["sector"], "measures": [{"name": "mv", "fn": "sum", "of": "mv"}]}


def test_v1_and_v2_requests_agree(engine: CalcEngine) -> None:
    v1 = {"dataset": "pos", "what_if": [SHOCK, FORMULA], "query": QUERY}
    v2 = {
        "spec_version": 2,
        "dataset": "pos",
        "extensions": {"whatif": {"steps": [SHOCK, FORMULA]}},
        "query": QUERY,
    }
    a, b = engine.run(v1), engine.run(v2)
    assert a.frame.equals(b.frame) and a.meta.fingerprint == b.meta.fingerprint
    assert b.meta.cached
    assert a.meta.extensions == {
        "whatif": {"scenario": None, "scenario_head": None, "unmatched_edits": []}
    }
    tech = {r["sector"]: r["mv"] for r in a.frame.to_dicts()}["Tech"]
    assert tech == Decimal("2763.80")  # 111.38 * 10 + 55.00 * 30


def test_empty_block_is_the_plain_dataset(engine: CalcEngine) -> None:
    plain = engine.run({"dataset": "pos", "query": QUERY | {"measures": []}})
    empty = engine.run(
        {"dataset": "pos", "extensions": {"whatif": {}}, "query": QUERY | {"measures": []}}
    )
    assert plain.meta.fingerprint == empty.meta.fingerprint


def test_explain_reports_lineage(engine: CalcEngine) -> None:
    info = engine.explain(
        {
            "dataset": "pos",
            "extensions": {"whatif": {"steps": [SHOCK, FORMULA]}},
            "query": {"derive": [{"name": "d", "expr": "mv * 2"}]},
        }
    )
    assert info["extensions"]["whatif"] == {
        "scenario": None,
        "changed_by": {
            "price": ["/extensions/whatif/steps/0"],
            "mv": ["/extensions/whatif/steps/1"],
        },
        "formulas": {"mv": "price * qty"},
    }
    assert info["lineage"]["derived"] == {"d": "mv * 2"}
    assert info["columns"]["mv"] == "decimal(2)"
    assert [s["kind"] for s in info["steps"]] == ["shock", "formula"]


def test_error_paths_point_into_the_block(engine: CalcEngine) -> None:
    with pytest.raises(SpecError) as info:
        engine.run(
            {
                "dataset": "pos",
                "extensions": {"whatif": {"steps": [{"kind": "shock", "column": "nope"}]}},
            }
        )
    assert info.value.path == "/extensions/whatif/steps/0/shock/op"
    with pytest.raises(SpecError) as info:
        engine.run(
            {
                "dataset": "pos",
                "extensions": {
                    "whatif": {
                        "steps": [{"kind": "shock", "column": "nope", "op": "add", "value": 1}]
                    }
                },
            }
        )
    assert info.value.code == "unknown_column"
    assert info.value.path == "/extensions/whatif/steps/0/column"


def test_without_a_store_only_ad_hoc_steps_work(catalog: Catalog) -> None:
    engine = CalcEngine(catalog, plugins=[WhatIfPlugin()])
    assert engine.run({"dataset": "pos", "extensions": {"whatif": {"steps": [SHOCK]}}}).frame.height
    with pytest.raises(CalcError) as info:
        engine.run({"dataset": "pos", "extensions": {"whatif": {"scenario": "s"}}})
    assert info.value.code == "no_scenario_store"


def test_limits(catalog: Catalog) -> None:
    plugin = WhatIfPlugin(limits=WhatIfLimits(max_steps=1, max_edits=1))
    engine = CalcEngine(catalog, plugins=[plugin])
    with pytest.raises(LimitExceeded) as info:
        engine.run({"dataset": "pos", "extensions": {"whatif": {"steps": [SHOCK, SHOCK]}}})
    assert info.value.path == "/extensions/whatif/steps"
    edits = [{"key": {"id": i}, "column": "qty", "value": 1} for i in (1, 2)]
    with pytest.raises(LimitExceeded):
        engine.run(
            {
                "dataset": "pos",
                "extensions": {"whatif": {"steps": [{"kind": "override", "edits": edits}]}},
            }
        )


def test_a_plugin_instance_belongs_to_one_engine(catalog: Catalog) -> None:
    plugin = WhatIfPlugin()
    CalcEngine(catalog, plugins=[plugin])
    with pytest.raises(PluginError):
        CalcEngine(catalog, plugins=[plugin])


def test_discovered_through_the_entry_point() -> None:
    found = discover_plugins()
    assert [type(p) for p in found] == [WhatIfPlugin]


# --- Composition with another plugin ----------------------------------------------------------


class Usd(Model):
    rate: Decimal


class UsdPlan(TransformPlan):
    def __init__(self, rate: Decimal, env: Mapping[str, LType]) -> None:
        self.rate = rate
        self.env = {**env, "usd": FLOAT}
        self.canonical = [{"kind": "usd", "rate": str(rate)}]

    def apply(self, lf: pl.LazyFrame) -> pl.LazyFrame:
        return lf.with_columns((pl.col("price").cast(pl.Float64) * float(self.rate)).alias("usd"))

    def apply_reference(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rate = float(self.rate)
        return [
            {**r, "usd": None if r["price"] is None else float(r["price"]) * rate} for r in rows
        ]


class UsdBound(Bound):
    def __init__(self, rate: Decimal) -> None:
        self.rate = rate

    @property
    def identity(self) -> Any:
        return str(self.rate)

    def plan(self, env: Mapping[str, LType], frame: pl.LazyFrame, pc: PlanContext) -> UsdPlan:
        return UsdPlan(self.rate, env)


class UsdPlugin(Plugin):
    name = "usd"

    def register(self, registry: Registry) -> None:
        registry.add_transform(TransformDef("usd", Usd, lambda block, bc: UsdBound(block.rate)))
        registry.add_function(
            FunctionDef(
                "half",
                typecheck=lambda args: FLOAT,
                polars=lambda x: x.cast(pl.Float64) / 2,
                reference=lambda x: float(x) / 2,
            )
        )


def test_whatif_sees_columns_and_functions_of_earlier_plugins(catalog: Catalog) -> None:
    engine = CalcEngine(catalog, plugins=[UsdPlugin(), WhatIfPlugin(store=InMemoryScenarioStore())])
    request = {
        "dataset": "pos",
        "extensions": {
            "usd": {"rate": "2"},
            "whatif": {
                "steps": [
                    {"kind": "formula", "name": "usd_half", "expr": "half(usd)"},
                    {
                        "kind": "shock",
                        "column": "qty",
                        "op": "mul",
                        "value": 2,
                        "where": "half(usd) > 100",
                    },
                ]
            },
        },
        "query": {"select": ["id", "qty", "usd_half"], "sort": [{"by": "id"}]},
    }
    rows = engine.run(request).frame.to_dicts()
    assert rows[:2] == [
        {"id": 1, "qty": 20, "usd_half": 101.25},
        {"id": 2, "qty": 20, "usd_half": 99.5},
    ]
    assert verify(engine, request).ok
    meta = engine.run(request).meta
    assert set(meta.extensions) == {"usd", "whatif"}


# --- Package boundary -------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {"callscore_calc", "callscore_calc.ext", "callscore_calc.integrations.fastapi"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def test_plugin_uses_only_the_public_api() -> None:
    for path in (ROOT / "calc-whatif" / "src").rglob("*.py"):
        used = {m for m in _imports(path) if m.split(".")[0] == "callscore_calc"}
        assert used <= ALLOWED, (path, used - ALLOWED)


def test_core_does_not_know_the_plugin() -> None:
    for path in (ROOT / "calc" / "src").rglob("*.py"):
        assert not any(m.startswith("callscore_calc_whatif") for m in _imports(path)), path
