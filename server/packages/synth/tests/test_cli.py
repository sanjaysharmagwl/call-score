"""End-to-end CLI tests: generation, persistence, DB protection."""

import sqlite3
from pathlib import Path

import pytest

from callscore_synth.cli import main


def _counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(db)
    try:
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("rating_event", "security_daily", "benchmark_daily", "analyst")
            if t in tables
        }
    finally:
        conn.close()


def test_cli_end_to_end_populates_db(tmp_path: Path, capsys) -> None:
    out = tmp_path / "callscore.db"
    assert main(["--scale", "small", "--seed", "7", "--out", str(out)]) == 0
    out_txt = capsys.readouterr().out
    assert "seed=7" in out_txt
    assert "checksum:" in out_txt
    counts = _counts(out)
    assert counts["rating_event"] > 0
    assert counts["security_daily"] > 0
    assert counts["benchmark_daily"] > 0
    assert counts["analyst"] > 0
    # Generation manifest row was written.
    conn = sqlite3.connect(out)
    try:
        n = conn.execute("SELECT COUNT(*) FROM generation_manifest").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_cli_dry_run_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "dry.db"
    assert main(["--scale", "small", "--seed", "3", "--out", str(out), "--dry-run"]) == 0
    assert not out.exists()


def test_cli_refuses_existing_db(tmp_path: Path) -> None:
    out = tmp_path / "exists.db"
    assert main(["--scale", "small", "--seed", "5", "--out", str(out)]) == 0
    # Second run without --force refuses.
    assert main(["--scale", "small", "--seed", "5", "--out", str(out)]) == 2
    # --force overwrites.
    assert main(["--scale", "small", "--seed", "5", "--out", str(out), "--force"]) == 0


def test_cli_seed_override_changes_output(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    assert main(["--scale", "small", "--seed", "1", "--out", str(a)]) == 0
    assert main(["--scale", "small", "--seed", "2", "--out", str(b)]) == 0
    ca, cb = _counts(a), _counts(b)
    assert ca["rating_event"] != cb["rating_event"] or ca != cb


def test_cli_rejects_bad_scale(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--scale", "huge", "--out", str(tmp_path / "x.db")])
