"""Price-target family: matured target outcomes on the adjusted price basis."""

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.methodology import Methodology
from callscore_engine.metrics._base import MetricInputs, RawMetric, mean_metric, mean_of, register


def _matured(inputs: MetricInputs) -> pl.DataFrame:
    return inputs.frame("target_outcomes").filter(pl.col("is_matured"))


def _target_samples(frame: pl.DataFrame, value: pl.Expr) -> pl.DataFrame:
    return frame.select(
        "analyst_id",
        pl.col("event_id").alias("observation_id"),
        pl.col("entry_session").alias("observation_date"),
        value.alias("value"),
    )


@register("target_hit_rate")
def target_hit_rate(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    rows = _matured(inputs).filter((pl.col("implied_return") != 0) & pl.col("hit").is_not_null())
    return mean_metric(_target_samples(rows, pl.col("hit").cast(pl.Float64)))


@register("target_absolute_error")
def target_absolute_error(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    rows = _matured(inputs).filter(pl.col("realized_return").is_not_null())
    error = (pl.col("realized_return") - pl.col("implied_return")).abs()
    return mean_metric(_target_samples(rows, error))


@register("target_bias")
def target_bias(inputs: MetricInputs, methodology: Methodology, context: RunContext) -> RawMetric:
    rows = _matured(inputs).filter(pl.col("realized_return").is_not_null())
    return mean_of(rows, pl.col("implied_return") - pl.col("realized_return"))
