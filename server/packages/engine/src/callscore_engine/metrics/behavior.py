"""Behavior family: diligence (staleness) and style traits (independence, optimism, activity)."""

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.methodology import Methodology
from callscore_engine.metrics._base import (
    BUY_RATINGS,
    INVESTABLE_RATINGS,
    NO_PEER_GROUP,
    PEER_GROUP_TOO_SMALL,
    MetricInputs,
    RawMetric,
    mean_metric,
    mean_of,
    register,
    values_frame,
)
from callscore_engine.peers import assign_peer_groups

DAYS_PER_YEAR = 365.25


@register("stale_coverage")
def stale_coverage(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    rows = inputs.frame("coverage_daily")
    return mean_metric(
        rows.select(
            "analyst_id",
            pl.concat_str(
                [pl.col("security_id"), pl.col("session_date").dt.strftime("%Y-%m-%d")],
                separator="|",
            ).alias("observation_id"),
            pl.col("session_date").alias("observation_date"),
            pl.col("is_stale").cast(pl.Float64).alias("value"),
        )
    )


@register("independence")
def independence(inputs: MetricInputs, methodology: Methodology, context: RunContext) -> RawMetric:
    rows = inputs.frame("event_windows").filter(pl.col("consensus_deviation").is_not_null())
    return mean_of(rows, pl.col("consensus_deviation").abs())


def investable_events(inputs: MetricInputs) -> pl.DataFrame:
    """Rating events with an investable canonical rating, minus events a correction replaced."""
    events = inputs.frame("rating_events")
    corrected = events.filter(
        pl.col("is_correction") & pl.col("supersedes_event_id").is_not_null()
    ).select(pl.col("supersedes_event_id").alias("event_id"))
    return events.join(corrected, on="event_id", how="anti").filter(
        pl.col("canonical_rating").is_in(INVESTABLE_RATINGS)
    )


def _buy_fraction(inputs: MetricInputs) -> RawMetric:
    return mean_of(
        investable_events(inputs),
        pl.col("canonical_rating").is_in(BUY_RATINGS).cast(pl.Float64),
    )


@register("buy_fraction")
def buy_fraction(inputs: MetricInputs, methodology: Methodology, context: RunContext) -> RawMetric:
    return _buy_fraction(inputs)


@register("optimism_vs_peers")
def optimism_vs_peers(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    own = _buy_fraction(inputs).values.filter(pl.col("n_obs") >= 1)
    groups = assign_peer_groups(
        inputs.frame("coverage_spells"),
        inputs.frame("security_classifications"),
        context.as_of_date,
        methodology.peer_group_dims,
    )
    members = own.join(groups, on="analyst_id", how="left")
    stats = (
        members.filter(pl.col("peer_group").is_not_null())
        .group_by("peer_group")
        .agg(pl.len().alias("peer_size"), pl.col("value").mean().alias("peer_mean"))
    )
    values = members.join(stats, on="peer_group", how="left").with_columns(
        (pl.col("value") - pl.col("peer_mean")).alias("value"),
        pl.when(pl.col("peer_group").is_null())
        .then(pl.lit(NO_PEER_GROUP))
        .when(pl.col("peer_size") < methodology.min_peer_size)
        .then(pl.lit(PEER_GROUP_TOO_SMALL))
        .otherwise(None)
        .alias("unavailable_reason"),
    )
    return RawMetric(values_frame(values))


@register("ratings_per_year")
def ratings_per_year(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    as_of = context.as_of_date
    spells = (
        inputs.frame("coverage_spells")
        .filter(pl.col("from_date") <= as_of)
        .with_columns(
            pl.min_horizontal(pl.col("to_date").fill_null(as_of), pl.lit(as_of)).alias("end")
        )
        .with_columns(((pl.col("end") - pl.col("from_date")).dt.total_days() + 1).alias("days"))
        .filter(pl.col("days") > 0)
    )
    exposure = spells.group_by("analyst_id").agg(
        (pl.col("days").sum() / DAYS_PER_YEAR).alias("years"), pl.len().alias("n_obs")
    )
    counts = investable_events(inputs).group_by("analyst_id").agg(pl.len().alias("events"))
    values = exposure.join(counts, on="analyst_id", how="left").with_columns(
        (pl.col("events").fill_null(0) / pl.col("years")).alias("value")
    )
    return RawMetric(values_frame(values))
