"""``calc_run`` bookkeeping: creation, status transitions, progress, listing."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, cast

from sqlalchemy import Connection, Engine, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from callscore_core.enums import RunStatus
from callscore_core.methodology import Methodology, canonical_dumps
from callscore_store.models import CalcRun, MethodologyVersion
from callscore_store.repositories.base import Context, table_of, unit_of_work

# Allowed status transitions (see T08): retry is failed -> queued only.
_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING}),
    RunStatus.RUNNING: frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.INTERRUPTED}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset({RunStatus.QUEUED}),
    RunStatus.INTERRUPTED: frozenset(),
}

_TERMINAL = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.INTERRUPTED})

# Whitelisted sort columns for list_calc_runs (injection safety).
_RUN_SORT_COLUMNS = frozenset(
    {"run_id", "as_of_date", "knowledge_cutoff", "status", "started_at", "finished_at"}
)


class RunTransitionError(ValueError):
    """Illegal ``calc_run`` status transition."""


def _status_value(raw: object) -> RunStatus:
    if isinstance(raw, RunStatus):
        return raw
    return RunStatus(str(raw))


def register_methodology_version(
    ctx: Context,
    methodology: Methodology | Mapping[str, Any],
    *,
    is_baseline: bool = False,
) -> int:
    """Insert a methodology version; returns its ``id``.

    ``methodology`` may be a :class:`Methodology` or a plain mapping; the
    fingerprint is the methodology's canonical SHA-256 (computed when a
    ``Methodology`` is passed, otherwise over the canonical JSON).
    """
    if isinstance(methodology, Methodology):
        params_json = methodology.canonical_json()
        fingerprint = methodology.fingerprint()
    else:
        params_json = canonical_dumps(dict(methodology))
        fingerprint = hashlib.sha256(params_json.encode()).hexdigest()
    row = {
        "params_json": params_json,
        "fingerprint": fingerprint,
        "is_baseline": is_baseline,
        "created_at": datetime.now(UTC),
    }
    with unit_of_work(ctx) as conn:
        existing = conn.execute(
            select(MethodologyVersion.id).where(MethodologyVersion.fingerprint == fingerprint)
        ).scalar_one_or_none()
        if existing is not None:
            return int(existing)
        result = cast(
            CursorResult[object],
            conn.execute(table_of(MethodologyVersion).insert().values(**row)),
        )
        assert result.inserted_primary_key is not None
        return int(result.inserted_primary_key[0])


def create_calc_run(
    ctx: Context,
    *,
    run_id: str,
    methodology_version_id: int,
    as_of_date: date,
    knowledge_cutoff: datetime,
    algorithm_version: str,
    universe_filter_json: str | None = None,
    source_fingerprint: str | None = None,
    status: RunStatus | str = RunStatus.QUEUED,
) -> str:
    """Insert a ``calc_run`` row (default status ``queued``); returns ``run_id``."""
    row = {
        "run_id": run_id,
        "methodology_version_id": methodology_version_id,
        "as_of_date": as_of_date,
        "knowledge_cutoff": knowledge_cutoff,
        "universe_filter_json": universe_filter_json,
        "status": _status_value(status).value,
        "progress": 0.0,
        "algorithm_version": algorithm_version,
        "source_fingerprint": source_fingerprint,
    }
    with unit_of_work(ctx) as conn:
        conn.execute(table_of(CalcRun).insert().values(**row))
    return run_id


def _get_run(conn: Connection | Session, run_id: str) -> dict[str, Any]:
    row = conn.execute(select(table_of(CalcRun)).where(CalcRun.run_id == run_id)).mappings().first()
    if row is None:
        raise KeyError(f"calc_run {run_id!r} not found")
    return dict(row)


def update_run_status(
    ctx: Context,
    run_id: str,
    new_status: RunStatus | str,
    *,
    error: Mapping[str, Any] | str | None = None,
) -> RunStatus:
    """Transition a run's status, validating against :data:`_ALLOWED_TRANSITIONS`.

    Sets ``started_at`` on ``queued -> running`` and ``finished_at`` +
    ``progress = 1.0`` on terminal states. ``error`` is stored as JSON in
    ``error_json`` (mainly for ``failed``).
    """
    target = _status_value(new_status)
    now = datetime.now(UTC)
    with unit_of_work(ctx) as conn:
        run = _get_run(conn, run_id)
        current = _status_value(run["status"])
        if target == current:
            return current
        allowed = _ALLOWED_TRANSITIONS[current]
        if target not in allowed:
            raise RunTransitionError(
                f"illegal calc_run transition {current.value} -> {target.value}"
            )
        values: dict[str, Any] = {"status": target.value}
        if current == RunStatus.QUEUED and target == RunStatus.RUNNING:
            values["started_at"] = now
        if target in _TERMINAL:
            values["finished_at"] = now
            values["progress"] = 1.0
        if error is not None:
            values["error_json"] = (
                error if isinstance(error, str) else json.dumps(error, sort_keys=True)
            )
        conn.execute(table_of(CalcRun).update().where(CalcRun.run_id == run_id).values(**values))
        return target


def update_run_progress(ctx: Context, run_id: str, progress: float) -> None:
    """Set ``progress`` in ``[0, 1]`` (CHECK constraint enforced too)."""
    if not 0.0 <= progress <= 1.0:
        raise ValueError(f"progress must be in [0, 1], got {progress}")
    with unit_of_work(ctx) as conn:
        result = cast(
            CursorResult[object],
            conn.execute(
                table_of(CalcRun).update().where(CalcRun.run_id == run_id).values(progress=progress)
            ),
        )
        if result.rowcount == 0:
            raise KeyError(f"calc_run {run_id!r} not found")


def get_calc_run(ctx: Context, run_id: str) -> dict[str, Any] | None:
    """Fetch a run row as a dict, or ``None``."""
    stmt = select(table_of(CalcRun)).where(CalcRun.run_id == run_id)
    if isinstance(ctx, Session | Connection):
        row = ctx.execute(stmt).mappings().first()
        return dict(row) if row is not None else None
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return dict(row) if row is not None else None
    raise TypeError(f"unsupported context type: {type(ctx).__name__}")


def list_calc_runs(
    ctx: Context,
    *,
    status: RunStatus | str | None = None,
    as_of_date: date | None = None,
    sort: str = "as_of_date",
    descending: bool = True,
    limit: int | None = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List runs with optional filters; ``sort`` is whitelisted."""
    if sort not in _RUN_SORT_COLUMNS:
        raise ValueError(f"unknown sort column {sort!r}; allowed: {sorted(_RUN_SORT_COLUMNS)}")
    stmt = select(table_of(CalcRun))
    if status is not None:
        stmt = stmt.where(CalcRun.status == _status_value(status).value)
    if as_of_date is not None:
        stmt = stmt.where(CalcRun.as_of_date == as_of_date)
    col = table_of(CalcRun).c[sort]
    stmt = stmt.order_by(col.desc() if descending else col.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset:
        stmt = stmt.offset(offset)

    def _run(q: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in q.mappings()]

    if isinstance(ctx, Session | Connection):
        return _run(ctx.execute(stmt))
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            return _run(conn.execute(stmt))
    raise TypeError(f"unsupported context type: {type(ctx).__name__}")
