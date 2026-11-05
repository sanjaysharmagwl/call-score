"""Tests for the methodology registry endpoints (``/api/methodologies``)."""

from pathlib import Path

from fastapi.testclient import TestClient

from callscore_api import create_app, errors
from callscore_api.routers import methodologies as methodologies_router
from callscore_api.settings import Settings
from callscore_core.methodology import Methodology
from callscore_store import create_engine_for
from callscore_store.schema import bootstrap


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "callscore.db"
    engine = create_engine_for(db_path)
    bootstrap(engine)
    engine.dispose()
    return db_path


def _client(db_path: Path, cache_dir: Path) -> TestClient:
    app = create_app()
    app.state.settings = Settings(db_path=db_path, cache_dir=cache_dir)
    app.include_router(methodologies_router.router, prefix="/api")
    errors.register_error_handlers(app)
    return TestClient(app)


def _post(client: TestClient, methodology: dict[str, object], **extra: object):
    return client.post("/api/methodologies", json={"methodology": methodology, **extra})


def test_list_empty(tmp_path: Path) -> None:
    client = _client(_make_db(tmp_path), tmp_path / "cache")
    resp = client.get("/api/methodologies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["pagination"]["total"] == 0


def test_create_and_get_by_fingerprint(tmp_path: Path) -> None:
    client = _client(_make_db(tmp_path), tmp_path / "cache")
    payload = {"transaction_cost_bps": 5.0, "decay_days": 30}
    expected_fp = Methodology.model_validate(payload).fingerprint()

    resp = _post(client, payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["fingerprint"] == expected_fp
    assert body["is_baseline"] is False
    version_id = body["id"]
    assert body["params"]["transaction_cost_bps"] == 5.0

    got = client.get(f"/api/methodologies/{expected_fp}")
    assert got.status_code == 200
    assert got.json()["id"] == version_id
    assert got.json()["params"]["decay_days"] == 30

    missing = client.get("/api/methodologies/" + "0" * 64)
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


def test_create_idempotent_no_duplicate(tmp_path: Path) -> None:
    client = _client(_make_db(tmp_path), tmp_path / "cache")
    payload = {"confidence_level": 0.9}
    first = _post(client, payload)
    second = _post(client, payload)
    assert first.status_code == 201
    assert second.status_code == 200  # existing row returned
    assert first.json()["id"] == second.json()["id"]
    listing = client.get("/api/methodologies").json()
    assert listing["pagination"]["total"] == 1


def test_create_invalid_methodology_422(tmp_path: Path) -> None:
    client = _client(_make_db(tmp_path), tmp_path / "cache")
    # Family weights must sum to one -> model_validator failure.
    resp = _post(client, {"family_weights": {"return_skill": 0.9}})
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"
    # Unknown top-level field.
    resp = _post(client, {"not_a_field": 1})
    assert resp.status_code == 422
    # Missing required wrapper field.
    resp = client.post("/api/methodologies", json={})
    assert resp.status_code == 422


def test_baseline_immutability(tmp_path: Path) -> None:
    client = _client(_make_db(tmp_path), tmp_path / "cache")
    m1 = {"transaction_cost_bps": 1.0}
    m2 = {"transaction_cost_bps": 2.0}

    first = _post(client, m1, is_baseline=True)
    assert first.status_code == 201
    assert first.json()["is_baseline"] is True

    # A second, different baseline is refused — never silently replaced.
    second = _post(client, m2, is_baseline=True)
    assert second.status_code == 409
    assert second.json()["code"] == "baseline_exists"

    # Re-posting the same baseline methodology is idempotent.
    again = _post(client, m1, is_baseline=True)
    assert again.status_code == 200
    assert again.json()["is_baseline"] is True

    # The same fingerprint WITHOUT is_baseline is also fine (no mutation).
    plain = _post(client, m1)
    assert plain.status_code == 200

    # And m2 can still be stored as a non-baseline version.
    non_baseline = _post(client, m2)
    assert non_baseline.status_code == 201
    assert non_baseline.json()["is_baseline"] is False

    listing = client.get("/api/methodologies").json()
    assert listing["pagination"]["total"] == 2
    baselines = [v for v in listing["data"] if v["is_baseline"]]
    assert len(baselines) == 1
    assert baselines[0]["fingerprint"] == Methodology.model_validate(m1).fingerprint()


def test_diff_identical_and_changed(tmp_path: Path) -> None:
    client = _client(_make_db(tmp_path), tmp_path / "cache")
    m1 = {"transaction_cost_bps": 0.0, "decay_days": 90}
    m2 = {"transaction_cost_bps": 5.0, "decay_days": 90, "bootstrap_draws": 500}
    fp1 = _post(client, m1).json()["fingerprint"]
    fp2 = _post(client, m2).json()["fingerprint"]

    same = client.get(f"/api/methodologies/diff?a={fp1}&b={fp1}").json()
    assert same["identical"] is True
    assert same["changes"] == []

    diff = client.get(f"/api/methodologies/diff?a={fp1}&b={fp2}").json()
    assert diff["identical"] is False
    paths = {c["path"]: c for c in diff["changes"]}
    assert paths["transaction_cost_bps"]["a"] == 0.0
    assert paths["transaction_cost_bps"]["b"] == 5.0
    assert paths["bootstrap_draws"]["a"] == 1000
    assert paths["bootstrap_draws"]["b"] == 500
    # Untouched fields produce no changes.
    assert "decay_days" not in paths


def test_diff_missing_fingerprint_404(tmp_path: Path) -> None:
    client = _client(_make_db(tmp_path), tmp_path / "cache")
    fp = _post(client, {"decay_days": 45}).json()["fingerprint"]
    resp = client.get(f"/api/methodologies/diff?a={fp}&b={'0' * 64}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_methodology_pagination_bounds(tmp_path: Path) -> None:
    client = _client(_make_db(tmp_path), tmp_path / "cache")
    for i in range(3):
        assert _post(client, {"decay_days": 30 + i}).status_code == 201
    page = client.get("/api/methodologies?limit=2&offset=1").json()
    assert page["pagination"]["total"] == 3
    assert page["pagination"]["count"] == 2
    assert client.get("/api/methodologies?limit=501").status_code == 422
