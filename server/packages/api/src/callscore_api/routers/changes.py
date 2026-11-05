"""GET /api/changes — audit/change log over rating events and run lifecycle."""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import Engine

from callscore_api.schemas.changes import ChangesResponse
from callscore_api.services import market_queries as mq

router = APIRouter(prefix="/changes", tags=["changes"])

EngineDep = Annotated[Engine, Depends(mq.get_engine)]
LimitParam = Annotated[int, Query(ge=1, le=mq.MAX_LIMIT)]
OffsetParam = Annotated[int, Query(ge=0)]
FromParam = Annotated[date | None, Query(alias="from")]
TypeParam = Annotated[str | None, Query(alias="type", max_length=50)]
CsvParam = Literal["json", "csv"] | None


@router.get("", response_model=ChangesResponse)
def list_changes(
    engine: EngineDep,
    *,
    run_id: str | None = None,
    from_: FromParam = None,
    to: date | None = None,
    change_type: TypeParam = None,
    limit: LimitParam = mq.DEFAULT_LIMIT,
    offset: OffsetParam = 0,
    format: CsvParam = None,
) -> ChangesResponse | Response:
    """Audit log: rating actions + run lifecycle events in a window.

    ``type`` filters by rating action (initiation/upgrade/downgrade/drop/...)
    or run pseudo-type (run_created/run_succeeded/run_failed/run_interrupted);
    ``type=correction`` selects correction events. ``is_correction`` is always
    populated on rating rows.
    """
    try:
        rows, total = mq.list_changes(
            engine,
            run_id=run_id,
            from_date=from_,
            to_date=to,
            change_type=change_type,
            limit=limit,
            offset=offset,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0] if exc.args else exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if format == "csv":
        fields = [
            "change_id",
            "kind",
            "type",
            "ts",
            "run_id",
            "analyst_id",
            "analyst_name",
            "security_id",
            "security_name",
            "old_rating",
            "new_rating",
            "is_correction",
            "status",
        ]
        return mq.csv_response(mq.rows_to_csv(rows, fields), "changes.csv")
    return ChangesResponse(
        data=rows,
        pagination={"limit": limit, "offset": offset, "count": len(rows), "total": total},
        provenance=mq.provenance(engine),
    )
