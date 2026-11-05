"""Return/skill family: call contributions, IC and the simulated-book statistics."""

import math

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.frames import HORIZON_TO_EXIT, horizon_label
from callscore_core.methodology import Methodology
from callscore_engine.metrics._base import (
    ZERO_VARIANCE,
    MetricInputs,
    RawMetric,
    iso,
    mean_metric,
    register,
    values_frame,
)

TRADING_DAYS = 252


def to_exit_contributions(inputs: MetricInputs) -> pl.DataFrame:
    """Investable to-exit intervals with a non-null contribution."""
    return inputs.frame("contributions").filter(
        (pl.col("horizon") == HORIZON_TO_EXIT)
        & pl.col("position_weight").is_not_null()
        & pl.col("contribution").is_not_null()
    )


def _contribution_samples(frame: pl.DataFrame, value: pl.Expr) -> pl.DataFrame:
    return frame.select(
        "analyst_id",
        pl.col("interval_id").alias("observation_id"),
        pl.col("entry_session").alias("observation_date"),
        value.alias("value"),
    )


@register("mean_contribution")
def mean_contribution(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    rows = to_exit_contributions(inputs)
    return mean_metric(_contribution_samples(rows, pl.col("contribution")))


@register("hit_rate")
def hit_rate(inputs: MetricInputs, methodology: Methodology, context: RunContext) -> RawMetric:
    rows = to_exit_contributions(inputs).filter(pl.col("position_weight") != 0)
    hit = (pl.col("contribution") > 0).cast(pl.Float64)
    return mean_metric(_contribution_samples(rows, hit))


@register("information_coefficient")
def information_coefficient(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    horizon = horizon_label(methodology.primary_fixed_horizon_days)
    rows = inputs.frame("contributions").filter(
        (pl.col("horizon") == horizon)
        & pl.col("position_weight").is_not_null()
        & pl.col("excess_return").is_not_null()
    )
    ranked = rows.with_columns(
        pl.col("position_weight").rank("average").over("analyst_id").alias("rw"),
        pl.col("excess_return").rank("average").over("analyst_id").alias("rx"),
    )
    values = ranked.group_by("analyst_id").agg(
        pl.len().alias("n_obs"),
        pl.corr("rw", "rx").alias("value"),
        (
            (pl.col("position_weight").n_unique() < 2) | (pl.col("excess_return").n_unique() < 2)
        ).alias("constant"),
    )
    values = values.with_columns(
        pl.when(pl.col("constant"))
        .then(pl.lit(ZERO_VARIANCE))
        .otherwise(None)
        .alias("unavailable_reason")
    )
    return RawMetric(values_frame(values))


def _nav_samples(inputs: MetricInputs) -> pl.DataFrame:
    return (
        inputs.frame("portfolio_nav")
        .filter(pl.col("active_return").is_not_null())
        .select(
            "analyst_id",
            iso("session_date").alias("observation_id"),
            pl.col("session_date").alias("observation_date"),
            pl.col("active_return").alias("value"),
        )
    )


@register("information_ratio")
def information_ratio(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    raw = mean_metric(_nav_samples(inputs))
    assert raw.samples is not None
    stats = raw.samples.group_by("analyst_id", maintain_order=True).agg(
        pl.col("value").std(ddof=1).alias("sd"),
        (pl.col("value").max() == pl.col("value").min()).alias("constant"),
    )
    values = raw.values.join(stats, on="analyst_id", how="left").with_columns(
        (math.sqrt(TRADING_DAYS) * pl.col("value") / pl.col("sd")).alias("value"),
        pl.when(pl.col("constant"))
        .then(pl.lit(ZERO_VARIANCE))
        .otherwise(None)
        .alias("unavailable_reason"),
    )
    return RawMetric(values_frame(values), raw.samples)


@register("annualized_alpha")
def annualized_alpha(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    return mean_metric(_nav_samples(inputs), scale=float(TRADING_DAYS))


@register("max_drawdown")
def max_drawdown(inputs: MetricInputs, methodology: Methodology, context: RunContext) -> RawMetric:
    values = (
        inputs.frame("portfolio_nav")
        .group_by("analyst_id")
        .agg(pl.col("drawdown").min().alias("value"), pl.len().alias("n_obs"))
    )
    return RawMetric(values_frame(values))
