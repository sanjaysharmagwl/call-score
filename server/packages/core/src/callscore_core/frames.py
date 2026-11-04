"""Polars frame contracts exchanged between the store, the engine stages and the API.

Every engine input and stage output is declared here. Metric definitions reference these
names in ``required_inputs``; adding a column is a contract version bump.
"""

from typing import Literal, Self

from pydantic import model_validator

from callscore_core.methodology import FrozenModel, Identifier

Dtype = Literal["String", "Date", "Datetime[us, UTC]", "Float64", "Int64", "Boolean"]

HORIZON_TO_EXIT = "to_exit"


def horizon_label(days: int | None) -> str:
    """``None`` is the interval's own holding period; integers are fixed session horizons."""
    if days is None:
        return HORIZON_TO_EXIT
    if days <= 0:
        raise ValueError("Fixed horizons must be positive")
    return f"d{days}"


class ColumnContract(FrozenModel):
    name: Identifier
    dtype: Dtype
    nullable: bool = False


class FrameContract(FrozenModel):
    name: Identifier
    version: Literal[1] = 1
    columns: tuple[ColumnContract, ...]
    primary_key: tuple[str, ...]
    sort_by: tuple[str, ...]

    @model_validator(mode="after")
    def valid_keys(self) -> Self:
        names = {column.name for column in self.columns}
        if len(names) != len(self.columns):
            raise ValueError("Frame column names must be unique")
        if not self.primary_key or not self.sort_by:
            raise ValueError("Frame contracts require primary_key and sort_by")
        if len(set(self.primary_key)) != len(self.primary_key):
            raise ValueError("Primary key columns must be unique")
        if len(set(self.sort_by)) != len(self.sort_by):
            raise ValueError("Sort columns must be unique")
        if not set(self.primary_key + self.sort_by) <= names:
            raise ValueError("Frame keys and sort columns must reference declared columns")
        if not set(self.primary_key) <= set(self.sort_by):
            raise ValueError("Sort columns must include the primary key as a tie-breaker")
        if any(column.nullable for column in self.columns if column.name in self.primary_key):
            raise ValueError("Primary key columns cannot be nullable")
        return self

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


def _frame(
    name: str,
    columns: tuple[tuple[str, Dtype] | tuple[str, Dtype, bool], ...],
    primary_key: tuple[str, ...],
    sort_by: tuple[str, ...] | None = None,
) -> FrameContract:
    return FrameContract(
        name=name,
        columns=tuple(
            ColumnContract(name=c[0], dtype=c[1], nullable=len(c) == 3 and bool(c[2]))
            for c in columns
        ),
        primary_key=primary_key,
        sort_by=sort_by or primary_key,
    )


_TS: Dtype = "Datetime[us, UTC]"

# --- Engine inputs (point-in-time snapshot assembled by the application layer) -------------

SESSION_FRAME = _frame(
    "sessions",
    (("exchange_id", "String"), ("session_date", "Date"), ("open_ts", _TS), ("close_ts", _TS)),
    ("exchange_id", "session_date"),
)

RATING_EVENT_FRAME = _frame(
    "rating_events",
    (
        ("event_id", "String"),
        ("analyst_id", "String"),
        ("firm_id", "String"),
        ("security_id", "String"),
        ("announced_ts", _TS),
        ("effective_ts", _TS),
        ("knowledge_ts", _TS),
        ("action_type", "String"),
        ("native_rating", "String"),
        ("canonical_rating", "String"),
        ("prior_canonical_rating", "String", True),
        ("scale_id", "String"),
        ("price_target", "Float64", True),
        ("pt_currency", "String", True),
        ("pt_horizon_months", "Int64", True),
        ("expiry_date", "Date", True),
        ("supersedes_event_id", "String", True),
        ("is_correction", "Boolean"),
    ),
    ("event_id",),
    ("analyst_id", "security_id", "announced_ts", "event_id"),
)

COVERAGE_SPELL_FRAME = _frame(
    "coverage_spells",
    (
        ("analyst_id", "String"),
        ("security_id", "String"),
        ("from_date", "Date"),
        ("to_date", "Date", True),
        ("is_lead", "Boolean"),
    ),
    ("analyst_id", "security_id", "from_date"),
)

EMPLOYMENT_SPELL_FRAME = _frame(
    "employment_spells",
    (
        ("analyst_id", "String"),
        ("firm_id", "String"),
        ("from_date", "Date"),
        ("to_date", "Date", True),
    ),
    ("analyst_id", "from_date"),
)

SECURITY_CLASSIFICATION_FRAME = _frame(
    "security_classifications",
    (
        ("security_id", "String"),
        ("from_date", "Date"),
        ("to_date", "Date", True),
        ("exchange_id", "String"),
        ("currency", "String"),
        ("sector_id", "String"),
        ("region", "String"),
        ("cap_bucket", "String"),
        ("listing_date", "Date"),
        ("delisting_date", "Date", True),
    ),
    ("security_id", "from_date"),
)

SECURITY_DAILY_FRAME = _frame(
    "security_daily",
    (
        ("security_id", "String"),
        ("session_date", "Date"),
        ("open", "Float64"),
        ("close", "Float64"),
        ("high", "Float64"),
        ("low", "Float64"),
        ("adj_factor", "Float64"),
        ("cash_dividend", "Float64", True),
        ("tr_index", "Float64"),
        ("is_halted", "Boolean"),
    ),
    ("security_id", "session_date"),
)

BENCHMARK_DAILY_FRAME = _frame(
    "benchmark_daily",
    (
        ("benchmark_id", "String"),
        ("session_date", "Date"),
        ("open_level", "Float64"),
        ("close_level", "Float64"),
    ),
    ("benchmark_id", "session_date"),
)

BETA_FRAME = _frame(
    "security_beta",
    (
        ("security_id", "String"),
        ("as_of_date", "Date"),
        ("beta", "Float64"),
        ("window_days", "Int64"),
    ),
    ("security_id", "as_of_date"),
)

DELISTING_FRAME = _frame(
    "delistings",
    (
        ("security_id", "String"),
        ("delisting_date", "Date"),
        ("final_return", "Float64"),
        ("reason", "String"),
    ),
    ("security_id",),
)

FX_DAILY_FRAME = _frame(
    "fx_daily",
    (("from_ccy", "String"), ("to_ccy", "String"), ("rate_date", "Date"), ("rate", "Float64")),
    ("from_ccy", "to_ccy", "rate_date"),
)

# --- Stage outputs -------------------------------------------------------------------------

INTERVAL_FRAME = _frame(
    "intervals",
    (
        ("interval_id", "String"),
        ("event_id", "String"),
        ("analyst_id", "String"),
        ("firm_id", "String"),
        ("security_id", "String"),
        ("native_rating", "String"),
        ("canonical_rating", "String"),
        ("action_type", "String"),
        ("announced_ts", _TS),
        ("effective_ts", _TS),
        ("knowledge_ts", _TS),
        ("available_ts", _TS),
        ("entry_session", "Date"),
        ("entry_point", "String"),
        ("entry_ts", _TS),
        ("exit_session", "Date"),
        ("exit_point", "String"),
        ("exit_ts", _TS),
        ("exit_reason", "String"),
        ("expiry_date", "Date", True),
        ("decay_start_session", "Date", True),
        ("is_investable", "Boolean"),
        ("non_investable_reason", "String", True),
        ("flags", "String", True),
    ),
    ("interval_id",),
    ("analyst_id", "security_id", "entry_ts", "interval_id"),
)

EXPOSURE_FRAME = _frame(
    "exposures",
    (
        ("interval_id", "String"),
        ("segment_no", "Int64"),
        ("start_session", "Date"),
        ("start_point", "String"),
        ("end_session", "Date"),
        ("end_point", "String"),
        ("position_weight", "Float64", True),
        ("weight_source", "String"),
    ),
    ("interval_id", "segment_no"),
)

RETURN_FRAME = _frame(
    "returns",
    (
        ("interval_id", "String"),
        ("horizon", "String"),
        ("start_session", "Date"),
        ("start_point", "String"),
        ("end_session", "Date", True),
        ("end_point", "String", True),
        ("entry_price", "Float64", True),
        ("exit_price", "Float64", True),
        ("stock_return", "Float64", True),
        ("benchmark_id", "String", True),
        ("benchmark_return", "Float64", True),
        ("beta", "Float64", True),
        ("excess_return", "Float64", True),
        ("terminated_by_delisting", "Boolean"),
        ("unavailable_reason", "String", True),
    ),
    ("interval_id", "horizon"),
)

CONTRIBUTION_FRAME = _frame(
    "contributions",
    (
        ("interval_id", "String"),
        ("analyst_id", "String"),
        ("horizon", "String"),
        ("entry_session", "Date"),
        ("position_weight", "Float64", True),
        ("excess_return", "Float64", True),
        ("contribution", "Float64", True),
        ("unavailable_reason", "String", True),
    ),
    ("interval_id", "horizon"),
)

EVENT_WINDOW_FRAME = _frame(
    "event_windows",
    (
        ("event_id", "String"),
        ("analyst_id", "String"),
        ("security_id", "String"),
        ("event_session", "Date"),
        ("weight_change", "Float64", True),
        ("consensus_deviation", "Float64", True),
        ("car_pm1", "Float64", True),
        ("pre_event_excess_20d", "Float64", True),
        ("post_event_excess_20d", "Float64", True),
        ("unavailable_reason", "String", True),
    ),
    ("event_id",),
)

TARGET_OUTCOME_FRAME = _frame(
    "target_outcomes",
    (
        ("event_id", "String"),
        ("analyst_id", "String"),
        ("entry_session", "Date"),
        ("horizon_end_session", "Date"),
        ("entry_price", "Float64"),
        ("target_price", "Float64"),
        ("implied_return", "Float64"),
        ("realized_return", "Float64", True),
        ("hit", "Boolean", True),
        ("is_matured", "Boolean"),
    ),
    ("event_id",),
)

COVERAGE_DAILY_FRAME = _frame(
    "coverage_daily",
    (
        ("analyst_id", "String"),
        ("security_id", "String"),
        ("session_date", "Date"),
        ("is_stale", "Boolean"),
    ),
    ("analyst_id", "security_id", "session_date"),
)

PORTFOLIO_NAV_FRAME = _frame(
    "portfolio_nav",
    (
        ("analyst_id", "String"),
        ("session_date", "Date"),
        ("nav", "Float64"),
        ("benchmark_nav", "Float64"),
        ("active_return", "Float64", True),
        ("gross_exposure", "Float64"),
        ("turnover", "Float64"),
        ("cost", "Float64"),
        ("drawdown", "Float64"),
    ),
    ("analyst_id", "session_date"),
)

METRIC_SAMPLE_FRAME = _frame(
    "metric_samples",
    (
        ("analyst_id", "String"),
        ("metric_code", "String"),
        ("observation_id", "String"),
        ("observation_date", "Date"),
        ("value", "Float64"),
    ),
    ("analyst_id", "metric_code", "observation_id"),
)

METRIC_RESULT_FRAME = _frame(
    "metric_results",
    (
        ("analyst_id", "String"),
        ("metric_code", "String"),
        ("value", "Float64", True),
        ("n_obs", "Int64"),
        ("std_err", "Float64", True),
        ("t_stat", "Float64", True),
        ("ci_low", "Float64", True),
        ("ci_high", "Float64", True),
        ("shrunk_value", "Float64", True),
        ("peer_percentile", "Float64", True),
        ("is_significant", "Boolean", True),
        ("unavailable_reason", "String", True),
    ),
    ("analyst_id", "metric_code"),
)

COMPOSITE_FRAME = _frame(
    "composites",
    (
        ("analyst_id", "String"),
        ("peer_group", "String", True),
        ("composite_score", "Float64", True),
        ("rank_overall", "Int64", True),
        ("rank_in_peer_group", "Int64", True),
        ("is_ranked", "Boolean"),
        ("unranked_reason", "String", True),
    ),
    ("analyst_id",),
)

FRAME_CONTRACTS: dict[str, FrameContract] = {
    frame.name: frame
    for frame in (
        SESSION_FRAME,
        RATING_EVENT_FRAME,
        COVERAGE_SPELL_FRAME,
        EMPLOYMENT_SPELL_FRAME,
        SECURITY_CLASSIFICATION_FRAME,
        SECURITY_DAILY_FRAME,
        BENCHMARK_DAILY_FRAME,
        BETA_FRAME,
        DELISTING_FRAME,
        FX_DAILY_FRAME,
        INTERVAL_FRAME,
        EXPOSURE_FRAME,
        RETURN_FRAME,
        CONTRIBUTION_FRAME,
        EVENT_WINDOW_FRAME,
        TARGET_OUTCOME_FRAME,
        COVERAGE_DAILY_FRAME,
        PORTFOLIO_NAV_FRAME,
        METRIC_SAMPLE_FRAME,
        METRIC_RESULT_FRAME,
        COMPOSITE_FRAME,
    )
}
