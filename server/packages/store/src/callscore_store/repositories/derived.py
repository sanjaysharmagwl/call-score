"""Transactional persistence of engine stage outputs for a ``calc_run``.

``publish_run_results`` replaces ALL derived rows for ``run_id`` and marks the
run ``succeeded`` in ONE transaction: readers never observe a partially
written run, and any failure leaves the previous state untouched.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import polars as pl
from sqlalchemy import delete, select

from callscore_core.contracts import StageArtifact
from callscore_core.enums import RunStatus
from callscore_store.models import (
    AnalystComposite,
    AnalystMetric,
    CalcRun,
    EventWindow,
    IntervalContribution,
    IntervalExposure,
    IntervalReturn,
    PortfolioNav,
    PortfolioPosition,
    RatingInterval,
    TargetOutcome,
)
from callscore_store.repositories.base import Context, table_of, unit_of_work
from callscore_store.types import UTCDateTime

# Engine stage-output frame name -> derived table model.
# Frame column names match the table columns minus ``run_id``.
STAGE_TABLES: dict[str, type] = {
    "intervals": RatingInterval,
    "exposures": IntervalExposure,
    "returns": IntervalReturn,
    "contributions": IntervalContribution,
    "event_windows": EventWindow,
    "target_outcomes": TargetOutcome,
    "metric_results": AnalystMetric,
    "composites": AnalystComposite,
    "portfolio_nav": PortfolioNav,
    "portfolio_positions": PortfolioPosition,
}

# Delete children before parents (FKs are immediate in SQLite).
_DELETE_ORDER: tuple[type, ...] = (
    IntervalExposure,
    IntervalReturn,
    IntervalContribution,
    EventWindow,
    TargetOutcome,
    PortfolioPosition,
    PortfolioNav,
    AnalystMetric,
    AnalystComposite,
    RatingInterval,
)


def _utc_aware(value: Any) -> Any:
    """Attach UTC to naive datetimes coming from Polars frames."""
    if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
        return value.replace(tzinfo=UTC)
    return value


def _frame_rows(model: type, frame: pl.DataFrame, run_id: str) -> list[dict[str, Any]]:
    """Convert a Polars frame to insert dicts with ``run_id`` added."""
    table = table_of(model)
    expected = {c.name for c in table.columns} - {"run_id"}
    actual = set(frame.columns)
    if actual != expected:
        raise ValueError(
            f"{table_of(model).name}: frame columns {sorted(actual)} do not match "
            f"expected {sorted(expected)}"
        )
    ts_columns = {c.name for c in table.columns if isinstance(c.type, UTCDateTime)}
    rows: list[dict[str, Any]] = []
    for record in frame.to_dicts():
        for name in ts_columns:
            if name in record:
                record[name] = _utc_aware(record[name])
        record["run_id"] = run_id
        rows.append(record)
    return rows


def publish_run_results(
    ctx: Context,
    run_id: str,
    stage_outputs: Mapping[str, pl.DataFrame],
    stage_artifacts: Sequence[StageArtifact] = (),
    *,
    source_fingerprint: str | None = None,
) -> dict[str, int]:
    """Replace a run's derived rows and mark it ``succeeded`` atomically.

    ``stage_outputs`` maps frame names (``STAGE_TABLES`` keys, e.g.
    ``"intervals"``, ``"metric_results"``) to Polars DataFrames matching the
    ``callscore_core.frames`` contracts. ``stage_artifacts`` are recorded in
    ``calc_run.row_counts_json`` under ``"stage_artifacts"`` (there is no
    dedicated artifact table — see handoff/schema gap).

    All deletes, inserts and the status update happen in ONE transaction; a
    failure anywhere rolls back, leaving the run non-succeeded and no partial
    rows. Returns ``{table_name: inserted_count}``.
    """
    unknown = set(stage_outputs) - set(STAGE_TABLES)
    if unknown:
        raise ValueError(f"unknown stage output frames: {sorted(unknown)}")

    # Pre-convert every frame BEFORE opening the write transaction so a bad
    # frame fails fast without touching the DB.
    prepared: list[tuple[type, list[dict[str, Any]]]] = []
    for frame_name, model in STAGE_TABLES.items():
        frame = stage_outputs.get(frame_name)
        if frame is None:
            continue
        if not isinstance(frame, pl.DataFrame):
            raise TypeError(f"{frame_name}: expected pl.DataFrame, got {type(frame)}")
        prepared.append((model, _frame_rows(model, frame, run_id)))

    artifacts_json = [a.model_dump(mode="json") for a in stage_artifacts]
    row_counts = {
        "rows": {table_of(model).name: len(rows) for model, rows in prepared},
        "stage_artifacts": artifacts_json,
    }

    with unit_of_work(ctx) as conn:
        run = conn.execute(select(CalcRun).where(CalcRun.run_id == run_id)).scalar_one_or_none()
        if run is None:
            raise KeyError(f"calc_run {run_id!r} not found")

        for model in _DELETE_ORDER:
            tbl = table_of(model)
            conn.execute(delete(tbl).where(tbl.c.run_id == run_id))

        counts: dict[str, int] = {}
        for model, rows in prepared:
            if not rows:
                counts[table_of(model).name] = 0
                continue
            conn.execute(table_of(model).insert(), rows)
            counts[table_of(model).name] = len(rows)

        values: dict[str, Any] = {
            "status": RunStatus.SUCCEEDED.value,
            "progress": 1.0,
            "finished_at": datetime.now(UTC),
            "row_counts_json": json.dumps(row_counts, sort_keys=True),
        }
        if source_fingerprint is not None:
            values["source_fingerprint"] = source_fingerprint
        conn.execute(table_of(CalcRun).update().where(CalcRun.run_id == run_id).values(**values))
    return counts
