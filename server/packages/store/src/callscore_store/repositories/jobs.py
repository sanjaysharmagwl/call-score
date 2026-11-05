"""Single-writer lease for concurrent ingest/calc jobs.

The ``writer_lease`` table is a single-row table (``id = 1`` CHECK). Acquire is
a single atomic ``INSERT ... ON CONFLICT DO UPDATE ... WHERE expired``
statement, so two concurrent acquires can never both succeed: SQLite
serializes the writers and the second statement re-evaluates its ``WHERE``
against the committed lease.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Engine, Table, delete, text, update

from callscore_store.models import WriterLease

_BUSY_TIMEOUT_MS = 10_000


def _now() -> datetime:
    return datetime.now(UTC)


def acquire_writer_lease(
    engine: Engine,
    holder: str,
    job_id: str,
    ttl: timedelta | float = 60.0,
) -> bool:
    """Try to become the single writer; returns ``True`` on success.

    ``ttl`` is a :class:`~datetime.timedelta` or seconds. The lease is held
    until ``expires_at``; an expired lease can be taken over by anyone.
    """
    ttl_delta = ttl if isinstance(ttl, timedelta) else timedelta(seconds=float(ttl))
    now = _now()
    expires = now + ttl_delta
    with engine.connect() as conn, conn.begin():
        conn.exec_driver_sql(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        result = conn.execute(
            text(
                "INSERT INTO writer_lease (id, holder, job_id, acquired_at,"
                " heartbeat_at, expires_at)"
                " VALUES (1, :holder, :job_id, :now, :now, :expires)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   holder = :holder, job_id = :job_id, acquired_at = :now,"
                "   heartbeat_at = :now, expires_at = :expires"
                " WHERE writer_lease.expires_at <= :now"
            ),
            {
                "holder": holder,
                "job_id": job_id,
                "now": now,
                "expires": expires,
            },
        )
        return result.rowcount == 1


def release_writer_lease(engine: Engine, holder: str) -> bool:
    """Release the lease iff ``holder`` owns it; returns whether a row was removed."""
    lease = cast(Table, WriterLease.__table__)
    with engine.connect() as conn, conn.begin():
        conn.exec_driver_sql(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        result = conn.execute(delete(lease).where(lease.c.id == 1, lease.c.holder == holder))
        return result.rowcount == 1


def heartbeat_writer_lease(
    engine: Engine,
    holder: str,
    ttl: timedelta | float = 60.0,
) -> bool:
    """Extend the lease iff ``holder`` owns it; returns whether it was renewed."""
    ttl_delta = ttl if isinstance(ttl, timedelta) else timedelta(seconds=float(ttl))
    now = _now()
    expires = now + ttl_delta
    lease = cast(Table, WriterLease.__table__)
    with engine.connect() as conn, conn.begin():
        conn.exec_driver_sql(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        result = conn.execute(
            update(lease)
            .where(lease.c.id == 1, lease.c.holder == holder)
            .values(heartbeat_at=now, expires_at=expires)
        )
        return result.rowcount == 1


def get_writer_lease(engine: Engine) -> dict[str, Any] | None:
    """Return the current lease row as a dict, or ``None``."""
    with engine.connect() as conn:
        conn.exec_driver_sql(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        row = (
            conn.execute(
                text(
                    "SELECT id, holder, job_id, acquired_at, heartbeat_at, expires_at"
                    " FROM writer_lease WHERE id = 1"
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None
