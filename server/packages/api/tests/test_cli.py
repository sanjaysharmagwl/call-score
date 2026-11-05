"""Tests for the ``callscore`` CLI (``callscore_api.cli.main``)."""

import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from callscore_api.cli import main
from callscore_store import create_engine_for
from callscore_store.repositories import events, market, reference
from callscore_store.schema import bootstrap

AS_OF = date(2025, 1, 17)


def _weekdays(start: date, n: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


SESSION_DAYS = _weekdays(date(2025, 1, 6), 10)


def _seed(engine) -> None:
    sessions = [
        {
            "exchange_id": "XNYS",
            "session_date": d,
            "open_ts": datetime.combine(d, datetime.min.time(), UTC).replace(hour=14, minute=30),
            "close_ts": datetime.combine(d, datetime.min.time(), UTC).replace(hour=21),
        }
        for d in SESSION_DAYS
    ]
    reference.insert_reference(
        engine,
        firms=[{"firm_id": "F1", "name": "Fund One"}],
        analysts=[{"analyst_id": "A1", "name": "Analyst One", "firm_id": "F1"}],
        exchanges=[{"exchange_id": "XNYS", "name": "NYSE"}],
        sectors=[{"sector_id": "SEC1", "name": "Sector One"}],
        securities=[{"security_id": "S1", "name": "Security One"}],
        sessions=sessions,
        employment=[
            {"analyst_id": "A1", "firm_id": "F1", "from_date": date(2020, 1, 1), "to_date": None}
        ],
        coverage=[
            {
                "analyst_id": "A1",
                "security_id": "S1",
                "from_date": date(2020, 1, 1),
                "to_date": None,
                "is_lead": True,
            }
        ],
        classifications=[
            {
                "security_id": "S1",
                "from_date": date(2020, 1, 1),
                "to_date": None,
                "exchange_id": "XNYS",
                "currency": "USD",
                "sector_id": "SEC1",
                "region": "US",
                "cap_bucket": "large",
                "listing_date": date(2010, 1, 1),
                "delisting_date": None,
            }
        ],
        rating_scales=[
            {
                "scale_id": "F1:main",
                "firm_id": "F1",
                "effective_from": date(2020, 1, 1),
                "n_tiers": 5,
            }
        ],
    )
    market.insert_market(
        engine,
        benchmark_defs=[{"benchmark_id": "SECTOR_SEC1", "kind": "sector"}],
        security_daily=[
            {
                "security_id": "S1",
                "session_date": d,
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "adj_factor": 1.0,
                "cash_dividend": None,
                "tr_index": 100.0,
                "is_halted": False,
            }
            for d in SESSION_DAYS
        ],
        benchmark_daily=[
            {
                "benchmark_id": "SECTOR_SEC1",
                "session_date": d,
                "open_level": 100.0,
                "close_level": 100.0,
            }
            for d in SESSION_DAYS
        ],
    )
    first = SESSION_DAYS[0]
    ts = datetime.combine(first, datetime.min.time(), UTC).replace(hour=12)
    events.insert_rating_events(
        engine,
        [
            {
                "event_id": "e1",
                "analyst_id": "A1",
                "firm_id": "F1",
                "security_id": "S1",
                "announced_ts": ts,
                "effective_ts": ts,
                "knowledge_ts": ts,
                "action_type": "initiation",
                "native_rating": "Buy",
                "canonical_rating": "buy",
                "prior_canonical_rating": None,
                "scale_id": "F1:main",
                "price_target": None,
                "pt_currency": None,
                "pt_horizon_months": None,
                "expiry_date": None,
                "supersedes_event_id": None,
                "is_correction": False,
            }
        ],
    )


def _make_db(tmp_path: Path, *, seed: bool = True) -> Path:
    path = tmp_path / "callscore.db"
    engine = create_engine_for(path)
    bootstrap(engine)
    if seed:
        _seed(engine)
    engine.dispose()
    return path


def test_run_and_leaderboard(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    code = main(
        [
            "run",
            "--db",
            str(db),
            "--as-of",
            AS_OF.isoformat(),
            "--methodology",
            "default",
            "--run-id",
            "cli-run-1",
        ],
        out=out,
        err=err,
    )
    assert code == 0, err.getvalue()
    assert "run_id=cli-run-1" in out.getvalue()
    assert "status=succeeded" in out.getvalue()

    out2 = io.StringIO()
    code = main(["leaderboard", "--db", str(db), "--run", "cli-run-1"], out=out2, err=io.StringIO())
    assert code == 0
    text = out2.getvalue()
    assert "run_id=cli-run-1" in text
    assert "methodology_fingerprint=" in text
    assert "A1" in text


def test_run_failure_exits_nonzero(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    # as_of before any data -> empty sessions -> engine failure.
    code = main(
        [
            "run",
            "--db",
            str(db),
            "--as-of",
            "2024-01-01",
            "--methodology",
            "default",
            "--run-id",
            "cli-run-bad",
        ],
        out=out,
        err=err,
    )
    assert code == 1
    assert "failed" in err.getvalue()


def test_run_unknown_methodology_exits_nonzero(tmp_path: Path) -> None:
    db = _make_db(tmp_path, seed=False)
    out, err = io.StringIO(), io.StringIO()
    code = main(
        [
            "run",
            "--db",
            str(db),
            "--as-of",
            AS_OF.isoformat(),
            "--methodology",
            "does-not-exist",
            "--run-id",
            "cli-run-x",
        ],
        out=out,
        err=err,
    )
    assert code == 2


def test_leaderboard_missing_run_exits_nonzero(tmp_path: Path) -> None:
    db = _make_db(tmp_path, seed=False)
    out, err = io.StringIO(), io.StringIO()
    code = main(["leaderboard", "--db", str(db), "--run", "ghost"], out=out, err=err)
    assert code == 1


def test_methodology_json_path(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    spec = tmp_path / "meth.json"
    spec.write_text(json.dumps({"decay_days": 30}))
    out, err = io.StringIO(), io.StringIO()
    code = main(
        [
            "run",
            "--db",
            str(db),
            "--as-of",
            AS_OF.isoformat(),
            "--methodology",
            str(spec),
            "--run-id",
            "cli-run-json",
        ],
        out=out,
        err=err,
    )
    assert code == 0, err.getvalue()
