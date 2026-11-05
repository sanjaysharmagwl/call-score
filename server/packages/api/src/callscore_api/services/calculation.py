"""DB-facing orchestration: load a snapshot, run the engine, persist results.

``execute_run`` drives one ``calc_run`` row through its lifecycle
(``queued -> running -> succeeded|failed``) and publishes every derived
frame via ``repositories.derived.publish_run_results`` in a single
transaction — a failure leaves the run ``failed`` with ``error_json`` and no
partial rows.
"""

import json
import logging
from datetime import UTC, date, datetime, time, timedelta
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from sqlalchemy import Engine, select

from callscore_core.contracts import RunContext
from callscore_core.enums import RunStatus
from callscore_core.methodology import Methodology
from callscore_engine.runner import SnapshotFrames, run_pipeline
from callscore_store.models import MethodologyVersion
from callscore_store.repositories import derived, runs
from callscore_store.repositories.base import Context
from callscore_store.snapshots import SecurityFilter, Snapshot, load_input_snapshot

logger = logging.getLogger(__name__)


def _engine_version() -> str:
    try:
        return version("callscore-engine")
    except PackageNotFoundError:
        return "0.0.0"


ALGORITHM_VERSION = _engine_version()


def default_knowledge_cutoff(as_of_date: date) -> datetime:
    """End-of-day-UTC cutoff used when a run does not specify one."""
    return datetime.combine(as_of_date + timedelta(days=1), time.min, tzinfo=UTC)


def _methodology_for_run(ctx: Context, run: dict[str, Any]) -> Methodology:
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


def prepare_run(
    ctx: Context,
    methodology: Methodology,
    *,
    run_id: str,
    as_of_date: date,
    knowledge_cutoff: datetime | None = None,
    algorithm_version: str = ALGORITHM_VERSION,
    universe_filter_json: str | None = None,
) -> str:
    """Register the methodology and create a ``queued`` ``calc_run`` row."""
    methodology_version_id = runs.register_methodology_version(ctx, methodology)
    return runs.create_calc_run(
        ctx,
        run_id=run_id,
        methodology_version_id=methodology_version_id,
        as_of_date=as_of_date,
        knowledge_cutoff=knowledge_cutoff or default_knowledge_cutoff(as_of_date),
        algorithm_version=algorithm_version,
        universe_filter_json=universe_filter_json,
    )


def execute_run(
    ctx: Context,
    run_id: str,
    *,
    knowledge_cutoff: datetime | None = None,
    universe_filter: SecurityFilter = None,
) -> dict[str, int]:
    """Execute a ``queued`` run end-to-end; returns ``{table: row_count}``.

    On any failure the run transitions to ``failed`` with ``error_json`` and
    no derived rows are written (``publish_run_results`` is atomic).
    """
    run = runs.get_calc_run(ctx, run_id)
    if run is None:
        raise KeyError(f"calc_run {run_id!r} not found")
    methodology = _methodology_for_run(ctx, run)
    as_of_date = run["as_of_date"]
    if not isinstance(as_of_date, date):
        raise TypeError(f"calc_run.as_of_date has unexpected type {type(as_of_date)}")
    cutoff = knowledge_cutoff or run["knowledge_cutoff"]
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        cutoff = cutoff.replace(tzinfo=UTC)

    runs.update_run_status(ctx, run_id, RunStatus.RUNNING)
    logger.info(
        "run_started",
        extra={"run_id": run_id, "as_of_date": as_of_date.isoformat()},
    )
    try:
        snapshot: Snapshot = load_input_snapshot(
            ctx, as_of_date, cutoff, universe_filter=universe_filter
        )
        run_context = RunContext(
            as_of_date=as_of_date,
            knowledge_cutoff=cutoff,
            source_revision="callscore-store",
            source_fingerprint=snapshot.source_fingerprint,
            universe_fingerprint=snapshot.universe_fingerprint,
            algorithm_version=str(run["algorithm_version"] or ALGORITHM_VERSION),
        )
        result = run_pipeline(
            SnapshotFrames.from_mapping(snapshot.frames), methodology, run_context
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
            "portfolio_positions": result.portfolio_positions,
        }
        counts = derived.publish_run_results(
            ctx,
            run_id,
            stage_outputs,
            stage_artifacts=result.stage_artifacts,
            source_fingerprint=snapshot.source_fingerprint,
        )
        logger.info(
            "run_succeeded",
            extra={"run_id": run_id, "rows": sum(counts.values())},
        )
    except Exception as exc:
        runs.update_run_status(
            ctx,
            run_id,
            RunStatus.FAILED,
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        logger.error(
            "run_failed",
            extra={"run_id": run_id, "error_type": type(exc).__name__},
        )
        raise
    return counts


__all__ = [
    "ALGORITHM_VERSION",
    "default_knowledge_cutoff",
    "execute_run",
    "prepare_run",
]
