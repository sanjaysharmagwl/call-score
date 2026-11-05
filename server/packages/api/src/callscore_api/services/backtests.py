"""Backtest submission, execution and result queries.

Lifecycle mirrors ``services.jobs`` but for the ``backtest`` table (the
``JobCoordinator`` only drains ``calc_run`` rows — contract gap; a dedicated
``BacktestRunner`` below reuses the same ``writer_lease`` machinery)::

    submit_backtest()   -> validate BacktestSpec, insert ``queued`` row
    execute_backtest()  -> queued -> running -> succeeded|failed, atomic publish
    BacktestRunner      -> background thread claiming queued backtests under
                           the single-writer lease (same pattern as
                           JobCoordinator)

PIT rule: the request ``spec`` dict is passed to ``BacktestSpec.model_validate``
faithfully (request-level ``start_date``/``end_date``/``benchmark_id`` only
override the spec's own fields before validation). Dynamic ``top_n`` /
``top_quartile`` selection therefore keeps its ``score_lookback_sessions``
trailing-window semantics — the API never precomputes ranks.

Storage notes (contract gaps, documented):

* ``backtest`` has no ``error_json``/``started_at`` writer in the repo — the
  service writes ``started_at``/``finished_at`` and merges an ``error`` object
  into ``config_json`` on failure.
* ``monthly_returns`` and ``attribution`` have no dedicated tables; they are
  stored inside ``config_json`` under the reserved keys ``monthly_returns`` /
  ``attribution`` (the flat config is otherwise exactly the validated
  ``BacktestSpec`` dump, so the keys cannot collide with spec fields).
* ``backtest_stat`` stores the scalar stats (one row per ``BacktestStats``
  field plus ``n_sessions``).
"""

import hashlib
import json
import os
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.orm import Session

from callscore_api.services.benchmarks import load_liquidity_daily
from callscore_core.enums import RunStatus
from callscore_core.methodology import canonical_dumps
from callscore_engine.backtest import BacktestSpec, run_backtest
from callscore_store import create_engine_for
from callscore_store.models import (
    Backtest,
    BacktestNav,
    BacktestStat,
    BacktestTrade,
    TradingSession,
)
from callscore_store.repositories import backtests as repo_backtests
from callscore_store.repositories import jobs as repo_jobs
from callscore_store.repositories.base import (
    Context,
    table_of,
    unit_of_work,
)
from callscore_store.snapshots import load_input_snapshot

# Reserved keys merged into ``backtest.config_json`` at publish/failure time.
_CONFIG_ARTIFACT_KEYS = ("monthly_returns", "attribution", "error")

NAV_DOWNSAMPLE_TARGET = 2000
"""NAV series longer than this are stride-downsampled (chart bound)."""

EXPORT_ROW_CAP = 50_000
"""Hard cap for CSV exports; excess rows are truncated with a note."""

_NAV_INTERVALS = ("1d", "1w", "1M")

_STAT_FIELDS = (
    "annualized_return",
    "annualized_volatility",
    "sharpe_ratio",
    "information_ratio",
    "max_drawdown",
    "turnover",
    "hit_rate",
)


@dataclass(frozen=True)
class SubmitBacktestResult:
    """Outcome of :func:`submit_backtest`."""

    backtest_id: str
    status: str
    created: bool


# ---------------------------------------------------------------------------
# Spec handling
# ---------------------------------------------------------------------------


def _spec_mapping(spec: BacktestSpec | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(spec, BacktestSpec):
        return spec.model_dump(mode="json")
    if not isinstance(spec, Mapping):
        raise TypeError(f"spec must be a mapping or BacktestSpec, got {type(spec).__name__}")
    return dict(spec)


def _coerce_spec(
    spec: BacktestSpec | Mapping[str, Any],
    *,
    start_date: date | None,
    end_date: date | None,
    benchmark_id: str | None,
) -> BacktestSpec:
    """Apply request-level overrides, then validate (422 on any violation)."""
    raw = _spec_mapping(spec)
    if start_date is not None:
        raw["start_date"] = start_date.isoformat()
    if end_date is not None:
        raw["end_date"] = end_date.isoformat()
    if benchmark_id is not None:
        raw["benchmark_id"] = benchmark_id
    # Strip artifact keys a previous execution may have merged into config_json.
    for key in _CONFIG_ARTIFACT_KEYS:
        raw.pop(key, None)
    return BacktestSpec.model_validate(raw)


def _default_backtest_id(spec: BacktestSpec) -> str:
    digest = hashlib.sha256(canonical_dumps(spec.model_dump(mode="json")).encode()).hexdigest()
    return f"bt-{digest[:48]}"


def _json_object(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


def submit_backtest(
    ctx: Context,
    *,
    spec: BacktestSpec | Mapping[str, Any],
    name: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    benchmark_id: str | None = None,
    backtest_id: str | None = None,
) -> SubmitBacktestResult:
    """Validate the spec and insert a ``queued`` ``backtest`` row.

    Idempotent on ``backtest_id`` (deterministic by default): re-submitting
    the same spec returns the existing row with ``created=False``.
    """
    spec_obj = _coerce_spec(
        spec, start_date=start_date, end_date=end_date, benchmark_id=benchmark_id
    )
    config = spec_obj.model_dump(mode="json")
    rid = backtest_id or _default_backtest_id(spec_obj)

    existing = repo_backtests.get_backtest(ctx, rid)
    if existing is not None:
        return SubmitBacktestResult(backtest_id=rid, status=str(existing["status"]), created=False)
    repo_backtests.insert_backtest(
        ctx,
        backtest_id=rid,
        config=config,
        name=name,
        status=RunStatus.QUEUED,
    )
    return SubmitBacktestResult(backtest_id=rid, status=RunStatus.QUEUED.value, created=True)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _max_session_date(ctx: Context) -> date | None:
    stmt = select(func.max(TradingSession.session_date))
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            value = conn.execute(stmt).scalar_one_or_none()
    elif isinstance(ctx, Session | Connection):
        value = ctx.execute(stmt).scalar_one_or_none()
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")
    return value if isinstance(value, date) else None


def _knowledge_cutoff(as_of: date) -> datetime:
    return datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=UTC)


def _as_of_for_spec(ctx: Context, spec: BacktestSpec) -> date:
    """Snapshot bound: ``spec.end_date`` else the latest trading session."""
    as_of = spec.end_date or _max_session_date(ctx)
    if as_of is None:
        raise ValueError("cannot run backtest: no trading sessions in store")
    return as_of


def _transition(
    conn: Any,
    backtest_id: str,
    *,
    status: RunStatus,
    config_patch: Mapping[str, Any] | None = None,
    clear_error: bool = False,
) -> None:
    """Update status/timestamps and optionally merge keys into ``config_json``."""
    tbl = table_of(Backtest)
    row = conn.execute(
        select(tbl.c.config_json).where(tbl.c.backtest_id == backtest_id)
    ).scalar_one_or_none()
    config = _json_object(row)
    if config_patch:
        config.update(config_patch)
    if clear_error:
        config.pop("error", None)
    values: dict[str, Any] = {
        "status": status.value,
        "config_json": json.dumps(config, sort_keys=True),
    }
    now = datetime.now(UTC)
    if status == RunStatus.RUNNING:
        values["started_at"] = now
    if status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.INTERRUPTED):
        values["finished_at"] = now
    conn.execute(tbl.update().where(tbl.c.backtest_id == backtest_id).values(**values))


def _mark_failed(ctx: Context, backtest_id: str, exc: BaseException) -> None:
    """Best-effort failure recording; never masks the original error."""
    with suppress(Exception), unit_of_work(ctx) as conn:
        _transition(
            conn,
            backtest_id,
            status=RunStatus.FAILED,
            config_patch={"error": {"type": type(exc).__name__, "message": str(exc)}},
        )


def _result_rows(result: Any, backtest_id: str) -> dict[str, list[dict[str, Any]]]:
    """Map ``BacktestResult`` frames onto the store's result-table row shapes."""
    nav = [{"backtest_id": backtest_id, **r} for r in result.nav_daily.to_dicts()]
    trades = [{"backtest_id": backtest_id, **r} for r in result.trades.to_dicts()]
    stats = [
        {
            "backtest_id": backtest_id,
            "stat_code": code,
            "value": float(getattr(result.stats, code)),
            "std_err": None,
            "ci_low": None,
            "ci_high": None,
            "detail_json": None,
        }
        for code in _STAT_FIELDS
        if getattr(result.stats, code) is not None
    ]
    stats.append(
        {
            "backtest_id": backtest_id,
            "stat_code": "n_sessions",
            "value": float(result.stats.n_sessions),
            "std_err": None,
            "ci_low": None,
            "ci_high": None,
            "detail_json": None,
        }
    )
    return {"nav": nav, "trades": trades, "stats": stats}


def execute_backtest(ctx: Context, backtest_id: str) -> dict[str, int]:
    """Run one ``queued`` backtest end-to-end; returns persisted row counts.

    Lifecycle ``queued -> running -> succeeded|failed``. Results
    (``backtest_nav``/``backtest_trade``/``backtest_stat`` plus the
    ``monthly_returns``/``attribution`` artifacts in ``config_json``) are
    published atomically; on any failure the row is marked ``failed`` with an
    ``error`` object and the exception propagates — no partial publication.
    """
    row = repo_backtests.get_backtest(ctx, backtest_id)
    if row is None:
        raise KeyError(f"backtest {backtest_id!r} not found")
    status = str(row["status"])
    if status != RunStatus.QUEUED.value:
        raise ValueError(
            f"cannot execute backtest {backtest_id!r}: status {status!r} is not 'queued'"
        )
    spec = _coerce_spec(
        _json_object(row["config_json"]),
        start_date=None,
        end_date=None,
        benchmark_id=None,
    )

    with unit_of_work(ctx) as conn:
        _transition(conn, backtest_id, status=RunStatus.RUNNING, clear_error=True)

    try:
        as_of = _as_of_for_spec(ctx, spec)
        snapshot = load_input_snapshot(ctx, as_of, _knowledge_cutoff(as_of))
        frames: dict[str, pl.DataFrame] = dict(snapshot.frames)
        frames["liquidity_daily"] = load_liquidity_daily(ctx, as_of)
        result = run_backtest(spec, frames)

        groups = _result_rows(result, backtest_id)
        config_patch = {
            "monthly_returns": result.monthly_returns.to_dicts(),
            "attribution": result.attribution.to_dicts(),
        }
        with unit_of_work(ctx) as conn:
            counts = repo_backtests.insert_backtest_results(
                conn,
                backtest_id,
                nav=groups["nav"],
                trades=groups["trades"],
                stats=groups["stats"],
            )
            _transition(
                conn,
                backtest_id,
                status=RunStatus.SUCCEEDED,
                config_patch=config_patch,
                clear_error=True,
            )
    except Exception as exc:
        _mark_failed(ctx, backtest_id, exc)
        raise
    return counts


def recover_interrupted_backtests(ctx: Context) -> int:
    """Mark ``running`` backtests ``interrupted`` (orphaned by a dead worker).

    Call once at runner startup, BEFORE the worker thread claims jobs.
    Returns the number of rows transitioned.
    """
    stmt = select(Backtest.backtest_id).where(Backtest.status == RunStatus.RUNNING.value)
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            ids = [str(r) for r in conn.execute(stmt).scalars()]
    elif isinstance(ctx, Session | Connection):
        ids = [str(r) for r in ctx.execute(stmt).scalars()]
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")
    count = 0
    for backtest_id in ids:
        with suppress(Exception), unit_of_work(ctx) as conn:
            _transition(conn, backtest_id, status=RunStatus.INTERRUPTED)
            count += 1
    return count


# ---------------------------------------------------------------------------
# Read-side queries
# ---------------------------------------------------------------------------


def list_backtests(
    ctx: Context,
    *,
    status: RunStatus | str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """List backtests newest ``created_at`` first."""
    tbl = table_of(Backtest)
    stmt = select(tbl).order_by(tbl.c.created_at.desc(), tbl.c.backtest_id.asc())
    if status is not None:
        stmt = stmt.where(tbl.c.status == str(status))
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            rows = [dict(r) for r in conn.execute(stmt).mappings()]
    elif isinstance(ctx, Session | Connection):
        rows = [dict(r) for r in conn.execute(stmt).mappings()]
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")
    count_stmt = select(func.count()).select_from(tbl)
    if status is not None:
        count_stmt = count_stmt.where(tbl.c.status == str(status))
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            total = int(conn.execute(count_stmt).scalar_one())
    elif isinstance(ctx, Session | Connection):
        total = int(ctx.execute(count_stmt).scalar_one())
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")
    return rows[offset : offset + limit], total


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    config = _json_object(row.get("config_json"))
    error = config.get("error")
    return {
        "backtest_id": str(row["backtest_id"]),
        "name": row.get("name"),
        "status": str(row["status"]),
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "benchmark_id": config.get("benchmark_id"),
        "error": error if isinstance(error, dict) else None,
    }


def get_backtest_detail(ctx: Context, backtest_id: str) -> dict[str, Any] | None:
    """Backtest row + parsed spec/artifacts; ``None`` when unknown."""
    row = repo_backtests.get_backtest(ctx, backtest_id)
    if row is None:
        return None
    config = _json_object(row.get("config_json"))
    spec = {k: v for k, v in config.items() if k not in _CONFIG_ARTIFACT_KEYS}
    return _summary(row) | {
        "spec": spec,
        "monthly_returns": config.get("monthly_returns")
        if isinstance(config.get("monthly_returns"), list)
        else None,
        "attribution": config.get("attribution")
        if isinstance(config.get("attribution"), list)
        else None,
    }


def _require_backtest(ctx: Context, backtest_id: str) -> dict[str, Any]:
    detail = get_backtest_detail(ctx, backtest_id)
    if detail is None:
        raise KeyError(f"backtest {backtest_id!r} not found")
    return detail


# -- NAV ---------------------------------------------------------------------


def _resample_nav(rows: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    """Resample a level series to ``1w`` (ISO week) / ``1M`` (month) — last row."""
    if interval == "1d" or len(rows) <= 1:
        return rows

    def _key(d: date) -> tuple[int, int]:
        if interval == "1w":
            iso = d.isocalendar()
            return (iso[0], iso[1])
        return (d.year, d.month)

    last_by_key: dict[tuple[int, int], dict[str, Any]] = {}
    order: list[tuple[int, int]] = []
    for row in rows:
        key = _key(row["session_date"])
        if key not in last_by_key:
            order.append(key)
        last_by_key[key] = row
    return [last_by_key[k] for k in order]


def _stride_downsample(rows: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    """Evenly spaced subset keeping first/last; deterministic."""
    n = len(rows)
    if n <= target or target < 2:
        return rows
    indices = sorted({round(i * (n - 1) / (target - 1)) for i in range(target)})
    return [rows[i] for i in indices]


def nav_series(
    ctx: Context,
    backtest_id: str,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    interval: str = "1d",
    limit: int = 500,
    offset: int = 0,
) -> dict[str, Any]:
    """Daily NAV rows, optionally resampled/downsampled like ``/prices``."""
    _require_backtest(ctx, backtest_id)
    if interval not in _NAV_INTERVALS:
        raise ValueError(f"unknown interval {interval!r}; allowed: {list(_NAV_INTERVALS)}")
    tbl = table_of(BacktestNav)
    conds = [tbl.c.backtest_id == backtest_id]
    if from_date is not None:
        conds.append(tbl.c.session_date >= from_date)
    if to_date is not None:
        conds.append(tbl.c.session_date <= to_date)
    stmt = select(tbl).where(*conds).order_by(tbl.c.session_date.asc())
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            rows = [dict(r) for r in conn.execute(stmt).mappings()]
    elif isinstance(ctx, Session | Connection):
        rows = [dict(r) for r in conn.execute(stmt).mappings()]
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")
    source_rows = len(rows)
    series = _resample_nav(rows, interval)
    downsampled = False
    if len(series) > NAV_DOWNSAMPLE_TARGET:
        series = _stride_downsample(series, NAV_DOWNSAMPLE_TARGET)
        downsampled = True
    total = len(series)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    return {
        "rows": series[offset : offset + limit],
        "total": total,
        "source_rows": source_rows,
        "downsampled": downsampled,
        "interval": interval,
    }


# -- stats / trades / artifacts ------------------------------------------------


def backtest_stats(ctx: Context, backtest_id: str) -> list[dict[str, Any]]:
    """``backtest_stat`` rows for one backtest (empty when not yet run)."""
    _require_backtest(ctx, backtest_id)
    tbl = table_of(BacktestStat)
    stmt = select(tbl).where(tbl.c.backtest_id == backtest_id).order_by(tbl.c.stat_code.asc())
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]
    if isinstance(ctx, Session | Connection):
        return [dict(r) for r in ctx.execute(stmt).mappings()]
    raise TypeError(f"unsupported context type: {type(ctx).__name__}")


def backtest_trades(
    ctx: Context,
    backtest_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Trade rows with pagination; ``max_rows`` hard-caps the fetch (exports).

    Returns ``{"rows", "total", "truncated"}`` — ``truncated`` is True when
    ``total`` exceeds the number of rows that could be returned under
    ``max_rows`` (CSV export cap).
    """
    _require_backtest(ctx, backtest_id)
    tbl = table_of(BacktestTrade)
    count_stmt = select(func.count()).select_from(tbl).where(tbl.c.backtest_id == backtest_id)
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            total = int(conn.execute(count_stmt).scalar_one())
    elif isinstance(ctx, Session | Connection):
        total = int(ctx.execute(count_stmt).scalar_one())
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")

    fetch_limit = max(1, min(int(limit), EXPORT_ROW_CAP))
    if max_rows is not None:
        fetch_limit = min(fetch_limit, max(0, int(max_rows) - offset))
    stmt = (
        select(tbl)
        .where(tbl.c.backtest_id == backtest_id)
        .order_by(tbl.c.trade_seq.asc())
        .limit(fetch_limit)
        .offset(max(0, int(offset)))
    )
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            rows = [dict(r) for r in conn.execute(stmt).mappings()]
    elif isinstance(ctx, Session | Connection):
        rows = [dict(r) for r in ctx.execute(stmt).mappings()]
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")
    truncated = offset + len(rows) < total
    return {"rows": rows, "total": total, "truncated": truncated}


def _artifacts(ctx: Context, backtest_id: str, key: str) -> list[dict[str, Any]]:
    detail = _require_backtest(ctx, backtest_id)
    value = detail.get(key)
    return [dict(r) for r in value] if isinstance(value, list) else []


def backtest_monthly(ctx: Context, backtest_id: str) -> list[dict[str, Any]]:
    """``monthly_returns`` artifact rows (stored in ``config_json``)."""
    return _artifacts(ctx, backtest_id, "monthly_returns")


def backtest_attribution(ctx: Context, backtest_id: str) -> list[dict[str, Any]]:
    """``attribution`` artifact rows (stored in ``config_json``)."""
    return _artifacts(ctx, backtest_id, "attribution")


# ---------------------------------------------------------------------------
# Background runner (JobCoordinator pattern, backtest-scoped)
# ---------------------------------------------------------------------------


class BacktestRunner:
    """Single-threaded worker draining ``queued`` backtests under the lease.

    Same process model as ``services.jobs.JobCoordinator``: CPU work runs on
    one daemon thread, the ``writer_lease`` row serializes writers across
    processes, and orphaned ``running`` rows become ``interrupted`` on
    ``start()`` via :func:`recover_interrupted_backtests`.
    """

    def __init__(
        self,
        db: str | Path | Engine,
        *,
        holder: str | None = None,
        lease_ttl: float = 300.0,
        poll_interval: float = 0.5,
        recover_on_start: bool = True,
    ) -> None:
        self._owns_engine = not isinstance(db, Engine)
        self._engine = db if isinstance(db, Engine) else create_engine_for(db)
        self._holder = holder or f"callscore-backtests-{os.getpid()}"
        self._lease_ttl = float(lease_ttl)
        self._poll_interval = float(poll_interval)
        self._recover_on_start = recover_on_start
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self.last_error: str | None = None

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _next_queued_backtest_id(self) -> str | None:
        stmt = (
            select(Backtest.backtest_id)
            .where(Backtest.status == RunStatus.QUEUED.value)
            .order_by(Backtest.created_at.asc(), Backtest.backtest_id.asc())
            .limit(1)
        )
        with self._engine.connect() as conn:
            value = conn.execute(stmt).scalar_one_or_none()
        return str(value) if value is not None else None

    def run_once(self) -> str | None:
        """Claim and execute at most one queued backtest; ``None`` if none/busy.

        Synchronous — for tests and embedded workers. Raises if the backtest
        itself failed (the row is already ``failed`` with an ``error``).
        """
        backtest_id = self._next_queued_backtest_id()
        if backtest_id is None:
            return None
        if not repo_jobs.acquire_writer_lease(
            self._engine, self._holder, backtest_id, ttl=self._lease_ttl
        ):
            return None
        try:
            execute_backtest(self._engine, backtest_id)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            repo_jobs.release_writer_lease(self._engine, self._holder)
        return backtest_id

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self.run_once()
            except Exception as exc:  # failure already recorded on the row
                self.last_error = f"{type(exc).__name__}: {exc}"
                ran = None
            if ran is None:
                self._stop.wait(self._poll_interval)

    def start(self) -> None:
        with self._state_lock:
            if self.is_running:
                return
            if self._recover_on_start:
                recover_interrupted_backtests(self._engine)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="callscore-backtest-worker", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def close(self) -> None:
        self.stop()
        if self._owns_engine:
            self._engine.dispose()

    def __enter__(self) -> "BacktestRunner":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "EXPORT_ROW_CAP",
    "NAV_DOWNSAMPLE_TARGET",
    "BacktestRunner",
    "SubmitBacktestResult",
    "backtest_attribution",
    "backtest_monthly",
    "backtest_stats",
    "backtest_trades",
    "execute_backtest",
    "get_backtest_detail",
    "list_backtests",
    "nav_series",
    "recover_interrupted_backtests",
    "submit_backtest",
]
