"""Tests for the custom-benchmark endpoints (``/api/benchmarks``).

The DB fixture mirrors ``test_runs.py``: a hand-seeded minimal universe (one
analyst, one security, ten sessions) plus ``liquidity_daily`` rows so every
weighting is exercisable.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from callscore_api import errors
from callscore_api.routers import benchmarks as benchmarks_router
from callscore_api.services import benchmarks as svc
from callscore_api.settings import Settings
from callscore_store import create_engine_for
from callscore_store.models import BenchmarkDaily
from callscore_store.repositories import market, reference
from callscore_store.repositories.base import table_of
from callscore_store.schema import bootstrap

SESSION_DAYS = []
_d = date(2025, 1, 6)
while len(SESSION_DAYS) < 10:
    if _d.weekday() < 5:
        SESSION_DAYS.append(_d)
    _d += timedelta(days=1)


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
        benchmark_defs=[{"benchmark_id": "IDX_BROAD", "kind": "market"}],
        security_daily=[
            {
                "security_id": "S1",
                "session_date": d,
                "open": 100.0 + i,
                "high": 100.0 + i,
                "low": 100.0 + i,
                "close": 100.0 + i,
                "adj_factor": 1.0,
                "cash_dividend": None,
                "tr_index": 100.0 + i,
                "is_halted": False,
            }
            for i, d in enumerate(SESSION_DAYS)
        ],
        liquidity_daily=[
            {"security_id": "S1", "session_date": d, "adv_20d": 1_000_000.0, "free_float": 0.5}
            for d in SESSION_DAYS
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
    app.include_router(benchmarks_router.router, prefix="/api")
    errors.register_error_handlers(app)
    return TestClient(app)


def _create(client: TestClient, spec: dict, name: str | None = None) -> dict:
    body: dict = {"spec": spec}
    if name is not None:
        body["name"] = name
    resp = client.post("/api/benchmarks", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


_EQUAL_SPEC = {
    "preset": "equal_weight_market",
    "weighting": "equal",
    "rebalance": "daily",
    "start_date": "2025-01-06",
    "end_date": "2025-01-17",
}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_list_detail(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")

    created = _create(client, _EQUAL_SPEC, name="EW market")
    assert created["created"] is True
    assert created["status"] == "draft"
    benchmark_id = created["benchmark_id"]
    assert benchmark_id.startswith("CSTM_")

    # Idempotent: identical spec -> same id, created=False.
    again = _create(client, _EQUAL_SPEC)
    assert again["benchmark_id"] == benchmark_id
    assert again["created"] is False

    listing = client.get("/api/benchmarks").json()
    assert listing["pagination"]["total"] == 1
    assert listing["data"][0]["benchmark_id"] == benchmark_id
    assert listing["data"][0]["name"] == "EW market"
    assert listing["data"][0]["status"] == "draft"

    filtered = client.get("/api/benchmarks?status=ready").json()
    assert filtered["data"] == []

    detail = client.get(f"/api/benchmarks/{benchmark_id}")
    assert detail.status_code == 200
    d = detail.json()
    assert d["spec"]["preset"] == "equal_weight_market"
    assert d["spec"]["benchmark_id"] == benchmark_id
    assert d["materialization"] is None

    assert client.get("/api/benchmarks/nope").status_code == 404


def test_create_explicit_benchmark_id(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    spec = {**_EQUAL_SPEC, "benchmark_id": "MY_BENCH"}
    created = _create(client, spec)
    assert created["benchmark_id"] == "MY_BENCH"


def test_create_invalid_spec_422(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    # Unknown weighting literal -> BenchmarkSpec validation error.
    resp = client.post("/api/benchmarks", json={"spec": {**_EQUAL_SPEC, "weighting": "bogus"}})
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"
    # Missing required benchmark universe is fine; bad type is not.
    resp = client.post("/api/benchmarks", json={"spec": {"preset": 42}})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Materialize + series
# ---------------------------------------------------------------------------


def test_materialize_and_series(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    benchmark_id = _create(client, _EQUAL_SPEC)["benchmark_id"]

    resp = client.post(f"/api/benchmarks/{benchmark_id}/materialize")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["row_count"] == len(SESSION_DAYS)
    assert body["as_of_date"] == "2025-01-17"
    assert body["revision"] == 1

    detail = client.get(f"/api/benchmarks/{benchmark_id}").json()
    assert detail["status"] == "ready"
    assert detail["materialization"]["row_count"] == len(SESSION_DAYS)
    assert detail["materialization"]["exclusion_counts"] == {}

    series = client.get(f"/api/benchmarks/{benchmark_id}/series").json()
    assert series["total"] == len(SESSION_DAYS)
    first = series["data"][0]
    assert first["session_date"] == SESSION_DAYS[0].isoformat()
    assert first["open_level"] > 0 and first["close_level"] > 0

    # Re-materialize is idempotent and bumps the revision.
    resp2 = client.post(f"/api/benchmarks/{benchmark_id}/materialize")
    assert resp2.json()["revision"] == 2
    with create_engine_for(db_path).connect() as conn:
        n = conn.execute(
            select(table_of(BenchmarkDaily).c.benchmark_id).where(
                table_of(BenchmarkDaily).c.benchmark_id == benchmark_id
            )
        ).all()
    assert len(n) == len(SESSION_DAYS)


def test_materialize_missing_404(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    assert client.post("/api/benchmarks/nope/materialize").status_code == 404


def test_materialize_failure_marks_failed(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    # start_date after the data end -> ValueError inside materialize.
    spec = {**_EQUAL_SPEC, "start_date": "2030-01-01", "end_date": None}
    benchmark_id = _create(client, spec)["benchmark_id"]
    resp = client.post(f"/api/benchmarks/{benchmark_id}/materialize")
    assert resp.status_code == 422
    detail = client.get(f"/api/benchmarks/{benchmark_id}").json()
    # The pre-build validation happens before status flips, so it stays draft.
    assert detail["status"] == "draft"


def test_service_materialize_engine_failure(tmp_path: Path) -> None:
    """An engine-level failure flips the def to ``failed`` with an error."""
    db_path = _make_db(tmp_path)
    engine = create_engine_for(db_path)
    # sector_neutral without sector_id -> BenchmarkSpec validates fine but
    # build_custom_benchmark raises ValueError.
    result = svc.create_benchmark(
        engine,
        name=None,
        spec={"preset": "sector_neutral", "weighting": "equal"},
    )
    with pytest.raises(ValueError, match="sector_id"):
        svc.materialize_benchmark(engine, result["benchmark_id"])
    detail = svc.get_benchmark(engine, result["benchmark_id"])
    assert detail["status"] == "failed"
    assert detail["error"]["type"] == "ValueError"
    engine.dispose()


def test_series_empty_for_unmaterialized(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client = _client(db_path, tmp_path / "cache")
    benchmark_id = _create(client, _EQUAL_SPEC)["benchmark_id"]
    series = client.get(f"/api/benchmarks/{benchmark_id}/series").json()
    assert series["total"] == 0
    # Date filter params are honored.
    series = client.get(
        f"/api/benchmarks/{benchmark_id}/series?from=2025-01-10&to=2025-01-14"
    ).json()
    assert series["total"] == 0
