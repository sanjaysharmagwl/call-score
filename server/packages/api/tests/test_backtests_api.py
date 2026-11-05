"""Tests for the backtest endpoints (``/api/backtests``).

The DB fixture mirrors ``test_runs.py`` plus an ``IDX_BROAD`` benchmark leg
(``BacktestSpec.benchmark_id`` defaults to ``MARKET_BENCHMARK_ID``).
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from callscore_api import errors
from callscore_api.routers import backtests as backtests_router
from callscore_api.services import backtests as svc
from callscore_api.settings import Settings
from callscore_core.enums import RunStatus
from callscore_store import create_engine_for
from callscore_store.models import Backtest, BacktestNav
from callscore_store.repositories import events, market, reference
from callscore_store.repositories.base import table_of
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
        benchmark_defs=[
            {"benchmark_id": "IDX_BROAD", "kind": "market"},
            {"benchmark_id": "SECTOR_SEC1", "kind": "sector"},
        ],
        security_daily=[
            {
                "security_id": "S1",
                "session_date": d,
                "open": 100.0,
                "high": 103.0,
                "low": 99.0,
                "close": 100.0 + (i % 3),
                "adj_factor": 1.0,
                "cash_dividend": None,
                "tr_index": 100.0 + i,
                "is_halted": False,
            }
            for i, d in enumerate(SESSION_DAYS)
        ],
        benchmark_daily=[
            {
                "benchmark_id": bid,
                "session_date": d,
                "open_level": 100.0,
                "close_level": 100.0 + i,
            }
            for bid in ("IDX_BROAD", "SECTOR_SEC1")
            for i, d in enumerate(SESSION_DAYS)
        ],
        liquidity_daily=[
            {"security_id": "S1", "session_date": d, "adv_20d": 1_000_000.0, "free_float": 0.5}
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


def _make_db(tmp_path: Path, *, seed: bool = True) -> Path:
    db_path = tmp_path / "callscore.db"
    engine = create_engine_for(db_path)
    bootstrap(engine)
    if seed:
        _seed(engine)
    engine.dispose()
    return db_path


def _client(db_path: Path, cache_dir: Path) -> TestClient:
    # Bare app: create_app()'s SPA catch-all would shadow routes included
    # after it; deps resolve lazily from app.state.settings.
    app = FastAPI()
    app.state.settings = Settings(db_path=db_path, cache_dir=cache_dir)
    app.include_router(backtests_router.router, prefix="/api")
    errors.register_error_handlers(app)
    return TestClient(app)


def _submit(client: TestClient, spec: dict | None = None, **extra: object) -> str:
    body: dict = {"spec": spec if spec is not None else {}}
    body.update(extra)
    resp = client.post("/api/backtests", json=body)
    assert resp.status_code == 202, resp.text
    return str(resp.json()["backtest_id"])


def _run(client_db: Path, backtest_id: str) -> None:
    engine = create_engine_for(client_db)
    try:
        svc.execute_backtest(engine, backtest_id)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Submit + lifecycle
# ---------------------------------------------------------------------------


def test_submit_execute_full_lifecycle(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")

    backtest_id = _submit(client, {"start_date": "2025-01-06", "end_date": "2025-01-17"})
    assert backtest_id.startswith("bt-")

    # Queued row visible in list/detail before execution.
    detail = client.get(f"/api/backtests/{backtest_id}").json()
    assert detail["status"] == "queued"
    assert detail["spec"]["benchmark_id"] == "IDX_BROAD"

    _run(db_path, backtest_id)

    detail = client.get(f"/api/backtests/{backtest_id}").json()
    assert detail["status"] == "succeeded"
    assert detail["started_at"] is not None and detail["finished_at"] is not None
    assert detail["error"] is None
    assert detail["monthly_returns"]
    assert detail["attribution"]

    nav = client.get(f"/api/backtests/{backtest_id}/nav").json()
    assert nav["total"] == len(SESSION_DAYS)
    assert nav["data"][0]["nav"] == pytest.approx(1.0, abs=1e-9)

    stats = client.get(f"/api/backtests/{backtest_id}/stats").json()["data"]
    codes = {s["stat_code"] for s in stats}
    assert {"sharpe_ratio", "max_drawdown", "n_sessions"} <= codes
    n_sessions = next(s for s in stats if s["stat_code"] == "n_sessions")
    assert n_sessions["value"] == len(SESSION_DAYS)

    monthly = client.get(f"/api/backtests/{backtest_id}/monthly").json()["data"]
    assert monthly and monthly[0]["year"] == 2025 and monthly[0]["month"] == 1

    attribution = client.get(f"/api/backtests/{backtest_id}/attribution").json()["data"]
    dims = {a["dimension"] for a in attribution}
    assert dims <= {"sector", "analyst", "rating_action"}
    assert attribution


def test_submit_idempotent_and_request_overrides(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    spec = {"selection": "all"}
    first = client.post("/api/backtests", json={"spec": spec, "name": "bt1"})
    second = client.post("/api/backtests", json={"spec": spec, "name": "bt1"})
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["backtest_id"] == second.json()["backtest_id"]

    # Request-level overrides land in the stored spec.
    rid = _submit(client, {"selection": "all"}, start_date="2025-01-06", end_date="2025-01-17")
    detail = client.get(f"/api/backtests/{rid}").json()
    assert detail["spec"]["start_date"] == "2025-01-06"
    assert detail["spec"]["end_date"] == "2025-01-17"


def test_submit_invalid_spec_422(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    # Dynamic selection without score_lookback_sessions -> engine spec error.
    resp = client.post("/api/backtests", json={"spec": {"selection": "top_n", "top_n": 3}})
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"
    # Bogus enum value.
    resp = client.post("/api/backtests", json={"spec": {"mode": "bogus"}})
    assert resp.status_code == 422
    # Missing spec entirely.
    resp = client.post("/api/backtests", json={})
    assert resp.status_code == 422


def test_failed_backtest_records_error(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    # Unknown benchmark -> MissingBenchmarkData (ValueError) during execution.
    backtest_id = _submit(client, {"benchmark_id": "NOPE"})
    engine = create_engine_for(db_path)
    with pytest.raises(ValueError):
        svc.execute_backtest(engine, backtest_id)
    engine.dispose()

    detail = client.get(f"/api/backtests/{backtest_id}").json()
    assert detail["status"] == "failed"
    assert detail["error"]["type"] == "MissingBenchmarkData"
    # No partial publication: nav/stats are empty.
    assert client.get(f"/api/backtests/{backtest_id}/nav").json()["total"] == 0
    assert client.get(f"/api/backtests/{backtest_id}/stats").json()["data"] == []


def test_list_filter_and_404(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    backtest_id = _submit(
        client,
        {"selection": "all", "start_date": "2025-01-06"},
        end_date="2025-01-17",
    )
    body = client.get("/api/backtests?status=queued").json()
    assert body["pagination"]["total"] == 1
    row = body["data"][0]
    # spec_summary is derived from stored config_json; key_stats null pre-run.
    assert row["spec_summary"]["selection"] == "all"
    assert row["spec_summary"]["mode"] == "long_only"
    assert row["spec_summary"]["sizing"] == "equal"
    assert row["spec_summary"]["rebalance"] == "event_driven"
    assert row["spec_summary"]["start_date"] == "2025-01-06"
    assert row["spec_summary"]["end_date"] == "2025-01-17"
    assert row["key_stats"] is None

    _run(db_path, backtest_id)
    body = client.get("/api/backtests?status=succeeded").json()
    row = body["data"][0]
    assert row["key_stats"] is not None
    assert row["key_stats"]["n_sessions"] == len(SESSION_DAYS)
    assert row["key_stats"]["annualized_return"] is not None
    assert row["key_stats"]["max_drawdown"] is not None
    assert "sharpe" in row["key_stats"]

    assert client.get("/api/backtests?status=bogus").status_code == 422
    assert client.get("/api/backtests/nope").status_code == 404
    assert client.get("/api/backtests/nope/nav").status_code == 404


# ---------------------------------------------------------------------------
# Runner + recovery
# ---------------------------------------------------------------------------


def test_runner_run_once_and_recovery(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    backtest_id = _submit(client, {"end_date": "2025-01-17"})

    engine = create_engine_for(db_path)
    runner = svc.BacktestRunner(engine, poll_interval=0.01)
    assert runner.run_once() == backtest_id
    row = svc.get_backtest_detail(engine, backtest_id)
    assert row is not None and row["status"] == "succeeded"
    runner.close()
    engine.dispose()

    # Simulate an orphaned running row -> recovery marks it interrupted.
    engine = create_engine_for(db_path)
    second = svc.submit_backtest(engine, spec={})
    with engine.begin() as conn:
        conn.execute(
            table_of(Backtest)
            .update()
            .where(table_of(Backtest).c.backtest_id == second.backtest_id)
            .values(status=RunStatus.RUNNING.value)
        )
    assert svc.recover_interrupted_backtests(engine) == 1
    row = svc.get_backtest_detail(engine, second.backtest_id)
    assert row is not None and row["status"] == "interrupted"
    engine.dispose()


def test_execute_requires_queued(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    backtest_id = _submit(client, {})
    _run(db_path, backtest_id)
    engine = create_engine_for(db_path)
    with pytest.raises(ValueError, match="not 'queued'"):
        svc.execute_backtest(engine, backtest_id)
    engine.dispose()


# ---------------------------------------------------------------------------
# Series endpoints: nav resample, trades csv, bounds
# ---------------------------------------------------------------------------


def test_nav_intervals_and_bounds(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    backtest_id = _submit(client, {"end_date": "2025-01-17"})
    _run(db_path, backtest_id)

    weekly = client.get(f"/api/backtests/{backtest_id}/nav?interval=1w").json()
    assert weekly["interval"] == "1w"
    assert weekly["total"] <= weekly["source_rows"]

    bounded = client.get(f"/api/backtests/{backtest_id}/nav?from=2025-01-13&to=2025-01-15").json()
    assert bounded["total"] == 3
    assert bounded["data"][-1]["session_date"] == "2025-01-15"

    assert client.get(f"/api/backtests/{backtest_id}/nav?interval=5m").status_code == 422


def test_trades_json_and_csv(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    backtest_id = _submit(client, {"end_date": "2025-01-17"})
    _run(db_path, backtest_id)

    resp = client.get(f"/api/backtests/{backtest_id}/trades?format=json")
    assert resp.status_code == 200
    body = resp.json()
    assert "pagination" in body and "truncated" in body
    for trade in body["data"]:
        assert trade["side"] in ("buy", "sell")
        assert trade["quantity"] > 0

    csv_resp = client.get(f"/api/backtests/{backtest_id}/trades?format=csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "trade_seq,session_date" in csv_resp.text


def test_backtest_count_rows(tmp_path: Path) -> None:
    """Sanity: results landed in the store tables."""
    db_path = _make_db(tmp_path)
    engine = create_engine_for(db_path)
    result = svc.submit_backtest(engine, spec={"end_date": "2025-01-17"})
    svc.execute_backtest(engine, result.backtest_id)
    with engine.connect() as conn:
        nav_count = conn.execute(
            select(func.count())
            .select_from(table_of(BacktestNav))
            .where(table_of(BacktestNav).c.backtest_id == result.backtest_id)
        ).scalar_one()
    assert nav_count == len(SESSION_DAYS)
    engine.dispose()
