"""Pure pipeline composition: engine input frames + methodology -> stage outputs.

``run_pipeline`` wires the Wave-1 stage modules together in a fixed order,
validates every stage output against its ``callscore_core.frames`` contract
and computes the five coarse stage fingerprints of decision 0003
(``contracts.STAGE_ORDER``) plus per-frame :class:`StageArtifact` records.

The runner performs no I/O: the caller supplies every input frame (a
``SnapshotFrames`` or a plain mapping of frame name -> ``pl.DataFrame``) and a
fully-populated :class:`RunContext` (including ``knowledge_cutoff``).
"""

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import polars as pl

from callscore_core.contracts import STAGE_ORDER, RunContext, StageArtifact, stage_fingerprint
from callscore_core.enums import Stage
from callscore_core.frames import FRAME_CONTRACTS, FrameContract
from callscore_core.methodology import Methodology, canonical_dumps
from callscore_engine.composite import compute_composites
from callscore_engine.contributions import build_contributions
from callscore_engine.coverage import build_coverage_daily
from callscore_engine.event_windows import build_event_windows
from callscore_engine.exposures import build_exposures
from callscore_engine.intervals import build_intervals
from callscore_engine.metrics import MetricInputs, compute_metrics
from callscore_engine.metrics._base import contract_schema
from callscore_engine.peers import assign_peer_groups
from callscore_engine.point_in_time import visible_events
from callscore_engine.portfolio import simulate_portfolios
from callscore_engine.returns import build_returns
from callscore_engine.stats import infer_metrics
from callscore_engine.targets import build_target_outcomes

# Engine input frames consumed by the pipeline (contract names in frames.py).
INPUT_FRAME_NAMES: tuple[str, ...] = (
    "sessions",
    "rating_events",
    "coverage_spells",
    "employment_spells",
    "security_classifications",
    "security_daily",
    "benchmark_daily",
    "security_beta",
    "delistings",
    "fx_daily",
)


@dataclass(frozen=True)
class SnapshotFrames:
    """Engine input frames; ``None`` means "absent" (read as an empty frame).

    ``fx_daily`` is part of the input contract for completeness; no Wave-1
    stage consumes it (``reporting_currency`` is USD-only for now).
    """

    sessions: pl.DataFrame | None = None
    rating_events: pl.DataFrame | None = None
    coverage_spells: pl.DataFrame | None = None
    employment_spells: pl.DataFrame | None = None
    security_classifications: pl.DataFrame | None = None
    security_daily: pl.DataFrame | None = None
    benchmark_daily: pl.DataFrame | None = None
    security_beta: pl.DataFrame | None = None
    delistings: pl.DataFrame | None = None
    fx_daily: pl.DataFrame | None = None

    @classmethod
    def from_mapping(cls, frames: Mapping[str, pl.DataFrame]) -> "SnapshotFrames":
        """Build from a ``{frame_name: DataFrame}`` mapping (store snapshot)."""
        return cls(**{name: frames.get(name) for name in INPUT_FRAME_NAMES})


@dataclass(frozen=True)
class PipelineResult:
    """All stage outputs of one run plus provenance.

    ``metric_values``/``metric_samples`` are the raw per-analyst metric values
    (pre-inference) and bootstrap samples emitted by ``compute_metrics``;
    ``metric_results`` is the inferred ``METRIC_RESULT_FRAME`` and
    ``composites`` the ``COMPOSITE_FRAME``.
    """

    intervals: pl.DataFrame
    exposures: pl.DataFrame
    returns: pl.DataFrame
    contributions: pl.DataFrame
    event_windows: pl.DataFrame
    target_outcomes: pl.DataFrame
    coverage_daily: pl.DataFrame
    portfolio_nav: pl.DataFrame
    metric_values: pl.DataFrame
    metric_samples: pl.DataFrame
    metric_results: pl.DataFrame
    composites: pl.DataFrame
    peer_groups: pl.DataFrame
    n_investable: pl.DataFrame
    stage_fingerprints: Mapping[Stage, str]
    stage_artifacts: tuple[StageArtifact, ...]


def _empty_frame(contract: FrameContract) -> pl.DataFrame:
    return pl.DataFrame(schema=contract_schema(contract))


def _input_frames(frames: SnapshotFrames | Mapping[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    """Normalize the input to contract-typed frames (absent -> empty)."""
    if isinstance(frames, SnapshotFrames):
        mapping: Mapping[str, pl.DataFrame | None] = {
            name: getattr(frames, name) for name in INPUT_FRAME_NAMES
        }
    else:
        mapping = frames
    out: dict[str, pl.DataFrame] = {}
    for name in INPUT_FRAME_NAMES:
        frame = mapping.get(name)
        contract = FRAME_CONTRACTS[name]
        out[name] = _empty_frame(contract) if frame is None else frame
    return out


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _content_fingerprint(frame: pl.DataFrame, frame_name: str, sort_by: tuple[str, ...]) -> str:
    """Canonical digest of a frame sorted by ``sort_by`` (contract sort keys)."""
    sorted_frame = frame.sort(list(sort_by)) if sort_by else frame
    rows = [{k: _jsonable(v) for k, v in record.items()} for record in sorted_frame.to_dicts()]
    payload = {"frame": frame_name, "rows": rows}
    return hashlib.sha256(canonical_dumps(payload).encode()).hexdigest()


def assert_frame_contract(frame: pl.DataFrame, contract: FrameContract) -> None:
    """Raise ``ValueError`` unless ``frame`` satisfies ``contract`` exactly.

    Checks column names/order, dtypes, non-nullable columns, primary-key
    uniqueness and ``sort_by`` ordering.
    """
    name = contract.name
    if frame.columns != list(contract.column_names):
        raise ValueError(
            f"{name}: columns {frame.columns} != contract {list(contract.column_names)}"
        )
    schema = contract_schema(contract)
    for column in contract.columns:
        expected = schema[column.name]
        actual = frame[column.name].dtype
        if actual != expected:
            raise ValueError(f"{name}.{column.name}: dtype {actual} != {expected}")
        if not column.nullable and frame[column.name].null_count() > 0:
            raise ValueError(f"{name}.{column.name}: nulls in non-nullable column")
    if frame.select(list(contract.primary_key)).is_duplicated().any():
        raise ValueError(f"{name}: duplicate primary key {contract.primary_key}")
    if frame.height > 1 and not frame.equals(frame.sort(list(contract.sort_by))):
        raise ValueError(f"{name}: not sorted by {contract.sort_by}")


# Coarse stage (contracts.Stage) governing each output frame. Chosen so every
# methodology field that can change the frame is covered by the frame's stage
# fingerprint or one of its upstreams in STAGE_ORDER:
#
#   intervals/target_outcomes/coverage_daily -> INTERVALS
#     (entry/expiry/decay fields; coverage_daily resolves expiry dates)
#   exposures/contributions/event_windows    -> EXPOSURES
#     (position weights, scoring mode, consensus; upstream covers returns legs
#      and interval boundaries, which contributions/event windows also read)
#   returns                                  -> RETURNS
#   portfolio_nav/metric_values/metric_samples -> METRICS
#     (transaction_cost_bps, primary_fixed_horizon_days; upstream covers all
#      inputs including exposures and returns)
#   metric_results/composites                -> STATISTICS
#     (shrinkage, peer groups, bootstrap, family weights; upstream covers the
#      metric stage whose catalog fingerprint gates inference inputs)
_FRAME_STAGE: dict[str, Stage] = {
    "intervals": Stage.INTERVALS,
    "target_outcomes": Stage.INTERVALS,
    "coverage_daily": Stage.INTERVALS,
    "exposures": Stage.EXPOSURES,
    "contributions": Stage.EXPOSURES,
    "event_windows": Stage.EXPOSURES,
    "returns": Stage.RETURNS,
    "portfolio_nav": Stage.METRICS,
    "metric_values": Stage.METRICS,
    "metric_samples": Stage.METRICS,
    "metric_results": Stage.STATISTICS,
    "composites": Stage.STATISTICS,
}


def _stage_fingerprints(
    methodology: Methodology, context: RunContext
) -> tuple[dict[Stage, str], dict[Stage, tuple[str, ...]]]:
    """One fingerprint per coarse stage, chained in ``STAGE_ORDER``."""
    fingerprints: dict[Stage, str] = {}
    upstreams: dict[Stage, tuple[str, ...]] = {}
    upstream: tuple[str, ...] = ()
    for stage in STAGE_ORDER:
        fingerprints[stage] = stage_fingerprint(stage, methodology, context, upstream)
        upstreams[stage] = upstream
        upstream = (*upstream, fingerprints[stage])
    return fingerprints, upstreams


def _analyst_roster(*frames: pl.DataFrame) -> list[str]:
    """Sorted unique analyst ids across the given frames (missing col -> skip)."""
    ids: set[str] = set()
    for frame in frames:
        if "analyst_id" in frame.columns:
            ids.update(str(v) for v in frame["analyst_id"].drop_nulls().to_list())
    return sorted(ids)


def run_pipeline(
    frames: SnapshotFrames | Mapping[str, pl.DataFrame],
    methodology: Methodology,
    context: RunContext,
) -> PipelineResult:
    """Compose all engine stages; pure function of ``frames`` + ``methodology``.

    Stage order::

        intervals -> exposures -> returns -> contributions -> event_windows
        -> target_outcomes -> coverage_daily -> metric values+samples
        -> portfolio_nav -> metric_results -> composites

    Every contract-frame output is validated against ``FRAME_CONTRACTS``;
    a violation raises ``ValueError``.
    """
    data = _input_frames(frames)
    sessions = data["sessions"]
    rating_events = data["rating_events"]
    coverage_spells = data["coverage_spells"]
    employment_spells = data["employment_spells"]
    classifications = data["security_classifications"]
    security_daily = data["security_daily"]
    benchmark_daily = data["benchmark_daily"]
    beta = data["security_beta"]
    delistings = data["delistings"]

    intervals = build_intervals(
        rating_events,
        sessions,
        coverage_spells,
        employment_spells,
        classifications,
        delistings,
        methodology,
        context,
    )
    exposures = build_exposures(intervals, rating_events, sessions, methodology)
    returns = build_returns(
        intervals,
        security_daily,
        benchmark_daily,
        beta,
        delistings,
        classifications,
        sessions,
        methodology,
        context,
    )
    contributions = build_contributions(
        intervals,
        exposures,
        returns,
        security_daily,
        benchmark_daily,
        beta,
        delistings,
        classifications,
        methodology,
    )
    event_windows = build_event_windows(
        intervals,
        exposures,
        security_daily,
        benchmark_daily,
        classifications,
        methodology,
        context,
    )
    target_outcomes = build_target_outcomes(
        rating_events, intervals, security_daily, sessions, context
    )
    coverage_daily = build_coverage_daily(
        rating_events, intervals, coverage_spells, sessions, classifications, context
    )
    portfolio_nav = simulate_portfolios(
        intervals,
        exposures,
        security_daily,
        benchmark_daily,
        beta,
        delistings,
        classifications,
        sessions,
        methodology,
        context,
    )

    peer_groups = assign_peer_groups(
        coverage_spells, classifications, context.as_of_date, methodology.peer_group_dims
    )
    n_investable = (
        intervals.filter(pl.col("is_investable"))
        .group_by("analyst_id")
        .len()
        .rename({"len": "n_obs"})
        .select("analyst_id", "n_obs")
    )

    # Metrics see only events knowable at the run's knowledge cutoff.
    visible_events_frame = visible_events(rating_events, context.knowledge_cutoff)
    metric_inputs = MetricInputs(
        contributions=contributions,
        event_windows=event_windows,
        target_outcomes=target_outcomes,
        coverage_daily=coverage_daily,
        portfolio_nav=portfolio_nav,
        rating_events=visible_events_frame,
        coverage_spells=coverage_spells,
        security_classifications=classifications,
        benchmark_daily=benchmark_daily,
        sessions=sessions,
        intervals=intervals,
    )
    roster = _analyst_roster(peer_groups, intervals, rating_events)
    metric_outputs = compute_metrics(metric_inputs, roster, methodology, context)
    metric_results = infer_metrics(
        metric_outputs.values, metric_outputs.samples, peer_groups, methodology
    )
    composites = compute_composites(metric_results, peer_groups, n_investable, methodology)

    outputs: dict[str, pl.DataFrame] = {
        "intervals": intervals,
        "exposures": exposures,
        "returns": returns,
        "contributions": contributions,
        "event_windows": event_windows,
        "target_outcomes": target_outcomes,
        "coverage_daily": coverage_daily,
        "portfolio_nav": portfolio_nav,
        "metric_values": metric_outputs.values,
        "metric_samples": metric_outputs.samples,
        "metric_results": metric_results,
        "composites": composites,
    }
    contract_names = {name for name in outputs if name in FRAME_CONTRACTS}
    for frame_name in contract_names:
        assert_frame_contract(outputs[frame_name], FRAME_CONTRACTS[frame_name])

    fingerprints, upstreams = _stage_fingerprints(methodology, context)

    def _sort_keys(frame_name: str) -> tuple[str, ...]:
        contract = FRAME_CONTRACTS.get(frame_name)
        if contract is not None:
            return contract.sort_by
        # ``metric_values`` has no FrameContract; its implicit key is
        # (analyst_id, metric_code).
        return ("analyst_id", "metric_code")

    artifacts = tuple(
        StageArtifact(
            stage=_FRAME_STAGE[frame_name],
            content_fingerprint=_content_fingerprint(frame, frame_name, _sort_keys(frame_name)),
            cache_fingerprint=fingerprints[_FRAME_STAGE[frame_name]],
            upstream_fingerprints=upstreams[_FRAME_STAGE[frame_name]],
            row_count=frame.height,
        )
        for frame_name, frame in outputs.items()
    )

    return PipelineResult(
        intervals=intervals,
        exposures=exposures,
        returns=returns,
        contributions=contributions,
        event_windows=event_windows,
        target_outcomes=target_outcomes,
        coverage_daily=coverage_daily,
        portfolio_nav=portfolio_nav,
        metric_values=metric_outputs.values,
        metric_samples=metric_outputs.samples,
        metric_results=metric_results,
        composites=composites,
        peer_groups=peer_groups,
        n_investable=n_investable,
        stage_fingerprints=fingerprints,
        stage_artifacts=artifacts,
    )
