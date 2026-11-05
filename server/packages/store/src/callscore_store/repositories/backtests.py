"""Backtest persistence: definitions plus NAV/trade/stat result rows."""

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Connection, Engine, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from callscore_core.enums import RunStatus
from callscore_store.models import Backtest, BacktestNav, BacktestStat, BacktestTrade
from callscore_store.repositories.base import (
    Context,
    normalize_rows,
    table_of,
    unit_of_work,
)
from callscore_store.validation import ValidationError, validate_table

_MODELS = {
    "nav": BacktestNav,
    "trades": BacktestTrade,
    "stats": BacktestStat,
}


def insert_backtest(
    ctx: Context,
    *,
    backtest_id: str,
    config: Mapping[str, Any],
    name: str | None = None,
    status: RunStatus | str = RunStatus.QUEUED,
    calc_run_id: str | None = None,
    scenario_id: str | None = None,
) -> str:
    """Insert a ``backtest`` row; returns ``backtest_id``."""
    status_value = status.value if isinstance(status, RunStatus) else str(status)
    row = {
        "backtest_id": backtest_id,
        "name": name,
        "config_json": json.dumps(config, sort_keys=True),
        "status": status_value,
        "calc_run_id": calc_run_id,
        "scenario_id": scenario_id,
        "created_at": datetime.now(UTC),
    }
    with unit_of_work(ctx) as conn:
        conn.execute(table_of(Backtest).insert().values(**row))
    return backtest_id


def update_backtest_status(
    ctx: Context,
    backtest_id: str,
    status: RunStatus | str,
) -> None:
    """Set a backtest's status (no transition validation — backtests are jobs)."""
    status_value = status.value if isinstance(status, RunStatus) else str(status)
    now = datetime.now(UTC)
    values: dict[str, Any] = {"status": status_value}
    if status_value in {
        s.value for s in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.INTERRUPTED)
    }:
        values["finished_at"] = now
    with unit_of_work(ctx) as conn:
        result = cast(
            CursorResult[object],
            conn.execute(
                table_of(Backtest)
                .update()
                .where(Backtest.backtest_id == backtest_id)
                .values(**values)
            ),
        )
        if result.rowcount == 0:
            raise KeyError(f"backtest {backtest_id!r} not found")


def insert_backtest_results(
    ctx: Context,
    backtest_id: str,
    *,
    nav: Iterable[object] = (),
    trades: Iterable[object] = (),
    stats: Iterable[object] = (),
) -> dict[str, int]:
    """Bulk-insert backtest result rows (nav/trades/stats) in one transaction.

    Replaces any existing rows for ``backtest_id`` in each provided group.
    """
    groups = {"nav": nav, "trades": trades, "stats": stats}
    normalized: dict[Any, list[dict[str, Any]]] = {}
    for key, rows in groups.items():
        model = _MODELS[key]
        model_rows = normalize_rows(model, rows)
        if not model_rows:
            continue
        table_name = table_of(model).name
        errors, _warnings = validate_table(table_name, model_rows)
        if errors:
            raise ValidationError(table_name, errors)
        normalized[model] = model_rows

    counts: dict[str, int] = {}
    with unit_of_work(ctx) as conn:
        for model, rows in normalized.items():
            # Replace semantics per group.
            conn.execute(
                table_of(model).delete().where(table_of(model).c.backtest_id == backtest_id)
            )
            if rows:
                conn.execute(table_of(model).insert(), rows)
            counts[model.__tablename__] = len(rows)
    return counts


def get_backtest(ctx: Context, backtest_id: str) -> dict[str, Any] | None:
    """Fetch a backtest row as a dict, or ``None``."""
    stmt = select(table_of(Backtest)).where(Backtest.backtest_id == backtest_id)
    if isinstance(ctx, Session | Connection):
        row = ctx.execute(stmt).mappings().first()
        return dict(row) if row is not None else None
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return dict(row) if row is not None else None
    raise TypeError(f"unsupported context type: {type(ctx).__name__}")
