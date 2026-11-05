"""Schemas for meta/catalog endpoints (``/api/meta/*``) and readiness."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from callscore_core.enums import MetricFamily
from callscore_core.methodology import Methodology


class EnumCatalog(BaseModel):
    """All ``callscore_core.enums`` StrEnum classes, name -> member values."""

    enums: dict[str, list[str]]


class MetricCatalogEntry(BaseModel):
    """One metric-catalog entry (``callscore_core.metrics.MetricDefinition``).

    ``min_obs`` is the catalog ``minimum_observations``; ``scored`` marks
    metrics with a sampling inference (bootstrap); ``contributes_to_composite``
    is the catalog ``in_composite`` flag.
    """

    code: str
    family: MetricFamily
    unit: Literal["return", "ratio", "fraction", "count", "per_year"]
    direction: Literal["higher", "lower", "descriptive"]
    horizon: Literal["to_exit", "primary_fixed", "daily", "event_window", "target", "none"]
    required_inputs: list[str]
    formula: str
    estimand: str
    unavailable_when: str
    min_obs: int = Field(ge=1)
    scored: bool
    contributes_to_composite: bool
    inference: Literal["quarter_block_bootstrap", "none"]
    shrink: bool
    aggregate: Literal["mean", "annualized_mean", "annualized_sharpe", "none"]
    null_value: float | None = None


class MetricCatalogResponse(BaseModel):
    """Full metric catalog plus the composite spec and its fingerprint."""

    metrics: list[MetricCatalogEntry]
    composite_spec: dict[str, Any]
    fingerprint: str
    """``callscore_core.metrics.METRIC_CATALOG_FINGERPRINT``."""


class MethodologyDefaultsResponse(BaseModel):
    """Built-in methodology presets (currently just ``"default"``)."""

    methodology: Methodology
    fingerprint: str
    presets: list[str]


class ReadinessCheck(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class LastRunInfo(BaseModel):
    """Summary of the most recent ``succeeded`` calc run."""

    run_id: str
    as_of_date: date
    finished_at: datetime | None = None
    methodology_fingerprint: str | None = None


class ReadinessResponse(BaseModel):
    """``GET /api/ready`` payload.

    Always HTTP 200; ``status`` is ``"ready"`` only when every check passed
    (database reachable, schema at ``SCHEMA_VERSION``), else ``"degraded"``.
    """

    status: Literal["ready", "degraded"]
    checks: list[ReadinessCheck]
    schema_version: int | None = None
    expected_schema_version: int
    row_counts: dict[str, int] = Field(default_factory=dict)
    last_successful_run: LastRunInfo | None = None


__all__ = [
    "EnumCatalog",
    "LastRunInfo",
    "MetricCatalogEntry",
    "MetricCatalogResponse",
    "MethodologyDefaultsResponse",
    "ReadinessCheck",
    "ReadinessResponse",
]
