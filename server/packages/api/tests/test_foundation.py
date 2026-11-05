"""Tests for the API foundation: settings, error handlers, meta endpoints,
and the readiness probe."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from callscore_api import create_app, errors
from callscore_api.routers import meta as meta_router
from callscore_api.services.jobs import JobCoordinator
from callscore_api.settings import Settings, SettingsError
from callscore_core.methodology import Methodology
from callscore_core.metrics import METRIC_CATALOG_FINGERPRINT
from callscore_store import create_engine_for
from callscore_store.repositories.runs import RunTransitionError
from callscore_store.schema import SCHEMA_VERSION, bootstrap


def _settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "callscore.db", cache_dir=tmp_path / "cache")


def _app(tmp_path: Path, *, routers: bool = True) -> FastAPI:
    app = create_app()
    app.state.settings = _settings(tmp_path)
    if routers:
        app.include_router(meta_router.router, prefix="/api")
    errors.register_error_handlers(app)
    return app


# ---------------------------------------------------------------------------
# settings.py
# ---------------------------------------------------------------------------


def test_settings_defaults_are_repo_relative() -> None:
    settings = Settings.from_env({})
    assert settings.db_path.name == "callscore.db"
    assert settings.db_path.is_absolute()
    # Default cache dir follows the db's parent directory.
    assert settings.cache_dir == settings.db_path.parent / "cache"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000


def test_settings_from_env_overrides(tmp_path: Path) -> None:
    env = {
        "CALLSCORE_DB_PATH": str(tmp_path / "nested" / "x.db"),
        "CALLSCORE_CACHE_DIR": str(tmp_path / "c"),
        "CALLSCORE_HOST": "0.0.0.0",
        "CALLSCORE_PORT": "9000",
        "CALLSCORE_JOB_LEASE_TTL_SECONDS": "42",
    }
    settings = Settings.from_env(env)
    assert settings.db_path == tmp_path / "nested" / "x.db"
    assert settings.cache_dir == tmp_path / "c"
    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.job_lease_ttl_seconds == 42.0
    # ensure_ready created both directories.
    assert (tmp_path / "nested").is_dir()
    assert (tmp_path / "c").is_dir()


def test_settings_validation_rejects_bad_values(tmp_path: Path) -> None:
    with pytest.raises(SettingsError):
        Settings(
            db_path=tmp_path / "x.db",
            cache_dir=tmp_path / "c",
            port=0,
        ).ensure_ready()
    with pytest.raises(SettingsError):
        Settings(
            db_path=tmp_path / "x.db",
            cache_dir=tmp_path / "c",
            job_lease_ttl_seconds=0.0,
        ).ensure_ready()


# ---------------------------------------------------------------------------
# errors.py
# ---------------------------------------------------------------------------


def test_error_handlers_map_domain_errors(tmp_path: Path) -> None:
    app = _app(tmp_path, routers=False)

    @app.get("/api/boom/key")
    def key() -> None:
        raise KeyError("missing thing")

    @app.get("/api/boom/value")
    def value() -> None:
        raise ValueError("bad value")

    @app.get("/api/boom/transition")
    def transition() -> None:
        raise RunTransitionError("illegal move")

    @app.get("/api/boom/unhandled")
    def unhandled() -> None:
        raise RuntimeError("secret internals xyz")

    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/api/boom/key")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    assert "missing thing" in resp.json()["detail"]

    resp = client.get("/api/boom/value")
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"

    resp = client.get("/api/boom/transition")
    assert resp.status_code == 409
    assert resp.json()["code"] == "invalid_transition"

    resp = client.get("/api/boom/unhandled")
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "internal_error"
    assert "secret internals" not in resp.text


def test_request_validation_error_is_422(tmp_path: Path) -> None:
    from callscore_api.routers import runs as runs_router

    app = create_app()
    app.state.settings = _settings(tmp_path)
    app.include_router(runs_router.router, prefix="/api")
    errors.register_error_handlers(app)
    client = TestClient(app)
    resp = client.post("/api/runs", json={"methodology": "default"})  # missing as_of_date
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert body["errors"]


# ---------------------------------------------------------------------------
# meta endpoints
# ---------------------------------------------------------------------------


def test_meta_enums(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    resp = client.get("/api/meta/enums")
    assert resp.status_code == 200
    enums = resp.json()["enums"]
    assert enums["RunStatus"] == ["queued", "running", "succeeded", "failed", "interrupted"]
    assert "strong_buy" in enums["CanonicalRating"]
    assert "next_open" in enums["EntryConvention"]
    # Every core enum is present.
    from enum import StrEnum

    from callscore_core import enums as core_enums

    for name, obj in vars(core_enums).items():
        if (
            name.startswith("_")
            or obj is StrEnum
            or not (isinstance(obj, type) and issubclass(obj, StrEnum))
        ):
            continue
        assert name in enums, name


def test_meta_metrics_catalog(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    resp = client.get("/api/meta/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fingerprint"] == METRIC_CATALOG_FINGERPRINT
    by_code = {m["code"]: m for m in body["metrics"]}
    assert "mean_contribution" in by_code
    entry = by_code["mean_contribution"]
    assert entry["min_obs"] == 1
    assert entry["scored"] is True
    assert entry["contributes_to_composite"] is True
    assert entry["family"] == "return_skill"
    # Descriptive, non-bootstrapped metric: not scored, not in composite.
    descriptive = by_code["max_drawdown"]
    assert descriptive["scored"] is False
    assert descriptive["contributes_to_composite"] is False
    assert body["composite_spec"]["z_clip"] == 3.0


def test_meta_methodology_defaults(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    resp = client.get("/api/meta/methodology-defaults")
    assert resp.status_code == 200
    body = resp.json()
    assert body["presets"] == ["default"]
    assert body["fingerprint"] == Methodology().fingerprint()
    assert body["methodology"]["contract_version"] == 1
    assert body["methodology"]["benchmark_mode"] == "sector"


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------


def test_ready_on_fresh_bootstrapped_db(tmp_path: Path) -> None:
    db_path = tmp_path / "callscore.db"
    engine = create_engine_for(db_path)
    bootstrap(engine)
    engine.dispose()

    client = TestClient(_app(tmp_path))
    resp = client.get("/api/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["expected_schema_version"] == SCHEMA_VERSION
    assert body["row_counts"]["calc_run"] == 0
    assert body["row_counts"]["methodology_version"] == 0
    assert body["last_successful_run"] is None
    names = {c["name"] for c in body["checks"]}
    assert {"database", "schema_version"} <= names


def test_ready_reports_last_successful_run(tmp_path: Path) -> None:
    db_path = tmp_path / "callscore.db"
    engine = create_engine_for(db_path)
    bootstrap(engine)
    # Register a methodology + a succeeded run directly (no engine needed).
    from datetime import UTC, date, datetime

    from callscore_core.enums import RunStatus
    from callscore_store.repositories import runs as runs_repo

    mv_id = runs_repo.register_methodology_version(engine, Methodology())
    runs_repo.create_calc_run(
        engine,
        run_id="run-x",
        methodology_version_id=mv_id,
        as_of_date=date(2025, 1, 17),
        knowledge_cutoff=datetime(2025, 1, 18, tzinfo=UTC),
        algorithm_version="test",
    )
    runs_repo.update_run_status(engine, "run-x", RunStatus.RUNNING)
    runs_repo.update_run_status(engine, "run-x", RunStatus.SUCCEEDED)
    engine.dispose()

    client = TestClient(_app(tmp_path))
    body = client.get("/api/ready").json()
    assert body["status"] == "ready"
    assert body["last_successful_run"]["run_id"] == "run-x"
    assert body["last_successful_run"]["as_of_date"] == "2025-01-17"
    assert body["row_counts"]["calc_run"] == 1


def test_job_coordinator_dep_is_app_singleton(tmp_path: Path) -> None:
    """get_job_coordinator caches one coordinator on app.state."""
    app = _app(tmp_path)
    # Trigger creation through any DB-backed route is not possible with only
    # meta mounted; call the dependency machinery directly instead.
    from callscore_api import deps

    class _Req:
        def __init__(self, a: FastAPI) -> None:
            self.app = a

    coord1 = deps.get_job_coordinator(_Req(app))  # type: ignore[arg-type]
    coord2 = deps.get_job_coordinator(_Req(app))  # type: ignore[arg-type]
    assert coord1 is coord2
    assert isinstance(coord1, JobCoordinator)
    assert coord1.engine is not None
