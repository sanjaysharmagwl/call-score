"""What-if routes for the FastAPI router: scenarios and AG Grid cell edits.

Added by :func:`callscore_calc.integrations.fastapi.create_router` when the plugin is installed.
"""

# No ``from __future__ import annotations`` here: FastAPI must see the route annotations, which
# refer to aliases defined inside ``add_routes``, as real objects rather than strings.

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Response

from callscore_calc import CalcContext
from callscore_calc.integrations.fastapi import RouterKit

from .aggrid import CellEdit, edit_to_override

if TYPE_CHECKING:
    from .plugin import WhatIfPlugin


def add_routes(router: APIRouter, kit: RouterKit, plugin: "WhatIfPlugin") -> None:
    Ctx = Annotated[CalcContext, Depends(kit.context)]
    JsonBody = Annotated[dict[str, Any], Body()]
    IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
    engine = kit.engine
    call = kit.call

    @router.post("/aggrid/edit")
    def aggrid_edit(
        body: JsonBody, ctx: Ctx, idempotency_key: IdempotencyKey = None
    ) -> dict[str, Any]:
        """Body: ``{"scenario", "expected_version", "edit": <cell edit event>, "note"?}``."""

        def run() -> dict[str, Any]:
            scenario = plugin.scenarios.get(str(body.get("scenario")), ctx=ctx)
            schema = engine.schema(scenario.dataset.id, scenario.dataset.version, ctx=ctx)
            step = edit_to_override(CellEdit.model_validate(body.get("edit") or {}), schema)
            updated = plugin.scenarios.append(
                scenario.id,
                [step],
                expected_version=int(body.get("expected_version", -1)),
                note=body.get("note"),
                client_op_id=idempotency_key,
                ctx=ctx,
            )
            return updated.model_dump(mode="json")

        return call(run)

    @router.get("/scenarios")
    def list_scenarios(
        ctx: Ctx, dataset: Annotated[str | None, Query()] = None
    ) -> list[dict[str, Any]]:
        return call(
            lambda: [
                s.model_dump(mode="json") for s in plugin.scenarios.list(dataset=dataset, ctx=ctx)
            ]
        )

    @router.post("/scenarios", status_code=201)
    def create_scenario(body: JsonBody, ctx: Ctx) -> dict[str, Any]:
        """Body: ``{"dataset", "name", "description"?}``."""
        return call(
            lambda: plugin.scenarios.create(
                body.get("dataset", ""),
                str(body.get("name", "")),
                description=body.get("description"),
                ctx=ctx,
            ).model_dump(mode="json")
        )

    @router.get("/scenarios/{scenario_id}")
    def get_scenario(scenario_id: str, ctx: Ctx) -> dict[str, Any]:
        return call(lambda: plugin.scenarios.get(scenario_id, ctx=ctx).model_dump(mode="json"))

    @router.get("/scenarios/{scenario_id}/log")
    def scenario_log(
        scenario_id: str, ctx: Ctx, upto: Annotated[int | None, Query(ge=0)] = None
    ) -> list[dict[str, Any]]:
        return call(
            lambda: [
                e.model_dump(mode="json")
                for e in plugin.scenarios.log(scenario_id, upto=upto, ctx=ctx)
            ]
        )

    @router.post("/scenarios/{scenario_id}/steps")
    def append_steps(
        scenario_id: str, body: JsonBody, ctx: Ctx, idempotency_key: IdempotencyKey = None
    ) -> dict[str, Any]:
        """Body: ``{"steps", "expected_version", "note"?}``; 409 if the version is stale."""
        return call(
            lambda: plugin.scenarios.append(
                scenario_id,
                body.get("steps") or [],
                expected_version=int(body.get("expected_version", -1)),
                note=body.get("note"),
                client_op_id=idempotency_key,
                ctx=ctx,
            ).model_dump(mode="json")
        )

    @router.post("/scenarios/{scenario_id}/fork", status_code=201)
    def fork_scenario(scenario_id: str, body: JsonBody, ctx: Ctx) -> dict[str, Any]:
        """Body: ``{"name", "at_version"?, "description"?}``."""
        return call(
            lambda: plugin.scenarios.fork(
                scenario_id,
                name=str(body.get("name", "")),
                at_version=body.get("at_version"),
                description=body.get("description"),
                ctx=ctx,
            ).model_dump(mode="json")
        )

    @router.delete("/scenarios/{scenario_id}", status_code=204)
    def delete_scenario(scenario_id: str, ctx: Ctx) -> Response:
        call(lambda: plugin.scenarios.delete(scenario_id, ctx=ctx))
        return Response(status_code=204)

    @router.get("/scenarios/{scenario_id}/verify")
    def verify_scenario(scenario_id: str, ctx: Ctx) -> dict[str, Any]:
        """Recompute the scenario's hash chain."""

        def run() -> dict[str, Any]:
            plugin.scenarios.get(scenario_id, ctx=ctx)
            return {"scenario": scenario_id, "intact": plugin.scenarios.verify(scenario_id)}

        return call(run)
