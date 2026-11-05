"""Schemas for the custom-benchmark endpoints (``/api/benchmarks``)."""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from callscore_api.schemas.common import PaginationMeta


class BenchmarkCreateRequest(BaseModel):
    """Body of ``POST /api/benchmarks``.

    ``spec`` is validated server-side as ``callscore_engine.benchmarks.
    BenchmarkSpec`` (422 on any violation). ``spec.benchmark_id`` may be
    omitted — a deterministic ``CSTM_<hash>`` id is generated.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=500)
    spec: dict[str, Any]


class BenchmarkSubmitResponse(BaseModel):
    """``201`` response for ``POST /api/benchmarks``."""

    benchmark_id: str
    status: str
    created: bool
    """``False`` when an identical spec already existed (idempotent create)."""


class BenchmarkMaterialization(BaseModel):
    """Last successful materialization metadata."""

    as_of_date: date | None = None
    row_count: int | None = None
    exclusion_counts: dict[str, int] = Field(default_factory=dict)
    materialized_at: datetime | None = None
    revision: int | None = None


class BenchmarkSummary(BaseModel):
    """List-row view of a ``benchmark_def``."""

    benchmark_id: str
    kind: str
    name: str | None = None
    status: str | None = None
    preset: str | None = None
    weighting: str | None = None
    rebalance: str | None = None
    row_count: int | None = None


class BenchmarkDetail(BenchmarkSummary):
    """Full single-benchmark view: stored spec + materialization info."""

    spec: dict[str, Any] | None = None
    materialization: BenchmarkMaterialization | None = None
    error: dict[str, Any] | None = None
    created_at: datetime | str | None = None


class BenchmarkListResponse(BaseModel):
    data: list[BenchmarkSummary]
    pagination: PaginationMeta


class BenchmarkMaterializeResponse(BaseModel):
    """Result of ``POST /api/benchmarks/{id}/materialize`` (synchronous)."""

    benchmark_id: str
    status: str
    row_count: int
    as_of_date: date
    exclusion_counts: dict[str, int]
    revision: int


class BenchmarkSeriesRow(BaseModel):
    session_date: date
    open_level: float
    close_level: float


class BenchmarkSeriesResponse(BaseModel):
    data: list[BenchmarkSeriesRow]
    from_date: date | None = None
    to_date: date | None = None
    total: int


__all__ = [
    "BenchmarkCreateRequest",
    "BenchmarkDetail",
    "BenchmarkListResponse",
    "BenchmarkMaterialization",
    "BenchmarkMaterializeResponse",
    "BenchmarkSeriesResponse",
    "BenchmarkSeriesRow",
    "BenchmarkSubmitResponse",
    "BenchmarkSummary",
]
