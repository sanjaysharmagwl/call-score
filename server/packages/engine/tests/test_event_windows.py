from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from callscore_core.contracts import RunContext
from callscore_core.methodology import Methodology
from callscore_engine.event_windows import (
    assert_event_windows_contract,
    build_event_windows,
)
from callscore_engine.pricing import MissingBenchmarkData

D0 = date(2024, 1, 1)  # Monday
N_DAYS = 55


def _weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DATES = _weekdays(D0, N_DAYS)


def _ts(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC)


def _sessions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "exchange_id": ["XNYS"] * N_DAYS,
            "session_date": DATES,
            "open_ts": [_ts(d, 13, 30) for d in DATES],
            "close_ts": [_ts(d, 20, 0) for d in DATES],
        }
    )


def _tr_index() -> list[float]:
    """Hand-computed TR levels: flat 100, jump to 110 at idx 10, to 121 at idx 30,
    to 127.05 at idx 50 (the post-event endpoint for an idx-30 entry)."""
    t = [100.0] * N_DAYS
    for i in range(10, 30):
        t[i] = 110.0
    for i in range(30, N_DAYS):
        t[i] = 121.0
    t[50] = 127.05
    for i in range(51, N_DAYS):
        t[i] = 127.05
    return t


def _bench_close() -> list[float]:
    """Benchmark close levels: 1000, 1020 from idx 10 (pre-end), 1030.2 from idx 30,
    1050.804 at idx 50."""
    b = [1000.0] * N_DAYS
    for i in range(10, 30):
        b[i] = 1020.0
    for i in range(30, N_DAYS):
        b[i] = 1030.2
    for i in range(50, N_DAYS):
        b[i] = 1050.804
    return b


def _security_daily() -> pl.DataFrame:
    tr = _tr_index()
    return pl.DataFrame(
        {
            "security_id": ["S1"] * N_DAYS,
            "session_date": DATES,
            "open": [100.0] * N_DAYS,
            "close": [100.0] * N_DAYS,
            "high": [101.0] * N_DAYS,
            "low": [99.0] * N_DAYS,
            "adj_factor": [1.0] * N_DAYS,
            "cash_dividend": [None] * N_DAYS,
            "tr_index": tr,
            "is_halted": [False] * N_DAYS,
        }
    )


def _benchmark_daily(*, drop: date | None = None) -> pl.DataFrame:
    close = _bench_close()
    keep = [i for i in range(N_DAYS) if drop is None or DATES[i] != drop]
    opens = [close[i - 1] if i > 0 else close[i] for i in keep]
    return pl.DataFrame(
        {
            "benchmark_id": ["B1"] * len(keep),
            "session_date": [DATES[i] for i in keep],
            "open_level": opens,
            "close_level": [close[i] for i in keep],
        }
    )


def _classifications() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "security_id": ["S1"],
            "from_date": [date(2020, 1, 1)],
            "to_date": [None],
            "exchange_id": ["XNYS"],
            "currency": ["USD"],
            "sector_id": ["TECH"],
            "region": ["NA"],
            "cap_bucket": ["large"],
            "listing_date": [date(2020, 1, 1)],
            "delisting_date": [None],
        }
    )


def _interval(
    interval_id: str,
    analyst: str,
    entry_idx: int,
    entry_point: str = "close",
    exit_idx: int | None = None,
    is_investable: bool = True,
    avail_idx: int | None = None,
) -> dict:
    entry_session = DATES[entry_idx]
    exit_idx = N_DAYS - 1 if exit_idx is None else exit_idx
    avail_idx = entry_idx - 1 if avail_idx is None else avail_idx
    point_ts = _ts(entry_session, 13, 30) if entry_point == "open" else _ts(entry_session, 20, 0)
    return {
        "interval_id": interval_id,
        "event_id": interval_id,
        "analyst_id": analyst,
        "firm_id": "F1",
        "security_id": "S1",
        "native_rating": "buy",
        "canonical_rating": "buy",
        "action_type": "initiation",
        "announced_ts": _ts(DATES[avail_idx], 12, 0),
        "effective_ts": _ts(DATES[avail_idx], 12, 0),
        "knowledge_ts": _ts(DATES[avail_idx], 12, 0),
        "available_ts": _ts(DATES[avail_idx], 12, 0),
        "entry_session": entry_session,
        "entry_point": entry_point,
        "entry_ts": point_ts,
        "exit_session": DATES[exit_idx],
        "exit_point": "close",
        "exit_ts": _ts(DATES[exit_idx], 20, 0),
        "exit_reason": "run_end",
        "expiry_date": None,
        "decay_start_session": None,
        "is_investable": is_investable,
        "non_investable_reason": None if is_investable else "under_review",
        "flags": None,
    }


def _intervals_frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def _exposures(interval_weights: dict[str, float | None]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "interval_id": list(interval_weights),
            "segment_no": [1] * len(interval_weights),
            "start_session": [DATES[0]] * len(interval_weights),
            "start_point": ["open"] * len(interval_weights),
            "end_session": [DATES[-1]] * len(interval_weights),
            "end_point": ["close"] * len(interval_weights),
            "position_weight": list(interval_weights.values()),
            "weight_source": ["rating"] * len(interval_weights),
        }
    )


def _methodology(**overrides: object) -> Methodology:
    return Methodology.model_validate(
        {"benchmark_mode": "market", "benchmark_id": "B1", **overrides}
    )


def _context(as_of: date | None = None) -> RunContext:
    return RunContext(
        as_of_date=as_of or DATES[-1],
        knowledge_cutoff=_ts(DATES[-1], 23, 59),
        source_revision="test",
        source_fingerprint="0" * 64,
        universe_fingerprint="0" * 64,
        algorithm_version="test",
    )


def _base_inputs() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """I1 (analyst A, entry idx 30) plus peer intervals B/C/D active at its avail."""
    rows = [
        _interval("I1", "A", 30, avail_idx=29),
        _interval("P1", "B", 10, "open", avail_idx=10),
        _interval("P2", "C", 10, "open", avail_idx=10),
        _interval("P3", "D", 10, "open", avail_idx=10),
    ]
    intervals = _intervals_frame(rows)
    exposures = _exposures({"I1": 0.5, "P1": 0.4, "P2": 0.6, "P3": 0.2})
    return intervals, exposures, _security_daily(), _benchmark_daily()


def test_event_windows_hand_computed() -> None:
    intervals, exposures, daily, bench = _base_inputs()
    out = build_event_windows(
        intervals, exposures, daily, bench, _classifications(), _methodology(), _context()
    )
    assert_event_windows_contract(out)
    row = out.filter(pl.col("event_id") == "I1").row(0, named=True)
    # weight_change: first interval of analyst A -> 0.5 - 0.0
    assert row["weight_change"] == pytest.approx(0.5)
    # consensus: mean(0.4, 0.6, 0.2) = 0.4 -> 0.5 - 0.4
    assert row["consensus_deviation"] == pytest.approx(0.1)
    # CAR over sessions idx 29,30,31: only idx 30 moves (stock +10%, bench +1%)
    assert row["car_pm1"] == pytest.approx(0.09)
    # pre: stock 110/100 - 1 = 0.10; bench 1020/1000 - 1 = 0.02
    assert row["pre_event_excess_20d"] == pytest.approx(0.08)
    # post: entry close_tr idx30 = 121 -> close idx50 = 127.05 (+5%);
    # bench 1030.2 -> 1050.804 (+2%)
    assert row["post_event_excess_20d"] == pytest.approx(0.03)
    assert row["event_session"] == DATES[30]
    assert row["unavailable_reason"] is None


def test_weight_change_first_and_subsequent() -> None:
    rows = [
        _interval("I1", "A", 30, avail_idx=29),
        _interval("I2", "A", 40, avail_idx=39),
        _interval("I3", "B", 30, avail_idx=29),
    ]
    intervals = _intervals_frame(rows)
    exposures = _exposures({"I1": 0.5, "I2": 0.25, "I3": None})
    out = build_event_windows(
        intervals,
        exposures,
        _security_daily(),
        _benchmark_daily(),
        _classifications(),
        _methodology(),
        _context(),
    )
    by_id = {r["event_id"]: r for r in out.iter_rows(named=True)}
    assert by_id["I1"]["weight_change"] == pytest.approx(0.5)
    assert by_id["I2"]["weight_change"] == pytest.approx(-0.25)
    # I3 is analyst B's first interval but its weight is null -> null change
    assert by_id["I3"]["weight_change"] is None
    assert "weight_change_unavailable" in by_id["I3"]["unavailable_reason"]


def test_consensus_requires_minimum_peers() -> None:
    intervals, exposures, daily, bench = _base_inputs()
    # Only two peers active -> below the default min_consensus_ratings = 3.
    intervals = intervals.filter(~pl.col("event_id").is_in(["P3"]))
    exposures = exposures.filter(pl.col("interval_id").is_in(["I1", "P1", "P2"]))
    out = build_event_windows(
        intervals,
        exposures,
        daily,
        bench,
        _classifications(),
        _methodology(min_consensus_ratings=3),
        _context(),
    )
    row = out.filter(pl.col("event_id") == "I1").row(0, named=True)
    assert row["consensus_deviation"] is None
    assert "insufficient_consensus" in row["unavailable_reason"]


def test_announcement_window_disabled() -> None:
    intervals, exposures, daily, bench = _base_inputs()
    out = build_event_windows(
        intervals,
        exposures,
        daily,
        bench,
        _classifications(),
        _methodology(include_announcement_window=False),
        _context(),
    )
    row = out.filter(pl.col("event_id") == "I1").row(0, named=True)
    assert row["car_pm1"] is None
    assert "announcement_window_disabled" in row["unavailable_reason"]
    assert row["pre_event_excess_20d"] == pytest.approx(0.08)
    assert row["post_event_excess_20d"] == pytest.approx(0.03)


def test_edge_censoring_nulls_post_window() -> None:
    intervals, exposures, daily, bench = _base_inputs()
    rows = [
        _interval("LATE", "A", 50, avail_idx=49),
        _interval("P1", "B", 10, "open", avail_idx=10),
        _interval("P2", "C", 10, "open", avail_idx=10),
        _interval("P3", "D", 10, "open", avail_idx=10),
    ]
    intervals = _intervals_frame(rows)
    exposures = _exposures({"LATE": 0.5, "P1": 0.4, "P2": 0.6, "P3": 0.2})
    out = build_event_windows(
        intervals, exposures, daily, bench, _classifications(), _methodology(), _context()
    )
    row = out.filter(pl.col("event_id") == "LATE").row(0, named=True)
    # post needs idx 70 -> beyond the data; pre needs idx 29/49 -> present.
    assert row["post_event_excess_20d"] is None
    assert "post_event_window_incomplete" in row["unavailable_reason"]
    assert row["pre_event_excess_20d"] is not None


def test_missing_benchmark_raises() -> None:
    intervals, exposures, daily, _ = _base_inputs()
    bench = _benchmark_daily(drop=DATES[50])
    with pytest.raises(MissingBenchmarkData):
        build_event_windows(
            intervals,
            exposures,
            daily,
            bench,
            _classifications(),
            _methodology(),
            _context(),
        )


def test_row_order_invariance() -> None:
    intervals, exposures, daily, bench = _base_inputs()
    shuffled = intervals.sample(fraction=1.0, seed=7)
    a = build_event_windows(
        intervals, exposures, daily, bench, _classifications(), _methodology(), _context()
    )
    b = build_event_windows(
        shuffled, exposures, daily, bench, _classifications(), _methodology(), _context()
    )
    assert a.equals(b)
