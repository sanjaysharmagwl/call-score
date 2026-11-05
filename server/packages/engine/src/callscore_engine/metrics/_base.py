"""Shared plumbing for metric implementations: inputs, raw results and the registry."""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, fields

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.frames import FRAME_CONTRACTS, FrameContract
from callscore_core.methodology import Methodology

INSUFFICIENT = "insufficient_observations"
ZERO_VARIANCE = "zero_variance"
NON_FINITE = "non_finite"
ANNOUNCEMENT_DISABLED = "announcement_window_disabled"
NO_PEER_GROUP = "no_peer_group"
PEER_GROUP_TOO_SMALL = "peer_group_too_small"
MISSING_SECURITY_MAPPING = "missing_security_mapping"
MISSING_REGIME = "missing_regime"

INVESTABLE_RATINGS = ("strong_buy", "buy", "hold", "sell", "strong_sell")
BUY_RATINGS = ("strong_buy", "buy")

_DTYPES: dict[str, pl.DataType] = {
    "String": pl.String(),
    "Date": pl.Date(),
    "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
    "Float64": pl.Float64(),
    "Int64": pl.Int64(),
    "Boolean": pl.Boolean(),
}

VALUE_SCHEMA: dict[str, pl.DataType] = {
    "analyst_id": pl.String(),
    "value": pl.Float64(),
    "n_obs": pl.Int64(),
    "unavailable_reason": pl.String(),
}
SAMPLE_SCHEMA: dict[str, pl.DataType] = {
    "analyst_id": pl.String(),
    "observation_id": pl.String(),
    "observation_date": pl.Date(),
    "value": pl.Float64(),
}


def contract_schema(contract: FrameContract) -> dict[str, pl.DataType]:
    return {column.name: _DTYPES[column.dtype] for column in contract.columns}


def _normalize(frame: pl.DataFrame | None, name: str) -> pl.DataFrame:
    """Contract-typed, NaN-free, totally ordered copy of an input frame (empty if absent)."""
    contract = FRAME_CONTRACTS[name]
    schema = contract_schema(contract)
    if frame is None:
        return pl.DataFrame(schema=schema)
    casts = [
        pl.col(column).cast(dtype)
        for column, dtype in schema.items()
        if column in frame.columns and frame.schema[column] != dtype
    ]
    out = frame.with_columns(casts) if casts else frame
    missing = [name for name in schema if name not in out.columns]
    if missing:
        out = out.with_columns(pl.lit(None, dtype=schema[name]).alias(name) for name in missing)
    floats = [c for c, dtype in out.schema.items() if dtype == pl.Float64]
    if floats:
        out = out.with_columns(pl.col(floats).fill_nan(None))
    order = [c for c in contract.sort_by if c in out.columns]
    return out.sort(order) if order else out


@dataclass(frozen=True)
class MetricInputs(Mapping[str, pl.DataFrame]):
    """Frames a metric may read, keyed by their ``FRAME_CONTRACTS`` name.

    Missing frames behave as empty contract-typed frames; frames may omit
    contract columns, which read as null. ``intervals`` is optional and only
    used to map ``interval_id`` to ``security_id`` for ``sector_dispersion``
    when the contributions frame does not carry a ``security_id`` column.
    """

    contributions: pl.DataFrame | None = None
    event_windows: pl.DataFrame | None = None
    target_outcomes: pl.DataFrame | None = None
    coverage_daily: pl.DataFrame | None = None
    portfolio_nav: pl.DataFrame | None = None
    rating_events: pl.DataFrame | None = None
    coverage_spells: pl.DataFrame | None = None
    security_classifications: pl.DataFrame | None = None
    benchmark_daily: pl.DataFrame | None = None
    sessions: pl.DataFrame | None = None
    intervals: pl.DataFrame | None = None

    def frame(self, name: str) -> pl.DataFrame:
        if name not in {f.name for f in fields(self)}:
            raise KeyError(f"Unknown metric input frame: {name}")
        value: pl.DataFrame | None = getattr(self, name)
        return _normalize(value, name)

    def __getitem__(self, name: str) -> pl.DataFrame:
        return self.frame(name)

    def __iter__(self) -> Iterator[str]:
        return (f.name for f in fields(self))

    def __len__(self) -> int:
        return len(fields(self))


@dataclass(frozen=True)
class RawMetric:
    """Per-analyst metric output before availability rules are applied.

    ``values``: ``analyst_id, value, n_obs, unavailable_reason``; analysts without a row get
    ``n_obs = 0``. ``samples``: ``analyst_id, observation_id, observation_date, value`` for
    bootstrapped metrics. ``default_reason``, when set, makes the metric unavailable for every
    analyst with that reason (e.g. a methodology toggle disabled it).
    """

    values: pl.DataFrame
    samples: pl.DataFrame | None = None
    default_reason: str | None = None


MetricFn = Callable[[MetricInputs, Methodology, RunContext], RawMetric]

REGISTRY: dict[str, MetricFn] = {}


def register(code: str) -> Callable[[MetricFn], MetricFn]:
    def decorator(fn: MetricFn) -> MetricFn:
        if code in REGISTRY:
            raise ValueError(f"Metric {code!r} registered twice")
        REGISTRY[code] = fn
        return fn

    return decorator


def values_frame(frame: pl.DataFrame) -> pl.DataFrame:
    """Select and type the raw value columns (``unavailable_reason`` optional)."""
    if "unavailable_reason" not in frame.columns:
        frame = frame.with_columns(pl.lit(None, dtype=pl.String).alias("unavailable_reason"))
    return frame.select(pl.col(name).cast(dtype) for name, dtype in VALUE_SCHEMA.items()).sort(
        "analyst_id"
    )


def samples_frame(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.select(pl.col(name).cast(dtype) for name, dtype in SAMPLE_SCHEMA.items()).sort(
        "analyst_id", "observation_id"
    )


def mean_metric(samples: pl.DataFrame, scale: float = 1.0) -> RawMetric:
    """``scale * mean(value)`` per analyst over ``samples`` (``SAMPLE_SCHEMA`` columns)."""
    samples = samples_frame(samples)
    values = samples.group_by("analyst_id", maintain_order=True).agg(
        (pl.col("value").mean() * scale).alias("value"), pl.len().alias("n_obs")
    )
    return RawMetric(values_frame(values), samples)


def mean_of(frame: pl.DataFrame, expr: pl.Expr) -> RawMetric:
    """Non-bootstrapped ``mean(expr)`` per analyst (rows already filtered)."""
    values = frame.group_by("analyst_id").agg(expr.mean().alias("value"), pl.len().alias("n_obs"))
    return RawMetric(values_frame(values))


def iso(column: str) -> pl.Expr:
    return pl.col(column).dt.strftime("%Y-%m-%d")
