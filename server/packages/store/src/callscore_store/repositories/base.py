"""Shared helpers for the callscore store repositories.

All public repository functions accept any of:

* ``Engine``      — a fresh connection and transaction per call;
* ``Session``     — joins the session's transaction (begun if needed);
* ``Connection``  — executes inside the caller's transaction.

Bulk inserts always run validate-then-insert inside ONE transaction: any
failure (validation or database) rolls the whole batch back.
"""

import csv
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Connection, Engine, Select, Table, and_, select
from sqlalchemy.orm import Session

from callscore_store.models import Base

Context = Engine | Session | Connection

_TABLES: dict[str, Table] = Base.metadata.tables


def table_of(model: Any) -> Table:
    """Return the ``Table`` for a mapped class (``__table__`` is untyped)."""
    return cast(Table, model.__table__)


def _coerce_mapping(row: object, columns: Sequence[str]) -> dict[str, Any]:
    """Turn a mapping, ORM object or attribute-bag into a plain column dict."""
    if isinstance(row, Mapping):
        return {col: row.get(col) for col in columns}
    out: dict[str, Any] = {}
    for col in columns:
        if hasattr(row, col):
            out[col] = getattr(row, col)
        else:
            out[col] = None
    return out


def normalize_rows(model: Any, rows: Iterable[object]) -> list[dict[str, Any]]:
    """Normalize an iterable of mappings/objects/Polars-row dicts to column dicts.

    ``model`` is a SQLAlchemy mapped class; ``rows`` may be a single mapping or
    an iterable of mappings, dataclasses, ORM instances or Polars
    ``iter_rows(named=True)`` dicts. Columns absent from a row become ``None``;
    callers fill defaults before insert.
    """
    if isinstance(rows, Mapping):
        items: Iterable[object] = (rows,)
    else:
        items = rows
    table = table_of(model)
    columns = [c.name for c in table.columns]
    return [_coerce_mapping(row, columns) for row in items]


def required_columns(model: Any) -> tuple[str, ...]:
    """Columns that must be non-NULL and have no Python/server default."""
    table = table_of(model)
    out: list[str] = []
    for col in table.columns:
        if col.primary_key or col.nullable is False:
            has_default = col.default is not None or col.server_default is not None
            if not has_default:
                out.append(col.name)
    return tuple(out)


@contextmanager
def unit_of_work(ctx: Context) -> Iterator[Connection | Session]:
    """Yield a connection/session inside exactly one transaction."""
    if isinstance(ctx, Session):
        if ctx.in_transaction():
            yield ctx
        else:
            with ctx.begin():
                yield ctx
    elif isinstance(ctx, Connection):
        yield ctx
    elif isinstance(ctx, Engine):
        with ctx.begin() as conn:
            yield conn
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")


def _check_required(
    model: Any, rows: Sequence[dict[str, Any]], *, skip: frozenset[str] = frozenset()
) -> None:
    """Raise ``ValueError`` listing rows missing required (non-defaulted) columns."""
    missing: list[str] = []
    for name in required_columns(model):
        if name in skip:
            continue
        for idx, row in enumerate(rows):
            if row.get(name) is None:
                missing.append(f"row {idx}: {name} is required")
    if missing:
        raise ValueError(
            f"{table_of(model).name}: missing required columns: " + "; ".join(missing[:20])
        )


def bulk_insert(
    ctx: Context,
    model: Any,
    rows: Sequence[dict[str, Any]],
    *,
    skip_required: frozenset[str] = frozenset(),
) -> int:
    """INSERT many rows in one executemany; returns the row count.

    Assumes the caller already ran validation inside the same transaction.
    """
    if not rows:
        return 0
    _check_required(model, rows, skip=skip_required)
    table = table_of(model)
    conn: Connection | Session
    with unit_of_work(ctx) as conn:
        conn.execute(table.insert(), rows)
    return len(rows)


def table_by_name(name: str) -> Table:
    """Look up a mapped table by ``__tablename__``; raises ``ValueError``."""
    for table in _TABLES.values():
        if table.name == name:
            return table
    raise ValueError(f"unknown table {name!r}")


def iter_rows(
    ctx: Context,
    table_name: str,
    *,
    filters: Mapping[str, object] | None = None,
    sort: str | None = None,
    descending: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> Iterator[dict[str, Any]]:
    """Parameterized, injection-safe row iterator.

    ``table_name``, ``filters`` keys and ``sort`` are whitelisted against the
    mapped schema: anything that is not a real table/column raises
    ``ValueError`` *before* any SQL is built, so hostile strings are treated as
    data, never as SQL. ``filters`` values are always bound parameters.
    Rows are yielded as plain dicts ordered by ``sort`` (default: PK order is
    not guaranteed — pass ``sort`` for deterministic pagination).
    """
    table = table_by_name(table_name)
    conditions = []
    for key, value in (filters or {}).items():
        col = table.c.get(key)
        if col is None:
            raise ValueError(f"{table_name}: unknown filter column {key!r}")
        conditions.append(col == value)
    stmt: Select[Any] = select(table)
    if conditions:
        stmt = stmt.where(and_(*conditions))
    if sort is not None:
        col = table.c.get(sort)
        if col is None:
            raise ValueError(f"{table_name}: unknown sort column {sort!r}")
        stmt = stmt.order_by(col.desc() if descending else col.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset:
        stmt = stmt.offset(offset)

    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            for row in conn.execute(stmt):
                yield dict(row._mapping)
    elif isinstance(ctx, Session | Connection):
        for row in ctx.execute(stmt):
            yield dict(row._mapping)
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")


def write_csv(
    ctx: Context,
    table_name: str,
    path: str | Path,
    *,
    filters: Mapping[str, object] | None = None,
    sort: str | None = None,
    descending: bool = False,
    limit: int | None = None,
    offset: int = 0,
    fieldnames: Sequence[str] | None = None,
) -> int:
    """Export ``iter_rows`` results to a CSV file; returns rows written.

    ``fieldnames`` defaults to the table's column order.
    """
    table = table_by_name(table_name)
    names = list(fieldnames) if fieldnames is not None else [c.name for c in table.columns]
    count = 0
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        for row in iter_rows(
            ctx,
            table_name,
            filters=filters,
            sort=sort,
            descending=descending,
            limit=limit,
            offset=offset,
        ):
            writer.writerow({k: _csv_value(v) for k, v in row.items()})
            count += 1
    return count


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    return value
