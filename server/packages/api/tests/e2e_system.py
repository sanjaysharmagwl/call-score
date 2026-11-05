"""System-level correctness suite (T25): reproducibility, provenance,
cold-vs-warm stage cache, concurrent submits, and restart recovery.

These tests exercise the *real* product surface — the ``callscore-synth``
and ``callscore`` CLIs plus the API services — against fresh temporary
SQLite databases. They never touch ``data/db/callscore.db``.

Location note: this file lives in ``packages/api/tests`` so the standard
``just server-test api`` / ``uv run pytest packages`` runs pick it up; it is
self-contained (no conftest fixtures) and safe to run alone via::

    cd server && uv run pytest packages/api/tests/e2e_system.py -q
"""

import hashlib
import io
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

import callscore_api.cli as api_cli
from callscore_api import create_app, errors
from callscore_api.services import calculation, jobs
from callscore_api.services.cache import StageCache
from callscore_api.settings import Settings
from callscore_core.contracts import RunContext
from callscore_core.enums import RunStatus, Stage
from callscore_core.methodology import Methodology
from callscore_engine.runner import SnapshotFrames
from callscore_store import create_engine_for
from callscore_store.models import CalcRun, MethodologyVersion
from callscore_store.repositories import runs as runs_repo
from callscore_store.snapshots import load_input_snapshot
from callscore_synth import cli as synth_cli

AS_OF = "2025-06-30"

# Derived tables published per run; compared as sorted multisets so row order
# never matters. ``calc_run`` itself is excluded (timestamps differ).
_DERIVED_TABLES = (
    "analyst_composite",
    "rating_interval",
    "interval_return",
    "interval_exposure",
    "interval_contribution",
    "analyst_metric",
    "target_outcome",
    "event_window",
    "portfolio_nav",
    "portfolio_position",
)


# ---------------------------------------------------------------------------
# Helpers: real CLIs on fresh tmp DBs
# ---------------------------------------------------------------------------


def _synth_db(db_path: Path, *, seed: int) -> Path:
    """Seed a fresh small database via the real ``callscore-synth`` CLI."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = synth_cli.main(
            ["--scale", "small", "--seed", str(seed), "--out", str(db_path), "--force"]
        )
    assert code == 0, f"callscore-synth failed:\n{out.getvalue()}\n{err.getvalue()}"
    return db_path


def _cli_run(db_path: Path, *, as_of: str = AS_OF) -> str:
    """Execute a run via the real ``callscore run`` CLI; returns run_id."""
    out, err = io.StringIO(), io.StringIO()
    code = api_cli.main(
        ["run", "--db", str(db_path), "--methodology", "default", "--as-of", as_of],
        out=out,
        err=err,
    )
    assert code == 0, f"callscore run failed:\n{out.getvalue()}\n{err.getvalue()}"
    match = re.search(r"run_id=(\S+)", out.getvalue())
    assert match is not None
    return match.group(1)


def _derived_fingerprint(db_path: Path) -> str:
    """Content hash over every published derived table (sorted row multiset)."""
    digest = hashlib.sha256()
    conn = sqlite3.connect(db_path)
    try:
        for table in _DERIVED_TABLES:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            digest.update(table.encode())
            for row in sorted(rows, key=repr):
                digest.update(repr(row).encode())
    finally:
        conn.close()
    return digest.hexdigest()


def _api_client(db_path: Path, cache_dir: Path) -> tuple[TestClient, FastAPI]:
    app = create_app()
    app.state.settings = Settings(db_path=db_path, cache_dir=cache_dir)
    errors.register_error_handlers(app)
    return TestClient(app), app


# ---------------------------------------------------------------------------
# 1. Reproducibility: same seed -> identical published results
# ---------------------------------------------------------------------------


def test_reproducibility_two_independently_seeded_dbs(tmp_path: Path) -> None:
    db_a = _synth_db(tmp_path / "a" / "callscore.db", seed=11)
    db_b = _synth_db(tmp_path / "b" / "callscore.db", seed=11)
    run_a = _cli_run(db_a)
    run_b = _cli_run(db_b)

    # Same inputs -> same deterministic run id and identical published frames.
    assert run_a == run_b
    assert _derived_fingerprint(db_a) == _derived_fingerprint(db_b)


def test_different_seed_produces_different_data(tmp_path: Path) -> None:
    """Guard against a vacuous reproducibility test: distinct seeds differ."""
    db_a = _synth_db(tmp_path / "a" / "callscore.db", seed=11)
    db_b = _synth_db(tmp_path / "b" / "callscore.db", seed=12)
    _cli_run(db_a)
    _cli_run(db_b)
    assert _derived_fingerprint(db_a) != _derived_fingerprint(db_b)


# ---------------------------------------------------------------------------
# 2. Provenance: run row carries fingerprints + stage artifacts
# ---------------------------------------------------------------------------


def test_run_provenance(tmp_path: Path) -> None:
    db_path = _synth_db(tmp_path / "callscore.db", seed=21)
    run_id = _cli_run(db_path)

    engine = create_engine_for(db_path)
    try:
        run = runs_repo.get_calc_run(engine, run_id)
        assert run is not None
        assert run["status"] == RunStatus.SUCCEEDED.value
        assert re.fullmatch(r"[0-9a-f]{64}", str(run["source_fingerprint"]))

        with engine.connect() as conn:
            fingerprint = conn.execute(
                select(MethodologyVersion.fingerprint)
                .join(CalcRun, CalcRun.methodology_version_id == MethodologyVersion.id)
                .where(CalcRun.run_id == run_id)
            ).scalar_one()
        assert re.fullmatch(r"[0-9a-f]{64}", str(fingerprint))
        # The run's methodology_version_id resolves to that fingerprint.
        row_counts = json.loads(run["row_counts_json"] or "{}")
        assert "stage_artifacts" in row_counts
    finally:
        engine.dispose()

    # API detail exposes methodology fingerprint + non-empty stage artifacts.
    client, _app = _api_client(db_path, tmp_path / "cache")
    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert re.fullmatch(r"[0-9a-f]{64}", body["methodology_fingerprint"])
    artifacts = body["stage_artifacts"]
    assert isinstance(artifacts, list) and len(artifacts) >= 1
    for artifact in artifacts:
        assert artifact["stage"]
        assert re.fullmatch(r"[0-9a-f]{64}", artifact["content_fingerprint"])

    # The CLI leaderboard prints the run's provenance header.
    out = io.StringIO()
    code = api_cli.main(
        ["leaderboard", "--db", str(db_path), "--run", run_id, "--limit", "3"],
        out=out,
        err=io.StringIO(),
    )
    assert code == 0
    assert run_id in out.getvalue()


# ---------------------------------------------------------------------------
# 3. Cold vs warm stage cache -> identical published frames
# ---------------------------------------------------------------------------


def _warm_cache(db_path: Path, cache_dir: Path) -> None:
    """Populate the StageCache exactly as a real run would (same context)."""
    as_of = date.fromisoformat(AS_OF)
    cutoff = calculation.default_knowledge_cutoff(as_of)
    engine = create_engine_for(db_path)
    try:
        snapshot = load_input_snapshot(engine, as_of, cutoff)
        context = RunContext(
            as_of_date=as_of,
            knowledge_cutoff=cutoff,
            source_revision="callscore-store",
            source_fingerprint=snapshot.source_fingerprint,
            universe_fingerprint=snapshot.universe_fingerprint,
            algorithm_version=calculation.ALGORITHM_VERSION,
        )
        frames = SnapshotFrames.from_mapping(snapshot.frames)
        jobs.run_pipeline_cached(frames, Methodology(), context, StageCache(cache_dir))
    finally:
        engine.dispose()


def _coordinator_run(db_path: Path, cache_dir: Path) -> str:
    """Submit + execute one run through JobCoordinator (the cached path)."""
    engine = create_engine_for(db_path)
    try:
        coord = jobs.JobCoordinator(engine, cache_dir=cache_dir, poll_interval=0.02)
        submitted = coord.submit(methodology=Methodology(), as_of_date=date.fromisoformat(AS_OF))
        assert coord.run_once() == submitted.run_id
        run = runs_repo.get_calc_run(engine, submitted.run_id)
        assert run is not None and run["status"] == RunStatus.SUCCEEDED.value
        return submitted.run_id
    finally:
        engine.dispose()


def test_cold_vs_warm_cache_identical_published_frames(tmp_path: Path) -> None:
    db_cold = _synth_db(tmp_path / "cold" / "callscore.db", seed=31)
    db_warm = _synth_db(tmp_path / "warm" / "callscore.db", seed=31)

    run_cold = _coordinator_run(db_cold, db_cold.parent / "cache")

    # Pre-warm the second DB's cache, then run through the same coordinator.
    warm_cache_dir = db_warm.parent / "cache"
    _warm_cache(db_warm, warm_cache_dir)
    assert {p.name for p in warm_cache_dir.iterdir()} == {s.value for s in Stage}
    run_warm = _coordinator_run(db_warm, warm_cache_dir)

    assert run_cold == run_warm  # deterministic run id
    assert _derived_fingerprint(db_cold) == _derived_fingerprint(db_warm)


# ---------------------------------------------------------------------------
# 4. Concurrent submits: idempotent same-key, serialized different-key
# ---------------------------------------------------------------------------


def test_concurrent_submits_same_params_one_run(tmp_path: Path) -> None:
    db_path = _synth_db(tmp_path / "callscore.db", seed=41)
    client, _app = _api_client(db_path, tmp_path / "cache")
    payload = {"as_of_date": AS_OF, "methodology": "default"}

    def post() -> tuple[int, str, bool]:
        resp = client.post("/api/runs", json=payload)
        body = resp.json()
        return resp.status_code, body["run_id"], bool(body["created"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _i: post(), range(12)))

    assert {code for code, _rid, _c in results} == {202}
    run_ids = {rid for _c, rid, _cr in results}
    assert len(run_ids) == 1
    # Exactly one submitter observed `created`; the rest were idempotent hits.
    assert sum(1 for _c, _r, created in results if created) == 1

    engine = create_engine_for(db_path)
    try:
        with engine.connect() as conn:
            count = conn.execute(select(CalcRun.__table__.c.run_id)).all()
        assert len(count) == 1
    finally:
        engine.dispose()


def test_concurrent_submits_different_params_both_queued(tmp_path: Path) -> None:
    db_path = _synth_db(tmp_path / "callscore.db", seed=42)
    client, _app = _api_client(db_path, tmp_path / "cache")

    payloads = [
        {"as_of_date": AS_OF, "methodology": "default"},
        {"as_of_date": AS_OF, "methodology": {"transaction_cost_bps": 5.0}},
    ]

    def post(payload: dict[str, object]) -> tuple[int, str]:
        resp = client.post("/api/runs", json=payload)
        assert resp.status_code == 202
        return resp.json()["run_id"], resp.json()["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(post, payloads * 3))

    run_ids = {rid for rid, _s in results}
    assert len(run_ids) == 2  # two distinct canonical keys
    # The coordinator is not running under a bare TestClient: both stay queued.
    for rid in run_ids:
        detail = client.get(f"/api/runs/{rid}")
        assert detail.status_code == 200
        assert detail.json()["status"] == RunStatus.QUEUED.value


# ---------------------------------------------------------------------------
# 5. Restart recovery: orphaned running -> interrupted -> retry succeeds
# ---------------------------------------------------------------------------


def test_restart_recovery_marks_interrupted_and_retry_succeeds(tmp_path: Path) -> None:
    db_path = _synth_db(tmp_path / "callscore.db", seed=51)
    client, _app = _api_client(db_path, tmp_path / "cache")

    resp = client.post("/api/runs", json={"as_of_date": AS_OF, "methodology": "default"})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    engine = create_engine_for(db_path)
    # Simulate a crash mid-run: queued -> running, then process "restarts".
    runs_repo.update_run_status(engine, run_id, RunStatus.RUNNING)

    # JobCoordinator.start() runs recover_interrupted_runs on boot.
    coord = jobs.JobCoordinator(engine, cache_dir=tmp_path / "cache", poll_interval=0.02)
    coord.start()
    coord.stop()
    coord.close()

    run = runs_repo.get_calc_run(engine, run_id)
    assert run is not None and run["status"] == RunStatus.INTERRUPTED.value

    # Retry via the API re-queues; a fresh coordinator executes it.
    retry = client.post(f"/api/runs/{run_id}/retry")
    assert retry.status_code == 200
    assert retry.json()["status"] == RunStatus.QUEUED.value

    coord2 = jobs.JobCoordinator(engine, cache_dir=tmp_path / "cache", poll_interval=0.02)
    try:
        assert coord2.run_once() == run_id
    finally:
        coord2.close()
        engine.dispose()

    check = create_engine_for(db_path)
    try:
        run = runs_repo.get_calc_run(check, run_id)
        assert run is not None and run["status"] == RunStatus.SUCCEEDED.value
    finally:
        check.dispose()
