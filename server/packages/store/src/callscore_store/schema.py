"""Schema bootstrap and versioning for the callscore SQLite store.

Versioning model
----------------

A single-row ``schema_version`` table records the schema version. Versions are
non-negative integers; ``SCHEMA_VERSION`` is the newest version this code
understands.

* ``bootstrap(engine)`` on an empty database creates the version-1 baseline
  (``schema_version`` plus the ``firm`` and ``analyst`` tables in their v1
  shape), then applies every registered upgrade in order — the same path a v1
  database takes. It is idempotent: bootstrapping a current database is a
  no-op returning ``SCHEMA_VERSION``.
* Upgrades are declared in ``_UPGRADES``: key ``v`` maps to a callable that
  migrates a version-``v`` database to version ``v + 1``. Upgrades must be
  additive — they never drop tables, columns or data.
* A database stamped with a version newer than ``SCHEMA_VERSION`` is refused
  with :class:`SchemaTooNewError`.
* A database that has user tables but no ``schema_version`` row is unmanaged;
  bootstrap refuses to touch it rather than guessing.
"""

from collections.abc import Callable

from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from callscore_store.models import Base

SCHEMA_VERSION = 2


class SchemaError(RuntimeError):
    """Base class for schema bootstrap/versioning failures."""


class SchemaTooNewError(SchemaError):
    """The database was created by a newer version of the code."""


class UnmanagedSchemaError(SchemaError):
    """The database has tables but no ``schema_version`` bookkeeping."""


_UPGRADES: dict[int, Callable[[Connection], None]] = {}


def _table_names(conn: Connection) -> set[str]:
    rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).scalars()
    return set(rows)


def _read_version(conn: Connection) -> int | None:
    """Return the recorded schema version, or ``None`` if bookkeeping is absent."""
    if "schema_version" not in _table_names(conn):
        return None
    row = conn.execute(text("SELECT version FROM schema_version WHERE id = 1")).scalar()
    if row is None:
        return None
    return int(row)


def current_version(engine: Engine) -> int:
    """Return the schema version of the database, or 0 for a completely empty one.

    Raises :class:`UnmanagedSchemaError` if tables exist without version
    bookkeeping, and :class:`SchemaTooNewError` is *not* raised here — callers
    that need to act on the version should use :func:`bootstrap`.
    """
    with engine.connect() as conn:
        tables = _table_names(conn)
        version = _read_version(conn)
    if version is not None:
        return version
    if tables - {"schema_version"}:
        raise UnmanagedSchemaError(
            "database contains tables but no schema_version row; refusing to guess its provenance"
        )
    return 0


def _create_v1_baseline(conn: Connection) -> None:
    """Create the version-1 schema: bookkeeping plus minimal firm/analyst tables."""
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  id INTEGER PRIMARY KEY CHECK (id = 1),"
            "  version INTEGER NOT NULL CHECK (version >= 1)"
            ")"
        )
    )
    conn.execute(text("INSERT INTO schema_version (id, version) VALUES (1, 1)"))
    conn.execute(text("CREATE TABLE firm (firm_id TEXT PRIMARY KEY, name TEXT NOT NULL)"))
    conn.execute(text("CREATE TABLE analyst (analyst_id TEXT PRIMARY KEY, name TEXT NOT NULL)"))


def _upgrade_1_to_2(conn: Connection) -> None:
    """v1 -> v2: grow firm/analyst columns, then create the full current schema.

    ``Base.metadata.create_all(checkfirst=True)`` creates every table and index
    the v1 baseline lacks; the two ``ALTER TABLE`` statements evolve the v1
    ``firm``/``analyst`` tables into their current shape (``firm.country``,
    ``analyst.firm_id``).
    """
    conn.execute(text("ALTER TABLE firm ADD COLUMN country TEXT"))
    conn.execute(text("ALTER TABLE analyst ADD COLUMN firm_id TEXT REFERENCES firm(firm_id)"))
    Base.metadata.create_all(conn, checkfirst=True)


_UPGRADES[1] = _upgrade_1_to_2


def bootstrap(engine: Engine) -> int:
    """Create or upgrade the database schema; returns the resulting version.

    Idempotent: a database already at :data:`SCHEMA_VERSION` is left untouched.
    """
    try:
        with engine.begin() as conn:
            tables = _table_names(conn)
            version = _read_version(conn)
            if version is None:
                if tables - {"schema_version"}:
                    raise UnmanagedSchemaError(
                        "database contains tables but no schema_version row; "
                        "refusing to guess its provenance"
                    )
                _create_v1_baseline(conn)
                version = 1
            if version > SCHEMA_VERSION:
                raise SchemaTooNewError(
                    f"database schema version {version} is newer than this code "
                    f"supports (max {SCHEMA_VERSION}); upgrade the application"
                )
            while version < SCHEMA_VERSION:
                upgrade = _UPGRADES.get(version)
                if upgrade is None:
                    raise SchemaError(
                        f"no upgrade registered from schema version {version} to {version + 1}"
                    )
                upgrade(conn)
                version += 1
            conn.execute(
                text("UPDATE schema_version SET version = :v WHERE id = 1"),
                {"v": version},
            )
            return version
    except SchemaError:
        raise
    except SQLAlchemyError as exc:
        raise SchemaError(f"schema bootstrap failed: {exc}") from exc


def table_names(engine: Engine) -> set[str]:
    """Return all table names in the database (convenience for tests/tools)."""
    insp = inspect(engine)
    return set(insp.get_table_names())
