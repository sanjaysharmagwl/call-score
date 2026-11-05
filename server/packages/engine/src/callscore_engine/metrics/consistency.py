"""Consistency family: stability of to-exit contributions across quarters, sectors, regimes."""

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.frames import MARKET_BENCHMARK_ID
from callscore_core.methodology import Methodology
from callscore_engine.metrics._base import (
    MISSING_REGIME,
    MISSING_SECURITY_MAPPING,
    MetricInputs,
    RawMetric,
    register,
    values_frame,
)
from callscore_engine.metrics.return_skill import to_exit_contributions

MIN_SECTOR_INTERVALS = 3


def _quarter(column: str) -> pl.Expr:
    return (pl.col(column).dt.year() * 4 + pl.col(column).dt.quarter() - 1).alias("quarter")


def _quarter_means(inputs: MetricInputs) -> pl.DataFrame:
    return (
        to_exit_contributions(inputs)
        .with_columns(_quarter("entry_session"))
        .group_by("analyst_id", "quarter")
        .agg(pl.col("contribution").mean().alias("q_mean"))
        .sort("analyst_id", "quarter")
    )


@register("positive_quarter_fraction")
def positive_quarter_fraction(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    values = (
        _quarter_means(inputs)
        .group_by("analyst_id", maintain_order=True)
        .agg(
            (pl.col("q_mean") > 0).cast(pl.Float64).mean().alias("value"),
            pl.len().alias("n_obs"),
        )
    )
    return RawMetric(values_frame(values))


@register("quarterly_contribution_std")
def quarterly_contribution_std(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    values = (
        _quarter_means(inputs)
        .group_by("analyst_id", maintain_order=True)
        .agg(pl.col("q_mean").std(ddof=1).alias("value"), pl.len().alias("n_obs"))
    )
    return RawMetric(values_frame(values))


def _with_security(inputs: MetricInputs, rows: pl.DataFrame) -> pl.DataFrame | None:
    if "security_id" in rows.columns:
        return rows
    if inputs.intervals is None or not {"interval_id", "security_id"} <= set(
        inputs.intervals.columns
    ):
        return None
    mapping = inputs.frame("intervals").select("interval_id", "security_id").unique("interval_id")
    return rows.join(mapping, on="interval_id", how="left")


@register("sector_dispersion")
def sector_dispersion(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    rows = to_exit_contributions(inputs)
    with_security = _with_security(inputs, rows)
    if with_security is None:
        missing = (
            rows.group_by("analyst_id")
            .agg(pl.lit(0, dtype=pl.Int64).alias("n_obs"))
            .with_columns(
                pl.lit(None, dtype=pl.Float64).alias("value"),
                pl.lit(MISSING_SECURITY_MAPPING).alias("unavailable_reason"),
            )
        )
        return RawMetric(values_frame(missing))
    classes = inputs.frame("security_classifications").select(
        "security_id", "from_date", "to_date", "sector_id"
    )
    sectors = (
        with_security.join(classes, on="security_id", how="inner")
        .filter(
            (pl.col("from_date") <= pl.col("entry_session"))
            & (pl.col("to_date").is_null() | (pl.col("to_date") >= pl.col("entry_session")))
        )
        .sort("interval_id", "from_date", descending=[False, True])
        .unique("interval_id", keep="first", maintain_order=True)
    )
    sector_means = (
        sectors.group_by("analyst_id", "sector_id")
        .agg(pl.col("contribution").mean().alias("s_mean"), pl.len().alias("n"))
        .filter(pl.col("n") >= MIN_SECTOR_INTERVALS)
        .sort("analyst_id", "sector_id")
    )
    values = sector_means.group_by("analyst_id", maintain_order=True).agg(
        pl.col("s_mean").std(ddof=1).alias("value"), pl.len().alias("n_obs")
    )
    return RawMetric(values_frame(values))


def market_regimes(inputs: MetricInputs) -> pl.DataFrame:
    """``quarter, regime`` ("up"/"down") from the market benchmark's in-quarter close change.

    Quarters with a single session or no change are unclassified (absent).
    """
    return (
        inputs.frame("benchmark_daily")
        .filter(pl.col("benchmark_id") == MARKET_BENCHMARK_ID)
        .sort("session_date")
        .with_columns(_quarter("session_date"))
        .group_by("quarter", maintain_order=True)
        .agg(
            (pl.col("close_level").last() - pl.col("close_level").first()).alias("change"),
        )
        .filter(pl.col("change") != 0)
        .select(
            "quarter",
            pl.when(pl.col("change") > 0)
            .then(pl.lit("up"))
            .otherwise(pl.lit("down"))
            .alias("regime"),
        )
    )


@register("regime_spread")
def regime_spread(inputs: MetricInputs, methodology: Methodology, context: RunContext) -> RawMetric:
    rows = (
        to_exit_contributions(inputs)
        .with_columns(_quarter("entry_session"))
        .join(market_regimes(inputs), on="quarter", how="inner")
    )
    up = pl.col("regime") == "up"
    values = (
        rows.group_by("analyst_id")
        .agg(
            pl.col("quarter").n_unique().alias("n_obs"),
            pl.col("contribution").filter(up).mean().alias("up_mean"),
            pl.col("contribution").filter(~up).mean().alias("down_mean"),
        )
        .with_columns(
            (pl.col("up_mean") - pl.col("down_mean")).alias("value"),
            pl.when(pl.col("up_mean").is_null() | pl.col("down_mean").is_null())
            .then(pl.lit(MISSING_REGIME))
            .otherwise(None)
            .alias("unavailable_reason"),
        )
    )
    return RawMetric(values_frame(values))
