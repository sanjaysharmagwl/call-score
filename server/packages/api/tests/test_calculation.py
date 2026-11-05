"""Tests for ``callscore_api.services.calculation`` on a tmp SQLite DB.

The DB is seeded by hand with the minimum reference/market/event rows needed
for one investable interval (one analyst, one security, a few sessions).
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from callscore_api.services import calculation
from callscore_core.methodology import Methodology
from callscore_store import create_engine_for
from callscore_store.models import CalcRun, MethodologyVersion
from callscore_store.repositories import events, market, reference, runs
from callscore_store.schema import bootstrap

AS_OF = date(2025, 1, 17)  # Friday; 10 weekdays seeded from 2025-01-06
CUTOFF = calculation.default_knowledge_cutoff(AS_OF)


def _weekdays(start: date, n: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


SESSION_DAYS = _weekdays(date(2025, 1, 6), 10)


def _seed(engine: Engine) -> None:
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
            {
                "analyst_id": "A1",
                "firm_id": "F1",
                "from_date": date(2020, 1, 1),
                "to_date": None,
            }
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
    events.insert_rating_events(
        engine,
        [
            {
                "event_id": "e1",
                "analyst_id": "A1",
                "firm_id": "F1",
                "security_id": "S1",
                "announced_ts": datetime.combine(first, datetime.min.time(), UTC).replace(hour=12),
                "effective_ts": datetime.combine(first, datetime.min.time(), UTC).replace(hour=12),
                "knowledge_ts": datetime.combine(first, datetime.min.time(), UTC).replace(hour=12),
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


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "callscore.db"
    engine = create_engine_for(path)
    bootstrap(engine)
    _seed(engine)
    engine.dispose()
    return path


def _counts(engine: Engine, run_id: str) -> dict[str, int]:
    run = runs.get_calc_run(engine, run_id)
    assert run is not None
    row_counts = json.loads(run["row_counts_json"] or "{}")
    return row_counts["rows"]


def test_execute_run_publishes_all_stage_tables(db_path: Path) -> None:
    engine = create_engine_for(db_path)
    methodology = Methodology()
    calculation.prepare_run(
        engine,
        methodology,
        run_id="run-1",
        as_of_date=AS_OF,
        knowledge_cutoff=CUTOFF,
    )
    counts = calculation.execute_run(engine, "run-1")
    assert counts["rating_interval"] == 1
    assert counts["interval_exposure"] >= 1
    assert counts["interval_return"] >= 1
    assert counts["interval_contribution"] >= 1
    assert counts["event_window"] == 1
    assert counts["portfolio_nav"] >= 1
    assert counts["portfolio_position"] >= 1
    assert counts["analyst_metric"] > 0
    assert counts["analyst_composite"] == 1

    run = runs.get_calc_run(engine, "run-1")
    assert run is not None
    assert run["status"] == "succeeded"
    artifacts = json.loads(run["row_counts_json"])["stage_artifacts"]
    assert len(artifacts) == 13
    assert run["source_fingerprint"] is not None


def test_execute_run_failure_marks_run_failed(db_path: Path) -> None:
    engine = create_engine_for(db_path)
    # as_of before any session -> empty sessions frame -> build_intervals fails.
    calculation.prepare_run(
        engine,
        Methodology(),
        run_id="run-bad",
        as_of_date=date(2024, 1, 1),
        knowledge_cutoff=CUTOFF,
    )
    with pytest.raises(ValueError, match="sessions frame is empty"):
        calculation.execute_run(engine, "run-bad")
    run = runs.get_calc_run(engine, "run-bad")
    assert run is not None
    assert run["status"] == "failed"
    assert run["error_json"] is not None
    assert json.loads(run["error_json"])["type"]


def test_execute_run_unknown_run(db_path: Path) -> None:
    engine = create_engine_for(db_path)
    with pytest.raises(KeyError):
        calculation.execute_run(engine, "nope")


def test_methodology_version_roundtrip(db_path: Path) -> None:
    engine = create_engine_for(db_path)
    methodology = Methodology(transaction_cost_bps=5.0)
    calculation.prepare_run(
        engine,
        methodology,
        run_id="run-m",
        as_of_date=AS_OF,
        knowledge_cutoff=CUTOFF,
    )
    with engine.connect() as conn:
        row = conn.execute(
            select(MethodologyVersion.params_json, MethodologyVersion.fingerprint)
            .select_from(CalcRun.__table__.join(MethodologyVersion.__table__))
            .where(CalcRun.run_id == "run-m")
        ).one()
    loaded = Methodology.model_validate(json.loads(row.params_json))
    assert loaded == methodology
    assert row.fingerprint == methodology.fingerprint()
