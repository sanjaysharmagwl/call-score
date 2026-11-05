"""Custom-benchmark endpoints: CRUD, materialization, series.

Routes (mounted under ``/api`` by the integrator)::

    POST /benchmarks                        create a custom benchmark (201)
    GET  /benchmarks                        list definitions (status filter)
    GET  /benchmarks/{benchmark_id}         detail: spec + materialization
    POST /benchmarks/{benchmark_id}/materialize   synchronous rebuild (200)
    GET  /benchmarks/{benchmark_id}/series?from&to   materialized levels

Materialization is synchronous by design (bounded by the spec's date range);
see ``services.benchmarks.materialize_benchmark``.
"""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Engine

from callscore_api.deps import get_engine
from callscore_api.schemas.benchmarks import (
    BenchmarkCreateRequest,
    BenchmarkDetail,
    BenchmarkListResponse,
    BenchmarkMaterialization,
    BenchmarkMaterializeResponse,
    BenchmarkSeriesResponse,
    BenchmarkSeriesRow,
    BenchmarkSubmitResponse,
    BenchmarkSummary,
)
from callscore_api.schemas.common import PaginationMeta, PaginationParams, get_pagination
from callscore_api.services import benchmarks as svc

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])

EngineDep = Annotated[Engine, Depends(get_engine)]
PaginationDep = Annotated[PaginationParams, Depends(get_pagination)]

StatusFilter = Annotated[str | None, Query(description="Filter by lifecycle status")]


def _detail(row: dict[str, Any]) -> BenchmarkDetail:
    materialization = row.get("materialization")
    return BenchmarkDetail(
        **{k: v for k, v in row.items() if k not in {"materialization"}},
        materialization=(
            BenchmarkMaterialization(**materialization)
            if isinstance(materialization, dict)
            else None
        ),
    )


@router.post("", status_code=201, response_model=BenchmarkSubmitResponse)
def create_benchmark(body: BenchmarkCreateRequest, engine: EngineDep) -> BenchmarkSubmitResponse:
    """Create a ``kind='custom'`` benchmark definition (status ``draft``).

    ``spec`` is validated as ``BenchmarkSpec`` — 422 on any violation.
    Idempotent: an identical spec returns the existing row.
    """
    result = svc.create_benchmark(engine, name=body.name, spec=body.spec)
    return BenchmarkSubmitResponse(
        benchmark_id=str(result["benchmark_id"]),
        status=str(result["status"]),
        created=bool(result["created"]),
    )


@router.get("", response_model=BenchmarkListResponse)
def list_benchmarks(
    engine: EngineDep,
    pagination: PaginationDep,
    status: StatusFilter = None,
) -> BenchmarkListResponse:
    """List benchmark definitions, ``benchmark_id`` order."""
    if status is not None and status not in svc.BENCHMARK_STATUSES:
        raise ValueError(f"unknown benchmark status {status!r}")
    rows, total = svc.list_benchmarks(
        engine, status=status, limit=pagination.limit, offset=pagination.offset
    )
    return BenchmarkListResponse(
        data=[BenchmarkSummary(**r) for r in rows],
        pagination=PaginationMeta(
            limit=pagination.limit,
            offset=pagination.offset,
            count=len(rows),
            total=total,
        ),
    )


@router.get("/{benchmark_id}", response_model=BenchmarkDetail)
def get_benchmark(benchmark_id: str, engine: EngineDep) -> BenchmarkDetail:
    """Detail for one benchmark: stored spec + materialization info."""
    return _detail(svc.get_benchmark(engine, benchmark_id))


@router.post("/{benchmark_id}/materialize", response_model=BenchmarkMaterializeResponse)
def materialize_benchmark(benchmark_id: str, engine: EngineDep) -> BenchmarkMaterializeResponse:
    """Synchronously rebuild ``benchmark_daily`` rows for a custom benchmark.

    Idempotent (delete + insert in one transaction). Engine/spec failures
    mark the def ``failed`` and surface as 422/500 via the error handlers.
    """
    result = svc.materialize_benchmark(engine, benchmark_id)
    return BenchmarkMaterializeResponse(
        benchmark_id=str(result["benchmark_id"]),
        status=str(result["status"]),
        row_count=int(result["row_count"]),
        as_of_date=result["as_of_date"],
        exclusion_counts={str(k): int(v) for k, v in result["exclusion_counts"].items()},
        revision=int(result["revision"]),
    )


@router.get("/{benchmark_id}/series", response_model=BenchmarkSeriesResponse)
def benchmark_series(
    benchmark_id: str,
    engine: EngineDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query(alias="to")] = None,
) -> BenchmarkSeriesResponse:
    """Materialized ``benchmark_daily`` rows (open/close levels) for charts."""
    result = svc.benchmark_series(engine, benchmark_id, from_date=from_, to_date=to)
    return BenchmarkSeriesResponse(
        data=[BenchmarkSeriesRow(**r) for r in result["rows"]],
        from_date=from_,
        to_date=to,
        total=int(result["total"]),
    )


__all__ = ["router"]
