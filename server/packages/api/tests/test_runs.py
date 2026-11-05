"""Tests for the run lifecycle endpoints (``/api/runs``).

The DB fixture mirrors ``test_jobs.py``: a hand-seeded minimal universe (one
analyst, one security, ten sessions, one initiation) — small enough for fast
pipeline runs, real enough to exercise every stage.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from callscore_api import create_app, errors
from callscore_api.routers import runs as runs_router
from callscore_api.services.jobs import JobCoordinator
from callscore_api.settings import Settings
from callscore_core.enums import RunStatus
from callscore_core.methodology import Methodology
from callscore_store import create_engine_for
from callscore_store.repositories import events, market, reference
from callscore_store.repositories import runs as runs_repo
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


def _make_db(tmp_path: Path, *, seed: bool = False) -> Path:
    db_path = tmp_path / "callscore.db"
    engine = create_engine_for(db_path)
    bootstrap(engine)
    if seed:
        _seed(engine)
    engine.dispose()
    return db_path


def _app(db_path: Path, cache_dir: Path) -> FastAPI:
    app = create_app()
    app.state.settings = Settings(db_path=db_path, cache_dir=cache_dir)
    app.include_router(runs_router.router, prefix="/api")
    errors.register_error_handlers(app)
    return app


def _client(db_path: Path, cache_dir: Path) -> tuple[TestClient, FastAPI]:
    app = _app(db_path, cache_dir)
    return TestClient(app), app


def _coordinator(app: FastAPI) -> JobCoordinator:
    coord = getattr(app.state, "job_coordinator", None)
    assert isinstance(coord, JobCoordinator)
    return coord


# ---------------------------------------------------------------------------
# Submit + lifecycle
# ---------------------------------------------------------------------------


def test_submit_202_then_execute_to_succeeded(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, seed=True)
    client, app = _client(db_path, tmp_path / "cache")

    resp = client.post(
        "/api/runs",
        json={"as_of_date": "2025-01-17", "methodology": "default"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["created"] is True
    run_id = body["run_id"]

    # The background worker is not started in tests; run one job synchronously.
    coord = _coordinator(app)
    assert coord.run_once() == run_id

    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    d = detail.json()
    assert d["status"] == "succeeded"
    assert d["progress"] == 1.0
    assert d["started_at"] is not None and d["finished_at"] is not None
    assert d["methodology_fingerprint"] == Methodology().fingerprint()
    assert d["methodology"]["contract_version"] == 1
    assert d["row_counts"]["rating_interval"] == 1
    assert d["row_counts"]["analyst_composite"] == 1
    assert isinstance(d["stage_artifacts"], list) and d["stage_artifacts"]
    assert d["error"] is None


def test_submit_idempotent_same_params(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _app_ = _client(db_path, tmp_path / "cache")
    payload = {
        "as_of_date": "2025-01-17",
        "knowledge_cutoff": "2025-01-18T00:00:00Z",
        "methodology": "default",
        "universe_filter": ["S1", "S2"],
    }
    first = client.post("/api/runs", json=payload)
    second = client.post("/api/runs", json=payload)
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    # Reordered universe filter is the same canonical key.
    third = client.post("/api/runs", json={**payload, "universe_filter": ["S2", "S1"]})
    assert third.json()["run_id"] == first.json()["run_id"]
    assert third.json()["created"] is False


def test_submit_inline_methodology_object(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _app_ = _client(db_path, tmp_path / "cache")
    resp = client.post(
        "/api/runs",
        json={
            "as_of_date": "2025-01-17",
            "methodology": {"transaction_cost_bps": 5.0},
        },
    )
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    detail = client.get(f"/api/runs/{run_id}")
    assert detail.json()["methodology"]["transaction_cost_bps"] == 5.0


def test_submit_invalid_methodology_422(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _app_ = _client(db_path, tmp_path / "cache")

    # Unknown extra field -> Methodology validation error.
    resp = client.post(
        "/api/runs",
        json={"as_of_date": "2025-01-17", "methodology": {"bogus": 1}},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"

    # Unresolvable string reference.
    resp = client.post(
        "/api/runs",
        json={"as_of_date": "2025-01-17", "methodology": "no-such-thing"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"

    # Missing required field.
    resp = client.post("/api/runs", json={"methodology": "default"})
    assert resp.status_code == 422


def test_get_run_missing_404(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _app_ = _client(db_path, tmp_path / "cache")
    resp = client.get("/api/runs/no-such-run")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "not_found"
    assert "no-such-run" in body["detail"]


# ---------------------------------------------------------------------------
# List + pagination
# ---------------------------------------------------------------------------


def test_list_runs_empty_and_filters(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _app_ = _client(db_path, tmp_path / "cache")
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["pagination"]["total"] == 0
    assert body["pagination"]["limit"] == 50
    assert body["pagination"]["offset"] == 0


def test_list_runs_pagination_bounds(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _app_ = _client(db_path, tmp_path / "cache")
    for params in ("limit=0", "limit=501", "offset=-1", "limit=abc"):
        resp = client.get(f"/api/runs?{params}")
        assert resp.status_code == 422, params
        assert resp.json()["code"] == "validation_error"


def test_list_runs_pagination_and_filters(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _app_ = _client(db_path, tmp_path / "cache")
    for i, as_of in enumerate(("2025-01-10", "2025-01-11", "2025-01-12")):
        resp = client.post(
            "/api/runs",
            json={
                "as_of_date": as_of,
                "methodology": {"transaction_cost_bps": float(i)},
            },
        )
        assert resp.status_code == 202

    page1 = client.get("/api/runs?limit=2&offset=0").json()
    assert page1["pagination"]["total"] == 3
    assert page1["pagination"]["count"] == 2
    assert len(page1["data"]) == 2
    # Newest as_of first.
    assert page1["data"][0]["as_of_date"] == "2025-01-12"

    page2 = client.get("/api/runs?limit=2&offset=2").json()
    assert page2["pagination"]["count"] == 1
    assert page2["data"][0]["as_of_date"] == "2025-01-10"

    filtered = client.get("/api/runs?status=queued&as_of=2025-01-11").json()
    assert filtered["pagination"]["total"] == 1
    assert filtered["data"][0]["as_of_date"] == "2025-01-11"

    bad_status = client.get("/api/runs?status=bogus")
    assert bad_status.status_code == 422

    bad_sort = client.get("/api/runs?sort=run_id;DROP")
    assert bad_sort.status_code == 422


def test_list_runs_includes_error_summary(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, app = _client(db_path, tmp_path / "cache")
    client.post("/api/runs", json={"as_of_date": "2024-01-01", "methodology": "default"})
    coord = _coordinator(app)
    with pytest.raises(ValueError):
        coord.run_once()  # empty DB -> engine failure -> run failed
    body = client.get("/api/runs?status=failed").json()
    assert body["pagination"]["total"] == 1
    run = body["data"][0]
    assert run["status"] == "failed"
    assert run["error"] is not None
    assert run["error"]["type"]
    assert run["error"]["message"]


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------


def _succeeded_run(client: TestClient, app: FastAPI, methodology: object, as_of: str) -> str:
    resp = client.post("/api/runs", json={"as_of_date": as_of, "methodology": methodology})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert _coordinator(app).run_once() == run_id
    return run_id


def test_compare_runs(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, seed=True)
    client, app = _client(db_path, tmp_path / "cache")
    run_a = _succeeded_run(client, app, "default", "2025-01-17")
    run_b = _succeeded_run(client, app, {"transaction_cost_bps": 5.0}, "2025-01-17")

    resp = client.get(f"/api/runs/compare?a={run_a}&b={run_b}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["a"]["run"]["run_id"] == run_a
    assert body["b"]["run"]["run_id"] == run_b
    assert (
        body["a"]["run"]["methodology_fingerprint"] != body["b"]["run"]["methodology_fingerprint"]
    )
    for side in (body["a"], body["b"]):
        assert len(side["composites"]) == 1
        assert side["composites"][0]["analyst_id"] == "A1"
        assert side["composites"][0]["is_ranked"] is False  # single-analyst peer group
        assert isinstance(side["metric_summary"], list)
        codes = {m["metric_code"] for m in side["metric_summary"]}
        assert "mean_contribution" in codes


def test_compare_missing_run_404(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _app_ = _client(db_path, tmp_path / "cache")
    resp = client.get("/api/runs/compare?a=nope&b=alsono")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def test_retry_failed_run(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, app = _client(db_path, tmp_path / "cache")
    resp = client.post("/api/runs", json={"as_of_date": "2024-01-01", "methodology": "default"})
    run_id = resp.json()["run_id"]
    with pytest.raises(ValueError):
        _coordinator(app).run_once()

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "failed"
    assert detail["error"] is not None

    retry = client.post(f"/api/runs/{run_id}/retry")
    assert retry.status_code == 200
    assert retry.json()["status"] == "queued"
    assert retry.json()["progress"] == 0.0
    assert retry.json()["error"] is None

    # Queued is not retryable -> 409.
    again = client.post(f"/api/runs/{run_id}/retry")
    assert again.status_code == 409
    assert again.json()["code"] == "invalid_transition"


def test_retry_interrupted_run(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    engine = create_engine_for(db_path)
    client, _app_ = _client(db_path, tmp_path / "cache")
    resp = client.post("/api/runs", json={"as_of_date": "2025-01-17", "methodology": "default"})
    run_id = resp.json()["run_id"]
    # Simulate an orphaned run: queued -> running -> interrupted.
    runs_repo.update_run_status(engine, run_id, RunStatus.RUNNING)
    runs_repo.update_run_status(engine, run_id, RunStatus.INTERRUPTED)
    engine.dispose()

    retry = client.post(f"/api/runs/{run_id}/retry")
    assert retry.status_code == 200
    assert retry.json()["status"] == "queued"


def test_retry_missing_and_succeeded_409(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, seed=True)
    client, app = _client(db_path, tmp_path / "cache")
    assert client.post("/api/runs/nope/retry").status_code == 404

    run_id = _succeeded_run(client, app, "default", "2025-01-17")
    resp = client.post(f"/api/runs/{run_id}/retry")
    assert resp.status_code == 409
    assert resp.json()["code"] == "invalid_transition"


def test_retry_via_json_error_cleared_in_detail(tmp_path: Path) -> None:
    """After retry the run detail no longer reports the old error."""
    db_path = _make_db(tmp_path)
    client, app = _client(db_path, tmp_path / "cache")
    resp = client.post("/api/runs", json={"as_of_date": "2024-01-01", "methodology": "default"})
    run_id = resp.json()["run_id"]
    with pytest.raises(ValueError):
        _coordinator(app).run_once()
    assert client.post(f"/api/runs/{run_id}/retry").status_code == 200
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "queued"
    assert detail["error"] is None
