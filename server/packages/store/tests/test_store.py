from pathlib import Path

from sqlalchemy import text

from callscore_store import create_engine_for


def test_engine_sets_pragmas(tmp_path: Path) -> None:
    engine = create_engine_for(tmp_path / "test.db")
    with engine.connect() as conn:
        journal = conn.execute(text("PRAGMA journal_mode")).scalar()
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert journal == "wal"
    assert fk == 1


def test_engine_creates_db_file(tmp_path: Path) -> None:
    db = tmp_path / "created.db"
    engine = create_engine_for(db)
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
    assert db.exists()
