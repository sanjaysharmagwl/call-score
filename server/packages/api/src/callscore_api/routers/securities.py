"""Security master-data and per-security market/event endpoints.

All routes are relative to ``/api`` (the integrator mounts this router with
``prefix="/api"``). DB access goes through ``market_queries.get_engine`` — a
local stand-in for ``callscore_api.deps.get_engine`` until T13's foundation
lands (merge note in handoff).
"""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import Engine

from callscore_api.schemas.securities import (
    ConsensusResponse,
    PricesResponse,
    RatingsResponse,
    SecurityDetailResponse,
    SecurityListResponse,
    SecurityPerformanceResponse,
)
from callscore_api.services import market_queries as mq

router = APIRouter(prefix="/securities", tags=["securities"])

EngineDep = Annotated[Engine, Depends(mq.get_engine)]
LimitParam = Annotated[int, Query(ge=1, le=mq.MAX_LIMIT)]
OffsetParam = Annotated[int, Query(ge=0)]
FromParam = Annotated[date | None, Query(alias="from")]
CsvParam = Literal["json", "csv"] | None


def _not_found(exc: KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc.args[0] if exc.args else exc))


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.get("", response_model=SecurityListResponse)
def list_securities(
    engine: EngineDep,
    *,
    q: Annotated[str | None, Query(max_length=200)] = None,
    sector: str | None = None,
    cap_bucket: str | None = None,
    as_of: date | None = None,
    limit: LimitParam = mq.DEFAULT_LIMIT,
    offset: OffsetParam = 0,
    format: CsvParam = None,
) -> SecurityListResponse | Response:
    """Search securities; PIT classification at ``as_of`` (default: latest).

    Delisted securities are always included (``is_delisted`` flag). ``format=csv``
    exports the current page.
    """
    rows, total = mq.search_securities(
        engine,
        q=q,
        sector=sector,
        cap_bucket=cap_bucket,
        as_of=as_of,
        limit=limit,
        offset=offset,
    )
    if format == "csv":
        fields = [
            "security_id",
            "name",
            "ticker",
            "sector_id",
            "sector_name",
            "region",
            "currency",
            "exchange_id",
            "cap_bucket",
            "listing_date",
            "is_delisted",
            "classification_from_date",
            "classification_to_date",
        ]
        return mq.csv_response(mq.rows_to_csv(rows, fields), "securities.csv")
    return SecurityListResponse(
        data=rows,
        pagination={"limit": limit, "offset": offset, "total": total},
        provenance=mq.provenance(engine),
    )


@router.get("/{security_id}", response_model=SecurityDetailResponse)
def get_security(
    security_id: str,
    engine: EngineDep,
    *,
    as_of: date | None = None,
) -> SecurityDetailResponse:
    """Security detail: identifiers, classification spells, delisting, coverage."""
    try:
        detail = mq.security_detail(engine, security_id, as_of=as_of)
    except KeyError as exc:
        raise _not_found(exc) from exc
    sec = detail["security"]
    return SecurityDetailResponse(
        security_id=sec["security_id"],
        name=sec["name"],
        latest_classification=detail["latest_classification"],
        sector_name=detail["sector_name"],
        is_delisted=detail["is_delisted"],
        delisting=detail["delisting"],
        identifiers=detail["identifiers"],
        classification_spells=detail["classification_spells"],
        coverage_analysts=detail["coverage_analysts"],
        price_coverage=detail["price_coverage"],
        rating_event_count=detail["rating_event_count"],
        provenance=mq.provenance(engine),
    )


@router.get("/{security_id}/prices", response_model=PricesResponse)
def get_prices(
    security_id: str,
    engine: EngineDep,
    *,
    from_: FromParam = None,
    to: date | None = None,
    adjusted: bool = True,
    interval: Annotated[str, Query(pattern="^(1d|1w|1M)$")] = "1d",
    limit: LimitParam = mq.DEFAULT_LIMIT,
    offset: OffsetParam = 0,
    format: CsvParam = None,
) -> PricesResponse | Response:
    """OHLC/adjustment/total-return series; bucket-extrema downsampled if large."""
    try:
        result = mq.security_prices(
            engine,
            security_id,
            from_date=from_,
            to_date=to,
            adjusted=adjusted,
            interval=interval,
            limit=limit,
            offset=offset,
        )
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc
    if format == "csv":
        return mq.csv_response(mq.rows_to_csv(result["rows"]), f"{security_id}-prices.csv")
    return PricesResponse(
        security_id=security_id,
        interval=result["interval"],
        rows=result["rows"],
        total=result["total"],
        downsampled=result["downsampled"],
        source_rows=result["source_rows"],
        provenance=mq.provenance(engine),
    )


@router.get("/{security_id}/ratings", response_model=RatingsResponse)
def get_ratings(
    security_id: str,
    engine: EngineDep,
    *,
    run_id: str | None = None,
    from_: FromParam = None,
    to: date | None = None,
    limit: LimitParam = mq.DEFAULT_LIMIT,
    offset: OffsetParam = 0,
) -> RatingsResponse:
    """Rating events for the security; ``run_id`` adds interval outcomes."""
    try:
        rows, total = mq.security_ratings(
            engine,
            security_id,
            run_id=run_id,
            from_date=from_,
            to_date=to,
            limit=limit,
            offset=offset,
        )
    except KeyError as exc:
        raise _not_found(exc) from exc
    return RatingsResponse(
        security_id=security_id,
        run_id=run_id,
        data=rows,
        pagination={"limit": limit, "offset": offset, "total": total},
    )


@router.get("/{security_id}/consensus", response_model=ConsensusResponse)
def get_consensus(
    security_id: str,
    engine: EngineDep,
    *,
    run_id: str | None = None,
    from_: FromParam = None,
    to: date | None = None,
) -> ConsensusResponse:
    """Consensus daily series; ``run_id`` adds that run's target outcomes."""
    try:
        result = mq.security_consensus(
            engine, security_id, run_id=run_id, from_date=from_, to_date=to
        )
    except KeyError as exc:
        raise _not_found(exc) from exc
    return ConsensusResponse(
        security_id=security_id,
        run_id=run_id,
        consensus=result["consensus"],
        target_outcomes=result["target_outcomes"],
        provenance=mq.provenance(engine),
    )


@router.get("/{security_id}/performance", response_model=SecurityPerformanceResponse)
def get_performance(
    security_id: str,
    run_id: str,
    engine: EngineDep,
) -> SecurityPerformanceResponse:
    """Aggregate interval stats (n ratings, mean excess, hit rate) for a run."""
    try:
        result = mq.security_performance(engine, security_id, run_id)
    except KeyError as exc:
        raise _not_found(exc) from exc
    return SecurityPerformanceResponse.model_validate(result)
