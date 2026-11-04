"""The plugin API, exercised by a small FX plugin: a function, an aggregate, a transform, an
operation and a route."""

import statistics
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from pydantic import Field

from callscore_calc import (
    AggregateDef,
    BindContext,
    Bound,
    CalcContext,
    CalcEngine,
    CalcRequest,
    CalcResult,
    Catalog,
    FunctionDef,
    Kernel,
    OperationDef,
    PlanContext,
    Plugin,
    PluginError,
    Registry,
    SpecError,
    TransformDef,
    TransformPlan,
)
from callscore_calc.ext import FLOAT, Kind, LType, Model, join_path
from callscore_calc.integrations.fastapi import RouterKit, create_router
from callscore_calc.testing import assert_matches_reference, frames
from callscore_calc.verify import verify


def _numeric(types: Any) -> None:
    for t in types:
        if t.kind not in (Kind.INT, Kind.FLOAT, Kind.DECIMAL, Kind.NULL):
            raise ValueError(f"needs numbers, got {t}")


def clip_reference(x: float, lo: float, hi: float) -> float:
    # As Polars does it, which matters when lo > hi (the property test found this).
    return lo if x < lo else hi if x > hi else x


def _clip_type(args: Any) -> LType:
    _numeric(args)
    return FLOAT


CLIP = FunctionDef(
    name="clip",
    typecheck=_clip_type,
    polars=lambda x, lo, hi: x.cast(pl.Float64).clip(lo.cast(pl.Float64), hi.cast(pl.Float64)),
    reference=lambda x, lo, hi: clip_reference(float(x), float(lo), float(hi)),
    min_args=3,
    max_args=3,
)


def _median_type(arg: LType) -> LType:
    _numeric([arg])
    return FLOAT


MEDIAN = AggregateDef(
    name="median",
    typecheck=_median_type,
    polars=lambda x: x.cast(pl.Float64).median(),
    reference=lambda values: statistics.median(float(v) for v in values),
)


class Fx(Model):
    rate: Decimal = Field(gt=0)
    column: str = "price"


class FxPlan(TransformPlan):
    def __init__(self, block: Fx, env: Mapping[str, LType]) -> None:
        self.block = block
        self.env = {**env, "usd": FLOAT}
        self.canonical = [{"kind": "fx", "rate": str(block.rate), "column": block.column}]

    def apply(self, lf: pl.LazyFrame) -> pl.LazyFrame:
        usd = pl.col(self.block.column).cast(pl.Float64) * float(self.block.rate)
        return lf.with_columns(usd.alias("usd"))

    def apply_reference(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rate = float(self.block.rate)
        column = self.block.column
        return [{**r, "usd": None if r[column] is None else float(r[column]) * rate} for r in rows]

    def meta(self) -> dict[str, Any]:
        return {"rate": str(self.block.rate)}

    def explain(self) -> dict[str, Any]:
        return {"usd": f"{self.block.column} * {self.block.rate}"}


class FxBound(Bound):
    def __init__(self, block: Fx, path: str) -> None:
        self.block = block
        self.path = path

    @property
    def identity(self) -> Any:
        return self.block

    def plan(self, env: Mapping[str, LType], frame: pl.LazyFrame, pc: PlanContext) -> FxPlan:
        if self.block.column not in env:
            raise SpecError(
                f"unknown column: {self.block.column}",
                code="unknown_column",
                path=join_path(self.path, "column"),
            )
        return FxPlan(self.block, env)


class Ladder(Model):
    dataset: str
    rates: tuple[Decimal, ...]
    query: dict[str, Any] = {}


def run_ladder(kernel: Kernel, req: Ladder, ctx: CalcContext) -> CalcResult:
    """The same query at several FX rates, stacked with a ``rate`` column."""
    frames = []
    with kernel.slot():
        base = kernel.parse(CalcRequest, {"dataset": req.dataset, "query": req.query})
        deadline = kernel.deadline(base.options)
        for rate in req.rates:
            view = kernel.resolve_view(base.dataset, {"fx": {"rate": rate}}, ctx, deadline=deadline)
            result = kernel.query(view, base.query, base.options, ctx, deadline=deadline)
            frames.append(result.frame.with_columns(pl.lit(float(rate)).alias("rate")))
    stacked = pl.concat(frames)
    meta = result.meta.model_copy(update={"rows": stacked.height, "total_rows": stacked.height})
    return kernel.finish(CalcResult(stacked, meta), ctx)


def fx_routes(router: Any, kit: RouterKit) -> None:
    @router.get("/fx/functions")
    def functions() -> list[str]:
        return sorted(kit.engine.registry.functions)


class FxPlugin(Plugin):
    name = "fx"
    version = "1"

    def __init__(self) -> None:
        self.attached: list[CalcEngine] = []

    def register(self, registry: Registry) -> None:
        registry.add_function(CLIP)
        registry.add_aggregate(MEDIAN)
        registry.add_transform(TransformDef("fx", Fx, lambda block, bc: FxBound(block, bc.path)))
        registry.add_operation(OperationDef("ladder", Ladder, run_ladder))
        registry.add_routes(fx_routes)

    def attach(self, engine: CalcEngine) -> None:
        self.attached.append(engine)


@pytest.fixture
def fx_engine(catalog: Catalog) -> CalcEngine:
    return CalcEngine(catalog, plugins=[FxPlugin()])


def test_attach_and_lookup(fx_engine: CalcEngine) -> None:
    plugin = fx_engine.plugin(FxPlugin)
    assert plugin.attached == [fx_engine] and fx_engine.plugin("fx") is plugin
    assert fx_engine.versions["plugin:fx"] == "1"
    with pytest.raises(Exception, match="not installed"):
        fx_engine.plugin("whatif")


def test_plugin_function(fx_engine: CalcEngine) -> None:
    request = {
        "dataset": "pos",
        "query": {
            "select": ["id", "c"],
            "derive": [{"name": "c", "expr": "clip(price, 20, 100)"}],
            "filter": "clip(qty, 0, 25) < 25",
            "sort": [{"by": "id"}],
        },
    }
    rows = fx_engine.run(request).frame.to_dicts()
    assert rows == [{"id": 1, "c": 100.0}, {"id": 2, "c": 99.5}]
    assert verify(fx_engine, request).ok


@pytest.mark.parametrize(
    ("expr", "code"),
    [
        ("clip(price, 1)", "type_mismatch"),
        ("clip(desk, 1, 2)", "type_mismatch"),
        ("nope(price)", "unknown_function"),
    ],
)
def test_plugin_function_errors(fx_engine: CalcEngine, expr: str, code: str) -> None:
    with pytest.raises(SpecError) as info:
        fx_engine.run({"dataset": "pos", "query": {"filter": f"{expr} > 0"}})
    assert info.value.code == code and info.value.path == "/query/filter/left"


def test_functions_are_per_engine(engine: CalcEngine) -> None:
    with pytest.raises(SpecError) as info:
        engine.run({"dataset": "pos", "query": {"filter": "clip(price, 1, 2) > 0"}})
    assert info.value.code == "unknown_function"


def test_plugin_aggregate_with_rollup_and_pivot(fx_engine: CalcEngine) -> None:
    request = {
        "dataset": "pos",
        "query": {
            "group_by": ["sector"],
            "rollup": True,
            "measures": [{"name": "med", "fn": "median", "of": "qty", "where": "qty > 10"}],
        },
    }
    rows = {r["sector"]: r["med"] for r in fx_engine.run(request).frame.to_dicts()}
    assert rows == {"Energy": 40.0, "Fin": 40.0, "Tech": 40.0, None: 40.0}
    assert verify(fx_engine, request).ok
    pivot = {
        "dataset": "pos",
        "query": {
            "group_by": ["sector"],
            "measures": [{"name": "med", "fn": "median", "of": "price"}],
            "pivot": {"on": ["desk"], "totals": True},
        },
    }
    assert verify(fx_engine, pivot).ok


def test_plugin_aggregate_errors(fx_engine: CalcEngine) -> None:
    with pytest.raises(SpecError) as info:
        fx_engine.run(
            {"dataset": "pos", "query": {"measures": [{"name": "m", "fn": "mode", "of": "qty"}]}}
        )
    assert info.value.code == "unknown_aggregate" and info.value.path == "/query/measures/0/fn"
    with pytest.raises(SpecError) as info:
        fx_engine.run(
            {"dataset": "pos", "query": {"measures": [{"name": "m", "fn": "median", "of": "desk"}]}}
        )
    assert info.value.code == "type_mismatch"


def test_transform(fx_engine: CalcEngine) -> None:
    request = {
        "dataset": "pos",
        "extensions": {"fx": {"rate": "2"}},
        "query": {"select": ["id", "usd"], "filter": "id <= 2", "sort": [{"by": "id"}]},
    }
    result = fx_engine.run(request)
    assert result.frame.to_dicts() == [{"id": 1, "usd": 202.5}, {"id": 2, "usd": 199.0}]
    assert result.meta.extensions == {"fx": {"rate": "2"}}
    assert verify(fx_engine, request).ok
    again = fx_engine.run(request)
    assert again.meta.cached and again.meta.fingerprint == result.meta.fingerprint
    other = fx_engine.run({**request, "extensions": {"fx": {"rate": "3"}}})
    assert other.meta.fingerprint != result.meta.fingerprint
    info = fx_engine.explain(request)
    assert info["steps"] == [{"kind": "fx", "rate": "2", "column": "price"}]
    assert info["extensions"] == {"fx": {"usd": "price * 2"}}


@pytest.mark.parametrize(
    ("extensions", "code", "path"),
    [
        ({"fx": {"rate": 0}}, "invalid_request", "/extensions/fx/rate"),
        ({"fx": {"rate": 1, "column": "nope"}}, "unknown_column", "/extensions/fx/column"),
        ({"fxx": {}}, "unknown_extension", "/extensions/fxx"),
    ],
)
def test_transform_errors(
    fx_engine: CalcEngine, extensions: dict[str, Any], code: str, path: str
) -> None:
    with pytest.raises(SpecError) as info:
        fx_engine.run({"dataset": "pos", "extensions": extensions})
    assert (info.value.code, info.value.path) == (code, path)


def test_operation(fx_engine: CalcEngine) -> None:
    result = fx_engine.call(
        "ladder",
        {
            "dataset": "pos",
            "rates": ["1", "2"],
            "query": {"measures": [{"name": "usd", "fn": "sum", "of": "usd"}]},
        },
    )
    assert result.frame.to_dicts() == [
        {"usd": 335.85, "rate": 1.0},
        {"usd": 671.7, "rate": 2.0},
    ]
    with pytest.raises(SpecError):
        fx_engine.call("ladder", {"dataset": "pos"})


def test_operation_and_routes_over_http(fx_engine: CalcEngine) -> None:
    app = FastAPI()
    app.include_router(create_router(fx_engine, prefix="/calc"))
    client = TestClient(app)
    assert client.get("/calc/operations").json() == [{"name": "ladder", "description": ""}]
    response = client.post(
        "/calc/operations/ladder",
        json={
            "dataset": "pos",
            "rates": [1],
            "query": {"measures": [{"name": "n", "fn": "count_rows"}]},
        },
    )
    assert response.json()["rows"] == [{"n": 6, "rate": 1.0}]
    assert client.get("/calc/fx/functions").json() == ["clip"]


@given(data=st.data())
def test_plugin_computations_match_reference(data: st.DataObject) -> None:
    catalog = Catalog()
    catalog.register_frame("t", data.draw(frames()), key_columns=["k"])
    engine = CalcEngine(catalog, plugins=[FxPlugin()])
    request = {
        "dataset": "t",
        "extensions": {"fx": {"rate": data.draw(st.sampled_from(["0.5", "1.25"])), "column": "d"}},
        "query": {
            "derive": [{"name": "c", "expr": "clip(f, -5, i)"}],
            "group_by": data.draw(st.sampled_from([[], ["g1"]])),
            "measures": [
                {"name": "m", "fn": "median", "of": data.draw(st.sampled_from(["usd", "c", "d"]))},
                {"name": "s", "fn": "sum", "of": "usd"},
            ],
        },
    }
    assert_matches_reference(engine, request)


class Named(Plugin):
    def __init__(self, name: str, register: Any) -> None:
        self.name = name
        self._register = register

    def register(self, registry: Registry) -> None:
        self._register(registry)


@pytest.mark.parametrize(
    ("plugins", "message"),
    [
        (lambda: [FxPlugin(), FxPlugin()], "installed twice"),
        (lambda: [FxPlugin(), Named("b", lambda r: r.add_function(CLIP))], "already registered"),
        (
            lambda: [Named("b", lambda r: r.add_function(FunctionDef("abs", *CLIP_IMPL)))],
            "built in",
        ),
        (
            lambda: [Named("b", lambda r: r.add_aggregate(AggregateDef("sum", *MEDIAN_IMPL)))],
            "built in",
        ),
        (
            lambda: [Named("b", lambda r: r.add_function(FunctionDef("Bad", *CLIP_IMPL)))],
            "invalid function name",
        ),
        (lambda: [Named("", lambda r: None)], "has no name"),
    ],
)
def test_registry_conflicts(catalog: Catalog, plugins: Any, message: str) -> None:
    with pytest.raises(PluginError, match=message):
        CalcEngine(catalog, plugins=plugins())


CLIP_IMPL = (CLIP.typecheck, CLIP.polars, CLIP.reference)
MEDIAN_IMPL = (MEDIAN.typecheck, MEDIAN.polars, MEDIAN.reference)


def test_bind_context_is_passed(catalog: Catalog) -> None:
    seen: list[BindContext] = []

    def bind(block: Fx, bc: BindContext) -> FxBound:
        seen.append(bc)
        return FxBound(block, bc.path)

    class Spy(Plugin):
        name = "spy"

        def register(self, registry: Registry) -> None:
            registry.add_transform(TransformDef("fx", Fx, bind))

    engine = CalcEngine(catalog, plugins=[Spy()])
    ctx = CalcContext(principal="ana")
    engine.run({"dataset": "pos", "extensions": {"fx": {"rate": 1}}}, ctx)
    assert seen[0].ctx is ctx and seen[0].dataset.id == "pos" and seen[0].path == "/extensions/fx"
