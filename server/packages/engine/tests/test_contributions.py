from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from callscore_core.contracts import RunContext
from callscore_core.enums import ScoringMode
from callscore_core.frames import CONTRIBUTION_FRAME, FrameContract
from callscore_core.methodology import Methodology
from callscore_engine.contributions import build_contributions
from callscore_engine.exposures import build_exposures
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
    decay_start: date | None = None,
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
        "entry_point": "open",
        "entry_ts": _ts(DAYS[entry], 9),
        "exit_session": DAYS[exit_],
        "exit_point": "close",
        "exit_ts": _ts(DAYS[exit_], 16),
        "exit_reason": "run_end",
        "expiry_date": None,
        "decay_start_session": decay_start,
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


def _benchmark() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "benchmark_id": ["IDX_BROAD"] * len(DAYS),
            "session_date": DAYS,
            "open_level": [100.0] * len(DAYS),
            "close_level": [100.0] * 4 + [103.0] * 6,
        }
    )


def _frames() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
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
            "sector_id": ["TECH"],
        }
    ).with_columns(pl.col("to_date").cast(pl.Date))
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


def _exposures(rows: list[dict[str, object]]) -> pl.DataFrame:
    schema = {
        "interval_id": pl.String(),
        "segment_no": pl.Int64(),
        "start_session": pl.Date(),
        "start_point": pl.String(),
        "end_session": pl.Date(),
        "end_point": pl.String(),
        "position_weight": pl.Float64(),
        "weight_source": pl.String(),
    }
    return pl.DataFrame(rows, schema=schema)


def _segment(
    interval_id: str,
    segment_no: int,
    start: int,
    start_point: str,
    end: int,
    end_point: str,
    weight: float | None,
    source: str = "rating",
) -> dict[str, object]:
    return {
        "interval_id": interval_id,
        "segment_no": segment_no,
        "start_session": DAYS[start],
        "start_point": start_point,
        "end_session": DAYS[end],
        "end_point": end_point,
        "position_weight": weight,
        "weight_source": source,
    }


def _assert_contract(frame: pl.DataFrame, contract: FrameContract = CONTRIBUTION_FRAME) -> None:
    assert frame.columns == list(contract.column_names)
    for column in contract.columns:
        assert frame.schema[column.name] == _DTYPES[column.dtype], column.name
    assert frame.equals(frame.sort(list(contract.sort_by)))
    assert not frame.select(list(contract.primary_key)).is_duplicated().any()


def _contributions(
    intervals: pl.DataFrame,
    exposures: pl.DataFrame,
    returns: pl.DataFrame,
    methodology: Methodology | None = None,
    *,
    context: RunContext | None = None,
) -> pl.DataFrame:
    beta, delistings, classifications, benchmark = _frames()
    return build_contributions(
        intervals,
        exposures,
        returns,
        _security(),
        benchmark,
        beta,
        delistings,
        classifications,
        methodology or _methodology(),
    )


def _returns(
    intervals: pl.DataFrame,
    methodology: Methodology | None = None,
    *,
    context: RunContext | None = None,
) -> pl.DataFrame:
    beta, delistings, classifications, benchmark = _frames()
    return build_returns(
        intervals,
        _security(),
        benchmark,
        beta,
        delistings,
        classifications,
        _sessions(),
        methodology or _methodology(),
        context or _context(),
    )


def _row(frame: pl.DataFrame, horizon: str) -> dict[str, object]:
    return frame.filter(pl.col("horizon") == horizon).to_dicts()[0]


def test_single_segment_equals_weight_times_excess() -> None:
    intervals = _intervals([_interval(entry=1, exit_=4)])
    exposures = _exposures([_segment("I1", 1, 1, "open", 4, "close", 0.5)])
    returns = _returns(intervals)
    out = _contributions(intervals, exposures, returns)
    _assert_contract(out)
    to_exit = _row(out, "to_exit")
    assert to_exit["position_weight"] == pytest.approx(0.5)
    assert to_exit["excess_return"] == pytest.approx(0.37)
    assert to_exit["contribution"] == pytest.approx(0.5 * 0.37)
    fixed = _row(out, "d2")
    # First-segment weight x d2 excess (13/10 - 1 = 0.3 stock, 0 benchmark).
    assert fixed["excess_return"] == pytest.approx(0.3)
    assert fixed["contribution"] == pytest.approx(0.15)
    assert fixed["entry_session"] == DAYS[1]
    assert to_exit["analyst_id"] == "A"


def test_weight_sign_reversal_negates_contribution_not_returns() -> None:
    buy = _intervals([_interval(canonical="buy", entry=1, exit_=4)])
    sell = _intervals([_interval(canonical="sell", entry=1, exit_=4)])
    exposures_buy = _exposures([_segment("I1", 1, 1, "open", 4, "close", 0.5)])
    exposures_sell = _exposures([_segment("I1", 1, 1, "open", 4, "close", -0.5)])
    returns_buy = _returns(buy)
    returns_sell = _returns(sell)
    # Returns are weight-independent: identical legs.
    assert returns_buy.equals(returns_sell)
    out_buy = _contributions(buy, exposures_buy, returns_buy)
    out_sell = _contributions(sell, exposures_sell, returns_sell)
    assert _row(out_buy, "to_exit")["contribution"] == pytest.approx(0.185)
    assert _row(out_sell, "to_exit")["contribution"] == pytest.approx(-0.185)
    assert _row(out_sell, "to_exit")["position_weight"] == pytest.approx(-0.5)


def test_decay_segments_sum_weighted_excess() -> None:
    intervals = _intervals([_interval(entry=1, exit_=6, decay_start=DAYS[3])])
    exposures = build_exposures(
        intervals, pl.DataFrame(), _sessions(), _methodology(expiry_policy="decay", decay_days=3)
    )
    returns = _returns(intervals, _methodology(expiry_policy="decay", decay_days=3))
    out = _contributions(
        intervals, exposures, returns, _methodology(expiry_policy="decay", decay_days=3)
    )
    _assert_contract(out)
    to_exit = _row(out, "to_exit")
    # Hand-computed segment excesses (stock TR == px; benchmark flat, +3% at DAYS[4]):
    # seg1 DAYS[1] open (10) -> DAYS[2] close (12): excess 0.2      * 0.5  = 0.1
    # seg2 DAYS[2] close -> DAYS[3] close (12 -> 13): excess 1/12    * 1/3  ~= 0.027778
    # seg3 DAYS[3] close -> DAYS[4] close (13 -> 14, bench 100->103):
    #     excess 1/13 - 0.03                                        * 1/6  ~= 0.007821
    # seg4 DAYS[4] close -> DAYS[5] close, weight 0.0                            = 0
    expected = 0.5 * (12 / 10 - 1) + (1 / 3) * (13 / 12 - 1) + (1 / 6) * (14 / 13 - 1 - 0.03)
    assert to_exit["contribution"] == pytest.approx(expected)
    assert to_exit["contribution"] != pytest.approx(
        0.5 * float(to_exit["excess_return"])  # decay != single weight x to-exit
    )
    # Fixed horizon still uses the first (full) segment weight.
    fixed = _row(out, "d2")
    assert fixed["position_weight"] == pytest.approx(0.5)


def test_non_investable_and_insufficient_consensus_reasons() -> None:
    intervals = _intervals(
        [
            _interval("I1", "A", "not_rated", investable=False, entry=1, exit_=4),
            _interval("I2", "B", "buy", entry=1, exit_=4),
        ]
    )
    exposures = _exposures(
        [
            _segment("I1", 1, 1, "open", 4, "close", None, "rating"),
            _segment("I2", 1, 1, "open", 4, "close", None, "consensus"),
        ]
    )
    methodology = _methodology(scoring_mode=ScoringMode.RELATIVE_TO_CONSENSUS)
    returns = _returns(intervals, methodology)
    out = _contributions(intervals, exposures, returns, methodology)
    _assert_contract(out)
    for horizon in ("to_exit", "d2"):
        row_i1 = out.filter(
            (pl.col("interval_id") == "I1") & (pl.col("horizon") == horizon)
        ).to_dicts()[0]
        assert row_i1["unavailable_reason"] == "non_investable"
        assert row_i1["contribution"] is None
        assert row_i1["position_weight"] is None
        row_i2 = out.filter(
            (pl.col("interval_id") == "I2") & (pl.col("horizon") == horizon)
        ).to_dicts()[0]
        assert row_i2["unavailable_reason"] == "insufficient_consensus"
        assert row_i2["contribution"] is None
        assert row_i2["position_weight"] is None


def test_horizon_incomplete_propagates() -> None:
    intervals = _intervals([_interval(entry=1, exit_=4)])
    exposures = _exposures([_segment("I1", 1, 1, "open", 4, "close", 0.5)])
    methodology = _methodology(fixed_horizons_days=(2, 5), primary_fixed_horizon_days=2)
    returns = _returns(intervals, methodology, context=_context(as_of=DAYS[5]))
    out = _contributions(
        intervals, exposures, returns, methodology, context=_context(as_of=DAYS[5])
    )
    _assert_contract(out)
    fixed = _row(out, "d5")
    assert fixed["unavailable_reason"] == "horizon_incomplete"
    assert fixed["contribution"] is None
    assert fixed["excess_return"] is None
    assert fixed["position_weight"] == pytest.approx(0.5)
    assert _row(out, "to_exit")["contribution"] == pytest.approx(0.185)
