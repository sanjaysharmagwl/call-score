"""``/api/analysts*`` routes: list, profile, metrics, ratings, portfolio.

Every route is scoped to ``run_id``; run-scoped payloads are immutable so
they carry an ``ETag`` (hash of run_id + methodology fingerprint) and honor
``If-None-Match`` with ``304``.

DB access goes through ``callscore_api.services.analyst_queries``; the engine
dependency is a local ``get_engine`` reading ``app.state.engine`` until
``callscore_api.deps`` (owned by the foundation task) lands — see handoff.
"""

import csv
import io
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import Engine

from callscore_api import deps
from callscore_api.schemas.analysts import (
    AnalystListResponse,
    AnalystProfileResponse,
    MetricDrilldownResponse,
    PortfolioResponse,
    RatingDetailResponse,
    RatingListResponse,
)
from callscore_api.schemas.common import Provenance
from callscore_api.services import analyst_queries as aq
from callscore_core.enums import RatingAction

router = APIRouter(prefix="/analysts", tags=["analysts"])


EngineDep = Annotated[Engine, Depends(deps.get_engine)]
RunId = Annotated[str, Query(..., description="calc_run id; all data is run-scoped")]


def _run(ctx: Engine, run_id: str) -> dict[str, Any]:
    run = aq.get_run(ctx, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    return run


def _etag(run: dict[str, Any]) -> str:
    return f'"{aq.run_etag(run)}"'


def _check_cache(request: Request, run: dict[str, Any], response: Response) -> Response | None:
    """Set the run ETag on ``response``; return a 304 when it still matches."""
    etag = _etag(run)
    response.headers["ETag"] = etag
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return None


def _provenance(run: dict[str, Any]) -> Provenance:
    return Provenance.model_validate(aq.provenance(run))


@router.get("", response_model=AnalystListResponse)
def list_analysts(
    request: Request,
    response: Response,
    engine: EngineDep,
    run_id: RunId,
    q: Annotated[str | None, Query(max_length=200)] = None,
    peer_group: Annotated[str | None, Query(max_length=500)] = None,
    eligible: Annotated[Literal["ranked", "unranked", "all"] | None, Query()] = None,
    sort: Annotated[str | None, Query(max_length=100)] = None,
    order: Annotated[Literal["asc", "desc"], Query()] = "asc",
    limit: Annotated[int | None, Query(ge=1, le=aq.MAX_LIMIT)] = None,
    offset: Annotated[int | None, Query(ge=0)] = None,
) -> AnalystListResponse | Response:
    run = _run(engine, run_id)
    cached = _check_cache(request, run, response)
    if cached is not None:
        return cached
    if sort is not None and sort not in aq.ANALYST_SORTS:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported sort {sort!r}; allowed: {sorted(aq.ANALYST_SORTS)}",
        )
    try:
        result = aq.list_analysts(
            engine,
            run_id,
            q=q,
            peer_group=peer_group,
            eligible=eligible,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )
    except aq.QueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AnalystListResponse(
        items=result["items"],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
        provenance=_provenance(run),
    )


@router.get("/{analyst_id}", response_model=AnalystProfileResponse)
def analyst_profile(
    request: Request,
    response: Response,
    analyst_id: str,
    engine: EngineDep,
    run_id: RunId,
) -> AnalystProfileResponse | Response:
    run = _run(engine, run_id)
    cached = _check_cache(request, run, response)
    if cached is not None:
        return cached
    profile = aq.analyst_profile(engine, run_id, analyst_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"analyst {analyst_id!r} not found")
    return AnalystProfileResponse(
        analyst_id=profile["analyst_id"],
        name=profile["name"],
        firm_id=profile["firm_id"],
        firm_name=profile["firm_name"],
        employment_spells=profile["employment_spells"],
        coverage_spells=profile["coverage_spells"],
        peer_group=profile["peer_group"],
        composite=profile["composite"],
        metrics=profile["metrics"],
        provenance=_provenance(run),
    )


_CSV_FIELDS = [
    "metric_code",
    "family",
    "value",
    "n_obs",
    "std_err",
    "ci_low",
    "ci_high",
    "shrunk_value",
    "peer_percentile",
    "unavailable_reason",
]


@router.get("/{analyst_id}/metrics", response_model=MetricDrilldownResponse)
def analyst_metrics(
    request: Request,
    response: Response,
    analyst_id: str,
    engine: EngineDep,
    run_id: RunId,
    family: Annotated[str | None, Query(max_length=50)] = None,
    format: Annotated[Literal["json", "csv"], Query()] = "json",
) -> MetricDrilldownResponse | Response:
    run = _run(engine, run_id)
    etag = _etag(run)
    response.headers["ETag"] = etag
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    if not aq.analyst_exists(engine, analyst_id):
        raise HTTPException(status_code=404, detail=f"analyst {analyst_id!r} not found")
    try:
        items = aq.analyst_metrics(engine, run_id, analyst_id, family=family)
    except aq.QueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if format == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow({k: item.get(k) for k in _CSV_FIELDS})
        return PlainTextResponse(
            buf.getvalue(),
            media_type="text/csv",
            headers={
                "ETag": etag,
                "Content-Disposition": f'attachment; filename="metrics-{analyst_id}.csv"',
            },
        )
    return MetricDrilldownResponse(
        analyst_id=analyst_id,
        items=items,
        provenance=_provenance(run),
    )


_VALID_ACTIONS = {a.value for a in RatingAction}


@router.get("/{analyst_id}/ratings", response_model=RatingListResponse)
def analyst_ratings(
    request: Request,
    response: Response,
    analyst_id: str,
    engine: EngineDep,
    run_id: RunId,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    action: Annotated[str | None, Query(max_length=50)] = None,
    security: Annotated[str | None, Query(max_length=200)] = None,
    sort: Annotated[str | None, Query(max_length=100)] = None,
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    limit: Annotated[int | None, Query(ge=1, le=aq.MAX_LIMIT)] = None,
    offset: Annotated[int | None, Query(ge=0)] = None,
) -> RatingListResponse | Response:
    run = _run(engine, run_id)
    cached = _check_cache(request, run, response)
    if cached is not None:
        return cached
    if not aq.analyst_exists(engine, analyst_id):
        raise HTTPException(status_code=404, detail=f"analyst {analyst_id!r} not found")
    if action is not None and action not in _VALID_ACTIONS:
        raise HTTPException(status_code=422, detail=f"unknown action {action!r}")
    try:
        result = aq.list_ratings(
            engine,
            run_id,
            analyst_id,
            date_from=date_from,
            date_to=date_to,
            action=action,
            security=security,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )
    except aq.QueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RatingListResponse(
        items=result["items"],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
        provenance=_provenance(run),
    )


@router.get("/{analyst_id}/ratings/{interval_id}", response_model=RatingDetailResponse)
def rating_detail(
    request: Request,
    response: Response,
    analyst_id: str,
    interval_id: str,
    engine: EngineDep,
    run_id: RunId,
) -> RatingDetailResponse | Response:
    run = _run(engine, run_id)
    cached = _check_cache(request, run, response)
    if cached is not None:
        return cached
    detail = aq.rating_detail(engine, run_id, analyst_id, interval_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"rating interval {interval_id!r} not found for analyst {analyst_id!r}",
        )
    return RatingDetailResponse(
        interval_id=detail["interval_id"],
        event_id=detail["event_id"],
        analyst_id=detail["analyst_id"],
        firm_id=detail["firm_id"],
        security_id=detail["security_id"],
        native_rating=detail["native_rating"],
        canonical_rating=detail["canonical_rating"],
        action_type=detail["action_type"],
        announced_ts=detail["announced_ts"],
        effective_ts=detail["effective_ts"],
        knowledge_ts=detail["knowledge_ts"],
        available_ts=detail["available_ts"],
        entry_session=detail["entry_session"],
        entry_point=detail["entry_point"],
        entry_ts=detail["entry_ts"],
        exit_session=detail["exit_session"],
        exit_point=detail["exit_point"],
        exit_ts=detail["exit_ts"],
        exit_reason=detail["exit_reason"],
        expiry_date=detail["expiry_date"],
        decay_start_session=detail["decay_start_session"],
        is_investable=detail["is_investable"],
        non_investable_reason=detail["non_investable_reason"],
        flags=detail["flags"],
        event=detail["event"],
        exposures=detail["exposures"],
        returns=detail["returns"],
        contributions=detail["contributions"],
        event_window=detail["event_window"],
        target_outcome=detail["target_outcome"],
        explanation=detail["explanation"],
        provenance=_provenance(run),
    )


@router.get("/{analyst_id}/portfolio", response_model=PortfolioResponse)
def analyst_portfolio(
    request: Request,
    response: Response,
    analyst_id: str,
    engine: EngineDep,
    run_id: RunId,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> PortfolioResponse | Response:
    run = _run(engine, run_id)
    cached = _check_cache(request, run, response)
    if cached is not None:
        return cached
    if not aq.analyst_exists(engine, analyst_id):
        raise HTTPException(status_code=404, detail=f"analyst {analyst_id!r} not found")
    result = aq.portfolio(engine, run_id, analyst_id, date_from=date_from, date_to=date_to)
    return PortfolioResponse(
        analyst_id=result["analyst_id"],
        points=result["points"],
        total_points=result["total_points"],
        downsampled=result["downsampled"],
        positions=result["positions"],
        provenance=_provenance(run),
    )
