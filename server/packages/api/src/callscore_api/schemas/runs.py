"""Schemas for the run lifecycle endpoints (``/api/runs``)."""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from callscore_api.schemas.common import PaginationMeta
from callscore_core.methodology import Methodology


class RunCreateRequest(BaseModel):
    """Body of ``POST /api/runs``.

    ``methodology`` accepts three forms (resolved server-side):

    * ``"default"`` — the built-in :class:`Methodology` preset;
    * a 64-hex fingerprint — the ``params_json`` of a stored
      ``methodology_version`` row (404 when unknown);
    * an inline methodology object — validated as :class:`Methodology`
      (422 on any violation).
    """

    model_config = ConfigDict(extra="forbid")

    as_of_date: date
    knowledge_cutoff: datetime | None = None
    """Defaults to end-of-day UTC on ``as_of_date + 1`` when omitted."""
    methodology: Methodology | str
    universe_filter: list[str] | None = None
    """Optional security-id allow-list restricting the run's universe."""


class RunSubmitResponse(BaseModel):
    """``202`` response for ``POST /api/runs``."""

    run_id: str
    status: str
    created: bool
    """``False`` when the idempotency key matched an existing run."""


class RunErrorInfo(BaseModel):
    """Parsed ``calc_run.error_json``."""

    type: str
    message: str


class RunSummary(BaseModel):
    """List-row view of a ``calc_run``."""

    run_id: str
    status: str
    progress: float = Field(ge=0.0, le=1.0)
    as_of_date: date
    knowledge_cutoff: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    methodology_fingerprint: str | None = None
    universe_filter: list[str] | None = None
    error: RunErrorInfo | None = None


class RunDetail(RunSummary):
    """Full single-run view: counts, fingerprints, methodology, timings."""

    algorithm_version: str
    source_fingerprint: str | None = None
    row_counts: dict[str, int] | None = None
    stage_artifacts: list[dict[str, Any]] | None = None
    methodology: Methodology | None = None
    """The resolved methodology parameters (from ``methodology_version``)."""


class RunListResponse(BaseModel):
    data: list[RunSummary]
    pagination: PaginationMeta


class CompositeRow(BaseModel):
    """One analyst's composite result inside a run comparison."""

    analyst_id: str
    peer_group: str | None = None
    composite_score: float | None = None
    rank_overall: int | None = None
    rank_in_peer_group: int | None = None
    is_ranked: bool
    unranked_reason: str | None = None


class MetricAggregate(BaseModel):
    """Per-metric cross-analyst summary inside a run comparison."""

    metric_code: str
    n_analysts: int
    n_observed: int
    """Analysts with a non-null metric ``value``."""
    mean_value: float | None = None


class RunCompareSide(BaseModel):
    """One side of ``GET /api/runs/compare``."""

    run: RunSummary
    composites: list[CompositeRow]
    metric_summary: list[MetricAggregate]


class RunCompareResponse(BaseModel):
    a: RunCompareSide
    b: RunCompareSide


__all__ = [
    "CompositeRow",
    "MetricAggregate",
    "RunCompareResponse",
    "RunCompareSide",
    "RunCreateRequest",
    "RunDetail",
    "RunErrorInfo",
    "RunListResponse",
    "RunSubmitResponse",
    "RunSummary",
]
