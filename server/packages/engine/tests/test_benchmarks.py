"""Hand-computed custom-benchmark tests (decision 0003 conventions)."""

from datetime import date, datetime, time, timedelta

import polars as pl
import pytest

from callscore_engine.benchmarks import (
    BenchmarkFrames,
    BenchmarkSpec,
    UniverseFilter,
    build_custom_benchmark,
)

DATES = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]


def _ts(day: date) -> datetime:
    return datetime.combine(day, time(9, 30))


def _sessions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "exchange_id": ["XNYS"] * len(DATES),
            "session_date": DATES,
            "open_ts": [_ts(d) for d in DATES],
            "close_ts": [_ts(d) + timedelta(hours=6) for d in DATES],
        }
    )


def _security_daily(security_id: str, tr_index: list[float]) -> pl.DataFrame:
    """open = close = 100 so open_tr = close_tr = tr_index and f_close = 1."""
    n = len(tr_index)
    return pl.DataFrame(
        {
            "security_id": [security_id] * n,
            "session_date": DATES[:n],
            "open": [100.0] * n,
            "close": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "adj_factor": [1.0] * n,
            "cash_dividend": [None] * n,
            "tr_index": tr_index,
            "is_halted": [False] * n,
        }
    )


def _classifications(*security_ids: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "security_id": list(security_ids),
            "from_date": [date(2023, 12, 1)] * len(security_ids),
            "to_date": [None] * len(security_ids),
            "exchange_id": ["XNYS"] * len(security_ids),
            "currency": ["USD"] * len(security_ids),
            "sector_id": ["SEC1"] * len(security_ids),
            "region": ["US"] * len(security_ids),
            "cap_bucket": ["large"] * len(security_ids),
            "listing_date": [date(2023, 12, 1)] * len(security_ids),
            "delisting_date": [None] * len(security_ids),
        }
    )


def _frames(
    securities: dict[str, list[float]],
    classified: tuple[str, ...] = (),
    liquidity: pl.DataFrame | None = None,
) -> BenchmarkFrames:
    daily = pl.concat([_security_daily(sid, tr) for sid, tr in securities.items()])
    classified_ids = classified or tuple(securities)
    return BenchmarkFrames(
        security_daily=daily,
        security_classifications=_classifications(*classified_ids),
        sessions=_sessions(),
        liquidity_daily=liquidity,
    )


def _opens(result) -> list[float]:
    return result.frame.sort("session_date")["open_level"].to_list()


def _closes(result) -> list[float]:
    return result.frame.sort("session_date")["close_level"].to_list()


def test_equal_weight_daily_rebalance_tr_consistency() -> None:
    """Two equal-weight members, open=close: basket factor is the mean of the
    members' open factors each day (f_close = 1 throughout)."""
    # A: +10% then +10%; B: -5% then -5% (close-to-close via tr_index).
    frames = _frames(
        {"A": [100.0, 110.0, 121.0], "B": [100.0, 95.0, 90.25]},
        classified=("A", "B"),
    )
    spec = BenchmarkSpec(benchmark_id="C1", preset="custom", weighting="equal", rebalance="daily")
    result = build_custom_benchmark(spec, frames)
    # Day 1: both members' first session -> F_open = F_close = 1 -> 100/100.
    # Day 2: F_open = (1.10 + 0.95)/2 = 1.025 -> open = close = 102.5.
    # Day 3: same factor -> 102.5 * 1.025 = 105.0625.
    assert _opens(result) == pytest.approx([100.0, 102.5, 105.0625])
    assert _closes(result) == pytest.approx([100.0, 102.5, 105.0625])
    assert result.exclusion_counts == {}
    # Contract conformance is asserted inside build_custom_benchmark.
    assert result.frame["benchmark_id"].unique().to_list() == ["C1"]


def test_monthly_rebalance_drifts_weights() -> None:
    """With monthly rebalancing weights drift with TR between rebalances."""
    frames = _frames(
        {"A": [100.0, 110.0, 121.0], "B": [100.0, 95.0, 90.25]},
        classified=("A", "B"),
    )
    spec = BenchmarkSpec(benchmark_id="C2", preset="custom", weighting="equal", rebalance="monthly")
    result = build_custom_benchmark(spec, frames)
    opens = _opens(result)
    closes = _closes(result)
    assert opens[0] == pytest.approx(100.0)
    # Day 2 drifted weights: A = 1.1/2.05, B = 0.95/2.05.
    # F_open = (1.1*1.1 + 0.95*0.95)/2.05 = 2.1125/2.05.
    f = 2.1125 / 2.05
    assert opens[1] == pytest.approx(100.0 * f)
    # Day 3 drift compounds from day-2 weights anchored at D1 closes:
    # drift A = 121/100 = 1.21, B = 90.25/100 = 0.9025 ->
    # F = (1.21*1.1 + 0.9025*0.95)/(1.21+0.9025) = 2.188375/2.1125.
    f3 = 2.188375 / 2.1125
    assert opens[2] == pytest.approx(closes[1] * f3)


def test_missing_classification_is_excluded_and_counted() -> None:
    frames = _frames(
        {"A": [100.0, 110.0, 121.0], "C": [100.0, 200.0, 400.0]},
        classified=("A",),  # C trades but has no classification spell
    )
    spec = BenchmarkSpec(benchmark_id="C3", preset="custom", weighting="equal", rebalance="daily")
    result = build_custom_benchmark(spec, frames)
    # Only A is a member: levels track A's tr_index exactly.
    assert _closes(result) == pytest.approx([100.0, 110.0, 121.0])
    # One no_classification exclusion per daily rebalance.
    assert result.exclusion_counts.get("no_classification") == 3


def test_cap_weighted_requires_and_uses_liquidity() -> None:
    liquidity = pl.DataFrame(
        {
            "security_id": ["A"] * 3,
            "session_date": DATES,
            "adv_20d": [1000.0] * 3,
            "free_float": [0.5] * 3,
        }
    )
    frames = _frames(
        {"A": [100.0, 110.0, 121.0], "B": [100.0, 95.0, 90.25]},
        classified=("A", "B"),
        liquidity=liquidity,
    )
    spec = BenchmarkSpec(
        benchmark_id="C4",
        preset="custom",
        weighting="cap_weighted",
        rebalance="daily",
    )
    result = build_custom_benchmark(spec, frames)
    # B lacks liquidity -> excluded every rebalance; A alone -> tracks A.
    assert _closes(result) == pytest.approx([100.0, 110.0, 121.0])
    assert result.exclusion_counts.get("no_liquidity") == 3


def test_cap_weighted_without_liquidity_data_is_flat_and_counted() -> None:
    frames = _frames({"A": [100.0, 110.0, 121.0]}, classified=("A",))
    spec = BenchmarkSpec(
        benchmark_id="C5",
        preset="custom",
        weighting="cap_weighted",
        rebalance="daily",
    )
    result = build_custom_benchmark(spec, frames)
    # No liquidity_daily at all: every member lacks adv -> flat 100 levels.
    assert _opens(result) == pytest.approx([100.0] * 3)
    assert _closes(result) == pytest.approx([100.0] * 3)
    assert result.exclusion_counts.get("no_liquidity") == 3


def test_universe_filter_sector() -> None:
    classifications = pl.DataFrame(
        {
            "security_id": ["A", "B"],
            "from_date": [date(2023, 12, 1)] * 2,
            "to_date": [None] * 2,
            "exchange_id": ["XNYS"] * 2,
            "currency": ["USD"] * 2,
            "sector_id": ["SEC1", "SEC2"],
            "region": ["US"] * 2,
            "cap_bucket": ["large"] * 2,
            "listing_date": [date(2023, 12, 1)] * 2,
            "delisting_date": [None] * 2,
        }
    )
    daily = pl.concat(
        [_security_daily("A", [100.0, 110.0, 121.0]), _security_daily("B", [100.0, 50.0, 25.0])]
    )
    frames = BenchmarkFrames(
        security_daily=daily,
        security_classifications=classifications,
        sessions=_sessions(),
    )
    spec = BenchmarkSpec(
        benchmark_id="C6",
        preset="custom",
        universe=UniverseFilter(sector_id="SEC1"),
        weighting="equal",
        rebalance="daily",
    )
    result = build_custom_benchmark(spec, frames)
    assert _closes(result) == pytest.approx([100.0, 110.0, 121.0])


def test_sector_neutral_requires_sector_id() -> None:
    frames = _frames({"A": [100.0, 110.0, 121.0]}, classified=("A",))
    spec = BenchmarkSpec(benchmark_id="C7", preset="sector_neutral")
    with pytest.raises(ValueError, match="sector_id"):
        build_custom_benchmark(spec, frames)


def test_sector_neutral_long_sector_short_broad() -> None:
    """Long SEC1 (A), short broad (A+B equal): net factor = F_long - F_short + 1.

    With open=close members, f_close = 1 and only the open leg moves.
    Day 2: F_long = 1.10 (A alone), F_short = (1.10 + 0.50)/2 = 0.80
    -> F = 1 + 0.10 - (-0.20) = 1.30? Careful: F = F_long - F_short + 1
    = 1.10 - 0.80 + 1 = 1.30. open_level = 100 * 1.30 = 130.
    """
    classifications = pl.DataFrame(
        {
            "security_id": ["A", "B"],
            "from_date": [date(2023, 12, 1)] * 2,
            "to_date": [None] * 2,
            "exchange_id": ["XNYS"] * 2,
            "currency": ["USD"] * 2,
            "sector_id": ["SEC1", "SEC2"],
            "region": ["US"] * 2,
            "cap_bucket": ["large"] * 2,
            "listing_date": [date(2023, 12, 1)] * 2,
            "delisting_date": [None] * 2,
        }
    )
    daily = pl.concat(
        [_security_daily("A", [100.0, 110.0, 121.0]), _security_daily("B", [100.0, 50.0, 25.0])]
    )
    frames = BenchmarkFrames(
        security_daily=daily,
        security_classifications=classifications,
        sessions=_sessions(),
    )
    spec = BenchmarkSpec(
        benchmark_id="C8",
        preset="sector_neutral",
        sector_id="SEC1",
        weighting="equal",
        rebalance="daily",
    )
    result = build_custom_benchmark(spec, frames)
    opens = _opens(result)
    closes = _closes(result)
    # Day 1: both legs flat -> 100/100.
    assert opens[0] == pytest.approx(100.0)
    # Day 2: F_open = F_long - F_short + 1 = 1.10 - 0.80 + 1 = 1.30;
    # F_close = 1 - 1 + 1 = 1 -> open = close = 130.
    assert opens[1] == pytest.approx(130.0)
    assert closes[1] == pytest.approx(130.0)
    # Day 3: long factor 121/110 = 1.1; short = (121/110 + 25/50)/2
    #        = (1.1 + 0.5)/2 = 0.8 -> F = 1.1 - 0.8 + 1 = 1.3 -> 169.
    assert opens[2] == pytest.approx(169.0)
    assert closes[2] == pytest.approx(169.0)
