"""``GET /api/leaderboard`` — ranked analyst table for one run.

Same run-scoping and ETag/304 semantics as ``routers.analysts``; the engine
dependency is shared from that module until ``callscore_api.deps`` lands.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response

from callscore_api.routers.analysts import (
    EngineDep,
    RunId,
    _check_cache,
    _provenance,
)
from callscore_api.schemas.leaderboard import LeaderboardResponse
from callscore_api.services import analyst_queries as aq

router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])


@router.get("", response_model=LeaderboardResponse)
def leaderboard(
    request: Request,
    response: Response,
    engine: EngineDep,
    run_id: RunId,
    peer_group: Annotated[str | None, Query(max_length=500)] = None,
    metric: Annotated[str | None, Query(max_length=200)] = None,
    min_obs: Annotated[int | None, Query(ge=0)] = None,
    eligible: Annotated[Literal["ranked", "unranked", "all"] | None, Query()] = None,
    sort: Annotated[str | None, Query(max_length=200)] = None,
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    limit: Annotated[int | None, Query(ge=1, le=aq.MAX_LIMIT)] = None,
    offset: Annotated[int | None, Query(ge=0)] = None,
) -> LeaderboardResponse | Response:
    run = aq.get_run(engine, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    cached = _check_cache(request, run, response)
    if cached is not None:
        return cached
    try:
        result = aq.leaderboard(
            engine,
            run_id,
            peer_group=peer_group,
            metric=metric,
            min_obs=min_obs,
            eligible=eligible,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )
    except aq.QueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LeaderboardResponse(
        items=result["items"],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
        provenance=_provenance(run),
    )
