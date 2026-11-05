"""SQLAlchemy models for the callscore SQLite schema.

Table layout follows the frame contracts in ``callscore_core.frames``:

* Reference data (firm, analyst, coverage/employment spells, sector, security,
  PIT identifiers/classifications, rating scales) is append-mostly and keyed by
  natural business keys.
* ``rating_event`` stores every column of ``RATING_EVENT_FRAME`` plus ``source``.
* Market tables mirror ``SECURITY_DAILY_FRAME``, ``BENCHMARK_DAILY_FRAME``,
  ``BETA_FRAME``, ``DELISTING_FRAME`` and ``FX_DAILY_FRAME``.
* Config/audit tables record methodology versions, generation manifests, run
  bookkeeping, writer leases and saved views.
* Derived tables are run-scoped: every row carries ``run_id`` referencing
  ``calc_run`` with ``ON DELETE RESTRICT`` and the run id is part of the
  primary key, so runs are immutable once written.

Money columns use :class:`~callscore_store.types.Money` (exact int64
micro-units); all ``*_ts`` columns use :class:`~callscore_store.types.UTCDateTime`.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from callscore_core.enums import CanonicalRating, ExitReason, RatingAction, RunStatus
from callscore_core.frames import (
    HORIZON_TO_EXIT,
    POINT_CLOSE,
    POINT_OPEN,
    WEIGHT_SOURCES,
)
from callscore_store.types import Money, UTCDateTime

# Delisting reasons are a store-level vocabulary (not a core enum).
DELISTING_REASONS = ("acquired", "bankrupt", "merged", "voluntary")

# Corporate action types stored in ``corporate_action.action_type``.
CORPORATE_ACTION_TYPES = ("split", "cash_dividend", "stock_dividend", "spinoff", "merger")

# ``benchmark_def.kind`` values.
BENCHMARK_KINDS = ("market", "sector", "custom")


def _in(column: str, members: tuple[str, ...] | type[StrEnum]) -> str:
    """Build a ``col IN ('a','b')`` CHECK body; members are trusted literals."""
    values = tuple(m.value if isinstance(m, StrEnum) else m for m in members)
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _points_check(column: str) -> CheckConstraint:
    return CheckConstraint(_in(column, (POINT_OPEN, POINT_CLOSE)))


def _weight_source_check() -> CheckConstraint:
    return CheckConstraint(_in("weight_source", WEIGHT_SOURCES))


def _horizon_check() -> CheckConstraint:
    # Horizons are ``to_exit`` or ``d<N>`` (see callscore_core.frames.horizon_label).
    return CheckConstraint(
        f"horizon = '{HORIZON_TO_EXIT}' OR horizon GLOB 'd[0-9]*'",
    )


class Base(DeclarativeBase):
    """Declarative base for all callscore tables."""


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #


class Firm(Base):
    __tablename__ = "firm"

    firm_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    country: Mapped[str | None] = mapped_column(String(3))


class Analyst(Base):
    __tablename__ = "analyst"

    analyst_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    firm_id: Mapped[str | None] = mapped_column(ForeignKey("firm.firm_id"))


class AnalystEmployment(Base):
    """Employment spell: analyst at firm over ``[from_date, to_date]`` (inclusive)."""

    __tablename__ = "analyst_employment"
    __table_args__ = (CheckConstraint("to_date IS NULL OR from_date <= to_date"),)

    analyst_id: Mapped[str] = mapped_column(ForeignKey("analyst.analyst_id"), primary_key=True)
    firm_id: Mapped[str] = mapped_column(ForeignKey("firm.firm_id"))
    from_date: Mapped[date] = mapped_column(Date, primary_key=True)
    to_date: Mapped[date | None] = mapped_column(Date)


class AnalystCoverage(Base):
    """Coverage spell: analyst covers security over ``[from_date, to_date]``."""

    __tablename__ = "analyst_coverage"
    __table_args__ = (CheckConstraint("to_date IS NULL OR from_date <= to_date"),)

    analyst_id: Mapped[str] = mapped_column(ForeignKey("analyst.analyst_id"), primary_key=True)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"), primary_key=True)
    from_date: Mapped[date] = mapped_column(Date, primary_key=True)
    to_date: Mapped[date | None] = mapped_column(Date)
    is_lead: Mapped[bool] = mapped_column(Boolean)


class Exchange(Base):
    __tablename__ = "exchange"

    exchange_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    country: Mapped[str | None] = mapped_column(String(3))
    timezone: Mapped[str | None] = mapped_column(String(100))


class TradingSession(Base):
    """Exchange trading session: ``SESSION_FRAME``."""

    __tablename__ = "trading_session"
    __table_args__ = (CheckConstraint("open_ts < close_ts"),)

    exchange_id: Mapped[str] = mapped_column(ForeignKey("exchange.exchange_id"), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    close_ts: Mapped[datetime] = mapped_column(UTCDateTime)


class Sector(Base):
    __tablename__ = "sector"
    __table_args__ = (CheckConstraint("parent_sector_id IS NULL OR parent_sector_id <> sector_id"),)

    sector_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    parent_sector_id: Mapped[str | None] = mapped_column(ForeignKey("sector.sector_id"))


class Security(Base):
    __tablename__ = "security"

    security_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str] = mapped_column(String(500))


class SecurityIdentifier(Base):
    """Point-in-time external identifier (ticker, ISIN, ...) for a security."""

    __tablename__ = "security_identifier"
    __table_args__ = (
        CheckConstraint("to_date IS NULL OR from_date <= to_date"),
        CheckConstraint("length(value) > 0"),
    )

    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"), primary_key=True)
    id_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(100), primary_key=True)
    from_date: Mapped[date] = mapped_column(Date, primary_key=True)
    to_date: Mapped[date | None] = mapped_column(Date)


class SecurityClassification(Base):
    """Point-in-time classification spell: ``SECURITY_CLASSIFICATION_FRAME``."""

    __tablename__ = "security_classification"
    __table_args__ = (
        CheckConstraint("to_date IS NULL OR from_date <= to_date"),
        CheckConstraint("delisting_date IS NULL OR listing_date <= delisting_date"),
        CheckConstraint("length(currency) = 3"),
    )

    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"), primary_key=True)
    from_date: Mapped[date] = mapped_column(Date, primary_key=True)
    to_date: Mapped[date | None] = mapped_column(Date)
    exchange_id: Mapped[str] = mapped_column(ForeignKey("exchange.exchange_id"))
    currency: Mapped[str] = mapped_column(String(3))
    sector_id: Mapped[str] = mapped_column(ForeignKey("sector.sector_id"))
    region: Mapped[str] = mapped_column(String(100))
    cap_bucket: Mapped[str] = mapped_column(String(50))
    listing_date: Mapped[date] = mapped_column(Date)
    delisting_date: Mapped[date | None] = mapped_column(Date)


class RatingScale(Base):
    """Effective-dated rating scale version for a firm.

    Primary key is ``(scale_id, effective_from)`` so a scale can be redefined
    over time; ``rating_scale_map`` rows belong to one scale version.
    ``scale_id`` is a global identifier (e.g. ``firm:scale``), so ``firm_id``
    is an attribute, not part of the key.
    """

    __tablename__ = "rating_scale"
    __table_args__ = (
        CheckConstraint("n_tiers > 0"),
        CheckConstraint("effective_to IS NULL OR effective_from <= effective_to"),
    )

    scale_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    firm_id: Mapped[str] = mapped_column(ForeignKey("firm.firm_id"))
    effective_from: Mapped[date] = mapped_column(Date, primary_key=True)
    effective_to: Mapped[date | None] = mapped_column(Date)
    n_tiers: Mapped[int] = mapped_column(Integer)


class RatingScaleMap(Base):
    """Maps a native scale label to a canonical rating within one scale version."""

    __tablename__ = "rating_scale_map"
    __table_args__ = (
        ForeignKeyConstraint(
            ["scale_id", "effective_from"],
            ["rating_scale.scale_id", "rating_scale.effective_from"],
        ),
        CheckConstraint(_in("canonical_rating", CanonicalRating)),
        CheckConstraint("rank_within_scale >= 1"),
        CheckConstraint("length(native_label) > 0"),
    )

    scale_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    effective_from: Mapped[date] = mapped_column(Date, primary_key=True)
    native_label: Mapped[str] = mapped_column(String(200), primary_key=True)
    canonical_rating: Mapped[str] = mapped_column(String(50))
    rank_within_scale: Mapped[int] = mapped_column(Integer)


# --------------------------------------------------------------------------- #
# Event data
# --------------------------------------------------------------------------- #


class RatingEvent(Base):
    """All ``RATING_EVENT_FRAME`` columns plus ``source``.

    ``scale_id`` references the scale *identifier*; the effective-dated
    ``rating_scale`` row is resolved by the application layer at the event's
    ``announced_ts``, so no FK is declared here.
    """

    __tablename__ = "rating_event"
    __table_args__ = (
        Index("ix_rating_event_security_announced", "security_id", "announced_ts"),
        Index("ix_rating_event_analyst_announced", "analyst_id", "announced_ts"),
        Index("ix_rating_event_knowledge_ts", "knowledge_ts"),
        # One non-correction event per (analyst, security, announced_ts); corrections
        # may legitimately share a timestamp, so they are excluded via partial index.
        Index(
            "uq_rating_event_dedup",
            "analyst_id",
            "security_id",
            "announced_ts",
            unique=True,
            sqlite_where=text("is_correction = 0"),
        ),
        CheckConstraint(_in("action_type", RatingAction)),
        CheckConstraint(_in("canonical_rating", CanonicalRating)),
        CheckConstraint(
            "prior_canonical_rating IS NULL OR " + _in("prior_canonical_rating", CanonicalRating)
        ),
        CheckConstraint("pt_horizon_months IS NULL OR pt_horizon_months > 0"),
    )

    event_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    analyst_id: Mapped[str] = mapped_column(ForeignKey("analyst.analyst_id"))
    firm_id: Mapped[str] = mapped_column(ForeignKey("firm.firm_id"))
    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"))
    announced_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    effective_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    knowledge_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    action_type: Mapped[str] = mapped_column(String(50))
    native_rating: Mapped[str] = mapped_column(String(200))
    canonical_rating: Mapped[str] = mapped_column(String(50))
    prior_canonical_rating: Mapped[str | None] = mapped_column(String(50))
    scale_id: Mapped[str] = mapped_column(String(200))
    price_target: Mapped[Decimal | None] = mapped_column(Money)
    pt_currency: Mapped[str | None] = mapped_column(String(3))
    pt_horizon_months: Mapped[int | None] = mapped_column(Integer)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    supersedes_event_id: Mapped[str | None] = mapped_column(ForeignKey("rating_event.event_id"))
    is_correction: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(50), server_default="manual")


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #


class SecurityDaily(Base):
    """Daily OHLC/adjustment frame: ``SECURITY_DAILY_FRAME``."""

    __tablename__ = "security_daily"
    __table_args__ = (
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0"),
        CheckConstraint("high >= low"),
        CheckConstraint("adj_factor > 0"),
        CheckConstraint("cash_dividend IS NULL OR cash_dividend >= 0"),
        CheckConstraint("tr_index > 0"),
    )

    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Money)
    high: Mapped[Decimal] = mapped_column(Money)
    low: Mapped[Decimal] = mapped_column(Money)
    close: Mapped[Decimal] = mapped_column(Money)
    adj_factor: Mapped[float] = mapped_column(Float)
    cash_dividend: Mapped[Decimal | None] = mapped_column(Money)
    tr_index: Mapped[float] = mapped_column(Float)
    is_halted: Mapped[bool] = mapped_column(Boolean)


class CorporateAction(Base):
    """Corporate action (split, dividend, spinoff, ...) affecting a security."""

    __tablename__ = "corporate_action"
    __table_args__ = (
        Index("ix_corporate_action_security_date", "security_id", "session_date"),
        CheckConstraint(_in("action_type", CORPORATE_ACTION_TYPES)),
        CheckConstraint("ratio IS NULL OR ratio > 0"),
        CheckConstraint("cash_amount IS NULL OR cash_amount >= 0"),
    )

    action_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"))
    session_date: Mapped[date] = mapped_column(Date)
    action_type: Mapped[str] = mapped_column(String(50))
    ratio: Mapped[float | None] = mapped_column(Float)
    cash_amount: Mapped[Decimal | None] = mapped_column(Money)
    notes: Mapped[str | None] = mapped_column(Text)


class Delisting(Base):
    """Terminal delisting record: ``DELISTING_FRAME``."""

    __tablename__ = "delisting"
    __table_args__ = (CheckConstraint(_in("reason", DELISTING_REASONS)),)

    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"), primary_key=True)
    delisting_date: Mapped[date] = mapped_column(Date)
    final_return: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(50))


class BenchmarkDef(Base):
    """Benchmark definition; ``kind`` in market/sector/custom."""

    __tablename__ = "benchmark_def"
    __table_args__ = (CheckConstraint(_in("kind", BENCHMARK_KINDS)),)

    benchmark_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    kind: Mapped[str] = mapped_column(String(50))
    rule_json: Mapped[str | None] = mapped_column(Text)


class BenchmarkDaily(Base):
    """Daily benchmark levels: ``BENCHMARK_DAILY_FRAME``."""

    __tablename__ = "benchmark_daily"
    __table_args__ = (CheckConstraint("open_level > 0 AND close_level > 0"),)

    benchmark_id: Mapped[str] = mapped_column(
        ForeignKey("benchmark_def.benchmark_id"), primary_key=True
    )
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open_level: Mapped[float] = mapped_column(Float)
    close_level: Mapped[float] = mapped_column(Float)


class FactorReturnDaily(Base):
    """Daily return of a named factor (market, sector, style, ...)."""

    __tablename__ = "factor_return_daily"

    factor_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    factor_return: Mapped[float] = mapped_column(Float)


class SecurityBeta(Base):
    """Point-in-time beta estimate: ``BETA_FRAME``."""

    __tablename__ = "security_beta"
    __table_args__ = (CheckConstraint("window_days > 0"),)

    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    beta: Mapped[float] = mapped_column(Float)
    window_days: Mapped[int] = mapped_column(Integer)


class FxRateDaily(Base):
    """Daily FX rate: ``FX_DAILY_FRAME``."""

    __tablename__ = "fx_rate_daily"
    __table_args__ = (
        CheckConstraint("length(from_ccy) = 3 AND length(to_ccy) = 3"),
        CheckConstraint("rate > 0"),
    )

    from_ccy: Mapped[str] = mapped_column(String(3), primary_key=True)
    to_ccy: Mapped[str] = mapped_column(String(3), primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date, primary_key=True)
    rate: Mapped[float] = mapped_column(Float)


class LiquidityDaily(Base):
    """Daily liquidity snapshot: average daily value and free float."""

    __tablename__ = "liquidity_daily"
    __table_args__ = (
        CheckConstraint("adv_20d IS NULL OR adv_20d >= 0"),
        CheckConstraint("free_float IS NULL OR (free_float >= 0 AND free_float <= 1)"),
    )

    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    adv_20d: Mapped[float | None] = mapped_column(Float)
    free_float: Mapped[float | None] = mapped_column(Float)


class ConsensusDaily(Base):
    """Daily consensus snapshot per security (sell-side estimate aggregate)."""

    __tablename__ = "consensus_daily"
    __table_args__ = (CheckConstraint("n_ratings >= 0"),)

    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    n_ratings: Mapped[int] = mapped_column(Integer)
    consensus_score: Mapped[float | None] = mapped_column(Float)
    target_price: Mapped[Decimal | None] = mapped_column(Money)
    target_currency: Mapped[str | None] = mapped_column(String(3))


# --------------------------------------------------------------------------- #
# Config / audit
# --------------------------------------------------------------------------- #


class MethodologyVersion(Base):
    """Immutable methodology parameter set; ``fingerprint`` is unique."""

    __tablename__ = "methodology_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    params_json: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class GenerationManifest(Base):
    """Record of a synthetic-data generation run."""

    __tablename__ = "generation_manifest"
    __table_args__ = (CheckConstraint("seed >= 0"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seed: Mapped[int] = mapped_column(BigInteger)
    profile: Mapped[str] = mapped_column(String(200))
    config_json: Mapped[str] = mapped_column(Text)
    table_checksums_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class SourceRevision(Base):
    """Revision marker for an upstream data source."""

    __tablename__ = "source_revision"

    source_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    revision: Mapped[str] = mapped_column(String(200), primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime)
    metadata_json: Mapped[str | None] = mapped_column(Text)


class CalcRun(Base):
    """One engine calculation run; derived tables reference ``run_id``."""

    __tablename__ = "calc_run"
    __table_args__ = (
        CheckConstraint(_in("status", RunStatus)),
        CheckConstraint("progress >= 0 AND progress <= 1"),
    )

    run_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    methodology_version_id: Mapped[int] = mapped_column(ForeignKey("methodology_version.id"))
    as_of_date: Mapped[date] = mapped_column(Date)
    knowledge_cutoff: Mapped[datetime] = mapped_column(UTCDateTime)
    universe_filter_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50))
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    # Insert timestamp (schema v3). Nullable only for rows migrated from v2
    # whose ``started_at`` was NULL (queued-never-run); readers fall back.
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    row_counts_json: Mapped[str | None] = mapped_column(Text)
    error_json: Mapped[str | None] = mapped_column(Text)
    algorithm_version: Mapped[str] = mapped_column(String(100))
    source_fingerprint: Mapped[str | None] = mapped_column(String(64))


class Scenario(Base):
    """Named what-if scenario (parameter overrides + metadata)."""

    __tablename__ = "scenario"

    scenario_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    config_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class WriterLease(Base):
    """Single-row writer lease guarding concurrent writers.

    Exactly one row exists; ``id`` is pinned to 1 by CHECK.
    """

    __tablename__ = "writer_lease"
    __table_args__ = (CheckConstraint("id = 1"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holder: Mapped[str] = mapped_column(String(200))
    job_id: Mapped[str] = mapped_column(String(200))
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class SavedView(Base):
    """User-saved analytical view configuration."""

    __tablename__ = "saved_view"

    view_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    owner_id: Mapped[str | None] = mapped_column(String(200))
    config_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


# --------------------------------------------------------------------------- #
# Derived run-scoped tables
#
# Every derived table carries ``run_id`` -> ``calc_run.run_id`` ON DELETE
# RESTRICT and includes it in the primary key.
# --------------------------------------------------------------------------- #


class RatingInterval(Base):
    """Engine interval output: ``INTERVAL_FRAME`` plus ``run_id``."""

    __tablename__ = "rating_interval"
    __table_args__ = (
        _points_check("entry_point"),
        _points_check("exit_point"),
        CheckConstraint(_in("action_type", RatingAction)),
        CheckConstraint(_in("canonical_rating", CanonicalRating)),
        CheckConstraint(_in("exit_reason", ExitReason)),
        Index("ix_rating_interval_run_analyst", "run_id", "analyst_id"),
        Index("ix_rating_interval_run_security", "run_id", "security_id"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("calc_run.run_id", ondelete="RESTRICT"), primary_key=True
    )
    interval_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("rating_event.event_id"))
    analyst_id: Mapped[str] = mapped_column(ForeignKey("analyst.analyst_id"))
    firm_id: Mapped[str] = mapped_column(ForeignKey("firm.firm_id"))
    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"))
    native_rating: Mapped[str] = mapped_column(String(200))
    canonical_rating: Mapped[str] = mapped_column(String(50))
    action_type: Mapped[str] = mapped_column(String(50))
    announced_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    effective_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    knowledge_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    available_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    entry_session: Mapped[date] = mapped_column(Date)
    entry_point: Mapped[str] = mapped_column(String(10))
    entry_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    exit_session: Mapped[date] = mapped_column(Date)
    exit_point: Mapped[str] = mapped_column(String(10))
    exit_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    exit_reason: Mapped[str] = mapped_column(String(50))
    expiry_date: Mapped[date | None] = mapped_column(Date)
    decay_start_session: Mapped[date | None] = mapped_column(Date)
    is_investable: Mapped[bool] = mapped_column(Boolean)
    non_investable_reason: Mapped[str | None] = mapped_column(String(200))
    flags: Mapped[str | None] = mapped_column(Text)


class IntervalExposure(Base):
    """Exposure segments of an interval: ``EXPOSURE_FRAME`` plus ``run_id``."""

    __tablename__ = "interval_exposure"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "interval_id"],
            ["rating_interval.run_id", "rating_interval.interval_id"],
        ),
        _points_check("start_point"),
        _points_check("end_point"),
        _weight_source_check(),
        CheckConstraint("segment_no >= 0"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("calc_run.run_id", ondelete="RESTRICT"), primary_key=True
    )
    interval_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    segment_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_session: Mapped[date] = mapped_column(Date)
    start_point: Mapped[str] = mapped_column(String(10))
    end_session: Mapped[date] = mapped_column(Date)
    end_point: Mapped[str] = mapped_column(String(10))
    position_weight: Mapped[float | None] = mapped_column(Float)
    weight_source: Mapped[str] = mapped_column(String(50))


class IntervalReturn(Base):
    """Return legs of an interval: ``RETURN_FRAME`` plus ``run_id``."""

    __tablename__ = "interval_return"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "interval_id"],
            ["rating_interval.run_id", "rating_interval.interval_id"],
        ),
        _points_check("start_point"),
        _points_check("end_point"),
        _horizon_check(),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("calc_run.run_id", ondelete="RESTRICT"), primary_key=True
    )
    interval_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    horizon: Mapped[str] = mapped_column(String(50), primary_key=True)
    start_session: Mapped[date] = mapped_column(Date)
    start_point: Mapped[str] = mapped_column(String(10))
    end_session: Mapped[date | None] = mapped_column(Date)
    end_point: Mapped[str | None] = mapped_column(String(10))
    entry_price: Mapped[float | None] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float)
    stock_return: Mapped[float | None] = mapped_column(Float)
    benchmark_id: Mapped[str | None] = mapped_column(String(200))
    benchmark_return: Mapped[float | None] = mapped_column(Float)
    beta: Mapped[float | None] = mapped_column(Float)
    excess_return: Mapped[float | None] = mapped_column(Float)
    terminated_by_delisting: Mapped[bool] = mapped_column(Boolean)
    unavailable_reason: Mapped[str | None] = mapped_column(String(200))


class IntervalContribution(Base):
    """Contributions of an interval: ``CONTRIBUTION_FRAME`` plus ``run_id``."""

    __tablename__ = "interval_contribution"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "interval_id"],
            ["rating_interval.run_id", "rating_interval.interval_id"],
        ),
        _horizon_check(),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("calc_run.run_id", ondelete="RESTRICT"), primary_key=True
    )
    interval_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    horizon: Mapped[str] = mapped_column(String(50), primary_key=True)
    analyst_id: Mapped[str] = mapped_column(ForeignKey("analyst.analyst_id"))
    entry_session: Mapped[date] = mapped_column(Date)
    position_weight: Mapped[float | None] = mapped_column(Float)
    excess_return: Mapped[float | None] = mapped_column(Float)
    contribution: Mapped[float | None] = mapped_column(Float)
    unavailable_reason: Mapped[str | None] = mapped_column(String(200))


class EventWindow(Base):
    """Announcement-window stats per event: ``EVENT_WINDOW_FRAME`` plus ``run_id``."""

    __tablename__ = "event_window"
    __table_args__ = ()

    run_id: Mapped[str] = mapped_column(
        ForeignKey("calc_run.run_id", ondelete="RESTRICT"), primary_key=True
    )
    event_id: Mapped[str] = mapped_column(ForeignKey("rating_event.event_id"), primary_key=True)
    analyst_id: Mapped[str] = mapped_column(ForeignKey("analyst.analyst_id"))
    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"))
    event_session: Mapped[date] = mapped_column(Date)
    weight_change: Mapped[float | None] = mapped_column(Float)
    consensus_deviation: Mapped[float | None] = mapped_column(Float)
    car_pm1: Mapped[float | None] = mapped_column(Float)
    pre_event_excess_20d: Mapped[float | None] = mapped_column(Float)
    post_event_excess_20d: Mapped[float | None] = mapped_column(Float)
    unavailable_reason: Mapped[str | None] = mapped_column(String(200))


class TargetOutcome(Base):
    """Price-target outcome per event: ``TARGET_OUTCOME_FRAME`` plus ``run_id``."""

    __tablename__ = "target_outcome"
    __table_args__ = ()

    run_id: Mapped[str] = mapped_column(
        ForeignKey("calc_run.run_id", ondelete="RESTRICT"), primary_key=True
    )
    event_id: Mapped[str] = mapped_column(ForeignKey("rating_event.event_id"), primary_key=True)
    analyst_id: Mapped[str] = mapped_column(ForeignKey("analyst.analyst_id"))
    entry_session: Mapped[date] = mapped_column(Date)
    horizon_end_session: Mapped[date] = mapped_column(Date)
    entry_price: Mapped[float] = mapped_column(Float)
    target_price: Mapped[float] = mapped_column(Float)
    implied_return: Mapped[float] = mapped_column(Float)
    realized_return: Mapped[float | None] = mapped_column(Float)
    hit: Mapped[bool | None] = mapped_column(Boolean)
    is_matured: Mapped[bool] = mapped_column(Boolean)


class AnalystMetric(Base):
    """Per-analyst metric results: ``METRIC_RESULT_FRAME`` plus ``run_id``."""

    __tablename__ = "analyst_metric"
    __table_args__ = (
        CheckConstraint("n_obs >= 0"),
        CheckConstraint(
            "peer_percentile IS NULL OR (peer_percentile >= 0 AND peer_percentile <= 100)"
        ),
        Index("ix_analyst_metric_run_metric", "run_id", "metric_code"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("calc_run.run_id", ondelete="RESTRICT"), primary_key=True
    )
    analyst_id: Mapped[str] = mapped_column(ForeignKey("analyst.analyst_id"), primary_key=True)
    metric_code: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[float | None] = mapped_column(Float)
    n_obs: Mapped[int] = mapped_column(Integer)
    std_err: Mapped[float | None] = mapped_column(Float)
    t_stat: Mapped[float | None] = mapped_column(Float)
    ci_low: Mapped[float | None] = mapped_column(Float)
    ci_high: Mapped[float | None] = mapped_column(Float)
    shrunk_value: Mapped[float | None] = mapped_column(Float)
    peer_percentile: Mapped[float | None] = mapped_column(Float)
    is_significant: Mapped[bool | None] = mapped_column(Boolean)
    unavailable_reason: Mapped[str | None] = mapped_column(String(200))


class AnalystComposite(Base):
    """Per-analyst composite score: ``COMPOSITE_FRAME`` plus ``run_id``."""

    __tablename__ = "analyst_composite"
    __table_args__ = (
        CheckConstraint("rank_overall IS NULL OR rank_overall >= 1"),
        CheckConstraint("rank_in_peer_group IS NULL OR rank_in_peer_group >= 1"),
        Index("ix_analyst_composite_run_rank", "run_id", "rank_overall"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("calc_run.run_id", ondelete="RESTRICT"), primary_key=True
    )
    analyst_id: Mapped[str] = mapped_column(ForeignKey("analyst.analyst_id"), primary_key=True)
    peer_group: Mapped[str | None] = mapped_column(String(200))
    composite_score: Mapped[float | None] = mapped_column(Float)
    rank_overall: Mapped[int | None] = mapped_column(Integer)
    rank_in_peer_group: Mapped[int | None] = mapped_column(Integer)
    is_ranked: Mapped[bool] = mapped_column(Boolean)
    unranked_reason: Mapped[str | None] = mapped_column(String(200))


class PortfolioPosition(Base):
    """Daily portfolio position of an analyst within a run."""

    __tablename__ = "portfolio_position"
    __table_args__ = (CheckConstraint("weight IS NULL OR weight >= -1.0 AND weight <= 1.0"),)

    run_id: Mapped[str] = mapped_column(
        ForeignKey("calc_run.run_id", ondelete="RESTRICT"), primary_key=True
    )
    analyst_id: Mapped[str] = mapped_column(ForeignKey("analyst.analyst_id"), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"), primary_key=True)
    weight: Mapped[float | None] = mapped_column(Float)
    notional: Mapped[Decimal | None] = mapped_column(Money)


class PortfolioNav(Base):
    """Daily portfolio NAV series: ``PORTFOLIO_NAV_FRAME`` plus ``run_id``."""

    __tablename__ = "portfolio_nav"
    __table_args__ = (
        CheckConstraint("gross_exposure >= 0"),
        CheckConstraint("turnover >= 0"),
        CheckConstraint("cost >= 0"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("calc_run.run_id", ondelete="RESTRICT"), primary_key=True
    )
    analyst_id: Mapped[str] = mapped_column(ForeignKey("analyst.analyst_id"), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    nav: Mapped[float] = mapped_column(Float)
    benchmark_nav: Mapped[float] = mapped_column(Float)
    active_return: Mapped[float | None] = mapped_column(Float)
    gross_exposure: Mapped[float] = mapped_column(Float)
    turnover: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float)
    drawdown: Mapped[float] = mapped_column(Float)


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #


class BacktestBenchmark(Base):
    """Materialized benchmark snapshot used by backtests (versioned/revisioned)."""

    __tablename__ = "backtest_benchmark"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    benchmark_id: Mapped[str | None] = mapped_column(ForeignKey("benchmark_def.benchmark_id"))
    revision: Mapped[str] = mapped_column(String(200))
    as_of_date: Mapped[date | None] = mapped_column(Date)
    config_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Backtest(Base):
    """Backtest definition/run with optional linkage to a calc run."""

    __tablename__ = "backtest"
    __table_args__ = (CheckConstraint(_in("status", RunStatus)),)

    backtest_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(500))
    config_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50))
    calc_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("calc_run.run_id", ondelete="RESTRICT")
    )
    scenario_id: Mapped[str | None] = mapped_column(ForeignKey("scenario.scenario_id"))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class BacktestNav(Base):
    """Daily NAV series of a backtest."""

    __tablename__ = "backtest_nav"
    __table_args__ = (
        CheckConstraint("gross_exposure >= 0"),
        CheckConstraint("turnover >= 0"),
        CheckConstraint("cost >= 0"),
    )

    backtest_id: Mapped[str] = mapped_column(ForeignKey("backtest.backtest_id"), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    nav: Mapped[float] = mapped_column(Float)
    benchmark_nav: Mapped[float] = mapped_column(Float)
    active_return: Mapped[float | None] = mapped_column(Float)
    gross_exposure: Mapped[float] = mapped_column(Float)
    turnover: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float)
    drawdown: Mapped[float] = mapped_column(Float)


class BacktestTrade(Base):
    """Individual simulated trade of a backtest."""

    __tablename__ = "backtest_trade"
    __table_args__ = (
        CheckConstraint(_in("side", ("buy", "sell"))),
        CheckConstraint("quantity > 0"),
        CheckConstraint("price > 0"),
        CheckConstraint("fee IS NULL OR fee >= 0"),
    )

    backtest_id: Mapped[str] = mapped_column(ForeignKey("backtest.backtest_id"), primary_key=True)
    trade_seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_date: Mapped[date] = mapped_column(Date)
    security_id: Mapped[str] = mapped_column(ForeignKey("security.security_id"))
    side: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[Decimal] = mapped_column(Money)
    notional: Mapped[Decimal | None] = mapped_column(Money)
    fee: Mapped[Decimal | None] = mapped_column(Money)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Money)


class BacktestStat(Base):
    """Named statistic computed for a backtest."""

    __tablename__ = "backtest_stat"

    backtest_id: Mapped[str] = mapped_column(ForeignKey("backtest.backtest_id"), primary_key=True)
    stat_code: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[float | None] = mapped_column(Float)
    std_err: Mapped[float | None] = mapped_column(Float)
    ci_low: Mapped[float | None] = mapped_column(Float)
    ci_high: Mapped[float | None] = mapped_column(Float)
    detail_json: Mapped[str | None] = mapped_column(Text)
