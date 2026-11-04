"""The ``whatif`` plugin: a dataset transform plus saved scenarios, routes and grid edits."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from importlib.metadata import version as package_version
from typing import TYPE_CHECKING, Any

import polars as pl

from callscore_calc import (
    BindContext,
    Bound,
    CalcContext,
    CalcEngine,
    CalcError,
    DatasetRef,
    LimitExceeded,
    PlanContext,
    Plugin,
    PluginError,
    Registry,
    SpecError,
    TransformDef,
    TransformPlan,
    VersionConflict,
    to_formula,
)
from callscore_calc.ext import LType, Scalar, join_path, json_value

from .planner import LabeledStep, LogicalMutations, WhatIfLimits, effective_steps, plan_mutations
from .polars import apply_mutations, edit_keys_frame
from .reference import apply_mutations as reference_mutations
from .spec import ScenarioStep, WhatIf

if TYPE_CHECKING:
    from callscore_calc.integrations.fastapi import RouterKit

    from .scenario.manager import ScenarioManager
    from .scenario.model import LogEntry, Scenario
    from .scenario.store import ScenarioStore

NAME = "whatif"


class WhatIfPlugin(Plugin):
    """What-if analysis: override cells, shock columns and add formula columns, ad hoc (the
    ``extensions.whatif.steps`` of a request) or saved as a scenario.

    ``store`` enables saved scenarios (:attr:`scenarios`); without one, only ad-hoc steps work.
    """

    name = NAME
    version = package_version("callscore-calc-whatif")

    def __init__(
        self, store: ScenarioStore | None = None, *, limits: WhatIfLimits | None = None
    ) -> None:
        self.store = store
        self.limits = limits or WhatIfLimits()
        self._engine: CalcEngine | None = None
        self._scenarios: ScenarioManager | None = None

    def register(self, registry: Registry) -> None:
        registry.add_transform(
            TransformDef(
                name=NAME,
                model=WhatIf,
                bind=self._bind,
                description="Overrides, shocks and formula columns, optionally from a saved "
                "scenario.",
            )
        )
        registry.add_routes(self._routes)

    def attach(self, engine: CalcEngine) -> None:
        if self._engine is not None:
            raise PluginError("a WhatIfPlugin can only be installed in one engine")
        self._engine = engine
        if self.store is not None:
            from .scenario.manager import ScenarioManager

            self._scenarios = ScenarioManager(self, self.store)

    @property
    def engine(self) -> CalcEngine:
        if self._engine is None:
            raise CalcError("the whatif plugin is not installed in an engine", code="not_attached")
        return self._engine

    @property
    def scenarios(self) -> ScenarioManager:
        if self._scenarios is None:
            raise CalcError(
                "this engine has no scenario store", code="no_scenario_store", detail={}
            )
        return self._scenarios

    # --- Transform --------------------------------------------------------------------------

    def _bind(self, block: WhatIf, bc: BindContext) -> WhatIfBound:
        if len(block.steps) > self.limits.max_steps:
            raise LimitExceeded(
                f"more than {self.limits.max_steps} what-if steps", path=join_path(bc.path, "steps")
            )
        scenario = version = head = None
        labeled: list[LabeledStep] = []
        if block.scenario is not None:
            spath = join_path(bc.path, "scenario")
            scenario = self.scenarios.get(block.scenario.id, ctx=bc.ctx)
            if scenario.dataset.id != bc.dataset.id:
                raise SpecError(
                    f"scenario {scenario.id} belongs to dataset {scenario.dataset.id}",
                    code="dataset_mismatch",
                    path=spath,
                )
            if bc.dataset.version is not None and bc.dataset.version != scenario.dataset.version:
                raise VersionConflict(
                    f"scenario {scenario.id} is pinned to version {scenario.dataset.version}",
                    path="/dataset/version",
                )
            version = scenario.version if block.scenario.version is None else block.scenario.version
            if version > scenario.version:
                raise SpecError(
                    f"scenario {scenario.id} has no version {version}",
                    code="invalid_version",
                    path=join_path(spath, "version"),
                )
            entries = self._entries(bc, scenario, version)
            head = entries[-1].hash if entries else scenario.genesis
            labeled = [LabeledStep(join_path(spath, "steps", e.seq), e.step) for e in entries]
        extra = [LabeledStep(join_path(bc.path, "steps", i), s) for i, s in enumerate(block.steps)]
        return WhatIfBound(
            self, bc.path, scenario, version, head, labeled, extra, block.strict_edits
        )

    def _entries(self, bc: BindContext, scenario: Scenario, version: int) -> list[LogEntry]:
        key = f"whatif:entries:{scenario.id}:{version}"
        cached = bc.kernel.plans.get(key)
        if cached is None:
            cached = self.scenarios.store.entries(scenario.id, upto=version)
            bc.kernel.plans.put(key, cached, 512 * (len(cached) + 1))
        return list(cached)

    def validate_steps(
        self,
        dataset: DatasetRef,
        steps: Sequence[tuple[str, ScenarioStep]],
        ctx: CalcContext,
    ) -> LogicalMutations:
        """Check a full scenario log against its dataset (used before every append): the steps
        must type-check and every override must match a row the caller can see."""
        kernel = self.engine.kernel
        ds = kernel.catalog.get(dataset.id, dataset.version)
        labeled = [LabeledStep(label, step) for label, step in steps]
        bound = WhatIfBound(self, "", None, None, None, [], labeled, True)
        with kernel.slot():
            view = kernel.plan_view(ds, [(NAME, bound)], ctx, deadline=None)
        plan = view.transform(NAME)
        assert isinstance(plan, WhatIfPlan)
        return plan.mutations

    # --- Routes -----------------------------------------------------------------------------

    def _routes(self, router: Any, kit: RouterKit) -> None:
        from .routes import add_routes

        add_routes(router, kit, self)


class WhatIfBound(Bound):
    """A ``whatif`` block resolved for one request: the saved scenario's log plus extra steps."""

    def __init__(
        self,
        plugin: WhatIfPlugin,
        path: str,
        scenario: Scenario | None,
        version: int | None,
        head: str | None,
        scenario_steps: list[LabeledStep],
        extra_steps: list[LabeledStep],
        strict: bool,
    ) -> None:
        self.plugin = plugin
        self.path = path
        self.scenario = scenario
        self.version = version
        self.head = head
        self.scenario_steps = scenario_steps
        self.extra_steps = extra_steps
        self.strict = strict
        self.pinned_version = None if scenario is None else scenario.dataset.version

    @property
    def identity(self) -> Any:
        # A saved scenario is identified by (id, version, head); only the extra steps count.
        return {
            "path": self.path,
            "scenario": None
            if self.scenario is None
            else [self.scenario.id, self.version, self.head],
            "steps": [[s.label, s.step] for s in self.extra_steps],
            "strict": self.strict,
            "limits": [self.plugin.limits.max_edits],
        }

    def plan(self, env: Mapping[str, LType], frame: pl.LazyFrame, pc: PlanContext) -> WhatIfPlan:
        mutations = plan_mutations(
            effective_steps(self.scenario_steps + self.extra_steps),
            pc.schema,
            pc.numeric,
            self.plugin.limits,
            expr_limits=pc.limits,
            env=env,
            functions=pc.registry.functions,
        )
        unmatched = _unmatched(frame, mutations, pc)
        if self.strict and unmatched:
            raise SpecError(
                f"{len(unmatched)} edit(s) match no row",
                code="unmatched_edits",
                detail={"keys": unmatched[:20]},
            )
        scenario = None
        if self.scenario is not None:
            scenario = {"id": self.scenario.id, "version": self.version}
        return WhatIfPlan(mutations, unmatched, scenario, self.head)


class WhatIfPlan(TransformPlan):
    def __init__(
        self,
        mutations: LogicalMutations,
        unmatched: list[dict[str, Scalar]],
        scenario: dict[str, Any] | None,
        head: str | None,
    ) -> None:
        self.mutations = mutations
        self.unmatched = unmatched
        self.scenario = scenario
        self.head = head
        self.env = dict(mutations.env)
        self.canonical = list(mutations.canonical)

    def apply(self, lf: pl.LazyFrame) -> pl.LazyFrame:
        return apply_mutations(lf, self.mutations)

    def apply_reference(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return reference_mutations(rows, self.mutations)

    def meta(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "scenario_head": self.head,
            "unmatched_edits": self.unmatched[:100],
        }

    def explain(self) -> dict[str, Any]:
        m = self.mutations
        return {
            "scenario": None if self.scenario is None else {**self.scenario, "head": self.head},
            "changed_by": m.lineage,
            "formulas": {f.name: to_formula(f.typed.node) for f in m.formulas},
        }

    def size(self) -> int:
        return 1024 + 64 * len(self.mutations.edit_keys)


def _unmatched(
    frame: pl.LazyFrame, mutations: LogicalMutations, pc: PlanContext
) -> list[dict[str, Scalar]]:
    if not mutations.edit_keys:
        return []
    keys = list(mutations.key_columns)
    check = edit_keys_frame(mutations).lazy().join(frame.select(keys), on=keys, how="anti")
    missing = pc.collect(check)
    types = [mutations.base_env[k] for k in keys]
    return [
        {k: json_value(v, t) for k, v, t in zip(keys, row, types, strict=True)}
        for row in missing.iter_rows()
    ]
