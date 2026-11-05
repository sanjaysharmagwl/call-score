"""Run lifecycle endpoints: submit, list, detail, compare, retry.

Routes (mounted under ``/api`` by the integrator)::

    POST /runs                      submit a run (202, idempotent)
    GET  /runs                      list runs (status/as_of filters, paging)
    GET  /runs/compare?a=&b=        side-by-side composite/metric summary
    GET  /runs/{run_id}             run detail
    POST /runs/{run_id}/retry       requeue a failed/interrupted run

``/runs/compare`` is registered BEFORE ``/runs/{run_id}`` so the literal path
wins over the path parameter.
"""

import json
import re
from collections.abc import Mapping
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from sqlalchemy import Engine, func, select, update

from callscore_api.deps import get_engine, get_job_coordinator
from callscore_api.schemas.common import (
    PaginationMeta,
    PaginationParams,
    SortParam,
    get_pagination,
)
from callscore_api.schemas.runs import (
    CompositeRow,
    MetricAggregate,
    RunCompareResponse,
    RunCompareSide,
    RunCreateRequest,
    RunDetail,
    RunErrorInfo,
    RunListResponse,
    RunSubmitResponse,
    RunSummary,
)
from callscore_api.services.jobs import JobCoordinator
from callscore_core.enums import RunStatus
from callscore_core.methodology import Methodology
from callscore_store.models import AnalystComposite, AnalystMetric, CalcRun, MethodologyVersion
from callscore_store.repositories import runs as runs_repo
from callscore_store.repositories.base import table_of
from callscore_store.repositories.runs import RunTransitionError

router = APIRouter(prefix="/runs", tags=["runs"])

EngineDep = Annotated[Engine, Depends(get_engine)]
CoordinatorDep = Annotated[JobCoordinator, Depends(get_job_coordinator)]
PaginationDep = Annotated[PaginationParams, Depends(get_pagination)]

# Whitelist mirroring callscore_store.repositories.runs._RUN_SORT_COLUMNS.
_RUN_SORT_COLUMNS = frozenset(
    {"run_id", "as_of_date", "knowledge_cutoff", "status", "started_at", "finished_at"}
)

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Row -> schema helpers
# ---------------------------------------------------------------------------


def _json_object(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _universe_filter(raw: object) -> list[str] | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(value, list) and all(isinstance(s, str) for s in value):
        return [str(s) for s in value]
    return None


def _error_info(raw: object) -> RunErrorInfo | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return RunErrorInfo(type="error", message=raw)
    if isinstance(value, dict):
        return RunErrorInfo(
            type=str(value.get("type") or "error"),
            message=str(value.get("message") or ""),
        )
    return RunErrorInfo(type="error", message=str(value))


def _fingerprint_for(engine: Engine, methodology_version_id: int) -> str | None:
    with engine.connect() as conn:
        value = conn.execute(
            select(MethodologyVersion.fingerprint).where(
                MethodologyVersion.id == methodology_version_id
            )
        ).scalar_one_or_none()
    return str(value) if value is not None else None


def _fingerprints_for(engine: Engine, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            select(MethodologyVersion.id, MethodologyVersion.fingerprint).where(
                MethodologyVersion.id.in_(ids)
            )
        ).all()
    return {int(r.id): str(r.fingerprint) for r in rows}


def _methodology_version_row(engine: Engine, version_id: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(select(MethodologyVersion).where(MethodologyVersion.id == version_id))
            .mappings()
            .first()
        )
    return dict(row) if row is not None else None


def _summary(run: Mapping[str, Any], fingerprint: str | None) -> RunSummary:
    return RunSummary(
        run_id=str(run["run_id"]),
        status=str(run["status"]),
        progress=float(run["progress"] or 0.0),
        as_of_date=run["as_of_date"],
        knowledge_cutoff=run["knowledge_cutoff"],
        started_at=run.get("started_at"),
        finished_at=run.get("finished_at"),
        methodology_fingerprint=fingerprint,
        universe_filter=_universe_filter(run.get("universe_filter_json")),
        error=_error_info(run.get("error_json")),
    )


def _count_runs(engine: Engine, *, status: RunStatus | None, as_of: date | None) -> int:
    tbl = table_of(CalcRun)
    stmt = select(func.count()).select_from(tbl)
    if status is not None:
        stmt = stmt.where(tbl.c.status == status.value)
    if as_of is not None:
        stmt = stmt.where(tbl.c.as_of_date == as_of)
    with engine.connect() as conn:
        return int(conn.execute(stmt).scalar_one())


def _resolve_methodology(spec: Methodology | str, engine: Engine) -> Methodology:
    """Resolve the ``methodology`` request field to a :class:`Methodology`."""
    if isinstance(spec, Methodology):
        return spec
    if spec == "default":
        return Methodology()
    if _FINGERPRINT_RE.fullmatch(spec):
        with engine.connect() as conn:
            params_json = conn.execute(
                select(MethodologyVersion.params_json).where(MethodologyVersion.fingerprint == spec)
            ).scalar_one_or_none()
        if params_json is None:
            raise KeyError(f"methodology {spec!r} not found")
        return Methodology.model_validate(json.loads(str(params_json)))
    raise ValueError(
        f"unknown methodology reference {spec!r}: expected 'default', "
        "a stored 64-hex fingerprint, or an inline methodology object"
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=202, response_model=RunSubmitResponse)
def submit_run(
    body: RunCreateRequest,
    coordinator: CoordinatorDep,
) -> RunSubmitResponse:
    """Submit a scoring run; idempotent on (methodology, as_of, cutoff, universe)."""
    methodology = _resolve_methodology(body.methodology, coordinator.engine)
    result = coordinator.submit(
        methodology=methodology,
        as_of_date=body.as_of_date,
        knowledge_cutoff=body.knowledge_cutoff,
        universe_filter=body.universe_filter,
    )
    return RunSubmitResponse(run_id=result.run_id, status=result.status, created=result.created)


@router.get("", response_model=RunListResponse)
def list_runs(
    engine: EngineDep,
    pagination: PaginationDep,
    status: RunStatus | None = None,
    as_of: date | None = None,
    sort: SortParam = "as_of_date",
    order: Literal["asc", "desc"] = "desc",
) -> RunListResponse:
    """List calc runs newest first; filter by ``status`` and ``as_of``."""
    if sort not in _RUN_SORT_COLUMNS:
        raise ValueError(f"unknown sort {sort!r}; allowed: {sorted(_RUN_SORT_COLUMNS)}")
    rows = runs_repo.list_calc_runs(
        engine,
        status=status,
        as_of_date=as_of,
        sort=sort,
        descending=order == "desc",
        limit=pagination.limit,
        offset=pagination.offset,
    )
    total = _count_runs(engine, status=status, as_of=as_of)
    fingerprints = _fingerprints_for(engine, {int(r["methodology_version_id"]) for r in rows})
    data = [_summary(r, fingerprints.get(int(r["methodology_version_id"]))) for r in rows]
    return RunListResponse(
        data=data,
        pagination=PaginationMeta(
            limit=pagination.limit,
            offset=pagination.offset,
            count=len(data),
            total=total,
        ),
    )


@router.get("/compare", response_model=RunCompareResponse)
def compare_runs(a: str, b: str, engine: EngineDep) -> RunCompareResponse:
    """Side-by-side composite rows and per-metric summaries for two runs."""
    return RunCompareResponse(a=_compare_side(engine, a), b=_compare_side(engine, b))


def _compare_side(engine: Engine, run_id: str) -> RunCompareSide:
    run = runs_repo.get_calc_run(engine, run_id)
    if run is None:
        raise KeyError(f"calc_run {run_id!r} not found")
    version_id = int(run["methodology_version_id"])
    fingerprint = _fingerprint_for(engine, version_id)

    composite_stmt = (
        select(
            AnalystComposite.analyst_id,
            AnalystComposite.peer_group,
            AnalystComposite.composite_score,
            AnalystComposite.rank_overall,
            AnalystComposite.rank_in_peer_group,
            AnalystComposite.is_ranked,
            AnalystComposite.unranked_reason,
        )
        .where(AnalystComposite.run_id == run_id)
        .order_by(
            AnalystComposite.rank_overall.is_(None),
            AnalystComposite.rank_overall,
            AnalystComposite.analyst_id,
        )
    )
    metric_stmt = (
        select(
            AnalystMetric.metric_code,
            func.count().label("n_analysts"),
            func.count(AnalystMetric.value).label("n_observed"),
            func.avg(AnalystMetric.value).label("mean_value"),
        )
        .where(AnalystMetric.run_id == run_id)
        .group_by(AnalystMetric.metric_code)
        .order_by(AnalystMetric.metric_code)
    )
    with engine.connect() as conn:
        composite_rows = [dict(r) for r in conn.execute(composite_stmt).mappings()]
        metric_rows = [dict(r) for r in conn.execute(metric_stmt).mappings()]

    return RunCompareSide(
        run=_summary(run, fingerprint),
        composites=[CompositeRow(**row) for row in composite_rows],
        metric_summary=[MetricAggregate(**row) for row in metric_rows],
    )


@router.get("/{run_id}", response_model=RunDetail)
def get_run(run_id: str, engine: EngineDep) -> RunDetail:
    """Full detail for one run: status, progress, counts, fingerprints, methodology."""
    run = runs_repo.get_calc_run(engine, run_id)
    if run is None:
        raise KeyError(f"calc_run {run_id!r} not found")
    version_id = int(run["methodology_version_id"])
    mv = _methodology_version_row(engine, version_id)
    fingerprint = str(mv["fingerprint"]) if mv is not None else None

    methodology: Methodology | None = None
    if mv is not None and isinstance(mv.get("params_json"), str):
        try:
            methodology = Methodology.model_validate(json.loads(mv["params_json"]))
        except Exception:  # noqa: BLE001 - stored params should validate; degrade gracefully
            methodology = None

    row_counts_raw = _json_object(run.get("row_counts_json"))
    rows = row_counts_raw.get("rows")
    artifacts = row_counts_raw.get("stage_artifacts")
    summary = _summary(run, fingerprint)
    return RunDetail(
        **summary.model_dump(),
        algorithm_version=str(run["algorithm_version"] or ""),
        source_fingerprint=(
            str(run["source_fingerprint"]) if run.get("source_fingerprint") else None
        ),
        row_counts={str(k): int(v) for k, v in rows.items()} if isinstance(rows, dict) else None,
        stage_artifacts=[a for a in artifacts if isinstance(a, dict)]
        if isinstance(artifacts, list)
        else None,
        methodology=methodology,
    )


@router.post("/{run_id}/retry", response_model=RunSummary)
def retry_run(run_id: str, engine: EngineDep) -> RunSummary:
    """Requeue a ``failed`` or ``interrupted`` run.

    Both states are reset to ``queued`` with cleared progress/error/timestamps
    so the job worker picks the run up again. Other statuses -> 409.

    NOTE: ``callscore_store.repositories.runs._ALLOWED_TRANSITIONS`` treats
    ``interrupted`` as terminal, so this endpoint performs a guarded direct
    update rather than ``update_run_status`` — flagged as a contract gap.
    """
    run = runs_repo.get_calc_run(engine, run_id)
    if run is None:
        raise KeyError(f"calc_run {run_id!r} not found")
    status = str(run["status"])
    retryable = {RunStatus.FAILED.value, RunStatus.INTERRUPTED.value}
    if status not in retryable:
        raise RunTransitionError(
            f"cannot retry calc_run {run_id!r}: status {status!r} is not retryable"
        )
    tbl = table_of(CalcRun)
    with engine.begin() as conn:
        conn.execute(
            update(tbl)
            .where(tbl.c.run_id == run_id)
            .values(
                status=RunStatus.QUEUED.value,
                progress=0.0,
                error_json=None,
                started_at=None,
                finished_at=None,
            )
        )
    fresh = runs_repo.get_calc_run(engine, run_id)
    assert fresh is not None  # just updated
    version_id = int(fresh["methodology_version_id"])
    return _summary(fresh, _fingerprint_for(engine, version_id))


__all__ = ["router"]
