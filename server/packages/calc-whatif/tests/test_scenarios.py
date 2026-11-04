from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import fakeredis
import polars as pl
import pytest

from callscore_calc import (
    CalcContext,
    CalcEngine,
    Catalog,
    EngineConfig,
    Forbidden,
    SpecError,
    VersionConflict,
)
from callscore_calc.verify import verify
from callscore_calc_whatif import (
    InMemoryScenarioStore,
    ScenarioManager,
    ScenarioNotFound,
    ScenarioStore,
    WhatIfPlugin,
)
from callscore_calc_whatif.scenario.redis_store import RedisScenarioStore

# Requests here mostly use the version 1 shape (top-level "scenario" and "what_if"), which the
# engine upgrades to "extensions.whatif"; test_plugin.py covers the version 2 shape.


@pytest.fixture(params=["memory", "redis"])
def store(request: pytest.FixtureRequest) -> Iterator[ScenarioStore]:
    if request.param == "memory":
        yield InMemoryScenarioStore()
    else:
        yield RedisScenarioStore(fakeredis.FakeRedis())


@pytest.fixture
def engine(catalog: Catalog, store: ScenarioStore) -> CalcEngine:
    return CalcEngine(catalog, plugins=[WhatIfPlugin(store)])


def scenarios(engine: CalcEngine) -> ScenarioManager:
    return engine.plugin(WhatIfPlugin).scenarios


def prices(engine: CalcEngine, **extra: Any) -> dict[int, Any]:
    rows = engine.run({"dataset": "pos", **extra}).frame.to_dicts()
    return {r["id"]: r["price"] for r in rows}


def shock(value: Any, where: str | None = None) -> dict[str, Any]:
    step = {"kind": "shock", "column": "price", "op": "pct", "value": value}
    if where:
        step["where"] = where
    return step


def test_create_append_and_run(engine: CalcEngine) -> None:
    scenario = scenarios(engine).create("pos", "tech +10%", ctx=CalcContext(principal="ana"))
    assert scenario.version == 0 and scenario.dataset.version == "v1" and scenario.owner == "ana"
    scenario = scenarios(engine).append(
        scenario.id, [shock(10, "sector == 'Tech'")], expected_version=0
    )
    assert scenario.version == 1
    shocked = prices(engine, scenario=scenario.id)
    assert shocked[1] == Decimal("111.38")  # 101.25 * 1.10 = 111.375 -> half-even
    assert shocked[2] == Decimal("99.50")
    assert prices(engine, scenario={"id": scenario.id, "version": 0})[1] == Decimal("101.25")


def test_stale_version_conflicts(engine: CalcEngine) -> None:
    scenario = scenarios(engine).create("pos", "s")
    scenarios(engine).append(scenario.id, [shock(1)], expected_version=0)
    with pytest.raises(VersionConflict) as info:
        scenarios(engine).append(scenario.id, [shock(2)], expected_version=0)
    assert info.value.status == 409 and info.value.detail["version"] == 1


def test_idempotent_retry(engine: CalcEngine) -> None:
    scenario = scenarios(engine).create("pos", "s")
    first = scenarios(engine).append(
        scenario.id, [shock(1)], expected_version=0, client_op_id="op-1"
    )
    again = scenarios(engine).append(
        scenario.id, [shock(1)], expected_version=0, client_op_id="op-1"
    )
    assert first.version == again.version == 1
    assert len(scenarios(engine).log(scenario.id)) == 1


def test_formulas_follow_later_overrides(engine: CalcEngine) -> None:
    scenario = scenarios(engine).create("pos", "s")
    scenarios(engine).append(
        scenario.id,
        [{"kind": "formula", "name": "mv", "expr": "price * qty"}],
        expected_version=0,
    )
    scenarios(engine).append(
        scenario.id,
        [{"kind": "override", "edits": [{"key": {"id": 1}, "column": "price", "value": "200"}]}],
        expected_version=1,
    )
    rows = engine.run({"dataset": "pos", "scenario": scenario.id, "query": {"filter": "id == 1"}})
    assert rows.frame["mv"].to_list() == [Decimal("2000.00")]


def test_shock_predicates_see_formula_values_at_that_point(engine: CalcEngine) -> None:
    scenario = scenarios(engine).create("pos", "s")
    scenarios(engine).append(
        scenario.id,
        [
            {"kind": "formula", "name": "mv", "expr": "price * qty"},
            shock(100, "mv > 1000"),  # only row 1 (1012.50) and rows 2, 3, 4 qualify
        ],
        expected_version=0,
    )
    shocked = prices(engine, scenario=scenario.id)
    assert shocked[1] == Decimal("202.50") and shocked[6] == Decimal("10.00")


def test_disable_undoes_a_step(engine: CalcEngine) -> None:
    scenario = scenarios(engine).create("pos", "s")
    scenarios(engine).append(scenario.id, [shock(10), shock(20)], expected_version=0)
    scenarios(engine).append(scenario.id, [{"kind": "disable", "seq": 1}], expected_version=2)
    assert prices(engine, scenario=scenario.id)[2] == Decimal("119.40")  # only +20%
    with pytest.raises(SpecError) as info:
        scenarios(engine).append(scenario.id, [{"kind": "disable", "seq": 3}], expected_version=3)
    assert info.value.code == "invalid_disable"


def test_what_if_on_top_of_a_scenario(engine: CalcEngine) -> None:
    scenario = scenarios(engine).create("pos", "s")
    scenarios(engine).append(scenario.id, [shock(10)], expected_version=0)
    result = prices(
        engine, scenario=scenario.id, what_if=[{"kind": "disable", "seq": 1}, shock(-50)]
    )
    assert result[2] == Decimal("49.75")
    assert len(scenarios(engine).log(scenario.id)) == 1  # the what-if was not saved


def test_fork_is_isolated(engine: CalcEngine) -> None:
    parent = scenarios(engine).create("pos", "parent")
    scenarios(engine).append(parent.id, [shock(10), shock(20)], expected_version=0)
    scenarios(engine).append(parent.id, [{"kind": "disable", "seq": 2}], expected_version=2)
    child = scenarios(engine).fork(parent.id, name="child", at_version=3)
    assert child.version == 1 and child.forked_from is not None
    assert child.forked_from.version == 3
    scenarios(engine).append(parent.id, [shock(50)], expected_version=3)
    assert prices(engine, scenario=child.id)[2] == Decimal("109.45")
    assert scenarios(engine).verify(child.id)


def test_hash_chain_detects_tampering(engine: CalcEngine, store: ScenarioStore) -> None:
    scenario = scenarios(engine).create("pos", "s")
    scenarios(engine).append(scenario.id, [shock(10)], expected_version=0, note="first")
    scenarios(engine).append(scenario.id, [shock(5)], expected_version=1)
    assert scenarios(engine).verify(scenario.id)
    entries = store.entries(scenario.id)
    forged = entries[0].model_copy(update={"note": "edited later"})
    if isinstance(store, InMemoryScenarioStore):
        store._records[scenario.id].entries[0] = forged
    else:
        assert isinstance(store, RedisScenarioStore)
        _, log, _ = store._keys(scenario.id)
        store._redis.lset(log, 0, forged.model_dump_json())
    assert not scenarios(engine).verify(scenario.id)


def test_validation_happens_before_append(engine: CalcEngine) -> None:
    scenario = scenarios(engine).create("pos", "s")
    bad_steps: list[dict[str, Any]] = [
        {"kind": "shock", "column": "desk", "op": "add", "value": 1},
        {"kind": "shock", "column": "id", "op": "add", "value": 1},
        {"kind": "shock", "column": "qty", "op": "pct", "value": 5},
        {"kind": "override", "edits": [{"key": {"id": 999}, "column": "qty", "value": 1}]},
        {"kind": "override", "edits": [{"key": {"id": 1}, "column": "price", "value": "1.001"}]},
        {"kind": "formula", "name": "qty", "expr": "1"},
        {"kind": "formula", "name": "a", "expr": "b + 1"},
    ]
    codes = []
    for step in bad_steps:
        with pytest.raises(SpecError) as info:
            scenarios(engine).append(scenario.id, [step], expected_version=0)
        codes.append(info.value.code)
    assert codes == [
        "type_mismatch",
        "not_editable",
        "needs_rounding",
        "unmatched_edits",
        "invalid_value",
        "name_conflict",
        "unknown_column",
    ]
    assert scenarios(engine).get(scenario.id).version == 0


def test_formula_cycles_are_rejected(engine: CalcEngine) -> None:
    scenario = scenarios(engine).create("pos", "s")
    with pytest.raises(SpecError) as info:
        scenarios(engine).append(
            scenario.id,
            [
                {"kind": "formula", "name": "a", "expr": "b + 1"},
                {"kind": "formula", "name": "b", "expr": "a + 1"},
            ],
            expected_version=0,
        )
    assert info.value.code == "formula_cycle"


def test_integer_shock_with_rounding(engine: CalcEngine) -> None:
    rows = engine.run(
        {
            "dataset": "pos",
            "what_if": [{"kind": "shock", "column": "qty", "op": "pct", "value": 5, "round": True}],
            "query": {"select": ["id", "qty"], "sort": [{"by": "id"}]},
        }
    ).frame
    assert rows["qty"].to_list() == [10, 21, 32, 42, 52, 63]  # 10.5 -> 10, 31.5 -> 32


def test_scenarios_pin_their_dataset_version(catalog: Catalog, store: ScenarioStore) -> None:
    engine = CalcEngine(catalog, plugins=[WhatIfPlugin(store)])
    scenario = scenarios(engine).create("pos", "s")
    catalog.register_frame(
        "pos", pl.DataFrame({"id": [1], "price": [1.0]}), key_columns=["id"], version="v2"
    )
    assert engine.run({"dataset": "pos"}).meta.dataset.version == "v2"
    result = engine.run({"dataset": "pos", "scenario": scenario.id})
    assert result.meta.dataset.version == "v1"
    with pytest.raises(VersionConflict):
        engine.run({"dataset": {"id": "pos", "version": "v2"}, "scenario": scenario.id})


def test_list_delete_and_not_found(engine: CalcEngine) -> None:
    a = scenarios(engine).create("pos", "a")
    scenarios(engine).create("pos", "b")
    assert [s.name for s in scenarios(engine).list(dataset="pos")] == ["a", "b"]
    scenarios(engine).delete(a.id)
    assert [s.name for s in scenarios(engine).list()] == ["b"]
    with pytest.raises(ScenarioNotFound):
        engine.run({"dataset": "pos", "scenario": a.id})
    with pytest.raises(ScenarioNotFound):
        scenarios(engine).get("missing")


def test_authorization_hook(catalog: Catalog, store: ScenarioStore) -> None:
    def authorize(ctx: CalcContext, action: str, scenario: Any) -> None:
        if action != "scenario.read" and scenario is not None and scenario.owner != ctx.principal:
            raise Forbidden("only the owner may change this scenario")

    engine = CalcEngine(catalog, EngineConfig(authorize=authorize), plugins=[WhatIfPlugin(store)])
    scenario = scenarios(engine).create("pos", "s", ctx=CalcContext(principal="ana"))
    with pytest.raises(Forbidden):
        scenarios(engine).append(
            scenario.id, [shock(1)], expected_version=0, ctx=CalcContext(principal="bob")
        )
    scenarios(engine).append(
        scenario.id, [shock(1)], expected_version=0, ctx=CalcContext(principal="ana")
    )


def test_composite_keys(store: ScenarioStore) -> None:
    frame = pl.DataFrame(
        {"book": ["A", "A", "B"], "leg": [1, 2, 1], "notional": [100.0, 200.0, 300.0]}
    )
    catalog = Catalog()
    catalog.register_frame("legs", frame, key_columns=["book", "leg"])
    engine = CalcEngine(catalog, plugins=[WhatIfPlugin(store)])
    request = {
        "dataset": "legs",
        "what_if": [
            {
                "kind": "override",
                "edits": [
                    {"key": {"book": "A", "leg": 2}, "column": "notional", "value": 250},
                    {"key": {"book": "B", "leg": 1}, "column": "notional", "value": None},
                ],
            }
        ],
        "query": {"sort": [{"by": "book"}, {"by": "leg"}]},
    }
    assert engine.run(request).frame["notional"].to_list() == [100.0, 250.0, None]
    assert verify(engine, request).ok


def test_non_strict_edits_report_unmatched_keys(engine: CalcEngine) -> None:
    result = engine.run(
        {
            "dataset": "pos",
            "what_if": [
                {"kind": "override", "edits": [{"key": {"id": 42}, "column": "qty", "value": 1}]}
            ],
            "options": {"strict_edits": False},
        }
    )
    assert result.meta.extensions["whatif"]["unmatched_edits"] == [{"id": 42}]
