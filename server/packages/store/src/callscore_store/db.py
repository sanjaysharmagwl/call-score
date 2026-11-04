"""Engine factory that enables WAL journaling and foreign keys on SQLite."""

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


def create_engine_for(path: str | Path) -> Engine:
    """Return a SQLAlchemy engine for a SQLite DB at ``path`` with WAL + FK pragmas."""
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn: object, _record: object) -> None:
        cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine
