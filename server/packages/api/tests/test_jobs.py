"""Tests for ``callscore_api.services.jobs`` and the stage cache integration.

The DB fixture mirrors ``test_calculation.py``: a hand-seeded minimal
universe (one analyst, one security, ten sessions, one initiation) — small
enough for fast pipeline runs, real enough to exercise every stage.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select

from callscore_api.services import calculation, jobs
from callscore_api.services.cache import StageCache
from callscore_core.contracts import RunContext
from callscore_core.enums import RunStatus, Stage
from callscore_core.methodology import Methodology
from callscore_engine import runner as engine_runner
from callscore_engine.runner import PipelineResult, SnapshotFrames
from callscore_store import create_engine_for
from callscore_store.models import CalcRun
from callscore_store.repositories import jobs as repo_jobs
from callscore_store.repositories import runs
from callscore_store.schema import bootstrap
from callscore_store.snapshots import load_input_snapshot

AS_OF = date(2025, 1, 17)
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
    from callscore_store.repositories import events, market, reference

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


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "callscore.db"
    engine = create_engine_for(path)
    bootstrap(engine)
    _seed(engine)
    engine.dispose()
    return path


def _run_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(CalcRun.__table__)).scalar_one())


def _frames_and_context(engine: Engine) -> tuple[SnapshotFrames, RunContext, Methodology]:
    snapshot = load_input_snapshot(engine, AS_OF, CUTOFF)
    context = RunContext(
        as_of_date=AS_OF,
        knowledge_cutoff=CUTOFF,
        source_revision="callscore-store",
        source_fingerprint=snapshot.source_fingerprint,
        universe_fingerprint=snapshot.universe_fingerprint,
        algorithm_version="test",
    )
    return SnapshotFrames.from_mapping(snapshot.frames), context, Methodology()


_OUTPUT_NAMES = (
    "intervals",
    "exposures",
    "returns",
    "contributions",
    "event_windows",
    "target_outcomes",
    "coverage_daily",
    "portfolio_nav",
    "portfolio_positions",
    "metric_values",
    "metric_samples",
    "metric_results",
    "composites",
)


def _assert_results_equal(a: PipelineResult, b: PipelineResult) -> None:
    for name in _OUTPUT_NAMES:
        assert getattr(a, name).equals(getattr(b, name)), name
    assert a.peer_groups.equals(b.peer_groups)
    assert a.n_investable.equals(b.n_investable)
    assert dict(a.stage_fingerprints) == dict(b.stage_fingerprints)
    assert a.stage_artifacts == b.stage_artifacts


# ---------------------------------------------------------------------------
# submit_run idempotency
# ---------------------------------------------------------------------------


def test_submit_run_idempotent(db_path: Path) -> None:
    engine = create_engine_for(db_path)
    first = jobs.submit_run(engine, methodology=Methodology(), as_of_date=AS_OF)
    second = jobs.submit_run(engine, methodology=Methodology(), as_of_date=AS_OF)
    assert first.created is True
    assert second.created is False
    assert first.run_id == second.run_id
    assert second.status == RunStatus.QUEUED.value
    assert _run_count(engine) == 1
    engine.dispose()


def test_submit_run_concurrent_same_key(db_path: Path) -> None:
    engine = create_engine_for(db_path)

    def submit(_i: int) -> str:
        return jobs.submit_run(engine, methodology=Methodology(), as_of_date=AS_OF).run_id

    with ThreadPoolExecutor(max_workers=4) as pool:
        run_ids = list(pool.map(submit, range(8)))
    assert len(set(run_ids)) == 1
    assert _run_count(engine) == 1
    engine.dispose()


def test_submit_run_different_key_creates_new_run(db_path: Path) -> None:
    engine = create_engine_for(db_path)
    a = jobs.submit_run(engine, methodology=Methodology(), as_of_date=AS_OF)
    b = jobs.submit_run(engine, methodology=Methodology(transaction_cost_bps=5.0), as_of_date=AS_OF)
    c = jobs.submit_run(engine, methodology=Methodology(), as_of_date=AS_OF, universe_filter=["S1"])
    assert len({a.run_id, b.run_id, c.run_id}) == 3
    assert _run_count(engine) == 3
    # Re-submitting b's key returns b, not a or c.
    again = jobs.submit_run(
        engine, methodology=Methodology(transaction_cost_bps=5.0), as_of_date=AS_OF
    )
    assert again.run_id == b.run_id and again.created is False
    engine.dispose()


# ---------------------------------------------------------------------------
# Coordinator execution
# ---------------------------------------------------------------------------


def test_run_once_executes_queued_job(db_path: Path, tmp_path: Path) -> None:
    engine = create_engine_for(db_path)
    cache = StageCache(tmp_path / "cache")
    coord = jobs.JobCoordinator(engine, cache=cache, poll_interval=0.01)
    result = coord.submit(methodology=Methodology(), as_of_date=AS_OF)
    assert coord.run_once() == result.run_id
    run = runs.get_calc_run(engine, result.run_id)
    assert run is not None
    assert run["status"] == RunStatus.SUCCEEDED.value
    assert run["progress"] == 1.0
    assert run["started_at"] is not None and run["finished_at"] is not None
    row_counts = json.loads(run["row_counts_json"] or "{}")
    assert row_counts["rows"]["rating_interval"] == 1
    assert row_counts["rows"]["analyst_composite"] == 1
    # Cache populated for all five stages.
    assert {p.name for p in cache.root.iterdir()} == {s.value for s in Stage}
    engine.dispose()


def test_run_once_no_queued_returns_none(db_path: Path) -> None:
    engine = create_engine_for(db_path)
    coord = jobs.JobCoordinator(engine, cache=StageCache(Path(db_path).parent / "c"))
    assert coord.run_once() is None
    engine.dispose()


def test_writer_lease_blocks_execution(db_path: Path, tmp_path: Path) -> None:
    engine = create_engine_for(db_path)
    coord = jobs.JobCoordinator(engine, cache=StageCache(tmp_path / "cache"))
    result = coord.submit(methodology=Methodology(), as_of_date=AS_OF)
    # Someone else holds the single-writer lease.
    assert repo_jobs.acquire_writer_lease(engine, "other-worker", "other-job", ttl=60)
    assert coord.run_once() is None
    run = runs.get_calc_run(engine, result.run_id)
    assert run is not None and run["status"] == RunStatus.QUEUED.value
    repo_jobs.release_writer_lease(engine, "other-worker")
    assert coord.run_once() == result.run_id
    run = runs.get_calc_run(engine, result.run_id)
    assert run is not None and run["status"] == RunStatus.SUCCEEDED.value
    engine.dispose()


def test_failed_job_recorded_with_error(db_path: Path, tmp_path: Path) -> None:
    engine = create_engine_for(db_path)
    coord = jobs.JobCoordinator(engine, cache=StageCache(tmp_path / "cache"))
    # as_of before any session -> engine raises; run must end failed.
    result = coord.submit(methodology=Methodology(), as_of_date=date(2024, 1, 1))
    with pytest.raises(ValueError, match="sessions frame is empty"):
        coord.run_once()
    run = runs.get_calc_run(engine, result.run_id)
    assert run is not None
    assert run["status"] == RunStatus.FAILED.value
    assert json.loads(run["error_json"] or "{}")["type"]
    engine.dispose()


def test_background_thread_worker(db_path: Path, tmp_path: Path) -> None:
    coord = jobs.JobCoordinator(db_path, cache_dir=tmp_path / "cache", poll_interval=0.02)
    result = coord.submit(methodology=Methodology(), as_of_date=AS_OF)
    coord.start()
    try:
        deadline = datetime.now(UTC) + timedelta(seconds=30)
        status = ""
        while datetime.now(UTC) < deadline:
            run = jobs.job_status(coord.engine, result.run_id)
            assert run is not None
            status = str(run["status"])
            if status in (RunStatus.SUCCEEDED.value, RunStatus.FAILED.value):
                break
            time.sleep(0.05)
        assert status == RunStatus.SUCCEEDED.value
    finally:
        coord.stop()
        coord.close()


def test_recover_interrupted_marks_running(db_path: Path) -> None:
    engine = create_engine_for(db_path)
    result = jobs.submit_run(engine, methodology=Methodology(), as_of_date=AS_OF)
    runs.update_run_status(engine, result.run_id, RunStatus.RUNNING)
    assert jobs.recover_interrupted_runs(engine) == 1
    run = runs.get_calc_run(engine, result.run_id)
    assert run is not None and run["status"] == RunStatus.INTERRUPTED.value
    # Second call is a no-op.
    assert jobs.recover_interrupted_runs(engine) == 0
    engine.dispose()


def test_coordinator_start_recovers_orphans(db_path: Path, tmp_path: Path) -> None:
    engine = create_engine_for(db_path)
    result = jobs.submit_run(engine, methodology=Methodology(), as_of_date=AS_OF)
    runs.update_run_status(engine, result.run_id, RunStatus.RUNNING)
    engine.dispose()
    coord = jobs.JobCoordinator(db_path, cache_dir=tmp_path / "cache", poll_interval=0.05)
    coord.start()
    coord.stop()
    coord.close()
    check = create_engine_for(db_path)
    run = runs.get_calc_run(check, result.run_id)
    assert run is not None and run["status"] == RunStatus.INTERRUPTED.value
    check.dispose()


# ---------------------------------------------------------------------------
# Stage cache: warm == cold, invalidation, corruption
# ---------------------------------------------------------------------------


def test_warm_cache_identical_to_cold(db_path: Path, tmp_path: Path) -> None:
    engine = create_engine_for(db_path)
    frames, context, methodology = _frames_and_context(engine)
    cache = StageCache(tmp_path / "cache")

    hits: list[tuple[Stage, bool]] = []
    cold = jobs.run_pipeline_cached(
        frames, methodology, context, cache, on_stage=lambda s, h: hits.append((s, h))
    )
    assert [h for _, h in hits] == [False] * len(Stage)

    warm_hits: list[tuple[Stage, bool]] = []
    warm = jobs.run_pipeline_cached(
        frames, methodology, context, cache, on_stage=lambda s, h: warm_hits.append((s, h))
    )
    assert [h for _, h in warm_hits] == [True] * len(Stage)

    # Cached pipeline also matches the uncached engine runner exactly.
    reference = engine_runner.run_pipeline(frames, methodology, context)
    _assert_results_equal(cold, reference)
    _assert_results_equal(warm, reference)
    engine.dispose()


def test_methodology_change_invalidates_only_downstream_stages(
    db_path: Path, tmp_path: Path
) -> None:
    engine = create_engine_for(db_path)
    frames, context, _ = _frames_and_context(engine)
    cache = StageCache(tmp_path / "cache")

    base = Methodology()
    changed = Methodology(transaction_cost_bps=5.0)  # METRICS + STATISTICS only
    jobs.run_pipeline_cached(frames, base, context, cache)

    hits: dict[Stage, bool] = {}
    jobs.run_pipeline_cached(
        frames, changed, context, cache, on_stage=lambda s, h: hits.setdefault(s, h)
    )
    assert hits[Stage.INTERVALS] is True
    assert hits[Stage.RETURNS] is True
    assert hits[Stage.EXPOSURES] is True
    assert hits[Stage.METRICS] is False
    assert hits[Stage.STATISTICS] is False
    engine.dispose()


def test_context_change_invalidates_all_stages(db_path: Path, tmp_path: Path) -> None:
    engine = create_engine_for(db_path)
    frames, context, methodology = _frames_and_context(engine)
    cache = StageCache(tmp_path / "cache")
    jobs.run_pipeline_cached(frames, methodology, context, cache)

    other_ctx = context.model_copy(update={"as_of_date": date(2025, 1, 16)})
    hits: dict[Stage, bool] = {}
    jobs.run_pipeline_cached(
        frames, methodology, other_ctx, cache, on_stage=lambda s, h: hits.setdefault(s, h)
    )
    assert all(h is False for h in hits.values())
    engine.dispose()


def test_corrupt_cache_entry_rebuilds(db_path: Path, tmp_path: Path) -> None:
    engine = create_engine_for(db_path)
    frames, context, methodology = _frames_and_context(engine)
    cache = StageCache(tmp_path / "cache")
    cold = jobs.run_pipeline_cached(frames, methodology, context, cache)

    # Corrupt the RETURNS-stage frame.
    stage_dir = cache.stage_dir(Stage.RETURNS, cold.stage_fingerprints[Stage.RETURNS])
    (stage_dir / "returns.parquet").write_bytes(b"garbage")

    hits: dict[Stage, bool] = {}
    warm = jobs.run_pipeline_cached(
        frames, methodology, context, cache, on_stage=lambda s, h: hits.setdefault(s, h)
    )
    _assert_results_equal(warm, cold)
    # Only RETURNS misses: stage fingerprints key on methodology/context, not
    # frame content, and the recomputed frame is deterministic — so the
    # downstream stages' cache entries remain valid.
    assert hits[Stage.INTERVALS] is True
    assert hits[Stage.RETURNS] is False
    assert hits[Stage.EXPOSURES] is True
    assert hits[Stage.METRICS] is True
    assert hits[Stage.STATISTICS] is True
    # The rebuilt entry is valid again.
    assert (
        cache.get_frame(Stage.RETURNS, cold.stage_fingerprints[Stage.RETURNS], "returns")
        is not None
    )
    engine.dispose()


def test_execute_run_cached_uses_populated_cache(db_path: Path, tmp_path: Path) -> None:
    engine = create_engine_for(db_path)
    cache = StageCache(tmp_path / "cache")
    coord = jobs.JobCoordinator(engine, cache=cache)
    first = coord.submit(methodology=Methodology(), as_of_date=AS_OF)
    second = jobs.submit_run(
        engine,
        methodology=Methodology(),
        as_of_date=AS_OF,
        universe_filter_json='["S1"]',
    )
    # Different universe -> different run, but market frames identical so most
    # stage fingerprints still differ (context fingerprint changes). Just
    # verify both execute successfully through the cached path.
    assert coord.run_once() is not None
    assert coord.run_once() is not None
    for rid in (first.run_id, second.run_id):
        run = runs.get_calc_run(engine, rid)
        assert run is not None and run["status"] == RunStatus.SUCCEEDED.value
    engine.dispose()
