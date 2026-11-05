from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from callscore_core.contracts import RunContext
from callscore_core.enums import ReturnBasis
from callscore_core.frames import RETURN_FRAME, FrameContract
from callscore_core.methodology import Methodology
from callscore_engine.pricing import MissingBenchmarkData
from callscore_engine.returns import build_returns

_DTYPES: dict[str, pl.DataType] = {
    "String": pl.String(),
    "Date": pl.Date(),
    "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
    "Float64": pl.Float64(),
    "Int64": pl.Int64(),
    "Boolean": pl.Boolean(),
}

DAYS = [d for i in range(14) if (d := date(2024, 1, 2) + timedelta(days=i)).weekday() < 5][:10]


def _ts(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


def _sessions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "exchange_id": ["X"] * len(DAYS),
            "session_date": DAYS,
            "open_ts": [_ts(d, 9) for d in DAYS],
            "close_ts": [_ts(d, 16) for d in DAYS],
        }
    )


def _interval(
    interval_id: str = "I1",
    analyst_id: str = "A",
    canonical: str = "buy",
    *,
    security_id: str = "S1",
    investable: bool = True,
    entry: int = 1,
    exit_: int = 4,
    entry_point: str = "open",
    exit_point: str = "close",
) -> dict[str, object]:
    return {
        "interval_id": interval_id,
        "event_id": f"E_{interval_id}",
        "analyst_id": analyst_id,
        "firm_id": "F",
        "security_id": security_id,
        "native_rating": canonical,
        "canonical_rating": canonical,
        "action_type": "initiation",
        "announced_ts": _ts(DAYS[0], 8),
        "effective_ts": _ts(DAYS[0], 8),
        "knowledge_ts": _ts(DAYS[0], 8),
        "available_ts": _ts(DAYS[0], 12),
        "entry_session": DAYS[entry],
        "entry_point": entry_point,
        "entry_ts": _ts(DAYS[entry], 9),
        "exit_session": DAYS[exit_],
        "exit_point": exit_point,
        "exit_ts": _ts(DAYS[exit_], 16),
        "exit_reason": "run_end",
        "expiry_date": None,
        "decay_start_session": None,
        "is_investable": investable,
        "non_investable_reason": None if investable else canonical,
        "flags": None,
    }


def _intervals(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows).with_columns(
        pl.col("entry_session").cast(pl.Date),
        pl.col("exit_session").cast(pl.Date),
        pl.col("expiry_date").cast(pl.Date),
        pl.col("decay_start_session").cast(pl.Date),
    )


def _security() -> pl.DataFrame:
    # open_tr == open and close_tr == close because tr_index == close, adj == 1, div == 0.
    opens = [10.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0]
    closes = [10.5, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]
    return pl.DataFrame(
        {
            "security_id": ["S1"] * len(DAYS),
            "session_date": DAYS,
            "open": opens,
            "close": closes,
            "high": [max(o, c) + 1 for o, c in zip(opens, closes, strict=True)],
            "low": [min(o, c) - 1 for o, c in zip(opens, closes, strict=True)],
            "adj_factor": [1.0] * len(DAYS),
            "cash_dividend": [None] * len(DAYS),
            "tr_index": closes,
            "is_halted": [False] * len(DAYS),
        }
    )


def _benchmark(close_levels: list[float] | None = None) -> pl.DataFrame:
    closes = close_levels or [100.0] * 4 + [103.0] + [103.0] * 5
    return pl.DataFrame(
        {
            "benchmark_id": ["IDX_BROAD"] * len(DAYS),
            "session_date": DAYS,
            "open_level": [100.0] * len(DAYS),
            "close_level": closes,
        }
    )


def _flat_frames() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    empty_str = pl.Series([], dtype=pl.String)
    empty_date = pl.Series([], dtype=pl.Date)
    empty_f = pl.Series([], dtype=pl.Float64)
    empty_i = pl.Series([], dtype=pl.Int64)
    beta = pl.DataFrame(
        {
            "security_id": empty_str,
            "as_of_date": empty_date,
            "beta": empty_f,
            "window_days": empty_i,
        }
    )
    delistings = pl.DataFrame(
        {
            "security_id": empty_str,
            "delisting_date": empty_date,
            "final_return": empty_f,
            "reason": empty_str,
        }
    )
    classifications = pl.DataFrame(
        {
            "security_id": ["S1"],
            "from_date": [date(2020, 1, 1)],
            "to_date": [None],
            "exchange_id": ["X"],
            "currency": ["USD"],
            "sector_id": ["TECH"],
            "region": ["NA"],
            "cap_bucket": ["large"],
            "listing_date": [date(2010, 1, 1)],
            "delisting_date": [None],
        }
    ).with_columns(pl.col("to_date").cast(pl.Date), pl.col("delisting_date").cast(pl.Date))
    return beta, delistings, classifications, _benchmark()


def _context(as_of: date | None = None) -> RunContext:
    return RunContext(
        as_of_date=as_of or DAYS[-1],
        knowledge_cutoff=_ts(DAYS[-1]),
        source_revision="r",
        source_fingerprint="0" * 64,
        universe_fingerprint="0" * 64,
        algorithm_version="v",
    )


def _methodology(**overrides: object) -> Methodology:
    params: dict[str, object] = {
        "benchmark_mode": "market",
        "fixed_horizons_days": (2,),
        "primary_fixed_horizon_days": 2,
    }
    params.update(overrides)
    return Methodology.model_validate(params)


def _assert_contract(frame: pl.DataFrame, contract: FrameContract = RETURN_FRAME) -> None:
    assert frame.columns == list(contract.column_names)
    for column in contract.columns:
        assert frame.schema[column.name] == _DTYPES[column.dtype], column.name
    assert frame.equals(frame.sort(list(contract.sort_by)))
    assert not frame.select(list(contract.primary_key)).is_duplicated().any()


def _returns(
    intervals: pl.DataFrame,
    methodology: Methodology | None = None,
    *,
    security_daily: pl.DataFrame | None = None,
    benchmark_daily: pl.DataFrame | None = None,
    beta: pl.DataFrame | None = None,
    delistings: pl.DataFrame | None = None,
    classifications: pl.DataFrame | None = None,
    context: RunContext | None = None,
) -> pl.DataFrame:
    beta_f, delist_f, cls_f, bench_f = _flat_frames()
    return build_returns(
        intervals,
        security_daily if security_daily is not None else _security(),
        benchmark_daily if benchmark_daily is not None else bench_f,
        beta if beta is not None else beta_f,
        delistings if delistings is not None else delist_f,
        classifications if classifications is not None else cls_f,
        _sessions(),
        methodology or _methodology(),
        context or _context(),
    )


def _row(frame: pl.DataFrame, horizon: str) -> dict[str, object]:
    return frame.filter(pl.col("horizon") == horizon).to_dicts()[0]


def test_next_open_includes_overnight_gap() -> None:
    out = _returns(_intervals([_interval(entry=1, exit_=4)]))
    _assert_contract(out)
    row = _row(out, "to_exit")
    # Entry at the DAYS[1] open (10.0), not the DAYS[0] close (10.5): 14/10 - 1 = 0.4.
    assert row["stock_return"] == pytest.approx(0.4)
    assert row["entry_price"] == pytest.approx(10.0)
    assert row["exit_price"] == pytest.approx(14.0)
    assert row["benchmark_return"] == pytest.approx(0.03)
    assert row["excess_return"] == pytest.approx(0.37)
    assert row["benchmark_id"] == "IDX_BROAD"
    assert row["beta"] is None
    assert row["terminated_by_delisting"] is False
    assert row["unavailable_reason"] is None
    fixed = _row(out, "d2")
    # d2 ends at the close two sessions after entry: 13/10 - 1 = 0.3, bench flat -> 0.
    assert fixed["stock_return"] == pytest.approx(0.3)
    assert fixed["excess_return"] == pytest.approx(0.3)


def test_fixed_horizon_censored_past_as_of() -> None:
    methodology = _methodology(fixed_horizons_days=(2, 5), primary_fixed_horizon_days=2)
    out = _returns(
        _intervals([_interval(entry=1, exit_=4)]),
        methodology,
        context=_context(as_of=DAYS[5]),
    )
    _assert_contract(out)
    censored = _row(out, "d5")
    assert censored["end_session"] is None
    assert censored["end_point"] is None
    assert censored["stock_return"] is None
    assert censored["excess_return"] is None
    assert censored["unavailable_reason"] == "horizon_incomplete"
    assert _row(out, "d2")["stock_return"] == pytest.approx(0.3)


def test_identical_stock_and_benchmark_zero_excess() -> None:
    security = _security()
    benchmark = pl.DataFrame(
        {
            "benchmark_id": ["IDX_BROAD"] * len(DAYS),
            "session_date": DAYS,
            "open_level": security["open"].to_list(),
            "close_level": security["close"].to_list(),
        }
    )
    out = _returns(_intervals([_interval(entry=1, exit_=4)]), benchmark_daily=benchmark)
    row = _row(out, "to_exit")
    assert row["stock_return"] == pytest.approx(row["benchmark_return"])
    assert row["excess_return"] == pytest.approx(0.0)


def test_delisting_terminates_with_final_return() -> None:
    delistings = pl.DataFrame(
        {
            "security_id": ["S1"],
            "delisting_date": [DAYS[3]],
            "final_return": [-1.0],
            "reason": ["bankruptcy"],
        }
    )
    out = _returns(_intervals([_interval(entry=1, exit_=4)]), delistings=delistings)
    _assert_contract(out)
    row = _row(out, "to_exit")
    assert row["terminated_by_delisting"] is True
    assert row["end_session"] == DAYS[3]
    assert row["end_point"] == "close"
    # Terminal level = close(DAYS[3]) * (1 - 1.0) = 0 -> stock return -1.0.
    assert row["stock_return"] == pytest.approx(-1.0)
    assert row["exit_price"] == pytest.approx(0.0)
    # Benchmark has no delisting: it still measures open -> DAYS[3] close = 0.0.
    assert row["benchmark_return"] == pytest.approx(0.0)
    assert row["excess_return"] == pytest.approx(-1.0)


def test_beta_adjusted_uses_latest_beta_before_entry() -> None:
    methodology = _methodology(benchmark_mode="beta_adjusted")
    beta = pl.DataFrame(
        {
            "security_id": ["S1", "S1"],
            "as_of_date": [DAYS[0], DAYS[2]],
            "beta": [2.0, 9.0],
            "window_days": [60, 60],
        }
    )
    benchmark = _benchmark(close_levels=[100.0] * 4 + [110.0] + [110.0] * 5)
    out = _returns(
        _intervals([_interval(entry=1, exit_=4)]),
        methodology,
        beta=beta,
        benchmark_daily=benchmark,
    )
    row = _row(out, "to_exit")
    # Market return = 110/100 - 1 = 0.1; beta = 2.0 (as_of DAYS[0], strictly before
    # entry DAYS[1]; the DAYS[2] beta is not yet known).
    assert row["beta"] == pytest.approx(2.0)
    assert row["benchmark_return"] == pytest.approx(0.2)
    assert row["stock_return"] == pytest.approx(0.4)
    assert row["excess_return"] == pytest.approx(0.2)


def test_beta_adjusted_missing_beta_fails() -> None:
    methodology = _methodology(benchmark_mode="beta_adjusted")
    with pytest.raises(MissingBenchmarkData):
        _returns(_intervals([_interval(entry=1, exit_=4)]), methodology)


def test_missing_benchmark_level_fails_run() -> None:
    benchmark = _benchmark().filter(pl.col("session_date") != DAYS[4])
    with pytest.raises(MissingBenchmarkData):
        _returns(_intervals([_interval(entry=1, exit_=4)]), benchmark_daily=benchmark)


def test_price_vs_total_basis_differ_on_dividend_day() -> None:
    days = DAYS[:5]
    security = pl.DataFrame(
        {
            "security_id": ["S1"] * 5,
            "session_date": days,
            "open": [10.0, 11.0, 12.0, 13.0, 14.0],
            "close": [10.0, 11.0, 12.0, 13.0, 14.0],
            "high": [15.0] * 5,
            "low": [9.0] * 5,
            "adj_factor": [1.0] * 5,
            "cash_dividend": [None, None, None, 1.0, None],
            # tr_index embeds the DAYS[3] dividend: 120 -> 135 (+12.5%) vs px +7.7%.
            "tr_index": [100.0, 110.0, 120.0, 135.0, 140.0],
            "is_halted": [False] * 5,
        }
    )
    benchmark = pl.DataFrame(
        {
            "benchmark_id": ["IDX_BROAD"] * 5,
            "session_date": days,
            "open_level": [100.0] * 5,
            "close_level": [100.0] * 5,
        }
    )
    sessions = pl.DataFrame(
        {
            "exchange_id": ["X"] * 5,
            "session_date": days,
            "open_ts": [_ts(d, 9) for d in days],
            "close_ts": [_ts(d, 16) for d in days],
        }
    )
    classifications = pl.DataFrame(
        {
            "security_id": ["S1"],
            "from_date": [date(2020, 1, 1)],
            "to_date": [None],
            "exchange_id": ["X"],
            "sector_id": ["TECH"],
        }
    ).with_columns(pl.col("to_date").cast(pl.Date))
    beta_f, delist_f, _, _ = _flat_frames()
    interval = _intervals([_interval(entry=1, exit_=3)])
    context = _context(as_of=days[-1])

    total = build_returns(
        interval,
        security,
        benchmark,
        beta_f,
        delist_f,
        classifications,
        sessions,
        _methodology(return_basis=ReturnBasis.TOTAL),
        context,
    )
    price = build_returns(
        interval,
        security,
        benchmark,
        beta_f,
        delist_f,
        classifications,
        sessions,
        _methodology(return_basis=ReturnBasis.PRICE),
        context,
    )
    total_row = _row(total, "to_exit")
    price_row = _row(price, "to_exit")
    # Price basis: 13/11 - 1. Total basis: 135/110 - 1 (dividend reinvested at open).
    assert price_row["stock_return"] == pytest.approx(13 / 11 - 1)
    assert total_row["stock_return"] == pytest.approx(135 / 110 - 1)
    assert total_row["stock_return"] != pytest.approx(price_row["stock_return"])


def test_non_investable_rows_have_null_legs() -> None:
    out = _returns(_intervals([_interval(canonical="not_rated", investable=False)]))
    _assert_contract(out)
    assert out.height == 2  # to_exit + d2
    for row in out.iter_rows(named=True):
        assert row["unavailable_reason"] == "non_investable"
        assert row["end_session"] is None
        assert row["stock_return"] is None
        assert row["excess_return"] is None
        assert row["benchmark_id"] is None
