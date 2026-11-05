"""Metric registry and the ``compute_metrics`` entry point (catalog: ``METRIC_DEFINITIONS``)."""

from collections.abc import Sequence
from dataclasses import dataclass

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.frames import METRIC_SAMPLE_FRAME
from callscore_core.methodology import Methodology
from callscore_core.metrics import METRIC_DEFINITIONS, MetricDefinition

# Importing the family modules populates REGISTRY via @register.
from callscore_engine.metrics import (  # noqa: F401
    behavior,
    consistency,
    price_target,
    return_skill,
    timing,
)
from callscore_engine.metrics._base import (
    INSUFFICIENT,
    NON_FINITE,
    REGISTRY,
    SAMPLE_SCHEMA,
    VALUE_SCHEMA,
    MetricInputs,
    contract_schema,
)

METRIC_VALUE_SCHEMA: dict[str, pl.DataType] = {
    "analyst_id": pl.String(),
    "metric_code": pl.String(),
    **{k: v for k, v in VALUE_SCHEMA.items() if k != "analyst_id"},
}
METRIC_SAMPLE_SCHEMA = contract_schema(METRIC_SAMPLE_FRAME)


def check_registry() -> None:
    """Raise if registered metric codes differ from the catalog."""
    catalog = {d.code for d in METRIC_DEFINITIONS}
    registered = set(REGISTRY)
    if catalog != registered:
        raise RuntimeError(
            "Metric registry does not match METRIC_DEFINITIONS: "
            f"missing={sorted(catalog - registered)} extra={sorted(registered - catalog)}"
        )


check_registry()


@dataclass(frozen=True)
class MetricOutputs:
    """``values``: one row per (analyst, catalog metric); ``samples``: ``metric_samples``."""

    values: pl.DataFrame
    samples: pl.DataFrame


def _finalize(
    roster: pl.DataFrame,
    raw: pl.DataFrame,
    definition: MetricDefinition,
    default_reason: str | None,
) -> pl.DataFrame:
    joined = roster.join(raw, on="analyst_id", how="left").with_columns(
        pl.col("n_obs").fill_null(0)
    )
    value = pl.col("value")
    reason = (
        pl.when(pl.lit(default_reason is not None))
        .then(pl.lit(default_reason, dtype=pl.String))
        .when(pl.col("n_obs") < definition.minimum_observations)
        .then(pl.lit(INSUFFICIENT))
        .when(pl.col("unavailable_reason").is_not_null())
        .then(pl.col("unavailable_reason"))
        .when(value.is_null() | ~value.is_finite())
        .then(pl.lit(NON_FINITE))
        .otherwise(None)
    )
    return (
        joined.with_columns(reason.alias("unavailable_reason"))
        .with_columns(
            pl.when(pl.col("unavailable_reason").is_null()).then(value).otherwise(None),
            pl.lit(definition.code).alias("metric_code"),
        )
        .select(pl.col(name).cast(dtype) for name, dtype in METRIC_VALUE_SCHEMA.items())
    )


def compute_metrics(
    inputs: MetricInputs,
    analysts: Sequence[str] | pl.Series,
    methodology: Methodology,
    context: RunContext,
) -> MetricOutputs:
    """Every catalog metric for every analyst in ``analysts``.

    ``value`` is null exactly when ``unavailable_reason`` is set; ``n_obs`` below the
    definition's ``minimum_observations`` yields ``insufficient_observations``. Samples are
    emitted only for bootstrapped metrics with an available value.
    """
    roster = (
        pl.DataFrame({"analyst_id": pl.Series(list(analysts), dtype=pl.String)})
        .unique()
        .sort("analyst_id")
    )
    values: list[pl.DataFrame] = []
    samples: list[pl.DataFrame] = []
    for definition in METRIC_DEFINITIONS:
        raw = REGISTRY[definition.code](inputs, methodology, context)
        final = _finalize(roster, raw.values, definition, raw.default_reason)
        values.append(final)
        if definition.inference == "none" or raw.samples is None:
            continue
        available = final.filter(pl.col("value").is_not_null()).select("analyst_id")
        samples.append(
            raw.samples.select(list(SAMPLE_SCHEMA))
            .join(available, on="analyst_id", how="semi")
            .with_columns(pl.lit(definition.code).alias("metric_code"))
        )
    value_frame = pl.concat(values).sort("analyst_id", "metric_code")
    sample_frame = (
        pl.concat(samples) if samples else pl.DataFrame(schema=METRIC_SAMPLE_SCHEMA)
    ).select(pl.col(name).cast(dtype) for name, dtype in METRIC_SAMPLE_SCHEMA.items())
    return MetricOutputs(value_frame, sample_frame.sort(list(METRIC_SAMPLE_FRAME.sort_by)))
