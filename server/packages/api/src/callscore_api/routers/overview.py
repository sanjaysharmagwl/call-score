"""GET /api/overview — dashboard aggregates reconciling with stored rows."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Engine

from callscore_api.schemas.overview import OverviewResponse
from callscore_api.services import market_queries as mq

router = APIRouter(prefix="/overview", tags=["overview"])

EngineDep = Annotated[Engine, Depends(mq.get_engine)]


@router.get("", response_model=OverviewResponse)
def get_overview(engine: EngineDep, run_id: str | None = None) -> OverviewResponse:
    """Dashboard aggregates for a run (default: latest succeeded run).

    All counts are direct SQL aggregates over the stored tables.
    """
    try:
        result = mq.overview(engine, run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0] if exc.args else exc)) from exc
    return OverviewResponse.model_validate(result)
