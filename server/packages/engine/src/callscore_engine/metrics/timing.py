"""Timing family: returns around rating changes, signed by the change direction."""

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.methodology import Methodology
from callscore_engine.metrics._base import (
    ANNOUNCEMENT_DISABLED,
    MetricInputs,
    RawMetric,
    mean_metric,
    mean_of,
    register,
    values_frame,
)


def _signed_changes(inputs: MetricInputs, column: str) -> pl.DataFrame:
    """Events with ``weight_change != 0`` and a non-null ``column``, plus ``signed``."""
    return (
        inputs.frame("event_windows")
        .filter(
            pl.col("weight_change").is_not_null()
            & (pl.col("weight_change") != 0)
            & pl.col(column).is_not_null()
        )
        .with_columns((pl.col("weight_change").sign() * pl.col(column)).alias("signed"))
    )


@register("revision_drift_20d")
def revision_drift_20d(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    rows = _signed_changes(inputs, "post_event_excess_20d")
    return mean_metric(
        rows.select(
            "analyst_id",
            pl.col("event_id").alias("observation_id"),
            pl.col("event_session").alias("observation_date"),
            pl.col("signed").alias("value"),
        )
    )


@register("signed_car")
def signed_car(inputs: MetricInputs, methodology: Methodology, context: RunContext) -> RawMetric:
    raw = mean_of(_signed_changes(inputs, "car_pm1"), pl.col("signed"))
    if methodology.include_announcement_window:
        return raw
    disabled = raw.values.with_columns(
        pl.lit(None, dtype=pl.Float64).alias("value"),
        pl.lit(ANNOUNCEMENT_DISABLED).alias("unavailable_reason"),
    )
    return RawMetric(values_frame(disabled), default_reason=ANNOUNCEMENT_DISABLED)


@register("momentum_chasing")
def momentum_chasing(
    inputs: MetricInputs, methodology: Methodology, context: RunContext
) -> RawMetric:
    return mean_of(_signed_changes(inputs, "pre_event_excess_20d"), pl.col("signed"))
