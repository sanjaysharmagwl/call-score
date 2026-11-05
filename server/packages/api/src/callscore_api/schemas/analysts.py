"""Response models for the analyst endpoints (``/api/analysts*``).

Run-scoped responses embed :class:`Provenance` (run_id, methodology
fingerprint, as_of_date) so clients can always tell which immutable run a
payload came from.
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class Provenance(BaseModel):
    """Which run/methodology produced the payload."""

    run_id: str
    methodology_fingerprint: str
    as_of_date: date
    run_status: str


class PageMeta(BaseModel):
    """Offset pagination metadata."""

    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class InlineMetric(BaseModel):
    """Compact per-metric summary embedded in list rows."""

    value: float | None = None
    n_obs: int | None = None
    unavailable_reason: str | None = None


class AnalystListItem(BaseModel):
    analyst_id: str
    name: str | None = None
    firm_id: str | None = None
    firm_name: str | None = None
    peer_group: str | None = None
    composite_score: float | None = None
    rank_overall: int | None = None
    rank_in_peer_group: int | None = None
    is_ranked: bool = False
    unranked_reason: str | None = None
    n_obs: int = Field(ge=0)
    key_metrics: dict[str, InlineMetric] = Field(default_factory=dict)


class AnalystListResponse(BaseModel):
    items: list[AnalystListItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    provenance: Provenance


class Spell(BaseModel):
    """Employment or coverage spell (``to_date`` inclusive, null open-ended)."""

    analyst_id: str | None = None
    firm_id: str | None = None
    security_id: str | None = None
    from_date: date
    to_date: date | None = None
    is_lead: bool | None = None


class FamilyScores(BaseModel):
    """Per-family mean oriented peer-z scores (display mirror of composite)."""

    return_skill: float | None = None
    timing: float | None = None
    price_target: float | None = None
    behavior: float | None = None
    consistency: float | None = None


class ShrinkageInfo(BaseModel):
    method: str
    shrunk_values_present: bool


class CompositeBlock(BaseModel):
    peer_group: str | None = None
    composite_score: float | None = None
    rank_overall: int | None = None
    rank_in_peer_group: int | None = None
    is_ranked: bool = False
    unranked_reason: str | None = None
    n_obs: int = Field(ge=0)
    family_scores: FamilyScores = Field(default_factory=FamilyScores)
    shrinkage: ShrinkageInfo


class MetricDetail(BaseModel):
    """One ``analyst_metric`` row enriched with catalog metadata."""

    metric_code: str
    family: str | None = None
    unit: str | None = None
    direction: str | None = None
    minimum_observations: int | None = None
    in_composite: bool | None = None
    formula: str | None = None
    unavailable_when: str | None = None
    value: float | None = None
    n_obs: int = Field(ge=0)
    std_err: float | None = None
    t_stat: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    shrunk_value: float | None = None
    peer_percentile: float | None = None
    is_significant: bool | None = None
    unavailable_reason: str | None = None


class AnalystProfileResponse(BaseModel):
    analyst_id: str
    name: str | None = None
    firm_id: str | None = None
    firm_name: str | None = None
    employment_spells: list[Spell]
    coverage_spells: list[Spell]
    peer_group: str | None = None
    composite: CompositeBlock | None = None
    metrics: list[MetricDetail]
    provenance: Provenance


class MetricDrilldownResponse(BaseModel):
    analyst_id: str
    items: list[MetricDetail]
    provenance: Provenance


class ExposureLeg(BaseModel):
    segment_no: int
    start_session: date
    start_point: str
    end_session: date
    end_point: str
    position_weight: float | None = None
    weight_source: str


class ReturnSummary(BaseModel):
    """``to_exit`` horizon return leg attached to a rating row."""

    stock_return: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    terminated_by_delisting: bool | None = None
    unavailable_reason: str | None = None


class RatingListItem(BaseModel):
    interval_id: str
    event_id: str
    security_id: str
    native_rating: str
    canonical_rating: str
    action_type: str
    announced_ts: datetime | None = None
    effective_ts: datetime | None = None
    entry_session: date
    entry_point: str
    entry_ts: datetime | None = None
    exit_session: date
    exit_point: str
    exit_ts: datetime | None = None
    exit_reason: str
    is_investable: bool
    non_investable_reason: str | None = None
    flags: str | None = None
    price_target: float | None = None
    pt_currency: str | None = None
    pt_horizon_months: int | None = None
    exposures: list[ExposureLeg] = Field(default_factory=list)
    return_summary: ReturnSummary


class RatingListResponse(BaseModel):
    items: list[RatingListItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    provenance: Provenance


class RatingDetailResponse(BaseModel):
    """Single rating/interval with legs and a server-built explanation."""

    interval_id: str
    event_id: str
    analyst_id: str
    firm_id: str
    security_id: str
    native_rating: str
    canonical_rating: str
    action_type: str
    announced_ts: datetime | None = None
    effective_ts: datetime | None = None
    knowledge_ts: datetime | None = None
    available_ts: datetime | None = None
    entry_session: date
    entry_point: str
    entry_ts: datetime | None = None
    exit_session: date
    exit_point: str
    exit_ts: datetime | None = None
    exit_reason: str
    expiry_date: date | None = None
    decay_start_session: date | None = None
    is_investable: bool
    non_investable_reason: str | None = None
    flags: str | None = None
    event: dict[str, Any] | None = None
    exposures: list[dict[str, Any]]
    returns: list[dict[str, Any]]
    contributions: list[dict[str, Any]]
    event_window: dict[str, Any] | None = None
    target_outcome: dict[str, Any] | None = None
    explanation: str
    provenance: Provenance


class NavPoint(BaseModel):
    session_date: date
    nav: float
    benchmark_nav: float
    active_return: float | None = None
    gross_exposure: float
    turnover: float
    cost: float
    drawdown: float


class PositionsSnapshot(BaseModel):
    session_date: date | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)


class PortfolioResponse(BaseModel):
    analyst_id: str
    points: list[NavPoint]
    total_points: int = Field(ge=0)
    downsampled: bool = False
    positions: PositionsSnapshot
    provenance: Provenance
