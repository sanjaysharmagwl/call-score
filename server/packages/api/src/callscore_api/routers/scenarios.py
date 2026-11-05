"""Scenario endpoints (``/api/scenarios``).

    POST /scenarios                     create a saved scenario (201)
    GET  /scenarios                     list scenarios (paging)
    GET  /scenarios/compare?a=&b=       extended delta report for two runs
    POST /scenarios/execute             execute an inline (optionally
                                        persisted) scenario -> 202
    POST /scenarios/sensitivity         bounded parameter sweep -> 202
    GET  /scenarios/{scenario_id}       scenario detail
    POST /scenarios/{scenario_id}/execute   execute -> 202 + run linkage
    GET  /scenarios/{scenario_id}/compare?other_run_id=   delta report

Literal routes (``compare``, ``execute``, ``sensitivity``) are registered
BEFORE ``/{scenario_id}`` so they win over the path parameter.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Engine

from callscore_api.deps import get_engine, get_job_coordinator
from callscore_api.schemas.common import (
    PaginationMeta,
    PaginationParams,
    get_pagination,
)
from callscore_api.schemas.scenarios import (
    ScenarioCompareResponse,
    ScenarioCreateRequest,
    ScenarioDetail,
    ScenarioExecuteRequest,
    ScenarioExecuteResponse,
    ScenarioInlineExecuteRequest,
    ScenarioListResponse,
    ScenarioSummary,
    SensitivitySweepRequest,
    SensitivitySweepResponse,
)
from callscore_api.services import scenarios as scenario_service
from callscore_api.services.jobs import JobCoordinator

router = APIRouter(prefix="/scenarios", tags=["scenarios"])

EngineDep = Annotated[Engine, Depends(get_engine)]
CoordinatorDep = Annotated[JobCoordinator, Depends(get_job_coordinator)]
PaginationDep = Annotated[PaginationParams, Depends(get_pagination)]


def _summary(view: dict[str, Any]) -> ScenarioSummary:
    return ScenarioSummary(
        scenario_id=view["scenario_id"],
        name=view["name"],
        description=view["description"],
        created_at=view["created_at"],
        base_fingerprint=view["base_fingerprint"],
        base_ref=view["base_ref"],
        overrides=view["overrides"],
        universe_filter=view["universe_filter"],
        run_count=view["run_count"],
        last_run_id=view["last_run_id"],
    )


def _detail(view: dict[str, Any]) -> ScenarioDetail:
    return ScenarioDetail(
        **_summary(view).model_dump(),
        base_methodology=view["base_methodology"],
        run_ids=view["run_ids"],
        last_execution=view["last_execution"],
        sweeps=view["sweeps"],
    )


# ---------------------------------------------------------------------------
# Literal routes first (must precede /{scenario_id})
# ---------------------------------------------------------------------------


@router.post("", status_code=201, response_model=ScenarioDetail)
def create_scenario(body: ScenarioCreateRequest, engine: EngineDep) -> ScenarioDetail:
    """Create a named scenario: base methodology + validated overrides."""
    view = scenario_service.create_scenario(
        engine,
        name=body.name,
        description=body.description,
        base=body.base_methodology,
        overrides=body.overrides,
        universe_filter=body.universe_filter,
    )
    return _detail(view)


@router.get("", response_model=ScenarioListResponse)
def list_scenarios(
    engine: EngineDep,
    pagination: PaginationDep,
) -> ScenarioListResponse:
    """List scenarios newest-first."""
    views, total = scenario_service.list_scenarios(
        engine, limit=pagination.limit, offset=pagination.offset
    )
    return ScenarioListResponse(
        data=[_summary(v) for v in views],
        pagination=PaginationMeta(
            limit=pagination.limit,
            offset=pagination.offset,
            count=len(views),
            total=total,
        ),
    )


@router.get("/compare", response_model=ScenarioCompareResponse)
def compare_runs_endpoint(a: str, b: str, engine: EngineDep) -> ScenarioCompareResponse:
    """Extended delta report between two arbitrary runs.

    Complements ``GET /api/runs/compare`` (generic side-by-side summary)
    with per-analyst score/rank deltas, ranked<->unranked transitions,
    Spearman rank correlation, per-family/metric deltas and an explicit
    universe-compatibility report.
    """
    return ScenarioCompareResponse(**scenario_service.compare_runs(engine, a, b))


@router.post("/execute", status_code=202, response_model=ScenarioExecuteResponse)
def execute_inline(
    body: ScenarioInlineExecuteRequest, coordinator: CoordinatorDep
) -> ScenarioExecuteResponse:
    """Execute an inline scenario spec (optionally ``persist=True``)."""
    result = scenario_service.execute_scenario(
        coordinator,
        as_of_date=body.as_of_date,
        spec={
            "name": body.name,
            "description": body.description,
            "base_methodology": body.base_methodology,
            "overrides": body.overrides,
            "universe_filter": body.universe_filter,
        },
        persist=body.persist,
        knowledge_cutoff=body.knowledge_cutoff,
        universe_filter=body.universe_filter,
    )
    return ScenarioExecuteResponse(**result)


@router.post("/sensitivity", status_code=202, response_model=SensitivitySweepResponse)
def sensitivity_sweep(
    body: SensitivitySweepRequest, coordinator: CoordinatorDep
) -> SensitivitySweepResponse:
    """Submit a bounded (<=25 combos) methodology parameter sweep."""
    result = scenario_service.sensitivity_sweep(
        coordinator,
        as_of_date=body.as_of_date,
        param_path=body.param_path,
        values=body.values,
        overrides=body.overrides,
        base=body.base_methodology,
        scenario_id=body.scenario_id,
        knowledge_cutoff=body.knowledge_cutoff,
        universe_filter=body.universe_filter,
    )
    return SensitivitySweepResponse(**result)


# ---------------------------------------------------------------------------
# {scenario_id} routes
# ---------------------------------------------------------------------------


@router.get("/{scenario_id}", response_model=ScenarioDetail)
def get_scenario(scenario_id: str, engine: EngineDep) -> ScenarioDetail:
    """Scenario detail: resolved base methodology, overrides, run history."""
    return _detail(scenario_service.get_scenario(engine, scenario_id))


@router.post("/{scenario_id}/execute", status_code=202, response_model=ScenarioExecuteResponse)
def execute_scenario(
    scenario_id: str,
    body: ScenarioExecuteRequest,
    coordinator: CoordinatorDep,
) -> ScenarioExecuteResponse:
    """Execute a stored scenario; links the submitted run into its config."""
    result = scenario_service.execute_scenario(
        coordinator,
        scenario_id=scenario_id,
        as_of_date=body.as_of_date,
        knowledge_cutoff=body.knowledge_cutoff,
        universe_filter=body.universe_filter,
    )
    return ScenarioExecuteResponse(**result)


@router.get("/{scenario_id}/compare", response_model=ScenarioCompareResponse)
def compare_scenario_run(
    scenario_id: str,
    engine: EngineDep,
    other_run_id: Annotated[str | None, Query()] = None,
) -> ScenarioCompareResponse:
    """Delta report: the scenario's latest run vs ``other_run_id``.

    When ``other_run_id`` is omitted the scenario's previous execution is
    used; 422 when the scenario has no (or only one) recorded run.
    """
    view = scenario_service.get_scenario(engine, scenario_id)
    run_ids = [str(r) for r in view["run_ids"]]
    last = view.get("last_run_id") or (run_ids[-1] if run_ids else None)
    if last is None:
        raise ValueError(f"scenario {scenario_id!r} has no recorded runs; execute it first")
    other = other_run_id
    if other is None:
        previous = [r for r in run_ids if r != last]
        other = previous[-1] if previous else None
    if other is None:
        raise ValueError(
            f"scenario {scenario_id!r} has only one recorded run; pass other_run_id explicitly"
        )
    return ScenarioCompareResponse(**scenario_service.compare_runs(engine, last, other))


__all__ = ["router"]
