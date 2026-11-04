"""Creating, editing, forking and auditing scenarios, with validation and authorization."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter, ValidationError

from callscore_calc import CalcContext, DatasetRef, LimitExceeded, SpecError, VersionConflict
from callscore_calc.ext import join_path, validation_error

from ..errors import ScenarioNotFound
from ..planner import LabeledStep, effective_steps
from ..spec import ScenarioRef, ScenarioStep
from .model import LogEntry, Scenario, genesis_hash, make_entries, now, verify_chain
from .store import ScenarioStore

if TYPE_CHECKING:
    from ..plugin import WhatIfPlugin

_STEPS: TypeAdapter[list[ScenarioStep]] = TypeAdapter(list[ScenarioStep])


def parse_steps(
    steps: Sequence[ScenarioStep | dict[str, Any]], path: str = "/steps"
) -> list[ScenarioStep]:
    try:
        return _STEPS.validate_python(list(steps))
    except ValidationError as exc:
        raise validation_error(exc, path) from None


class ScenarioManager:
    """Creating, editing, forking and auditing the scenarios of one :class:`WhatIfPlugin`."""

    def __init__(self, plugin: WhatIfPlugin, store: ScenarioStore) -> None:
        self.plugin = plugin
        self.store = store

    def create(
        self,
        dataset: str | DatasetRef,
        name: str,
        *,
        description: str | None = None,
        ctx: CalcContext | None = None,
    ) -> Scenario:
        """Start an empty scenario pinned to a dataset version (default: the latest)."""
        ctx = ctx or CalcContext()
        ref = DatasetRef.model_validate(dataset)
        pinned = self.plugin.engine.catalog.get(ref.id, ref.version)
        self.plugin.engine.authorize(ctx, "scenario.create", None)
        scenario_id = uuid.uuid4().hex
        at = now()
        ref = DatasetRef(id=pinned.id, version=pinned.version)
        genesis = genesis_hash(scenario_id, ref, None, at)
        scenario = Scenario(
            id=scenario_id,
            name=name,
            description=description,
            dataset=ref,
            owner=ctx.principal,
            genesis=genesis,
            head=genesis,
            created_at=at,
            updated_at=at,
        )
        self.store.create(scenario)
        return scenario

    def append(
        self,
        scenario_id: str,
        steps: Sequence[ScenarioStep | dict[str, Any]],
        *,
        expected_version: int,
        note: str | None = None,
        client_op_id: str | None = None,
        ctx: CalcContext | None = None,
    ) -> Scenario:
        """Validate and append steps. Fails with a 409 if someone else appended first."""
        ctx = ctx or CalcContext()
        parsed = parse_steps(steps)
        if not parsed:
            raise SpecError("nothing to append", code="invalid_spec", path="/steps")
        scenario = self.get(scenario_id, ctx=ctx, action="scenario.write")
        limits = self.plugin.limits
        if expected_version + len(parsed) > limits.max_scenario_steps:
            raise LimitExceeded(f"a scenario may hold at most {limits.max_scenario_steps} steps")
        if scenario.version != expected_version:
            if client_op_id is None:
                raise VersionConflict(
                    f"scenario {scenario_id} is at version {scenario.version}, "
                    f"not {expected_version}; reload it and retry",
                    detail={"version": scenario.version},
                )
            # A retry of an append that already succeeded returns the current state; the store
            # decides atomically, and raises the conflict otherwise.
            return self.store.append(
                scenario_id,
                [],
                expected_version=expected_version,
                head=scenario.head,
                updated_at=scenario.updated_at,
                client_op_id=client_op_id,
            )
        existing = self.store.entries(scenario_id, upto=expected_version)
        head = existing[-1].hash if existing else scenario.genesis
        # Validate the whole resulting log, so a step can't break the ones before or after it.
        self.plugin.validate_steps(
            scenario.dataset,
            [(join_path("/scenario", e.seq), e.step) for e in existing]
            + [(join_path("/steps", i), s) for i, s in enumerate(parsed)],
            ctx,
        )
        entries = make_entries(
            head,
            len(existing) + 1,
            parsed,
            author=ctx.principal,
            at=now(),
            note=note,
            client_op_id=client_op_id,
        )
        return self.store.append(
            scenario_id,
            entries,
            expected_version=expected_version,
            head=entries[-1].hash,
            updated_at=entries[-1].at,
            client_op_id=client_op_id,
        )

    def fork(
        self,
        scenario_id: str,
        *,
        name: str,
        at_version: int | None = None,
        description: str | None = None,
        ctx: CalcContext | None = None,
    ) -> Scenario:
        """Copy a scenario's effective steps (at a version) into a new, independent scenario."""
        ctx = ctx or CalcContext()
        parent = self.get(scenario_id, ctx=ctx)
        self.plugin.engine.authorize(ctx, "scenario.create", parent)
        version = parent.version if at_version is None else at_version
        if not 0 <= version <= parent.version:
            raise SpecError(
                f"scenario {scenario_id} has no version {version}", code="invalid_version"
            )
        entries = self.store.entries(scenario_id, upto=version)
        steps = [
            item.step
            for item in effective_steps([LabeledStep(str(e.seq), e.step) for e in entries])
        ]
        new_id = uuid.uuid4().hex
        at = now()
        origin = ScenarioRef(id=scenario_id, version=version)
        genesis = genesis_hash(new_id, parent.dataset, origin, at)
        copied = make_entries(
            genesis,
            1,
            steps,
            author=ctx.principal,
            at=at,
            note=f"forked from {scenario_id}@{version}",
            client_op_id=None,
        )
        scenario = Scenario(
            id=new_id,
            name=name,
            description=description,
            dataset=parent.dataset,
            owner=ctx.principal,
            forked_from=origin,
            version=len(copied),
            genesis=genesis,
            head=copied[-1].hash if copied else genesis,
            created_at=at,
            updated_at=at,
        )
        self.store.create(scenario, copied)
        return scenario

    def get(
        self, scenario_id: str, *, ctx: CalcContext | None = None, action: str = "scenario.read"
    ) -> Scenario:
        scenario = self.store.get(scenario_id)
        if scenario.deleted:
            raise ScenarioNotFound(f"scenario {scenario_id} was deleted")
        self.plugin.engine.authorize(ctx or CalcContext(), action, scenario)
        return scenario

    def log(
        self, scenario_id: str, *, upto: int | None = None, ctx: CalcContext | None = None
    ) -> list[LogEntry]:
        self.get(scenario_id, ctx=ctx)
        return self.store.entries(scenario_id, upto=upto)

    def list(self, *, dataset: str | None = None, ctx: CalcContext | None = None) -> list[Scenario]:
        ctx = ctx or CalcContext()
        visible = []
        for scenario in self.store.list(dataset_id=dataset):
            try:
                self.plugin.engine.authorize(ctx, "scenario.read", scenario)
            except Exception:
                continue
            visible.append(scenario)
        return visible

    def delete(self, scenario_id: str, *, ctx: CalcContext | None = None) -> None:
        self.get(scenario_id, ctx=ctx, action="scenario.delete")
        self.store.delete(scenario_id)

    def verify(self, scenario_id: str) -> bool:
        """Recompute the hash chain; False means the stored log was altered."""
        scenario = self.store.get(scenario_id)
        return verify_chain(scenario, self.store.entries(scenario_id))
