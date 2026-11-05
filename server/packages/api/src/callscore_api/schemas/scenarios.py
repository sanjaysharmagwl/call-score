"""Schemas for the scenario endpoints (``/api/scenarios``)."""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from callscore_api.schemas.common import PaginationMeta
from callscore_api.schemas.methodologies import FieldChange
from callscore_core.methodology import Methodology


class ScenarioCreateRequest(BaseModel):
    """Body of ``POST /api/scenarios``.

    ``base_methodology`` selects the methodology the overrides apply to:

    * ``None``/``"default"`` — the built-in :class:`Methodology` preset;
    * a 64-hex fingerprint — a stored ``methodology_version`` (404 unknown);
    * an inline methodology object — validated as :class:`Methodology`.

    ``overrides`` is a partial ``Methodology`` payload: every key must be a
    real methodology field (422 otherwise); ``position_weights`` and
    ``family_weights`` dicts are shallow-merged onto the base. The merged
    result must itself be a valid methodology.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=500)
    description: str | None = None
    base_methodology: Methodology | str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    universe_filter: list[str] | None = None
    """Default security-id allow-list applied to scenario executions."""


class ScenarioSummary(BaseModel):
    """List-row view of a stored scenario."""

    scenario_id: str
    name: str
    description: str | None = None
    created_at: datetime
    base_fingerprint: str | None = None
    base_ref: str | None = None
    """How the base was specified: ``default``, a fingerprint, or ``inline``."""
    overrides: dict[str, Any] = Field(default_factory=dict)
    universe_filter: list[str] | None = None
    run_count: int = 0
    last_run_id: str | None = None


class ScenarioDetail(ScenarioSummary):
    """Full scenario view: resolved base params and run/sweep history."""

    base_methodology: Methodology | None = None
    """Stored base parameters re-validated; ``None`` only on corruption."""
    run_ids: list[str] = Field(default_factory=list)
    last_execution: dict[str, Any] | None = None
    sweeps: list[dict[str, Any]] = Field(default_factory=list)


class ScenarioListResponse(BaseModel):
    data: list[ScenarioSummary]
    pagination: PaginationMeta


class ScenarioExecuteRequest(BaseModel):
    """Body of ``POST /api/scenarios/{id}/execute``."""

    model_config = ConfigDict(extra="forbid")

    as_of_date: date
    knowledge_cutoff: datetime | None = None
    universe_filter: list[str] | None = None
    """Overrides the scenario's stored default universe for this run."""


class ScenarioInlineExecuteRequest(ScenarioExecuteRequest):
    """Body of ``POST /api/scenarios/execute`` — run an unsaved scenario.

    Same fields as :class:`ScenarioCreateRequest` plus the execution
    parameters; ``persist=True`` also stores it as a scenario first.
    """

    name: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    base_methodology: Methodology | str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    persist: bool = False


class StageImpact(BaseModel):
    """Per-stage invalidation detail inside an :class:`InvalidationReport`."""

    stage: str
    """Stage name (``intervals`` ... ``statistics``)."""
    fields_changed: list[str] = Field(default_factory=list)
    """``STAGE_METHODOLOGY_FIELDS`` entries that differ directly."""
    fingerprint_changes: bool
    """True when this stage's cache fingerprint will differ — i.e. it or an
    upstream stage consumed a changed field (fingerprints chain upstreams)."""


class InvalidationReport(BaseModel):
    """Which pipeline stages a methodology change invalidates."""

    methodology_a_fingerprint: str
    methodology_b_fingerprint: str
    identical: bool
    changed_fields: list[FieldChange] = Field(default_factory=list)
    stages: list[StageImpact] = Field(default_factory=list)
    affected_stages: list[str] = Field(default_factory=list)
    first_affected_stage: str | None = None


class ScenarioExecuteResponse(BaseModel):
    """``202`` response for scenario execution."""

    scenario_id: str | None = None
    """``None`` for non-persisted inline executions."""
    name: str | None = None
    run_id: str
    status: str
    created: bool
    """``False`` when the idempotency key matched an existing run."""
    methodology_fingerprint: str
    as_of_date: date
    knowledge_cutoff: datetime
    universe_filter: list[str] | None = None
    invalidation: InvalidationReport
    """Stage-level impact of the scenario's overrides vs its base."""


class UniverseComparison(BaseModel):
    """Explicit universe-compatibility report for a run comparison."""

    a: list[str] | None = None
    b: list[str] | None = None
    compatible: bool
    """True only when both ``universe_filter`` sets are identical (``None``
    == ``None``). ``False`` means deltas mix different universes — interpret
    with care."""
    intersection: list[str] | None = None
    """Common security ids when both sides are concrete lists; else ``None``."""


class AnalystDelta(BaseModel):
    """One analyst's composite delta between two runs (``b - a``)."""

    analyst_id: str
    in_a: bool
    in_b: bool
    composite_score_a: float | None = None
    composite_score_b: float | None = None
    score_delta: float | None = None
    rank_overall_a: int | None = None
    rank_overall_b: int | None = None
    rank_delta: int | None = None
    """Positive = moved down in the ranking (rank numbers increase downward)."""
    rank_in_peer_group_a: int | None = None
    rank_in_peer_group_b: int | None = None
    peer_rank_delta: int | None = None
    is_ranked_a: bool = False
    is_ranked_b: bool = False
    transition: str
    """``ranked`` | ``lost_rank`` | ``gained_rank`` | ``unranked`` |
    ``only_in_a`` | ``only_in_b``."""


class CompareSummary(BaseModel):
    """Aggregate counts for a scenario run comparison."""

    n_analysts_a: int
    n_analysts_b: int
    n_common: int
    only_in_a: int
    only_in_b: int
    ranked_to_ranked: int
    ranked_to_unranked: int
    unranked_to_ranked: int
    unranked_to_unranked: int


class RankCorrelation(BaseModel):
    """Spearman rank correlation over analysts ranked in BOTH runs."""

    method: str = "spearman_overall_rank"
    n: int
    spearman: float | None = None
    """``None`` when fewer than 2 common ranked analysts or zero variance."""


class MetricDelta(BaseModel):
    """Per-metric cross-analyst delta summary (``b - a`` on ``value``)."""

    metric_code: str
    family: str
    """Metric family from the catalog; ``"other"`` for uncatalogued codes."""
    n_pairs: int
    """(analyst, metric) pairs where both runs have a non-null ``value``."""
    mean_delta: float
    mean_abs_delta: float
    max_abs_delta: float


class FamilyDelta(BaseModel):
    """Coarse per-family rollup of :class:`MetricDelta` mean deltas.

    ``mean_delta`` is the unweighted mean of the family's per-metric mean
    deltas — metric units differ, so treat it as a directional signal only.
    """

    family: str
    n_metrics: int
    mean_delta: float


class ScenarioCompareResponse(BaseModel):
    """Delta report between two runs (``GET /scenarios/compare`` and
    ``GET /scenarios/{id}/compare``)."""

    run_a_id: str
    run_b_id: str
    run_a_status: str
    run_b_status: str
    as_of_date_a: date | None = None
    as_of_date_b: date | None = None
    methodology_a_fingerprint: str | None = None
    methodology_b_fingerprint: str | None = None
    universe: UniverseComparison
    summary: CompareSummary
    rank_correlation: RankCorrelation
    analysts: list[AnalystDelta] = Field(default_factory=list)
    metric_deltas: list[MetricDelta] = Field(default_factory=list)
    family_deltas: list[FamilyDelta] = Field(default_factory=list)


class SweepJob(BaseModel):
    """One submitted combination inside a sensitivity sweep."""

    index: int
    spec: dict[str, Any]
    """``{"param_path": ..., "value": ...}`` or ``{"overrides": {...}}``."""
    methodology_fingerprint: str
    run_id: str
    status: str
    created: bool


class SensitivitySweepRequest(BaseModel):
    """Body of ``POST /api/scenarios/sensitivity``.

    Exactly one sweep dimension: ``param_path`` + ``values`` (dotted/indexed
    path into the methodology JSON, e.g. ``transaction_cost_bps`` or
    ``position_weights.buy``), or ``overrides`` whose values may be lists
    (cartesian product). Total combinations are hard-capped at 25.

    ``scenario_id`` sweeps on top of that scenario's effective methodology;
    otherwise ``base_methodology`` (default: the built-in preset) is used.
    """

    model_config = ConfigDict(extra="forbid")

    as_of_date: date
    knowledge_cutoff: datetime | None = None
    universe_filter: list[str] | None = None
    base_methodology: Methodology | str | None = None
    scenario_id: str | None = None
    param_path: str | None = None
    values: list[Any] | None = None
    overrides: dict[str, Any] | None = None


class SensitivitySweepResponse(BaseModel):
    """``202`` response for a sensitivity sweep."""

    sweep_id: str
    """Deterministic id derived from base + as_of + cutoff + combos."""
    count: int
    as_of_date: date
    knowledge_cutoff: datetime
    universe_filter: list[str] | None = None
    scenario_id: str | None = None
    jobs: list[SweepJob] = Field(default_factory=list)


__all__ = [
    "AnalystDelta",
    "CompareSummary",
    "FamilyDelta",
    "InvalidationReport",
    "MetricDelta",
    "RankCorrelation",
    "ScenarioCompareResponse",
    "ScenarioCreateRequest",
    "ScenarioDetail",
    "ScenarioExecuteRequest",
    "ScenarioExecuteResponse",
    "ScenarioInlineExecuteRequest",
    "ScenarioListResponse",
    "ScenarioSummary",
    "SensitivitySweepRequest",
    "SensitivitySweepResponse",
    "StageImpact",
    "SweepJob",
    "UniverseComparison",
]
