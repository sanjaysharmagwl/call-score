"""Sector list and per-sector run performance."""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import Engine

from callscore_api.schemas.sectors import SectorListResponse, SectorPerformanceResponse
from callscore_api.services import market_queries as mq

router = APIRouter(prefix="/sectors", tags=["sectors"])

EngineDep = Annotated[Engine, Depends(mq.get_engine)]
LimitParam = Annotated[int, Query(ge=1, le=mq.MAX_LIMIT)]
OffsetParam = Annotated[int, Query(ge=0)]
CsvParam = Literal["json", "csv"] | None


@router.get("", response_model=SectorListResponse)
def list_sectors(
    engine: EngineDep,
    *,
    as_of: date | None = None,
    limit: LimitParam = mq.DEFAULT_LIMIT,
    offset: OffsetParam = 0,
    format: CsvParam = None,
) -> SectorListResponse | Response:
    """All sectors with security/analyst/delisted counts (PIT via ``as_of``)."""
    rows = mq.list_sectors(engine, as_of=as_of)
    total = len(rows)
    page = rows[offset : offset + limit]
    if format == "csv":
        fields = [
            "sector_id",
            "name",
            "parent_sector_id",
            "security_count",
            "analyst_count",
            "delisted_count",
        ]
        return mq.csv_response(mq.rows_to_csv(page, fields), "sectors.csv")
    return SectorListResponse(
        data=page,
        pagination={"limit": limit, "offset": offset, "count": len(page), "total": total},
        provenance=mq.provenance(engine),
    )


@router.get("/{sector_id}/performance", response_model=SectorPerformanceResponse)
def get_sector_performance(
    sector_id: str,
    run_id: str,
    engine: EngineDep,
) -> SectorPerformanceResponse:
    """Sector aggregates within a run: mean composite, coverage, counts."""
    try:
        result = mq.sector_performance(engine, sector_id, run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0] if exc.args else exc)) from exc
    sector = result.pop("sector")
    return SectorPerformanceResponse(
        sector_id=sector["sector_id"],
        sector_name=sector["name"],
        parent_sector_id=sector["parent_sector_id"],
        **result,
    )
