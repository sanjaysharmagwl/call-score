"""Repository tests: ingest round-trip, run lifecycle, publish, lease, iter_rows."""

import csv
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from callscore_core.contracts import StageArtifact
from callscore_core.enums import RunStatus, Stage
from callscore_core.frames import FRAME_CONTRACTS
from callscore_core.methodology import Methodology
from callscore_store import create_engine_for
from callscore_store.repositories import (
    RunTransitionError,
    acquire_writer_lease,
    create_calc_run,
    get_calc_run,
    get_writer_lease,
    heartbeat_writer_lease,
    insert_market,
    insert_rating_events,
    insert_reference,
    iter_rows,
    list_calc_runs,
    publish_run_results,
    register_methodology_version,
    release_writer_lease,
    update_run_progress,
    update_run_status,
    write_csv,
)
from callscore_store.schema import bootstrap
from callscore_store.validation import ValidationError

D0 = date(2024, 1, 2)
TS = datetime(2024, 1, 3, 10, 0, tzinfo=UTC)
CUTOFF = datetime(2024, 2, 1, tzinfo=UTC)

_DTYPES = {
    "String": pl.String,
    "Date": pl.Date,
    "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
    "Float64": pl.Float64,
    "Int64": pl.Int64,
    "Boolean": pl.Boolean,
}


def _engine(tmp_path: Path):
    engine = create_engine_for(tmp_path / "t.db")
    bootstrap(engine)
    return engine


def _frame(name: str, rows: list[dict]) -> pl.DataFrame:
    contract = FRAME_CONTRACTS[name]
    schema = {c.name: _DTYPES[c.dtype] for c in contract.columns}
    return pl.DataFrame(rows, schema=schema)


def _seed(engine) -> None:
    insert_reference(
        engine,
        firms=[{"firm_id": "F1", "name": "Firm", "country": "US"}],
        analysts=[{"analyst_id": "A1", "name": "Analyst", "firm_id": "F1"}],
        securities=[{"security_id": "S1", "name": "Sec"}],
        exchanges=[{"exchange_id": "X1", "name": "Ex", "country": "US", "timezone": "UTC"}],
        sectors=[{"sector_id": "SE1", "name": "Tech", "parent_sector_id": None}],
        sessions=[
            {
                "exchange_id": "X1",
                "session_date": D0,
                "open_ts": datetime(2024, 1, 2, 14, 0, tzinfo=UTC),
                "close_ts": datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
            }
        ],
        employment=[{"analyst_id": "A1", "firm_id": "F1", "from_date": D0, "to_date": None}],
        coverage=[
            {
                "analyst_id": "A1",
                "security_id": "S1",
                "from_date": D0,
                "to_date": None,
                "is_lead": True,
            }
        ],
        classifications=[
            {
                "security_id": "S1",
                "from_date": D0,
                "to_date": None,
                "exchange_id": "X1",
                "currency": "USD",
                "sector_id": "SE1",
                "region": "NA",
                "cap_bucket": "large",
                "listing_date": D0,
                "delisting_date": None,
            }
        ],
        rating_scales=[
            {
                "scale_id": "F1:scale",
                "firm_id": "F1",
                "effective_from": D0,
                "effective_to": None,
                "n_tiers": 5,
            }
        ],
        scale_maps=[
            {
                "scale_id": "F1:scale",
                "effective_from": D0,
                "native_label": "Buy",
                "canonical_rating": "buy",
                "rank_within_scale": 2,
            }
        ],
    )
    insert_rating_events(
        engine,
        [
            {
                "event_id": "E1",
                "analyst_id": "A1",
                "firm_id": "F1",
                "security_id": "S1",
                "announced_ts": TS,
                "effective_ts": datetime(2024, 1, 4, 9, 0, tzinfo=UTC),
                "knowledge_ts": datetime(2024, 1, 3, 12, 0, tzinfo=UTC),
                "action_type": "initiation",
                "native_rating": "Buy",
                "canonical_rating": "buy",
                "prior_canonical_rating": None,
                "scale_id": "F1:scale",
                "price_target": Decimal("150.5"),
                "pt_currency": "USD",
                "pt_horizon_months": 12,
                "expiry_date": date(2025, 1, 4),
                "supersedes_event_id": None,
                "is_correction": False,
            }
        ],
    )
    insert_market(
        engine,
        benchmark_defs=[{"benchmark_id": "IDX_BROAD", "kind": "market", "rule_json": None}],
        benchmark_daily=[
            {
                "benchmark_id": "IDX_BROAD",
                "session_date": D0,
                "open_level": 100.0,
                "close_level": 101.0,
            }
        ],
        security_daily=[
            {
                "security_id": "S1",
                "session_date": D0,
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "adj_factor": 1.0,
                "cash_dividend": None,
                "tr_index": 100.0,
                "is_halted": False,
            }
        ],
        fx_daily=[{"from_ccy": "USD", "to_ccy": "USD", "rate_date": D0, "rate": 1.0}],
        security_beta=[{"security_id": "S1", "as_of_date": D0, "beta": 1.1, "window_days": 252}],
    )


def _run(engine, run_id: str = "run-1") -> int:
    """Register a methodology version and create a queued calc_run."""
    mv = register_methodology_version(engine, Methodology(), is_baseline=True)
    create_calc_run(
        engine,
        run_id=run_id,
        methodology_version_id=mv,
        as_of_date=D0,
        knowledge_cutoff=CUTOFF,
        algorithm_version="test-1",
    )
    return mv


def _interval_row(**over) -> dict:
    row = {
        "interval_id": "I1",
        "event_id": "E1",
        "analyst_id": "A1",
        "firm_id": "F1",
        "security_id": "S1",
        "native_rating": "Buy",
        "canonical_rating": "buy",
        "action_type": "initiation",
        "announced_ts": TS,
        "effective_ts": datetime(2024, 1, 4, 9, 0, tzinfo=UTC),
        "knowledge_ts": datetime(2024, 1, 3, 12, 0, tzinfo=UTC),
        "available_ts": TS,
        "entry_session": D0,
        "entry_point": "open",
        "entry_ts": datetime(2024, 1, 2, 14, 0, tzinfo=UTC),
        "exit_session": D0,
        "exit_point": "close",
        "exit_ts": datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
        "exit_reason": "run_end",
        "expiry_date": None,
        "decay_start_session": None,
        "is_investable": True,
        "non_investable_reason": None,
        "flags": None,
    }
    row.update(over)
    return row


def _metric_row(**over) -> dict:
    row = {
        "analyst_id": "A1",
        "metric_code": "hit_rate",
        "value": 0.6,
        "n_obs": 10,
        "std_err": None,
        "t_stat": None,
        "ci_low": None,
        "ci_high": None,
        "shrunk_value": None,
        "peer_percentile": None,
        "is_significant": None,
        "unavailable_reason": None,
    }
    row.update(over)
    return row


# --- ingest round trip ------------------------------------------------------ #


def test_insert_reference_roundtrip(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    result = insert_reference(
        engine,
        firms=[{"firm_id": "F9", "name": "F", "country": "GB"}],
        analysts=[{"analyst_id": "A9", "name": "A", "firm_id": "F9"}],
    )
    assert result == {"firm": 1, "analyst": 1}
    rows = list(iter_rows(engine, "analyst", filters={"firm_id": "F9"}))
    assert len(rows) == 1 and rows[0]["analyst_id"] == "A9"


def test_insert_reference_bad_batch_atomic(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with pytest.raises(ValidationError):
        insert_reference(
            engine,
            firms=[{"firm_id": "F1", "name": "ok"}],
            analysts=[{"analyst_id": "A1", "name": "a", "firm_id": "F1"}],
            employment=[
                {"analyst_id": "A1", "firm_id": "F1", "from_date": D0, "to_date": date(2023, 1, 1)}
            ],
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM firm")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM analyst")).scalar() == 0


# --- run lifecycle ----------------------------------------------------------- #


def test_run_status_transitions(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine)
    _run(engine)
    run = get_calc_run(engine, "run-1")
    assert run is not None and run["status"] == "queued"

    # Illegal: queued -> succeeded.
    with pytest.raises(RunTransitionError):
        update_run_status(engine, "run-1", RunStatus.SUCCEEDED)

    update_run_status(engine, "run-1", RunStatus.RUNNING)
    update_run_progress(engine, "run-1", 0.5)
    assert get_calc_run(engine, "run-1")["progress"] == 0.5
    update_run_status(engine, "run-1", RunStatus.SUCCEEDED)
    run = get_calc_run(engine, "run-1")
    assert run["status"] == "succeeded" and run["finished_at"] is not None

    # Terminal states have no outgoing transitions.
    with pytest.raises(RunTransitionError):
        update_run_status(engine, "run-1", RunStatus.QUEUED)


def test_run_failed_to_queued_retry(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine)
    _run(engine, "run-2")
    update_run_status(engine, "run-2", RunStatus.RUNNING)
    update_run_status(engine, "run-2", RunStatus.FAILED, error={"stage": "returns"})
    run = get_calc_run(engine, "run-2")
    assert run["status"] == "failed" and run["error_json"] is not None
    update_run_status(engine, "run-2", RunStatus.QUEUED)  # retry allowed
    assert get_calc_run(engine, "run-2")["status"] == "queued"


def test_create_calc_run_sets_created_at(tmp_path: Path) -> None:
    """``create_calc_run`` stamps ``created_at`` at insert (queued runs too)."""
    engine = _engine(tmp_path)
    _run(engine)
    run = get_calc_run(engine, "run-1")
    assert run is not None
    created_at = run["created_at"]
    assert created_at is not None
    assert created_at.tzinfo is not None
    assert created_at.utcoffset() == timedelta(0)
    assert created_at <= datetime.now(UTC)
    # A queued run that never starts still has created_at (unlike started_at).
    assert run["started_at"] is None


def test_list_calc_runs_filters_and_sort(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine)
    _run(engine, "run-a")
    _run(engine, "run-b")
    runs = list_calc_runs(engine, status=RunStatus.QUEUED, sort="run_id")
    assert {r["run_id"] for r in runs} == {"run-a", "run-b"}
    with pytest.raises(ValueError):
        list_calc_runs(engine, sort="run_id; DROP TABLE calc_run--")


# --- publish_run_results ------------------------------------------------------ #


def _stage_frames() -> dict[str, pl.DataFrame]:
    return {
        "intervals": _frame("intervals", [_interval_row()]),
        "exposures": _frame(
            "exposures",
            [
                {
                    "interval_id": "I1",
                    "segment_no": 0,
                    "start_session": D0,
                    "start_point": "open",
                    "end_session": D0,
                    "end_point": "close",
                    "position_weight": 0.5,
                    "weight_source": "rating",
                }
            ],
        ),
        "returns": _frame(
            "returns",
            [
                {
                    "interval_id": "I1",
                    "horizon": "to_exit",
                    "start_session": D0,
                    "start_point": "open",
                    "end_session": D0,
                    "end_point": "close",
                    "entry_price": 10.0,
                    "exit_price": 10.5,
                    "stock_return": 0.05,
                    "benchmark_id": "IDX_BROAD",
                    "benchmark_return": 0.01,
                    "beta": 1.1,
                    "excess_return": 0.04,
                    "terminated_by_delisting": False,
                    "unavailable_reason": None,
                }
            ],
        ),
        "contributions": _frame(
            "contributions",
            [
                {
                    "interval_id": "I1",
                    "analyst_id": "A1",
                    "horizon": "to_exit",
                    "entry_session": D0,
                    "position_weight": 0.5,
                    "excess_return": 0.04,
                    "contribution": 0.02,
                    "unavailable_reason": None,
                }
            ],
        ),
        "portfolio_positions": _frame(
            "portfolio_positions",
            [
                {
                    "analyst_id": "A1",
                    "session_date": D0,
                    "security_id": "S1",
                    "weight": 0.5,
                    "notional": 50000.0,
                }
            ],
        ),
        "metric_results": _frame("metric_results", [_metric_row()]),
        "composites": _frame(
            "composites",
            [
                {
                    "analyst_id": "A1",
                    "peer_group": None,
                    "composite_score": 55.0,
                    "rank_overall": 1,
                    "rank_in_peer_group": None,
                    "is_ranked": True,
                    "unranked_reason": None,
                }
            ],
        ),
    }


def _count(engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)


def test_publish_run_results_success(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine)
    _run(engine)
    update_run_status(engine, "run-1", RunStatus.RUNNING)
    artifact = StageArtifact(
        stage=Stage.INTERVALS,
        content_fingerprint="a" * 64,
        cache_fingerprint="b" * 64,
        row_count=1,
    )
    counts = publish_run_results(
        engine, "run-1", _stage_frames(), [artifact], source_fingerprint="c" * 64
    )
    assert counts["rating_interval"] == 1
    assert counts["analyst_metric"] == 1
    assert counts["portfolio_position"] == 1
    assert _count(engine, "rating_interval") == 1
    assert _count(engine, "interval_exposure") == 1
    assert _count(engine, "portfolio_position") == 1
    run = get_calc_run(engine, "run-1")
    assert run["status"] == "succeeded" and run["progress"] == 1.0
    assert run["source_fingerprint"] == "c" * 64
    assert "stage_artifacts" in (run["row_counts_json"] or "")


def test_publish_run_results_atomic_failure(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine)
    _run(engine, "run-ok")
    _run(engine, "run-bad")
    update_run_status(engine, "run-ok", RunStatus.RUNNING)
    publish_run_results(engine, "run-ok", _stage_frames())
    assert _count(engine, "rating_interval") == 1

    update_run_status(engine, "run-bad", RunStatus.RUNNING)
    bad_frames = _stage_frames()
    # entry_point "midday" violates the open/close CHECK -> mid-publish failure.
    bad_frames["intervals"] = _frame(
        "intervals", [_interval_row(interval_id="I2", entry_point="midday")]
    )
    with pytest.raises(IntegrityError):
        publish_run_results(engine, "run-bad", bad_frames)

    # run-bad stays running; no partial rows for it; run-ok rows untouched.
    assert get_calc_run(engine, "run-bad")["status"] == "running"
    with engine.connect() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) FROM rating_interval WHERE run_id = 'run-bad'")
        ).scalar()
    assert n == 0
    assert _count(engine, "rating_interval") == 1


def test_publish_replaces_prior_results(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine)
    _run(engine)
    update_run_status(engine, "run-1", RunStatus.RUNNING)
    publish_run_results(engine, "run-1", _stage_frames())
    frames2 = _stage_frames()
    frames2["metric_results"] = _frame(
        "metric_results",
        [_metric_row(metric_code="hit_rate"), _metric_row(metric_code="buy_fraction")],
    )
    publish_run_results(engine, "run-1", frames2)
    assert _count(engine, "rating_interval") == 1  # not duplicated
    assert _count(engine, "analyst_metric") == 2


# --- writer lease ------------------------------------------------------------- #


def test_writer_lease_mutual_exclusion(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    barrier = threading.Barrier(2)

    def try_acquire(holder: str) -> bool:
        barrier.wait(timeout=10)
        return acquire_writer_lease(engine, holder, f"job-{holder}", ttl=timedelta(minutes=5))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(try_acquire, ["h1", "h2"]))
    assert sorted(results) == [False, True]

    lease = get_writer_lease(engine)
    assert lease is not None and lease["holder"] in {"h1", "h2"}
    holder = str(lease["holder"])
    other = "h2" if holder == "h1" else "h1"
    # Live lease blocks the other holder; heartbeat extends it.
    assert not acquire_writer_lease(engine, other, "job-x", ttl=60)
    assert heartbeat_writer_lease(engine, holder, ttl=timedelta(minutes=5))
    assert not heartbeat_writer_lease(engine, other, ttl=60)
    assert release_writer_lease(engine, holder)
    assert acquire_writer_lease(engine, other, "job-x", ttl=60)


def test_writer_lease_expired_takeover(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert acquire_writer_lease(engine, "old", "job-old", ttl=0)
    # ttl=0 expires immediately -> a new holder can take over.
    assert acquire_writer_lease(engine, "new", "job-new", ttl=timedelta(minutes=1))


# --- iter_rows / csv ------------------------------------------------------------ #


def test_iter_rows_injection_safe(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine)
    # Hostile strings are treated as data, not SQL.
    with pytest.raises(ValueError):
        list(iter_rows(engine, "analyst", sort="name; DROP TABLE analyst--"))
    with pytest.raises(ValueError):
        list(iter_rows(engine, "analyst", filters={"firm_id; DROP": "x"}))
    with pytest.raises(ValueError):
        list(iter_rows(engine, "not_a_table"))
    rows = list(iter_rows(engine, "analyst", sort="analyst_id", limit=10, offset=0))
    assert [r["analyst_id"] for r in rows] == ["A1"]


def test_write_csv(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine)
    out = tmp_path / "analysts.csv"
    n = write_csv(engine, "analyst", out, sort="analyst_id")
    assert n == 1
    with out.open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["analyst_id"] == "A1" and rows[0]["firm_id"] == "F1"
