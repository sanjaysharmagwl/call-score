"""Backtest endpoints: submit, list, detail, nav/stats/trades/attribution/monthly.

Routes (mounted under ``/api`` by the integrator)::

    POST /backtests                          submit a backtest (202, queued)
    GET  /backtests                          list (status filter, paging)
    GET  /backtests/{backtest_id}            detail: status/spec/artifacts
    GET  /backtests/{backtest_id}/nav?from&to&interval   downsampled NAV
    GET  /backtests/{backtest_id}/stats      scalar stats
    GET  /backtests/{backtest_id}/trades?limit&offset&format=csv
    GET  /backtests/{backtest_id}/attribution
    GET  /backtests/{backtest_id}/monthly

Execution is asynchronous: ``POST`` only queues the row. A
``services.backtests.BacktestRunner`` worker (same writer-lease pattern as
``JobCoordinator``) drains queued backtests; tests may also call
``services.backtests.execute_backtest`` / ``runner.run_once()`` directly.
"""

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import Engine

from callscore_api.deps import get_engine
from callscore_api.schemas.backtests import (
    BacktestAttributionResponse,
    BacktestAttributionRow,
    BacktestCreateRequest,
    BacktestDetail,
    BacktestErrorInfo,
    BacktestListResponse,
    BacktestMonthlyResponse,
    BacktestMonthlyRow,
    BacktestNavResponse,
    BacktestNavRow,
    BacktestStatRow,
    BacktestStatsResponse,
    BacktestSubmitResponse,
    BacktestSummary,
    BacktestTradeRow,
    BacktestTradesResponse,
)
from callscore_api.schemas.common import PaginationMeta, PaginationParams, get_pagination
from callscore_api.services import backtests as svc
from callscore_api.services import market_queries as mq
from callscore_core.enums import RunStatus

router = APIRouter(prefix="/backtests", tags=["backtests"])

EngineDep = Annotated[Engine, Depends(get_engine)]
PaginationDep = Annotated[PaginationParams, Depends(get_pagination)]

_TRADE_FIELDS = (
    "trade_seq",
    "session_date",
    "security_id",
    "side",
    "quantity",
    "price",
    "notional",
    "fee",
    "realized_pnl",
)


def _summary(row: dict[str, Any]) -> BacktestSummary:
    error = row.get("error")
    return BacktestSummary(
        backtest_id=str(row["backtest_id"]),
        name=row.get("name"),
        status=str(row["status"]),
        created_at=row.get("created_at"),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        benchmark_id=row.get("benchmark_id"),
        error=BacktestErrorInfo(**error) if isinstance(error, dict) else None,
    )


def _detail(row: dict[str, Any]) -> BacktestDetail:
    return BacktestDetail(
        **_summary(row).model_dump(),
        spec=row.get("spec"),
        monthly_returns=row.get("monthly_returns"),
        attribution=row.get("attribution"),
    )


@router.post("", status_code=202, response_model=BacktestSubmitResponse)
def submit_backtest(body: BacktestCreateRequest, engine: EngineDep) -> BacktestSubmitResponse:
    """Submit a backtest; the spec is validated (422) before queueing."""
    result = svc.submit_backtest(
        engine,
        spec=body.spec,
        name=body.name,
        start_date=body.start_date,
        end_date=body.end_date,
        benchmark_id=body.benchmark_id,
    )
    return BacktestSubmitResponse(backtest_id=result.backtest_id, status=result.status)


@router.get("", response_model=BacktestListResponse)
def list_backtests(
    engine: EngineDep,
    pagination: PaginationDep,
    status: RunStatus | None = None,
) -> BacktestListResponse:
    """List backtests newest first; filter by lifecycle ``status``."""
    rows, total = svc.list_backtests(
        engine,
        status=status,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return BacktestListResponse(
        data=[_summary(r) for r in rows],
        pagination=PaginationMeta(
            limit=pagination.limit,
            offset=pagination.offset,
            count=len(rows),
            total=total,
        ),
    )


@router.get("/{backtest_id}", response_model=BacktestDetail)
def get_backtest(backtest_id: str, engine: EngineDep) -> BacktestDetail:
    """Detail for one backtest: status, spec, artifacts, error."""
    detail = svc.get_backtest_detail(engine, backtest_id)
    if detail is None:
        raise KeyError(f"backtest {backtest_id!r} not found")
    return _detail(detail)


@router.get("/{backtest_id}/nav", response_model=BacktestNavResponse)
def backtest_nav(
    backtest_id: str,
    engine: EngineDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query(alias="to")] = None,
    interval: Literal["1d", "1w", "1M"] = "1d",
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> BacktestNavResponse:
    """NAV series; ``1w``/``1M`` resample to period-end, >2000 pts stride-downsample."""
    result = svc.nav_series(
        engine,
        backtest_id,
        from_date=from_,
        to_date=to,
        interval=interval,
        limit=limit,
        offset=offset,
    )
    return BacktestNavResponse(
        data=[BacktestNavRow(**r) for r in result["rows"]],
        interval=str(result["interval"]),
        downsampled=bool(result["downsampled"]),
        source_rows=int(result["source_rows"]),
        total=int(result["total"]),
    )


@router.get("/{backtest_id}/stats", response_model=BacktestStatsResponse)
def backtest_stats(backtest_id: str, engine: EngineDep) -> BacktestStatsResponse:
    """Scalar backtest statistics (empty until the run succeeds)."""
    rows = svc.backtest_stats(engine, backtest_id)
    return BacktestStatsResponse(data=[BacktestStatRow(**r) for r in rows])


@router.get("/{backtest_id}/trades", response_model=BacktestTradesResponse)
def backtest_trades(
    backtest_id: str,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=svc.EXPORT_ROW_CAP)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    format: Literal["json", "csv"] = "json",
) -> BacktestTradesResponse | Response:
    """Trade list; ``format=csv`` exports up to ``EXPORT_ROW_CAP`` (50k) rows.

    CSV exports are truncated at the cap with ``X-Export-Truncated: true``
    and an ``X-Export-Note`` header (documented choice over a 422).
    """
    is_csv = format == "csv"
    result = svc.backtest_trades(
        engine,
        backtest_id,
        limit=svc.EXPORT_ROW_CAP if is_csv else limit,
        offset=offset,
        max_rows=svc.EXPORT_ROW_CAP if is_csv else None,
    )
    rows = result["rows"]
    total = int(result["total"])
    truncated = bool(result["truncated"])
    if is_csv:
        csv_text = mq.rows_to_csv(rows, fieldnames=_TRADE_FIELDS)
        response = mq.csv_response(csv_text, filename=f"backtest-{backtest_id}-trades.csv")
        if truncated:
            response.headers["X-Export-Truncated"] = "true"
            response.headers["X-Export-Note"] = (
                f"export truncated at {svc.EXPORT_ROW_CAP} rows; total {total}"
            )
        return response
    return BacktestTradesResponse(
        data=[BacktestTradeRow(**r) for r in rows],
        pagination=PaginationMeta(
            limit=limit,
            offset=offset,
            count=len(rows),
            total=total,
        ),
        truncated=truncated,
    )


@router.get("/{backtest_id}/attribution", response_model=BacktestAttributionResponse)
def backtest_attribution(backtest_id: str, engine: EngineDep) -> BacktestAttributionResponse:
    """Return attribution by ``sector`` / ``analyst`` / ``rating_action``."""
    rows = svc.backtest_attribution(engine, backtest_id)
    return BacktestAttributionResponse(data=[BacktestAttributionRow(**r) for r in rows])


@router.get("/{backtest_id}/monthly", response_model=BacktestMonthlyResponse)
def backtest_monthly(backtest_id: str, engine: EngineDep) -> BacktestMonthlyResponse:
    """Monthly compounded returns (``prod(1 + month_return) == final nav``)."""
    rows = svc.backtest_monthly(engine, backtest_id)
    return BacktestMonthlyResponse(data=[BacktestMonthlyRow(**r) for r in rows])


__all__ = ["router"]
