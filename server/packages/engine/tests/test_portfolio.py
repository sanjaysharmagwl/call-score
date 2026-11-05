"""Hand-computed portfolio simulation tests (decision 0003 conventions)."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import polars as pl
import pytest

from callscore_core.contracts import RunContext
from callscore_core.methodology import Methodology
from callscore_engine.portfolio import portfolio_positions, simulate_portfolios
from callscore_engine.pricing import MissingBenchmarkData

# Trading calendar: five weekdays plus the following Monday/Tuesday.
DATES = [
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
    date(2024, 1, 9),
    date(2024, 1, 10),
]
AS_OF = DATES[-1]


def _ts(day: date) -> datetime:
    return datetime.combine(day, time(9, 30), tzinfo=UTC)


def _sessions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "exchange_id": ["XNYS"] * len(DATES),
            "session_date": DATES,
            "open_ts": [_ts(d) for d in DATES],
            "close_ts": [_ts(d) + timedelta(hours=6) for d in DATES],
        }
    )


def _security_daily(
    security_id: str,
    tr_index: list[float],
    opens: list[float] | None = None,
    closes: list[float] | None = None,
    adj_factors: list[float] | None = None,
    dividends: list[float | None] | None = None,
    start: int = 0,
) -> pl.DataFrame:
    """Contract-shaped ``security_daily``; open=close=100 unless overridden."""
    n = len(tr_index)
    opens = opens or [100.0] * n
    closes = closes or [100.0] * n
    adj = adj_factors or [1.0] * n
    divs = dividends if dividends is not None else [None] * n
    return pl.DataFrame(
        {
            "security_id": [security_id] * n,
            "session_date": DATES[start : start + n],
            "open": opens,
            "close": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "adj_factor": adj,
            "cash_dividend": divs,
            "tr_index": tr_index,
            "is_halted": [False] * n,
        }
    )


# S1: open=close=100 with tr_index driving returns, except:
#   D1 open=95 (entry-at-open earns 100/95 - 1),
#   D3 cash_dividend 0.5 and D4 a 2:1 split (adj 1->2, raw halves to 50).
# tr_index is already total-return, so the dividend/split days must NOT create
# artificial jumps: close_tr moves 101 -> 103 straight through.
S1_TR = [100.0, 102.0, 101.0, 103.0, 106.0, 107.0, 108.0]
S1 = _security_daily(
    "S1",
    S1_TR,
    opens=[95.0, 100.0, 100.0, 50.0, 50.0, 50.0, 50.0],
    closes=[100.0, 100.0, 100.0, 50.0, 50.0, 50.0, 50.0],
    adj_factors=[1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 2.0],
    dividends=[None, None, 0.5, None, None, None, None],
)

# S2: plain close=100 series, tr_index [100,102,101,103,105,106,107].
S2_TR = [100.0, 102.0, 101.0, 103.0, 105.0, 106.0, 107.0]
S2 = _security_daily("S2", S2_TR)

# S3: four sessions then delisted (bankruptcy, final_return = -1).
S3_TR = [100.0, 101.0, 102.0, 103.0]
S3 = _security_daily("S3", S3_TR)

SECURITY_DAILY = pl.concat([S1, S2, S3])


def _benchmark_daily() -> pl.DataFrame:
    # IDX_BROAD levels; open == close so point choice does not matter for it.
    levels = [1000.0, 1010.0, 1005.0, 1020.0, 1030.0, 1040.0, 1050.0]
    return pl.DataFrame(
        {
            "benchmark_id": ["IDX_BROAD"] * len(DATES),
            "session_date": DATES,
            "open_level": levels,
            "close_level": levels,
        }
    )


def _classifications() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "security_id": ["S1", "S2", "S3"],
            "from_date": [date(2020, 1, 1)] * 3,
            "to_date": [None] * 3,
            "exchange_id": ["XNYS"] * 3,
            "sector_id": ["TECH", "HLTH", "TECH"],
        }
    )


def _intervals(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def _exposures(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def _context() -> RunContext:
    return RunContext(
        as_of_date=AS_OF,
        knowledge_cutoff=datetime(2024, 1, 10, 23, 0, tzinfo=UTC),
        source_revision="test",
        source_fingerprint="0" * 64,
        universe_fingerprint="1" * 64,
        algorithm_version="test",
    )


def _methodology(**overrides: Any) -> Methodology:
    params: dict[str, Any] = {"benchmark_mode": "market", **overrides}
    return Methodology(**params)


def _seg(
    interval_id: str,
    segment_no: int,
    start: int,
    start_point: str,
    end: int,
    end_point: str,
    weight: float | None,
) -> dict[str, Any]:
    return {
        "interval_id": interval_id,
        "segment_no": segment_no,
        "start_session": DATES[start],
        "start_point": start_point,
        "end_session": DATES[end],
        "end_point": end_point,
        "position_weight": weight,
        "weight_source": "rating",
    }


def _interval(interval_id: str, analyst: str, security: str, entry: int) -> dict[str, Any]:
    return {
        "interval_id": interval_id,
        "event_id": f"E_{interval_id}",
        "analyst_id": analyst,
        "firm_id": "F1",
        "security_id": security,
        "entry_session": DATES[entry],
    }


def _run(
    intervals: pl.DataFrame,
    exposures: pl.DataFrame,
    *,
    methodology: Methodology | None = None,
    benchmark_daily: pl.DataFrame | None = None,
    beta: pl.DataFrame | None = None,
    delistings: pl.DataFrame | None = None,
    classifications: pl.DataFrame | None = None,
) -> pl.DataFrame:
    empty_beta = (
        beta
        if beta is not None
        else pl.DataFrame(
            schema={
                "security_id": pl.String(),
                "as_of_date": pl.Date(),
                "beta": pl.Float64(),
                "window_days": pl.Int64(),
            }
        )
    )
    return simulate_portfolios(
        intervals,
        exposures,
        SECURITY_DAILY,
        benchmark_daily if benchmark_daily is not None else _benchmark_daily(),
        empty_beta,
        delistings if delistings is not None else _DELISTINGS,
        classifications if classifications is not None else _classifications(),
        _sessions(),
        methodology if methodology is not None else _methodology(),
        _context(),
    )


_DELISTINGS = pl.DataFrame(
    {
        "security_id": ["S3"],
        "delisting_date": [DATES[3]],
        "final_return": [-1.0],
        "reason": "bankruptcy",
    }
)


def _col(frame: pl.DataFrame, name: str) -> list[float]:
    return [float(v) for v in frame[name].to_list()]


def test_open_entry_close_exit_multi_session() -> None:
    """A1 longs S1 from D1 open to D4 close; flat afterwards (idle tail)."""
    intervals = _intervals([_interval("I1", "A1", "S1", 0)])
    exposures = _exposures([_seg("I1", 1, 0, "open", 3, "close", 1.0)])
    out = _run(intervals, exposures)

    assert out.columns == [
        "analyst_id",
        "session_date",
        "nav",
        "benchmark_nav",
        "active_return",
        "gross_exposure",
        "turnover",
        "cost",
        "drawdown",
    ]
    assert out["session_date"].to_list() == DATES
    assert out["analyst_id"].unique().to_list() == ["A1"]

    # Hand-computed legs (TR levels; open==close except S1 D1 open=95):
    # stock r:  D1 100/95-1 = 1/19;  D2 102/100-1 = 1/50;  D3 101/102-1 = -1/102;
    #           D4 103/101-1 = 2/101
    # bench b:  D1 0 (open->close, levels equal);  D2 1010/1000-1 = 1/100;
    #           D3 1005/1010-1 = -1/202;           D4 1020/1005-1 = 1/67
    excess = [1 / 19, 1 / 100, -1 / 102 + 1 / 202, 2 / 101 - 1 / 67, 0.0, 0.0, 0.0]
    hedge = [0.0, 1 / 100, -1 / 202, 1 / 67, 0.0, 0.0, 0.0]
    nav, bench_nav, peak = 1.0, 1.0, 1.0
    exp_nav, exp_bench, exp_dd = [], [], []
    for e, h in zip(excess, hedge, strict=True):
        nav *= 1 + e
        bench_nav *= 1 + h
        peak = max(peak, nav)
        exp_nav.append(nav)
        exp_bench.append(bench_nav)
        exp_dd.append(nav / peak - 1)

    assert _col(out, "active_return") == pytest.approx(excess)
    assert _col(out, "nav") == pytest.approx(exp_nav)
    assert _col(out, "benchmark_nav") == pytest.approx(exp_bench)
    assert _col(out, "drawdown") == pytest.approx(exp_dd)
    # Weight is 1 on D1..D4; turnover on entry (D1) and first idle day (D5).
    assert _col(out, "gross_exposure") == pytest.approx([1, 1, 1, 1, 0, 0, 0])
    assert _col(out, "turnover") == pytest.approx([1, 0, 0, 0, 1, 0, 0])
    assert _col(out, "cost") == pytest.approx([0.0] * 7)
    # Drawdown is genuinely negative mid-path (D3 dip below the D2 peak).
    assert min(exp_dd) < 0


def test_long_short_renormalized() -> None:
    """S1 raw +1.0 and S2 raw -0.5 rescale to 2/3 and -1/3 (gross stays 1)."""
    intervals = _intervals([_interval("I1", "A1", "S1", 0), _interval("I2", "A1", "S2", 1)])
    exposures = _exposures(
        [
            _seg("I1", 1, 0, "open", 6, "close", 1.0),
            _seg("I2", 1, 1, "open", 5, "close", -0.5),
        ]
    )
    out = _run(intervals, exposures)
    positions = portfolio_positions(
        intervals,
        exposures,
        SECURITY_DAILY,
        _DELISTINGS,
        _classifications(),
        _sessions(),
        _context(),
    )

    # Position weights on a mid day: S1 = 1/1.5 = 2/3, S2 = -0.5/1.5 = -1/3.
    mid = positions.filter((pl.col("session_date") == DATES[2]) & (pl.col("security_id") == "S1"))
    assert mid["weight"].item() == pytest.approx(2 / 3)
    mid2 = positions.filter((pl.col("session_date") == DATES[2]) & (pl.col("security_id") == "S2"))
    assert mid2["weight"].item() == pytest.approx(-1 / 3)

    # Turnover: entry D1 = 1; S2 enters D2 -> |2/3-1| + |-1/3-0| = 2/3;
    # S2 exits after D6 -> same 2/3 on D7.
    assert _col(out, "turnover") == pytest.approx([1, 2 / 3, 0, 0, 0, 0, 2 / 3])
    assert _col(out, "gross_exposure")[0] == pytest.approx(1.0)

    # Hand-compute excess per day. S1 legs (entry D1 open, exit D7 close):
    #   r: 1/19, 1/50, -1/102, 2/101, 3/103, 1/106, 1/107
    #   b: 0,   1/100, -1/202, 1/67, 1/102, 1/103, 1/104
    # S2 legs (entry D2 open -> r=0,b=0 that day; exit D6 close):
    #   r: -, 0, -1/102, 2/101, 2/103, 1/105, -
    #   b: -, 0, -1/202, 1/67, 1/102, 1/103, -
    s1_r = [1 / 19, 1 / 50, -1 / 102, 2 / 101, 3 / 103, 1 / 106, 1 / 107]
    s1_b = [0.0, 1 / 100, -1 / 202, 1 / 67, 1 / 102, 1 / 103, 1 / 104]
    s2_r = [None, 0.0, -1 / 102, 2 / 101, 2 / 103, 1 / 105, None]
    s2_b = [None, 0.0, -1 / 202, 1 / 67, 1 / 102, 1 / 103, None]
    expected = []
    for i in range(7):
        day = {"S1": 2 / 3, "S2": -1 / 3} if 1 <= i <= 5 else {"S1": 1.0}
        if i == 0:
            day = {"S1": 1.0}
        gross = 0.0
        for sec, wt in day.items():
            r = (s1_r if sec == "S1" else s2_r)[i]
            b = (s1_b if sec == "S1" else s2_b)[i]
            if r is not None and b is not None:
                gross += wt * (r - b)
        expected.append(gross)
    assert _col(out, "active_return") == pytest.approx(expected)


def test_decay_step_turnover_and_cost() -> None:
    """S2 weight decays 1.0 -> 0.5 mid-book; rescaling creates turnover + cost."""
    intervals = _intervals([_interval("I1", "A1", "S1", 0), _interval("I2", "A1", "S2", 0)])
    exposures = _exposures(
        [
            _seg("I1", 1, 0, "open", 4, "close", 1.0),
            _seg("I2", 1, 0, "open", 2, "close", 1.0),
            _seg("I2", 2, 3, "open", 4, "close", 0.5),
        ]
    )
    out = _run(intervals, exposures, methodology=_methodology(transaction_cost_bps=300))

    # D1-D3: raw (1,1) -> w=(0.5,0.5); D4: raw (1,0.5) -> w=(2/3,1/3);
    # D5: both flat -> turnover = 2/3+1/3 = 1.
    assert _col(out, "turnover") == pytest.approx([1, 0, 0, 1 / 3, 0, 1, 0])
    # cost = turnover * 300/10000 = turnover * 0.03
    assert _col(out, "cost") == pytest.approx([0.03, 0, 0, 0.01, 0, 0.03, 0])
    # active_return = gross_excess - cost; on the all-cash D5 it is -cost.
    assert _col(out, "active_return")[5] == pytest.approx(-0.03)
    assert _col(out, "gross_exposure")[3] == pytest.approx(1.0)


def test_zero_vs_positive_cost() -> None:
    """Same book, transaction_cost_bps 0 vs 100 (1%): cost column and nav differ."""
    intervals = _intervals([_interval("I1", "A1", "S1", 0)])
    exposures = _exposures([_seg("I1", 1, 0, "open", 3, "close", 1.0)])
    free = _run(intervals, exposures)
    costly = _run(intervals, exposures, methodology=_methodology(transaction_cost_bps=100))

    assert _col(free, "cost") == pytest.approx([0.0] * 7)
    # turnover 1 on D1 (entry) and D5 (first flat day) -> cost 0.01 each.
    assert _col(costly, "cost") == pytest.approx([0.01, 0, 0, 0, 0.01, 0, 0])
    # active_return absorbs the cost.
    assert _col(costly, "active_return")[0] == pytest.approx(_col(free, "active_return")[0] - 0.01)
    assert _col(costly, "nav")[-1] < _col(free, "nav")[-1]


def test_bankruptcy_delisting_realizes_terminal_value() -> None:
    """S3 delists at D4 with final_return -1: the position loses everything."""
    intervals = _intervals([_interval("I1", "A2", "S3", 0)])
    exposures = _exposures([_seg("I1", 1, 0, "open", 6, "close", 1.0)])
    out = _run(intervals, exposures)

    assert out["session_date"].to_list() == DATES  # idle tail via exchange calendar
    # Legs: D1 entry open r=0,b=0; D2 r=1/100,b=1/100 -> e=0;
    # D3 r=102/101-1=1/101, b=-1/202 -> e=1/101+1/202; D4 terminal:
    # r = 103*(1-1)/102 - 1 = -1, b=1/67 -> e=-68/67; D5+ flat.
    e3 = 1 / 101 + 1 / 202
    e4 = -1 - 1 / 67
    assert _col(out, "active_return") == pytest.approx([0.0, 0.0, e3, e4, 0.0, 0.0, 0.0])
    # Position closes at the delisting: weight drops to 0 on D5 -> turnover 1.
    assert _col(out, "gross_exposure") == pytest.approx([1, 1, 1, 1, 0, 0, 0])
    assert _col(out, "turnover") == pytest.approx([1, 0, 0, 0, 1, 0, 0])
    # nav compounds through the -68/67 day (can go slightly negative: hedged
    # excess loss exceeds 100% because the short hedge leg gained).
    nav = 1.0
    for e in [0.0, 0.0, e3, e4, 0.0, 0.0, 0.0]:
        nav *= 1 + e
    assert _col(out, "nav")[-1] == pytest.approx(nav)


def test_idle_gap_between_positions() -> None:
    """Exit at D4 close, re-enter at D6 open: D5 is an idle all-cash row."""
    intervals = _intervals([_interval("I1", "A1", "S1", 0), _interval("I2", "A1", "S1", 5)])
    exposures = _exposures(
        [
            _seg("I1", 1, 0, "open", 3, "close", 1.0),
            _seg("I2", 1, 5, "open", 6, "close", 1.0),
        ]
    )
    out = _run(intervals, exposures)
    assert out["session_date"].to_list() == DATES
    # D5: no weight -> active_return 0, gross 0; turnover 1 (exit) with 0 cost.
    row5 = out.filter(pl.col("session_date") == DATES[4])
    assert row5["active_return"].item() == 0.0
    assert row5["gross_exposure"].item() == 0.0
    assert row5["turnover"].item() == pytest.approx(1.0)
    # D6 entry at open: S1 open_tr == close_tr (open==close) -> r = 0, b = 0.
    assert _col(out, "active_return")[5] == pytest.approx(0.0)
    assert _col(out, "turnover") == pytest.approx([1, 0, 0, 0, 1, 1, 0])
    # D7 close-to-close leg: r=108/107-1=1/107, b=1050/1040-1=1/104.
    assert _col(out, "active_return")[6] == pytest.approx(1 / 107 - 1 / 104)


def test_entry_at_close_earns_zero_that_session() -> None:
    """Entry point 'close' -> entry session return is 0 but turnover is charged."""
    intervals = _intervals([_interval("I1", "A1", "S1", 1)])
    exposures = _exposures([_seg("I1", 1, 1, "close", 3, "close", 1.0)])
    out = _run(intervals, exposures)
    # Rows still start at the first entry session (D2), not before.
    assert out["session_date"].to_list() == DATES[1:]
    assert _col(out, "active_return")[0] == pytest.approx(0.0)
    assert _col(out, "turnover")[0] == pytest.approx(1.0)
    # D3 is a normal close-to-close day: r=101/102-1=-1/102, b=-1/202.
    assert _col(out, "active_return")[1] == pytest.approx(-1 / 102 + 1 / 202)
    # D4 exits at close: r=103/101-1=2/101, b=1/67.
    assert _col(out, "active_return")[2] == pytest.approx(2 / 101 - 1 / 67)


def test_weight_sign_reversal_negates_active_return() -> None:
    """Buy vs sell on identical spans: active_return is exactly negated (cost 0)."""
    exposures = _exposures([_seg("I1", 1, 0, "open", 3, "close", 1.0)])
    buy = _run(_intervals([_interval("I1", "A1", "S1", 0)]), exposures)
    sell = _run(
        _intervals([_interval("I1", "A1", "S1", 0)]),
        _exposures([_seg("I1", 1, 0, "open", 3, "close", -1.0)]),
    )
    assert _col(sell, "active_return") == pytest.approx([-v for v in _col(buy, "active_return")])
    # Turnover is identical (|w| diffs), so with cost 0 nothing else differs
    # in the active leg; nav paths diverge only through compounding.
    assert _col(sell, "turnover") == pytest.approx(_col(buy, "turnover"))


def test_beta_adjusted_hedge_uses_latest_beta_before_entry() -> None:
    """beta_adjusted: hedge = beta * market; beta as_of must be < entry_session."""
    beta = pl.DataFrame(
        {
            "security_id": ["S1", "S1"],
            "as_of_date": [date(2023, 12, 29), DATES[0]],
            "beta": [1.5, 99.0],
            "window_days": [60, 60],
        }
    )
    intervals = _intervals([_interval("I1", "A1", "S1", 0)])
    exposures = _exposures([_seg("I1", 1, 0, "open", 3, "close", 1.0)])
    out = _run(
        intervals,
        exposures,
        methodology=_methodology(benchmark_mode="beta_adjusted"),
        beta=beta,
    )
    # The D1 as_of row (== entry session) is NOT strictly before -> ignored;
    # beta = 1.5 throughout. Market b on D2 = 1/100 -> hedged b = 3/200.
    r2, b2 = 1 / 50, 1.5 / 100
    assert _col(out, "active_return")[1] == pytest.approx(r2 - b2)
    # D3: b = 1.5 * (-1/202); r = -1/102.
    assert _col(out, "active_return")[2] == pytest.approx(-1 / 102 + 1.5 / 202)


def test_missing_benchmark_level_fails() -> None:
    bench = _benchmark_daily().filter(pl.col("session_date") != DATES[2])
    intervals = _intervals([_interval("I1", "A1", "S1", 0)])
    exposures = _exposures([_seg("I1", 1, 0, "open", 3, "close", 1.0)])
    with pytest.raises(MissingBenchmarkData):
        _run(intervals, exposures, benchmark_daily=bench)


def test_nothing_held_before_first_entry() -> None:
    """Rows start at the first entry session; earlier calendar days are absent."""
    intervals = _intervals([_interval("I1", "A1", "S1", 3)])
    exposures = _exposures([_seg("I1", 1, 3, "open", 5, "close", 1.0)])
    out = _run(intervals, exposures)
    assert out["session_date"].to_list() == DATES[3:]
    # First row is the entry day itself.
    assert out["session_date"][0] == DATES[3]


def test_portfolio_positions_columns_and_weights() -> None:
    intervals = _intervals([_interval("I1", "A1", "S1", 0), _interval("I2", "A1", "S2", 1)])
    exposures = _exposures(
        [
            _seg("I1", 1, 0, "open", 6, "close", 1.0),
            _seg("I2", 1, 1, "open", 5, "close", -0.5),
        ]
    )
    positions = portfolio_positions(
        intervals,
        exposures,
        SECURITY_DAILY,
        _DELISTINGS,
        _classifications(),
        _sessions(),
        _context(),
    )
    assert positions.columns == [
        "analyst_id",
        "session_date",
        "security_id",
        "interval_id",
        "weight",
    ]
    # D1: only S1 held -> weight 1. D7: only S1 -> weight 1.
    first = positions.filter(pl.col("session_date") == DATES[0])
    assert first.height == 1
    assert first["weight"].item() == pytest.approx(1.0)
    last = positions.filter(pl.col("session_date") == DATES[6])
    assert last["security_id"].to_list() == ["S1"]
    assert last["weight"].item() == pytest.approx(1.0)


def test_empty_inputs_return_empty_frame() -> None:
    empty_exposures = pl.DataFrame(schema={"position_weight": pl.Float64()})
    empty_intervals = pl.DataFrame(schema={"interval_id": pl.String()})
    out = simulate_portfolios(
        empty_intervals,
        empty_exposures,
        SECURITY_DAILY,
        _benchmark_daily(),
        pl.DataFrame(),
        _DELISTINGS,
        _classifications(),
        _sessions(),
        _methodology(),
        _context(),
    )
    assert out.height == 0
    assert out.columns == [
        "analyst_id",
        "session_date",
        "nav",
        "benchmark_nav",
        "active_return",
        "gross_exposure",
        "turnover",
        "cost",
        "drawdown",
    ]
