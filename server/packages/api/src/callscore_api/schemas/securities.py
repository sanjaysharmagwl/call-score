"""Response models for the securities endpoints.

Pagination reuses ``callscore_api.schemas.common.PaginationMeta``; run
provenance reuses ``common.Provenance``. ``SourceProvenance`` is distinct: it
describes *source-data* revisions/manifests, not calculation runs.
"""

from datetime import date, datetime

from pydantic import BaseModel

from callscore_api.schemas.common import PaginationMeta, Provenance


class SourceRevisionInfo(BaseModel):
    source_id: str
    revision: str
    captured_at: datetime


class GenerationManifestInfo(BaseModel):
    id: int
    seed: int
    profile: str
    table_checksums_json: str
    created_at: datetime


class SourceProvenance(BaseModel):
    """Source-data provenance (distinct from calculation ``run_id``)."""

    source_revisions: list[SourceRevisionInfo] = []
    generation_manifest: GenerationManifestInfo | None = None


class SecurityListItem(BaseModel):
    security_id: str
    name: str
    ticker: str | None = None
    sector_id: str | None = None
    sector_name: str | None = None
    region: str | None = None
    currency: str | None = None
    exchange_id: str | None = None
    cap_bucket: str | None = None
    listing_date: date | None = None
    is_delisted: bool = False
    classification_from_date: date | None = None
    classification_to_date: date | None = None


class SecurityListResponse(BaseModel):
    data: list[SecurityListItem]
    pagination: PaginationMeta
    provenance: SourceProvenance | None = None


class ClassificationSpell(BaseModel):
    from_date: date
    to_date: date | None = None
    exchange_id: str
    currency: str
    sector_id: str
    region: str
    cap_bucket: str
    listing_date: date
    delisting_date: date | None = None


class IdentifierSpell(BaseModel):
    id_type: str
    value: str
    from_date: date
    to_date: date | None = None


class DelistingInfo(BaseModel):
    delisting_date: date
    final_return: float
    reason: str


class PriceCoverage(BaseModel):
    n_rows: int
    first_session: date | None = None
    last_session: date | None = None


class SecurityDetailResponse(BaseModel):
    security_id: str
    name: str
    latest_classification: ClassificationSpell | None = None
    sector_name: str | None = None
    is_delisted: bool = False
    delisting: DelistingInfo | None = None
    identifiers: list[IdentifierSpell] = []
    classification_spells: list[ClassificationSpell] = []
    coverage_analysts: int = 0
    price_coverage: PriceCoverage
    rating_event_count: int = 0
    provenance: SourceProvenance | None = None


class PriceBar(BaseModel):
    session_date: date
    period_start: date | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    adj_factor: float | None = None
    cash_dividend: float | None = None
    tr_index: float | None = None
    is_halted: bool = False
    n_sessions: int | None = None
    adj_open: float | None = None
    adj_high: float | None = None
    adj_low: float | None = None
    adj_close: float | None = None


class PricesResponse(BaseModel):
    security_id: str
    interval: str
    rows: list[PriceBar]
    total: int
    downsampled: bool = False
    source_rows: int = 0
    provenance: SourceProvenance | None = None


class RatingEventItem(BaseModel):
    event_id: str
    analyst_id: str
    analyst_name: str | None = None
    firm_id: str | None = None
    security_id: str | None = None
    action_type: str
    native_rating: str | None = None
    canonical_rating: str
    prior_canonical_rating: str | None = None
    announced_ts: datetime
    effective_ts: datetime | None = None
    knowledge_ts: datetime | None = None
    is_correction: bool = False
    price_target: float | None = None
    pt_currency: str | None = None
    pt_horizon_months: int | None = None
    expiry_date: date | None = None
    scale_id: str | None = None
    source: str | None = None
    interval_id: str | None = None
    entry_session: date | None = None
    entry_point: str | None = None
    exit_session: date | None = None
    exit_point: str | None = None
    exit_reason: str | None = None
    is_investable: bool | None = None
    stock_return: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    benchmark_id: str | None = None
    position_weight: float | None = None
    contribution: float | None = None


class RatingsResponse(BaseModel):
    security_id: str
    run_id: str | None = None
    data: list[RatingEventItem]
    pagination: PaginationMeta


class ConsensusPoint(BaseModel):
    session_date: date
    n_ratings: int
    consensus_score: float | None = None
    target_price: float | None = None
    target_currency: str | None = None


class TargetOutcomeItem(BaseModel):
    event_id: str
    analyst_id: str
    canonical_rating: str | None = None
    action_type: str | None = None
    announced_ts: datetime | None = None
    entry_session: date
    horizon_end_session: date
    entry_price: float
    target_price: float
    implied_return: float
    realized_return: float | None = None
    hit: bool | None = None
    is_matured: bool


class ConsensusResponse(BaseModel):
    security_id: str
    run_id: str | None = None
    consensus: list[ConsensusPoint]
    target_outcomes: list[TargetOutcomeItem] = []
    provenance: SourceProvenance | None = None


class HorizonAggregate(BaseModel):
    horizon: str
    n: int
    stock_return: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    stock_return_n: int = 0
    benchmark_return_n: int = 0
    excess_return_n: int = 0


class SecurityPerformanceResponse(BaseModel):
    security_id: str
    run: Provenance
    n_ratings: int = 0
    n_investable: int = 0
    mean_excess_return: float | None = None
    hit_rate: float | None = None
    mean_contribution: float | None = None
    target_hit_rate: float | None = None
    n_target_outcomes: int = 0
    by_horizon: list[HorizonAggregate] = []
