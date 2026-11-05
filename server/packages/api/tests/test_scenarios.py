"""Tests for the scenario backend (``/api/scenarios``).

Two layers are covered:

* API tests use the same minimal seeded universe as ``test_runs.py`` (one
  analyst, one security, ten sessions, one initiation) — real pipeline runs
  via ``JobCoordinator.run_once``.
* Service-level tests hand-build ``calc_run`` + ``analyst_composite`` /
  ``analyst_metric`` rows for deterministic, hand-computed delta reports.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert

from callscore_api import create_app, errors
from callscore_api.routers import scenarios as scenarios_router
from callscore_api.services import scenarios as scenario_service
from callscore_api.services.jobs import JobCoordinator
from callscore_api.settings import Settings
from callscore_core.methodology import Methodology, PositionWeights
from callscore_store import create_engine_for
from callscore_store.models import AnalystComposite, AnalystMetric
from callscore_store.repositories import events, market, reference
from callscore_store.repositories import runs as runs_repo
from callscore_store.repositories.base import table_of
from callscore_store.schema import bootstrap

AS_OF = date(2025, 1, 17)
CUTOFF = datetime(2025, 1, 18, tzinfo=UTC)


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
    """Same minimal universe as ``test_runs._seed``."""
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
    app.include_router(scenarios_router.router, prefix="/api")
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
# CRUD + validation
# ---------------------------------------------------------------------------


def test_create_list_get_scenario(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _ = _client(db_path, tmp_path / "cache")

    resp = client.post(
        "/api/scenarios",
        json={
            "name": "Cheaper costs",
            "description": "halve-ish transaction costs",
            "overrides": {"transaction_cost_bps": 5.0, "position_weights": {"buy": 0.7}},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Cheaper costs"
    assert body["base_fingerprint"] == Methodology().fingerprint()
    assert body["overrides"]["position_weights"] == {"buy": 0.7}
    assert body["run_count"] == 0 and body["last_run_id"] is None
    assert body["base_methodology"]["transaction_cost_bps"] == 0.0  # base unchanged
    scenario_id = body["scenario_id"]

    listing = client.get("/api/scenarios").json()
    assert listing["pagination"]["total"] == 1
    assert listing["data"][0]["scenario_id"] == scenario_id

    detail = client.get(f"/api/scenarios/{scenario_id}")
    assert detail.status_code == 200
    assert detail.json()["base_methodology"]["contract_version"] == 1
    assert detail.json()["run_ids"] == []


def test_create_scenario_validation_422(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _ = _client(db_path, tmp_path / "cache")

    # Unknown top-level override key.
    resp = client.post(
        "/api/scenarios",
        json={"name": "bad", "overrides": {"not_a_field": 1}},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"

    # Unknown nested weight key (merged then re-validated by pydantic).
    resp = client.post(
        "/api/scenarios",
        json={"name": "bad", "overrides": {"position_weights": {"bogus": 1}}},
    )
    assert resp.status_code == 422

    # Out-of-range value.
    resp = client.post(
        "/api/scenarios",
        json={"name": "bad", "overrides": {"decay_days": -3}},
    )
    assert resp.status_code == 422

    # Family weights must still sum to one after override.
    resp = client.post(
        "/api/scenarios",
        json={"name": "bad", "overrides": {"family_weights": {"timing": 0.9}}},
    )
    assert resp.status_code == 422

    # Unknown base fingerprint -> 404.
    resp = client.post(
        "/api/scenarios",
        json={"name": "bad", "base_methodology": "0" * 64},
    )
    assert resp.status_code == 404


def test_get_scenario_missing_404(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _ = _client(db_path, tmp_path / "cache")
    assert client.get("/api/scenarios/nope").status_code == 404


# ---------------------------------------------------------------------------
# Execute + linkage
# ---------------------------------------------------------------------------


def test_execute_scenario_links_run(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, seed=True)
    client, app = _client(db_path, tmp_path / "cache")

    created = client.post(
        "/api/scenarios",
        json={"name": "cost5", "overrides": {"transaction_cost_bps": 5.0}},
    ).json()
    scenario_id = created["scenario_id"]

    resp = client.post(
        f"/api/scenarios/{scenario_id}/execute",
        json={"as_of_date": "2025-01-17"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["scenario_id"] == scenario_id
    assert body["status"] == "queued"
    assert body["created"] is True
    run_id = body["run_id"]
    # Invalidation: transaction_cost_bps feeds METRICS only.
    assert body["invalidation"]["affected_stages"] == ["metrics", "statistics"]

    coord = _coordinator(app)
    assert coord.run_once() == run_id

    detail = client.get(f"/api/scenarios/{scenario_id}").json()
    assert detail["last_run_id"] == run_id
    assert detail["run_ids"] == [run_id]
    assert detail["run_count"] == 1
    assert detail["last_execution"]["run_id"] == run_id
    assert detail["last_execution"]["methodology_fingerprint"] == body["methodology_fingerprint"]

    # Idempotent re-execute: same run, no duplicate linkage.
    again = client.post(
        f"/api/scenarios/{scenario_id}/execute",
        json={"as_of_date": "2025-01-17"},
    ).json()
    assert again["run_id"] == run_id
    assert again["created"] is False
    detail = client.get(f"/api/scenarios/{scenario_id}").json()
    assert detail["run_ids"] == [run_id]


def test_execute_inline_scenario_persist(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, seed=True)
    client, app = _client(db_path, tmp_path / "cache")

    resp = client.post(
        "/api/scenarios/execute",
        json={
            "as_of_date": "2025-01-17",
            "name": "inline decay",
            "overrides": {"decay_days": 30},
            "persist": True,
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["scenario_id"] is not None
    coord = _coordinator(app)
    assert coord.run_once() == body["run_id"]
    detail = client.get(f"/api/scenarios/{body['scenario_id']}").json()
    assert detail["name"] == "inline decay"
    assert detail["last_run_id"] == body["run_id"]


def test_execute_missing_scenario_404(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _ = _client(db_path, tmp_path / "cache")
    resp = client.post("/api/scenarios/nope/execute", json={"as_of_date": "2025-01-17"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Compare (API + service)
# ---------------------------------------------------------------------------


def test_compare_api_two_runs(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, seed=True)
    client, app = _client(db_path, tmp_path / "cache")

    run_a = client.post(
        "/api/runs", json={"as_of_date": "2025-01-17", "methodology": "default"}
    ).json()["run_id"]
    run_b = client.post(
        "/api/runs",
        json={"as_of_date": "2025-01-17", "methodology": {"transaction_cost_bps": 5.0}},
    ).json()["run_id"]
    coord = _coordinator(app)
    coord.run_once()
    coord.run_once()

    resp = client.get(f"/api/scenarios/compare?a={run_a}&b={run_b}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_a_id"] == run_a and body["run_b_id"] == run_b
    assert body["universe"]["compatible"] is True
    s = body["summary"]
    assert s["n_common"] == 1 and s["only_in_a"] == 0 and s["only_in_b"] == 0
    # The single seeded analyst is unranked (peer group below min size).
    assert s["unranked_to_unranked"] == 1
    assert body["rank_correlation"]["n"] == 0
    assert body["rank_correlation"]["spearman"] is None
    assert body["analysts"][0]["analyst_id"] == "A1"
    assert body["analysts"][0]["transition"] == "unranked"
    codes = {m["metric_code"] for m in body["metric_deltas"]}
    assert "mean_contribution" in codes
    families = {f["family"] for f in body["family_deltas"]}
    assert "return_skill" in families


def _insert_run(
    engine: Engine,
    run_id: str,
    methodology: Methodology,
    *,
    universe_filter_json: str | None = None,
) -> None:
    version_id = runs_repo.register_methodology_version(engine, methodology)
    runs_repo.create_calc_run(
        engine,
        run_id=run_id,
        methodology_version_id=version_id,
        as_of_date=AS_OF,
        knowledge_cutoff=CUTOFF,
        algorithm_version="test",
        universe_filter_json=universe_filter_json,
    )


def _insert_composites(engine: Engine, run_id: str, rows: list[dict[str, object]]) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(table_of(AnalystComposite)),
            [{"run_id": run_id, **row} for row in rows],
        )


def _insert_metrics(engine: Engine, run_id: str, rows: list[dict[str, object]]) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(table_of(AnalystMetric)),
            [{"run_id": run_id, **row} for row in rows],
        )


def _composite(
    analyst_id: str,
    score: float | None,
    rank: int | None,
    *,
    is_ranked: bool,
    peer_rank: int | None = None,
) -> dict[str, object]:
    return {
        "analyst_id": analyst_id,
        "peer_group": "pg",
        "composite_score": score,
        "rank_overall": rank,
        "rank_in_peer_group": peer_rank if peer_rank is not None else rank,
        "is_ranked": is_ranked,
        "unranked_reason": None if is_ranked else "min_ratings_for_rank",
    }


def _metric(analyst_id: str, code: str, value: float) -> dict[str, object]:
    return {
        "analyst_id": analyst_id,
        "metric_code": code,
        "value": value,
        "n_obs": 10,
    }


def _seed_analysts(engine: Engine) -> None:
    reference.insert_reference(
        engine,
        firms=[{"firm_id": "F1", "name": "Fund One"}],
        analysts=[
            {"analyst_id": aid, "name": f"Analyst {aid}", "firm_id": "F1"}
            for aid in ("A1", "A2", "A3", "A4", "A5")
        ],
    )


def test_compare_runs_hand_computed(tmp_path: Path) -> None:
    """Synthetic composites/metrics -> hand-verified deltas."""
    db_path = tmp_path / "callscore.db"
    engine = create_engine_for(db_path)
    bootstrap(engine)
    _seed_analysts(engine)

    _insert_run(engine, "run-a", Methodology(), universe_filter_json='["S1","S2"]')
    _insert_run(
        engine,
        "run-b",
        Methodology(transaction_cost_bps=5.0),
        universe_filter_json='["S2","S3"]',
    )

    # run-a: A1..A4 ranked 1..4; A5 absent.
    _insert_composites(
        engine,
        "run-a",
        [
            _composite("A1", 90.0, 1, is_ranked=True),
            _composite("A2", 80.0, 2, is_ranked=True),
            _composite("A3", 70.0, 3, is_ranked=True),
            _composite("A4", 60.0, 4, is_ranked=True),
        ],
    )
    # run-b: A1 rank2, A2 rank1, A3 unranked (lost), A4 rank3, A5 rank4 (new).
    _insert_composites(
        engine,
        "run-b",
        [
            _composite("A1", 85.0, 2, is_ranked=True),
            _composite("A2", 95.0, 1, is_ranked=True),
            _composite("A3", None, None, is_ranked=False),
            _composite("A4", 75.0, 3, is_ranked=True),
            _composite("A5", 65.0, 4, is_ranked=True),
        ],
    )
    # Metrics: mean_contribution deltas [+.01, 0, +.02, -.03] -> mean 0.0;
    # hit_rate deltas [0, 0, +.2, -.4] -> mean -0.05 (both RETURN_SKILL).
    a_vals = {"A1": 0.01, "A2": 0.02, "A3": 0.03, "A4": 0.04}
    b_vals = {"A1": 0.02, "A2": 0.02, "A3": 0.05, "A4": 0.01}
    a_hit = {"A1": 0.5, "A2": 0.6, "A3": 0.7, "A4": 0.8}
    b_hit = {"A1": 0.5, "A2": 0.6, "A3": 0.9, "A4": 0.4}
    for run_id, vals, hits in (("run-a", a_vals, a_hit), ("run-b", b_vals, b_hit)):
        _insert_metrics(
            engine,
            run_id,
            [
                *[_metric(a, "mean_contribution", v) for a, v in vals.items()],
                *[_metric(a, "hit_rate", v) for a, v in hits.items()],
            ],
        )

    report = scenario_service.compare_runs(engine, "run-a", "run-b")

    # Universe mismatch reported explicitly with intersection.
    assert report["universe"]["compatible"] is False
    assert report["universe"]["intersection"] == ["S2"]

    s = report["summary"]
    assert s["n_analysts_a"] == 4 and s["n_analysts_b"] == 5
    assert s["n_common"] == 4 and s["only_in_a"] == 0 and s["only_in_b"] == 1
    assert s["ranked_to_ranked"] == 3
    assert s["ranked_to_unranked"] == 1  # A3 lost its rank
    assert s["unranked_to_ranked"] == 0
    assert s["unranked_to_unranked"] == 0

    # Spearman over commonly-ranked analysts A1,A2,A4:
    # pairs (a_rank, b_rank) = (1,2), (2,1), (4,3) -> ~0.6547 (hand-computed).
    rc = report["rank_correlation"]
    assert rc["n"] == 3
    assert rc["spearman"] == pytest.approx(0.6547, abs=1e-3)

    by_id = {a["analyst_id"]: a for a in report["analysts"]}
    assert by_id["A1"]["score_delta"] == pytest.approx(-5.0)
    assert by_id["A1"]["rank_delta"] == 1
    assert by_id["A2"]["score_delta"] == pytest.approx(15.0)
    assert by_id["A3"]["transition"] == "lost_rank"
    assert by_id["A3"]["score_delta"] is None  # unranked in b, no score
    assert by_id["A4"]["transition"] == "ranked"
    assert by_id["A5"]["transition"] == "only_in_b"
    assert by_id["A5"]["score_delta"] is None

    deltas = {m["metric_code"]: m for m in report["metric_deltas"]}
    assert deltas["mean_contribution"]["n_pairs"] == 4
    assert deltas["mean_contribution"]["mean_delta"] == pytest.approx(0.0)
    assert deltas["mean_contribution"]["mean_abs_delta"] == pytest.approx(0.015)
    assert deltas["mean_contribution"]["max_abs_delta"] == pytest.approx(0.03)
    assert deltas["hit_rate"]["mean_delta"] == pytest.approx(-0.05)

    fam = {f["family"]: f for f in report["family_deltas"]}
    assert fam["return_skill"]["n_metrics"] == 2
    assert fam["return_skill"]["mean_delta"] == pytest.approx(-0.025)

    # Missing run -> 404.
    with pytest.raises(KeyError):
        scenario_service.compare_runs(engine, "run-a", "nope")
    engine.dispose()


def test_compare_runs_identical_universe(tmp_path: Path) -> None:
    db_path = tmp_path / "callscore.db"
    engine = create_engine_for(db_path)
    bootstrap(engine)
    _seed_analysts(engine)
    _insert_run(engine, "run-a", Methodology())
    _insert_run(engine, "run-b", Methodology(transaction_cost_bps=1.0))
    report = scenario_service.compare_runs(engine, "run-a", "run-b")
    assert report["universe"] == {"a": None, "b": None, "compatible": True, "intersection": None}
    engine.dispose()


# ---------------------------------------------------------------------------
# Sensitivity sweep
# ---------------------------------------------------------------------------


def test_sensitivity_sweep_submit(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _ = _client(db_path, tmp_path / "cache")

    resp = client.post(
        "/api/scenarios/sensitivity",
        json={
            "as_of_date": "2025-01-17",
            "param_path": "transaction_cost_bps",
            "values": [0.0, 5.0, 10.0],
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["count"] == 3
    assert body["sweep_id"].startswith("sweep-")
    run_ids = [j["run_id"] for j in body["jobs"]]
    assert len(set(run_ids)) == 3
    fps = {j["methodology_fingerprint"] for j in body["jobs"]}
    assert len(fps) == 3  # distinct methodologies
    # Re-submitting the identical sweep is idempotent per combo.
    again = client.post(
        "/api/scenarios/sensitivity",
        json={
            "as_of_date": "2025-01-17",
            "param_path": "transaction_cost_bps",
            "values": [0.0, 5.0, 10.0],
        },
    ).json()
    assert again["sweep_id"] == body["sweep_id"]
    assert all(not j["created"] for j in again["jobs"])


def test_sensitivity_sweep_nested_path_and_overrides(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _ = _client(db_path, tmp_path / "cache")

    # Dotted path into a nested weight model.
    resp = client.post(
        "/api/scenarios/sensitivity",
        json={
            "as_of_date": "2025-01-17",
            "param_path": "position_weights.buy",
            "values": [0.4, 0.6],
        },
    )
    assert resp.status_code == 202
    methods = {j["methodology_fingerprint"]: j for j in resp.json()["jobs"]}
    assert len(methods) == 2

    # Cartesian product via overrides with list values (2x2 = 4 combos).
    resp = client.post(
        "/api/scenarios/sensitivity",
        json={
            "as_of_date": "2025-01-17",
            "overrides": {"decay_days": [30, 60], "transaction_cost_bps": [0.0, 5.0]},
        },
    )
    assert resp.status_code == 202
    assert resp.json()["count"] == 4

    # Bad path -> 422; both dimensions -> 422; empty values -> 422.
    for payload in (
        {"as_of_date": "2025-01-17", "param_path": "bogus.path", "values": [1]},
        {
            "as_of_date": "2025-01-17",
            "param_path": "decay_days",
            "values": [30],
            "overrides": {"decay_days": [30]},
        },
        {"as_of_date": "2025-01-17", "param_path": "decay_days", "values": []},
        {"as_of_date": "2025-01-17"},
    ):
        resp = client.post("/api/scenarios/sensitivity", json=payload)
        assert resp.status_code == 422, payload
        assert resp.json()["code"] == "validation_error"


def test_sensitivity_sweep_cap_422(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _ = _client(db_path, tmp_path / "cache")

    resp = client.post(
        "/api/scenarios/sensitivity",
        json={
            "as_of_date": "2025-01-17",
            "param_path": "transaction_cost_bps",
            "values": list(range(26)),  # 26 > MAX_SWEEP_COMBOS (25)
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"
    assert "25" in resp.json()["detail"]

    # Exactly 25 is allowed.
    resp = client.post(
        "/api/scenarios/sensitivity",
        json={
            "as_of_date": "2025-01-17",
            "param_path": "transaction_cost_bps",
            "values": list(range(25)),
        },
    )
    assert resp.status_code == 202
    assert resp.json()["count"] == 25


def test_sensitivity_sweep_with_scenario(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    client, _ = _client(db_path, tmp_path / "cache")

    scenario = client.post(
        "/api/scenarios",
        json={"name": "sweep base", "overrides": {"transaction_cost_bps": 2.0}},
    ).json()
    resp = client.post(
        "/api/scenarios/sensitivity",
        json={
            "as_of_date": "2025-01-17",
            "scenario_id": scenario["scenario_id"],
            "param_path": "decay_days",
            "values": [30, 45],
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["scenario_id"] == scenario["scenario_id"]
    # The sweep is recorded on the scenario.
    detail = client.get(f"/api/scenarios/{scenario['scenario_id']}").json()
    assert detail["sweeps"][-1]["sweep_id"] == body["sweep_id"]
    assert detail["sweeps"][-1]["run_ids"] == [j["run_id"] for j in body["jobs"]]

    # Unknown scenario -> 404.
    resp = client.post(
        "/api/scenarios/sensitivity",
        json={
            "as_of_date": "2025-01-17",
            "scenario_id": "nope",
            "param_path": "decay_days",
            "values": [30],
        },
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Invalidation report
# ---------------------------------------------------------------------------


def test_invalidation_report_position_weights() -> None:
    base = Methodology()
    changed = Methodology(position_weights=PositionWeights(buy=0.7))
    report = scenario_service.invalidation_report(base, changed)

    assert report["identical"] is False
    paths = {c["path"] for c in report["changed_fields"]}
    assert "position_weights.buy" in paths

    by_stage = {s["stage"]: s for s in report["stages"]}
    # position_weights is consumed by EXPOSURES; downstream stages inherit
    # the changed upstream fingerprint.
    assert by_stage["exposures"]["fields_changed"] == ["position_weights"]
    assert by_stage["exposures"]["fingerprint_changes"] is True
    assert by_stage["metrics"]["fields_changed"] == []
    assert by_stage["metrics"]["fingerprint_changes"] is True
    assert by_stage["statistics"]["fingerprint_changes"] is True
    assert by_stage["intervals"]["fingerprint_changes"] is False
    assert by_stage["returns"]["fingerprint_changes"] is False
    assert report["affected_stages"] == ["exposures", "metrics", "statistics"]
    assert report["first_affected_stage"] == "exposures"


def test_invalidation_report_statistics_only_and_identical() -> None:
    base = Methodology()
    changed = Methodology(bootstrap_seed=99)
    report = scenario_service.invalidation_report(base, changed)
    assert report["affected_stages"] == ["statistics"]
    assert report["first_affected_stage"] == "statistics"

    same = scenario_service.invalidation_report(base, Methodology())
    assert same["identical"] is True
    assert same["changed_fields"] == []
    assert same["affected_stages"] == []
    assert same["first_affected_stage"] is None
    assert all(not s["fingerprint_changes"] for s in same["stages"])


def test_invalidation_report_first_stage_change_flags_all() -> None:
    base = Methodology()
    changed = Methodology(entry_convention="same_close")
    report = scenario_service.invalidation_report(base, changed)
    assert report["affected_stages"] == [
        "intervals",
        "returns",
        "exposures",
        "metrics",
        "statistics",
    ]


# ---------------------------------------------------------------------------
# Scenario compare endpoint (uses recorded runs)
# ---------------------------------------------------------------------------


def test_scenario_compare_endpoint(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, seed=True)
    client, app = _client(db_path, tmp_path / "cache")

    # Scenario A: default; scenario B: cost override.
    scen_a = client.post("/api/scenarios", json={"name": "a"}).json()
    scen_b = client.post(
        "/api/scenarios",
        json={"name": "b", "overrides": {"transaction_cost_bps": 5.0}},
    ).json()
    for sid in (scen_a["scenario_id"], scen_b["scenario_id"]):
        resp = client.post(f"/api/scenarios/{sid}/execute", json={"as_of_date": "2025-01-17"})
        assert resp.status_code == 202
    coord = _coordinator(app)
    coord.run_once()
    coord.run_once()

    # Explicit other_run_id.
    run_a = client.get(f"/api/scenarios/{scen_a['scenario_id']}").json()["last_run_id"]
    run_b = client.get(f"/api/scenarios/{scen_b['scenario_id']}").json()["last_run_id"]
    resp = client.get(f"/api/scenarios/{scen_a['scenario_id']}/compare?other_run_id={run_b}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_a_id"] == run_a and body["run_b_id"] == run_b
    assert body["universe"]["compatible"] is True

    # No runs recorded -> 422.
    empty = client.post("/api/scenarios", json={"name": "empty"}).json()
    resp = client.get(f"/api/scenarios/{empty['scenario_id']}/compare")
    assert resp.status_code == 422

    # Missing scenario -> 404.
    assert client.get("/api/scenarios/nope/compare").status_code == 404


def test_json_roundtrip_of_compare(tmp_path: Path) -> None:
    """The compare report must be JSON-serializable end to end."""
    db_path = tmp_path / "callscore.db"
    engine = create_engine_for(db_path)
    bootstrap(engine)
    _seed_analysts(engine)
    _insert_run(engine, "run-a", Methodology())
    _insert_run(engine, "run-b", Methodology(transaction_cost_bps=1.0))
    _insert_composites(engine, "run-a", [_composite("A1", 1.0, 1, is_ranked=True)])
    report = scenario_service.compare_runs(engine, "run-a", "run-b")
    json.dumps(report)  # must not raise
    engine.dispose()
