"""Durable single-writer job coordinator and cache-aware pipeline execution.

Layering::

    submit_run()          -> idempotent ``calc_run`` creation (status ``queued``)
    JobCoordinator        -> background thread; claims queued runs under the
                             ``writer_lease`` single-writer lease, executes
                             ``execute_run_cached`` and releases the lease
    execute_run_cached()  -> snapshot -> ``run_pipeline_cached`` -> publish,
                             with per-stage Parquet caching via ``StageCache``
    run_pipeline_cached() -> ``runner.run_pipeline`` composition re-implemented
                             stage-by-stage so unchanged stage fingerprints
                             (``contracts.stage_fingerprint``) are served from
                             the cache instead of recomputed

Process model: ``JobCoordinator`` runs the CPU-bound pipeline on ONE daemon
thread per coordinator — never on the caller's (e.g. FastAPI event-loop)
thread. SQLite is the coordination point, so several processes may host
coordinators safely: the ``writer_lease`` row guarantees only one executes
at a time and expired leases are taken over. There is no hard per-job
timeout (a stuck stage would block the single worker); orphaned ``running``
rows are bounded by the lease TTL and marked ``interrupted`` on startup via
:func:`recover_interrupted_runs`.
"""

import hashlib
import json
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError

from callscore_api.services import calculation
from callscore_api.services.cache import StageCache
from callscore_core.contracts import (
    STAGE_ORDER,
    RunContext,
    StageArtifact,
    stage_fingerprint,
)
from callscore_core.enums import RunStatus, Stage
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
from callscore_engine.runner import (
    INPUT_FRAME_NAMES,
    PipelineResult,
    SnapshotFrames,
    assert_frame_contract,
)
from callscore_engine.stats import infer_metrics
from callscore_engine.targets import build_target_outcomes
from callscore_store import create_engine_for
from callscore_store.models import CalcRun, MethodologyVersion
from callscore_store.repositories import derived, runs
from callscore_store.repositories import jobs as repo_jobs
from callscore_store.repositories.base import Context
from callscore_store.repositories.runs import RunTransitionError
from callscore_store.snapshots import SecurityFilter, load_input_snapshot

# ---------------------------------------------------------------------------
# Stage cache wiring
# ---------------------------------------------------------------------------

# Output frames owned by each coarse stage. MUST mirror
# ``callscore_engine.runner._FRAME_STAGE`` (private there): the fingerprint
# chain in ``contracts.STAGE_ORDER`` guarantees cache hits form a prefix, so
# a stage can only be served from cache when every upstream stage was too.
STAGE_OUTPUTS: dict[Stage, tuple[str, ...]] = {
    Stage.INTERVALS: ("intervals", "target_outcomes", "coverage_daily"),
    Stage.RETURNS: ("returns",),
    Stage.EXPOSURES: ("exposures", "contributions", "event_windows"),
    Stage.METRICS: ("portfolio_nav", "metric_values", "metric_samples"),
    Stage.STATISTICS: ("metric_results", "composites"),
}

# Runner's ``outputs`` dict order — kept identical so ``stage_artifacts``
# tuples match ``runner.run_pipeline`` byte-for-byte.
_OUTPUT_ORDER: tuple[str, ...] = (
    "intervals",
    "exposures",
    "returns",
    "contributions",
    "event_windows",
    "target_outcomes",
    "coverage_daily",
    "portfolio_nav",
    "metric_values",
    "metric_samples",
    "metric_results",
    "composites",
)

_FRAME_STAGE: dict[str, Stage] = {
    name: stage for stage, names in STAGE_OUTPUTS.items() for name in names
}

OnStageCallback = Callable[[Stage, bool], None]
"""``(stage, cache_hit)`` invoked once per stage, in ``STAGE_ORDER``."""


def _empty_frame(contract: FrameContract) -> pl.DataFrame:
    return pl.DataFrame(schema=contract_schema(contract))


def _input_frames(
    frames: SnapshotFrames | Mapping[str, pl.DataFrame],
) -> dict[str, pl.DataFrame]:
    """Same normalization as ``runner._input_frames`` (absent -> empty)."""
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


def _stage_fingerprints(
    methodology: Methodology, context: RunContext
) -> tuple[dict[Stage, str], dict[Stage, tuple[str, ...]]]:
    """Same chained fingerprints as ``runner._stage_fingerprints``."""
    fingerprints: dict[Stage, str] = {}
    upstreams: dict[Stage, tuple[str, ...]] = {}
    upstream: tuple[str, ...] = ()
    for stage in STAGE_ORDER:
        fingerprints[stage] = stage_fingerprint(stage, methodology, context, upstream)
        upstreams[stage] = upstream
        upstream = (*upstream, fingerprints[stage])
    return fingerprints, upstreams


def _jsonable(value: Any) -> Any:
    """Same canonicalization as ``runner._content_fingerprint``."""
    import math
    from datetime import date, datetime
    from decimal import Decimal

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
    sorted_frame = frame.sort(list(sort_by)) if sort_by else frame
    rows = [{k: _jsonable(v) for k, v in record.items()} for record in sorted_frame.to_dicts()]
    payload = {"frame": frame_name, "rows": rows}
    return hashlib.sha256(canonical_dumps(payload).encode()).hexdigest()


def _sort_keys(frame_name: str) -> tuple[str, ...]:
    contract = FRAME_CONTRACTS.get(frame_name)
    if contract is not None:
        return contract.sort_by
    # ``metric_values`` has no FrameContract; implicit key (analyst_id, metric_code).
    return ("analyst_id", "metric_code")


def _analyst_roster(*frames: pl.DataFrame) -> list[str]:
    ids: set[str] = set()
    for frame in frames:
        if "analyst_id" in frame.columns:
            ids.update(str(v) for v in frame["analyst_id"].drop_nulls().to_list())
    return sorted(ids)


def _n_investable(intervals: pl.DataFrame) -> pl.DataFrame:
    return (
        intervals.filter(pl.col("is_investable"))
        .group_by("analyst_id")
        .len()
        .rename({"len": "n_obs"})
        .select("analyst_id", "n_obs")
    )


def _compute_stage(
    stage: Stage,
    data: Mapping[str, pl.DataFrame],
    out: dict[str, pl.DataFrame],
    methodology: Methodology,
    context: RunContext,
    peer_groups: pl.DataFrame,
) -> None:
    """Compute one coarse stage's output frames into ``out``.

    Mirrors the corresponding slice of ``runner.run_pipeline`` exactly; every
    engine stage function is pure, so results are identical whether the
    upstream frames came from the cache or fresh computation.
    """
    if stage == Stage.INTERVALS:
        intervals = build_intervals(
            data["rating_events"],
            data["sessions"],
            data["coverage_spells"],
            data["employment_spells"],
            data["security_classifications"],
            data["delistings"],
            methodology,
            context,
        )
        out["intervals"] = intervals
        out["target_outcomes"] = build_target_outcomes(
            data["rating_events"], intervals, data["security_daily"], data["sessions"], context
        )
        out["coverage_daily"] = build_coverage_daily(
            data["rating_events"],
            intervals,
            data["coverage_spells"],
            data["sessions"],
            data["security_classifications"],
            context,
        )
    elif stage == Stage.RETURNS:
        out["returns"] = build_returns(
            out["intervals"],
            data["security_daily"],
            data["benchmark_daily"],
            data["security_beta"],
            data["delistings"],
            data["security_classifications"],
            data["sessions"],
            methodology,
            context,
        )
    elif stage == Stage.EXPOSURES:
        exposures = build_exposures(
            out["intervals"], data["rating_events"], data["sessions"], methodology
        )
        out["exposures"] = exposures
        out["contributions"] = build_contributions(
            out["intervals"],
            exposures,
            out["returns"],
            data["security_daily"],
            data["benchmark_daily"],
            data["security_beta"],
            data["delistings"],
            data["security_classifications"],
            methodology,
        )
        out["event_windows"] = build_event_windows(
            out["intervals"],
            exposures,
            data["security_daily"],
            data["benchmark_daily"],
            data["security_classifications"],
            methodology,
            context,
        )
    elif stage == Stage.METRICS:
        out["portfolio_nav"] = simulate_portfolios(
            out["intervals"],
            out["exposures"],
            data["security_daily"],
            data["benchmark_daily"],
            data["security_beta"],
            data["delistings"],
            data["security_classifications"],
            data["sessions"],
            methodology,
            context,
        )
        metric_inputs = MetricInputs(
            contributions=out["contributions"],
            event_windows=out["event_windows"],
            target_outcomes=out["target_outcomes"],
            coverage_daily=out["coverage_daily"],
            portfolio_nav=out["portfolio_nav"],
            rating_events=visible_events(data["rating_events"], context.knowledge_cutoff),
            coverage_spells=data["coverage_spells"],
            security_classifications=data["security_classifications"],
            benchmark_daily=data["benchmark_daily"],
            sessions=data["sessions"],
            intervals=out["intervals"],
        )
        roster = _analyst_roster(peer_groups, out["intervals"], data["rating_events"])
        metric_outputs = compute_metrics(metric_inputs, roster, methodology, context)
        out["metric_values"] = metric_outputs.values
        out["metric_samples"] = metric_outputs.samples
    elif stage == Stage.STATISTICS:
        out["metric_results"] = infer_metrics(
            out["metric_values"], out["metric_samples"], peer_groups, methodology
        )
        out["composites"] = compute_composites(
            out["metric_results"], peer_groups, _n_investable(out["intervals"]), methodology
        )
    else:  # pragma: no cover - STAGE_ORDER is exhaustive
        raise ValueError(f"unknown stage: {stage}")


def run_pipeline_cached(
    frames: SnapshotFrames | Mapping[str, pl.DataFrame],
    methodology: Methodology,
    context: RunContext,
    cache: StageCache | None,
    *,
    on_stage: OnStageCallback | None = None,
) -> PipelineResult:
    """``runner.run_pipeline`` with per-stage Parquet caching.

    For each stage in ``STAGE_ORDER`` the cache is consulted under that
    stage's ``cache_fingerprint``; on a hit the stored frames are loaded
    (schema/row-count validated — corruption is a miss), on a miss the stage
    is recomputed from raw inputs plus already-resolved upstream frames and
    stored back. Because fingerprints chain upstreams, hits always form a
    prefix of ``STAGE_ORDER``: e.g. changing only ``transaction_cost_bps``
    recomputes METRICS and STATISTICS while INTERVALS/RETURNS/EXPOSURES are
    served from cache.

    Output is a ``runner.PipelineResult`` identical to ``run_pipeline``'s —
    warm and cold runs produce equal frames.
    """
    data = _input_frames(frames)
    fingerprints, upstreams = _stage_fingerprints(methodology, context)
    peer_groups = assign_peer_groups(
        data["coverage_spells"],
        data["security_classifications"],
        context.as_of_date,
        methodology.peer_group_dims,
    )

    out: dict[str, pl.DataFrame] = {}
    for stage in STAGE_ORDER:
        names = STAGE_OUTPUTS[stage]
        fingerprint = fingerprints[stage]
        cached: dict[str, pl.DataFrame] | None = None
        if cache is not None:
            cached = cache.load_stage(stage, fingerprint, names)
        if cached is not None:
            out.update(cached)
            hit = True
        else:
            _compute_stage(stage, data, out, methodology, context, peer_groups)
            if cache is not None:
                cache.store_stage(stage, fingerprint, {n: out[n] for n in names})
            hit = False
        if on_stage is not None:
            on_stage(stage, hit)

    # Same contract validation as runner.run_pipeline.
    for name in _OUTPUT_ORDER:
        contract = FRAME_CONTRACTS.get(name)
        if contract is not None:
            assert_frame_contract(out[name], contract)

    artifacts = tuple(
        StageArtifact(
            stage=_FRAME_STAGE[name],
            content_fingerprint=_content_fingerprint(out[name], name, _sort_keys(name)),
            cache_fingerprint=fingerprints[_FRAME_STAGE[name]],
            upstream_fingerprints=upstreams[_FRAME_STAGE[name]],
            row_count=out[name].height,
        )
        for name in _OUTPUT_ORDER
    )

    return PipelineResult(
        intervals=out["intervals"],
        exposures=out["exposures"],
        returns=out["returns"],
        contributions=out["contributions"],
        event_windows=out["event_windows"],
        target_outcomes=out["target_outcomes"],
        coverage_daily=out["coverage_daily"],
        portfolio_nav=out["portfolio_nav"],
        metric_values=out["metric_values"],
        metric_samples=out["metric_samples"],
        metric_results=out["metric_results"],
        composites=out["composites"],
        peer_groups=peer_groups,
        n_investable=_n_investable(out["intervals"]),
        stage_fingerprints=fingerprints,
        stage_artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# Idempotent submission
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of :func:`submit_run`."""

    run_id: str
    created: bool
    status: str


def _canonical_universe(universe_filter: Iterable[str] | None) -> str | None:
    if universe_filter is None:
        return None
    if isinstance(universe_filter, str):
        raise TypeError("universe_filter must be an iterable of security ids, not a str")
    return json.dumps(sorted({str(s) for s in universe_filter}))


def _parse_universe_filter(raw: str | None) -> SecurityFilter:
    if raw is None:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or not all(isinstance(s, str) for s in parsed):
        raise ValueError(f"universe_filter_json must be a JSON list of strings: {raw!r}")
    return parsed


def _status_rank(status: object) -> int:
    order = {
        RunStatus.QUEUED.value: 0,
        RunStatus.RUNNING.value: 1,
        RunStatus.SUCCEEDED.value: 2,
        RunStatus.INTERRUPTED.value: 3,
        RunStatus.FAILED.value: 4,
    }
    return order.get(str(status), 5)


def _norm_universe_json(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        return canonical_dumps(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return raw


def _find_existing_run(
    ctx: Context,
    methodology_fingerprint: str,
    as_of_date: date,
    knowledge_cutoff: datetime,
    universe_filter_json: str | None,
) -> str | None:
    """Find a ``calc_run`` with the same idempotency key, if any.

    Key = (methodology fingerprint, as_of_date, knowledge_cutoff, universe).
    When several rows match (e.g. created via ``prepare_run`` before this
    coordinator existed) the most actionable status wins: queued/running,
    then succeeded, then interrupted, then failed.
    """
    stmt = (
        select(CalcRun.run_id, CalcRun.status, CalcRun.universe_filter_json)
        .join(MethodologyVersion, CalcRun.methodology_version_id == MethodologyVersion.id)
        .where(
            MethodologyVersion.fingerprint == methodology_fingerprint,
            CalcRun.as_of_date == as_of_date,
            CalcRun.knowledge_cutoff == knowledge_cutoff,
        )
    )
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            rows = [dict(r) for r in conn.execute(stmt).mappings()]
    else:
        rows = [dict(r) for r in ctx.execute(stmt).mappings()]
    target = _norm_universe_json(universe_filter_json)
    matches = [
        r
        for r in rows
        if _norm_universe_json(r["universe_filter_json"] if r["universe_filter_json"] else None)
        == target
    ]
    if not matches:
        return None
    matches.sort(key=lambda r: (_status_rank(r["status"]), str(r["run_id"])))
    return str(matches[0]["run_id"])


def _default_run_id(
    methodology_fingerprint: str,
    as_of_date: date,
    knowledge_cutoff: datetime,
    universe_filter_json: str | None,
    algorithm_version: str,
) -> str:
    payload = {
        "algorithm_version": algorithm_version,
        "as_of_date": as_of_date.isoformat(),
        "knowledge_cutoff": knowledge_cutoff.isoformat(),
        "methodology_fingerprint": methodology_fingerprint,
        "universe_filter_json": universe_filter_json,
    }
    digest = hashlib.sha256(canonical_dumps(payload).encode()).hexdigest()
    return f"run-{digest[:48]}"


def _methodology_version_id(ctx: Context, methodology: Methodology) -> int:
    """``register_methodology_version`` with a race-safe fallback.

    The repository does check-then-insert; two concurrent submitters can both
    pass the check and one loses on the ``fingerprint`` UNIQUE constraint —
    in that case the winner's row is fetched instead.
    """
    try:
        return runs.register_methodology_version(ctx, methodology)
    except IntegrityError:
        stmt = select(MethodologyVersion.id).where(
            MethodologyVersion.fingerprint == methodology.fingerprint()
        )
        if isinstance(ctx, Engine):
            with ctx.connect() as conn:
                row = conn.execute(stmt).scalar_one_or_none()
        else:
            row = ctx.execute(stmt).scalar_one_or_none()
        if row is None:  # pragma: no cover - the conflicting row must exist
            raise
        return int(row)


def _coerce_methodology(methodology: Methodology | Mapping[str, Any]) -> Methodology:
    if isinstance(methodology, Methodology):
        return methodology
    return Methodology.model_validate(dict(methodology))


def submit_run(
    ctx: Context,
    *,
    methodology: Methodology | Mapping[str, Any],
    as_of_date: date,
    knowledge_cutoff: datetime | None = None,
    universe_filter: Iterable[str] | None = None,
    universe_filter_json: str | None = None,
    run_id: str | None = None,
    algorithm_version: str = calculation.ALGORITHM_VERSION,
) -> SubmitResult:
    """Create a ``queued`` ``calc_run``; idempotent on the run key.

    The key is ``(methodology fingerprint, as_of_date, knowledge_cutoff,
    universe_filter)``. Re-submitting the same key returns the existing run
    with ``created=False`` instead of duplicating it — enforced both by a
    pre-insert lookup and by a deterministic ``run_id`` (primary-key
    ``IntegrityError`` fallback for concurrent submitters).
    """
    if universe_filter is not None and universe_filter_json is not None:
        raise ValueError("pass either universe_filter or universe_filter_json, not both")
    methodology = _coerce_methodology(methodology)
    cutoff = knowledge_cutoff or calculation.default_knowledge_cutoff(as_of_date)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    else:
        cutoff = cutoff.astimezone(UTC)
    universe_json = (
        universe_filter_json
        if universe_filter_json is not None
        else _canonical_universe(universe_filter)
    )
    methodology_fingerprint = methodology.fingerprint()

    existing = _find_existing_run(ctx, methodology_fingerprint, as_of_date, cutoff, universe_json)
    if existing is not None:
        run = runs.get_calc_run(ctx, existing)
        return SubmitResult(
            run_id=existing, created=False, status=str(run["status"]) if run else "unknown"
        )

    rid = run_id or _default_run_id(
        methodology_fingerprint, as_of_date, cutoff, universe_json, algorithm_version
    )
    methodology_version_id = _methodology_version_id(ctx, methodology)
    try:
        runs.create_calc_run(
            ctx,
            run_id=rid,
            methodology_version_id=methodology_version_id,
            as_of_date=as_of_date,
            knowledge_cutoff=cutoff,
            algorithm_version=algorithm_version,
            universe_filter_json=universe_json,
        )
    except IntegrityError:
        # Concurrent submitter won the race (same deterministic run_id), or
        # the caller-supplied run_id already exists.
        existing = _find_existing_run(
            ctx, methodology_fingerprint, as_of_date, cutoff, universe_json
        )
        if existing is not None:
            run = runs.get_calc_run(ctx, existing)
            return SubmitResult(
                run_id=existing, created=False, status=str(run["status"]) if run else "unknown"
            )
        run = runs.get_calc_run(ctx, rid)
        if run is None:
            raise  # unrelated integrity failure (e.g. FK) — do not mask it
        return SubmitResult(run_id=rid, created=False, status=str(run["status"]))
    return SubmitResult(run_id=rid, created=True, status=RunStatus.QUEUED.value)


# ---------------------------------------------------------------------------
# Cached execution
# ---------------------------------------------------------------------------


def _load_methodology(ctx: Context, run: Mapping[str, Any]) -> Methodology:
    methodology_version_id = int(run["methodology_version_id"])
    stmt = select(MethodologyVersion.params_json).where(
        MethodologyVersion.id == methodology_version_id
    )
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            params_json = conn.execute(stmt).scalar_one_or_none()
    else:
        params_json = ctx.execute(stmt).scalar_one_or_none()
    if params_json is None:
        raise KeyError(f"methodology_version {methodology_version_id} not found")
    return Methodology.model_validate(json.loads(str(params_json)))


def execute_run_cached(
    ctx: Context,
    run_id: str,
    *,
    cache: StageCache | None = None,
    on_stage: OnStageCallback | None = None,
) -> dict[str, int]:
    """Execute a ``queued`` run like ``calculation.execute_run`` but stage-cached.

    Identical lifecycle and persistence (``queued -> running ->
    succeeded|failed``, atomic ``publish_run_results``, ``error_json`` on
    failure); only the pipeline composition differs — unchanged stage
    fingerprints are served from ``cache`` when provided.
    """
    run = runs.get_calc_run(ctx, run_id)
    if run is None:
        raise KeyError(f"calc_run {run_id!r} not found")
    methodology = _load_methodology(ctx, run)
    as_of_date = run["as_of_date"]
    if not isinstance(as_of_date, date):
        raise TypeError(f"calc_run.as_of_date has unexpected type {type(as_of_date)}")
    cutoff = run["knowledge_cutoff"]
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        cutoff = cutoff.replace(tzinfo=UTC)

    runs.update_run_status(ctx, run_id, RunStatus.RUNNING)
    try:
        snapshot = load_input_snapshot(
            ctx,
            as_of_date,
            cutoff,
            universe_filter=_parse_universe_filter(run["universe_filter_json"]),
        )
        run_context = RunContext(
            as_of_date=as_of_date,
            knowledge_cutoff=cutoff,
            source_revision="callscore-store",
            source_fingerprint=snapshot.source_fingerprint,
            universe_fingerprint=snapshot.universe_fingerprint,
            algorithm_version=str(run["algorithm_version"] or calculation.ALGORITHM_VERSION),
        )
        result = run_pipeline_cached(
            SnapshotFrames.from_mapping(snapshot.frames),
            methodology,
            run_context,
            cache,
            on_stage=on_stage,
        )
        stage_outputs = {
            "intervals": result.intervals,
            "exposures": result.exposures,
            "returns": result.returns,
            "contributions": result.contributions,
            "event_windows": result.event_windows,
            "target_outcomes": result.target_outcomes,
            "metric_results": result.metric_results,
            "composites": result.composites,
            "portfolio_nav": result.portfolio_nav,
        }
        counts = derived.publish_run_results(
            ctx,
            run_id,
            stage_outputs,
            stage_artifacts=result.stage_artifacts,
            source_fingerprint=snapshot.source_fingerprint,
        )
    except Exception as exc:
        runs.update_run_status(
            ctx,
            run_id,
            RunStatus.FAILED,
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        raise
    return counts


# ---------------------------------------------------------------------------
# Recovery + status helpers
# ---------------------------------------------------------------------------


def recover_interrupted_runs(ctx: Context) -> int:
    """Mark ``running`` runs ``interrupted`` (orphaned by a dead worker).

    Call once at coordinator/process startup, BEFORE the worker thread
    begins claiming jobs. Returns the number of runs transitioned.
    """
    stmt = select(CalcRun.run_id).where(CalcRun.status == RunStatus.RUNNING.value)
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            run_ids = [str(r) for r in conn.execute(stmt).scalars()]
    else:
        run_ids = [str(r) for r in ctx.execute(stmt).scalars()]
    count = 0
    for run_id in run_ids:
        try:
            runs.update_run_status(ctx, run_id, RunStatus.INTERRUPTED)
            count += 1
        except RunTransitionError:
            continue
    return count


def job_status(ctx: Context, run_id: str) -> dict[str, Any] | None:
    """Run status/progress/timings as a dict (``None`` if unknown)."""
    return runs.get_calc_run(ctx, run_id)


def list_jobs(
    ctx: Context,
    *,
    status: RunStatus | str | None = None,
    limit: int | None = 100,
) -> list[dict[str, Any]]:
    """List calc runs (jobs) newest ``as_of_date`` first."""
    return runs.list_calc_runs(ctx, status=status, sort="as_of_date", descending=True, limit=limit)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class JobCoordinator:
    """Background worker draining ``queued`` calc runs under the writer lease.

    ``db`` is a ``pathlib.Path``/``str`` DB location or an existing
    ``Engine``. ``cache`` defaults to ``<db dir>/cache`` for path inputs;
    pass ``cache=None, cache_dir=None`` with an ``Engine`` to disable stage
    caching. CPU work runs on one daemon thread — safe to call ``submit``
    from an async event loop.
    """

    def __init__(
        self,
        db: str | Path | Engine,
        *,
        cache: StageCache | None = None,
        cache_dir: str | Path | None = None,
        holder: str | None = None,
        lease_ttl: float = 300.0,
        poll_interval: float = 0.5,
        recover_on_start: bool = True,
    ) -> None:
        self._owns_engine = not isinstance(db, Engine)
        self._engine = db if isinstance(db, Engine) else create_engine_for(db)
        self._cache: StageCache | None
        if cache is not None:
            self._cache = cache
        elif cache_dir is not None:
            self._cache = StageCache(cache_dir)
        elif isinstance(db, (str, Path)):
            self._cache = StageCache(Path(db).parent / "cache")
        else:
            self._cache = None
        self._holder = holder or f"callscore-jobs-{os.getpid()}"
        self._lease_ttl = float(lease_ttl)
        self._poll_interval = float(poll_interval)
        self._recover_on_start = recover_on_start
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self.last_error: str | None = None

    # -- properties ----------------------------------------------------

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def cache(self) -> StageCache | None:
        return self._cache

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- submission ----------------------------------------------------

    def submit(self, **kwargs: Any) -> SubmitResult:
        """See :func:`submit_run`; ``ctx`` is bound to this coordinator's DB."""
        return submit_run(self._engine, **kwargs)

    # -- execution -----------------------------------------------------

    def _next_queued_run_id(self) -> str | None:
        queued = runs.list_calc_runs(
            self._engine, status=RunStatus.QUEUED, sort="as_of_date", descending=False, limit=1
        )
        return str(queued[0]["run_id"]) if queued else None

    def _execute_one(self, run_id: str) -> None:
        """Execute ``run_id``; caller must hold the writer lease."""
        stage_count = 0

        def on_stage(_stage: Stage, _hit: bool) -> None:
            nonlocal stage_count
            stage_count += 1
            # Progress is best-effort; never fail the job over it.
            with suppress(Exception):
                runs.update_run_progress(
                    self._engine,
                    run_id,
                    min(0.95, 0.1 + 0.8 * stage_count / len(STAGE_ORDER)),
                )
            repo_jobs.heartbeat_writer_lease(self._engine, self._holder, ttl=self._lease_ttl)

        execute_run_cached(self._engine, run_id, cache=self._cache, on_stage=on_stage)

    def run_once(self) -> str | None:
        """Claim and execute at most one queued run; ``None`` if none/lease busy.

        Synchronous — intended for tests and embedded workers. Raises if the
        job itself failed (the run row is already ``failed`` with
        ``error_json``).
        """
        run_id = self._next_queued_run_id()
        if run_id is None:
            return None
        if not repo_jobs.acquire_writer_lease(
            self._engine, self._holder, run_id, ttl=self._lease_ttl
        ):
            return None
        try:
            self._execute_one(run_id)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            repo_jobs.release_writer_lease(self._engine, self._holder)
        return run_id

    # -- lifecycle -----------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self.run_once()
            except Exception as exc:  # job failure already recorded on the run
                self.last_error = f"{type(exc).__name__}: {exc}"
                ran = None
            if ran is None:
                self._stop.wait(self._poll_interval)

    def start(self) -> None:
        """Start the worker thread; recovers orphaned ``running`` jobs first."""
        with self._state_lock:
            if self.is_running:
                return
            if self._recover_on_start:
                recover_interrupted_runs(self._engine)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="callscore-job-worker", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker to stop and wait up to ``timeout`` seconds."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def close(self) -> None:
        """Stop the worker and dispose the engine if we created it."""
        self.stop()
        if self._owns_engine:
            self._engine.dispose()

    def __enter__(self) -> "JobCoordinator":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "JobCoordinator",
    "STAGE_OUTPUTS",
    "SubmitResult",
    "execute_run_cached",
    "job_status",
    "list_jobs",
    "recover_interrupted_runs",
    "run_pipeline_cached",
    "submit_run",
]
