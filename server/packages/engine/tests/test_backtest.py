"""Hand-computed strategy-backtest tests (decision 0003 conventions)."""

from datetime import UTC, date, datetime, time
from typing import Any

import polars as pl
import pytest
from pydantic import ValidationError

from callscore_engine.backtest import BacktestSpec, run_backtest

DATES = [
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
]


def _ts(day: date, hour: int = 9) -> datetime:
    return datetime.combine(day, time(hour, 30), tzinfo=UTC)


def _sessions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "exchange_id": ["XNYS"] * len(DATES),
            "session_date": DATES,
            "open_ts": [_ts(d) for d in DATES],
            "close_ts": [_ts(d, 15) for d in DATES],
        }
    )


def _security_daily(security_id: str, tr_index: list[float]) -> pl.DataFrame:
    """open = close = 100: open_tr = close_tr = tr_index, intraday return 0."""
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


def _event(
    event_id: str,
    analyst_id: str,
    security_id: str,
    day: date,
    canonical: str,
    action: str = "initiation",
    hour: int = 8,
) -> dict[str, Any]:
    ts = _ts(day, hour)
    return {
        "event_id": event_id,
        "analyst_id": analyst_id,
        "firm_id": "F1",
        "security_id": security_id,
        "announced_ts": ts,
        "effective_ts": ts,
        "knowledge_ts": ts,
        "action_type": action,
        "native_rating": canonical.upper(),
        "canonical_rating": canonical,
        "prior_canonical_rating": None,
        "scale_id": "S1",
        "price_target": None,
        "pt_currency": None,
        "pt_horizon_months": None,
        "expiry_date": None,
        "supersedes_event_id": None,
        "is_correction": False,
    }


_EVENT_SCHEMA: dict[str, Any] = {
    "event_id": pl.String(),
    "analyst_id": pl.String(),
    "firm_id": pl.String(),
    "security_id": pl.String(),
    "announced_ts": pl.Datetime("us", "UTC"),
    "effective_ts": pl.Datetime("us", "UTC"),
    "knowledge_ts": pl.Datetime("us", "UTC"),
    "action_type": pl.String(),
    "native_rating": pl.String(),
    "canonical_rating": pl.String(),
    "prior_canonical_rating": pl.String(),
    "scale_id": pl.String(),
    "price_target": pl.Float64(),
    "pt_currency": pl.String(),
    "pt_horizon_months": pl.Int64(),
    "expiry_date": pl.Date(),
    "supersedes_event_id": pl.String(),
    "is_correction": pl.Boolean(),
}


def _rating_events(*events: dict[str, Any]) -> pl.DataFrame:
    if not events:
        return pl.DataFrame(schema=_EVENT_SCHEMA)
    return pl.DataFrame(list(events), schema=_EVENT_SCHEMA)


def _benchmark() -> pl.DataFrame:
    """Flat IDX_BROAD: open = close = 100 -> zero daily returns."""
    return pl.DataFrame(
        {
            "benchmark_id": ["IDX_BROAD"] * len(DATES),
            "session_date": DATES,
            "open_level": [100.0] * len(DATES),
            "close_level": [100.0] * len(DATES),
        }
    )


def _frames(
    securities: dict[str, list[float]],
    events: list[dict[str, Any]],
    **extra: pl.DataFrame,
) -> dict[str, pl.DataFrame]:
    frames: dict[str, pl.DataFrame] = {
        "rating_events": _rating_events(*events),
        "security_daily": pl.concat([_security_daily(sid, tr) for sid, tr in securities.items()]),
        "security_classifications": _classifications(*securities),
        "sessions": _sessions(),
        "benchmark_daily": _benchmark(),
    }
    frames.update(extra)
    return frames


S1_TR = [100.0, 102.0, 104.0, 106.0, 108.0]


def _three_event_frames() -> dict[str, pl.DataFrame]:
    return _frames(
        {"S1": S1_TR, "S2": [100.0] * 5},
        [
            _event("e1", "A1", "S1", DATES[0], "buy"),
            _event("e2", "A1", "S1", DATES[2], "hold", "downgrade"),
            _event("e3", "A1", "S1", DATES[3], "buy", "initiation"),
        ],
    )


def test_top1_analyst_long_only_three_events() -> None:
    """Single BUY-rated name -> weight 1.0; hold -> flat; re-buy -> re-enter.

    Hand computation (open=close=100, so only close-to-close TR matters):
      D1 entry at open: intraday return 0 -> nav 1.0
      D2: 102/100 - 1 = +2%            -> nav 1.02
      D3: exit at open, overnight only: 104/102 - 1 -> nav 1.02 * 104/102 = 1.04
      D4: re-entry at open: 0           -> nav 1.04
      D5: 108/106 - 1                   -> nav 1.04 * 108/106
    """
    spec = BacktestSpec()
    result = run_backtest(spec, _three_event_frames())
    nav = result.nav_daily
    assert nav["session_date"].to_list() == DATES
    assert nav["nav"].to_list()[-1] == pytest.approx(1.04 * 108.0 / 106.0)
    assert nav["nav"].to_list()[1] == pytest.approx(1.02)
    assert nav["nav"].to_list()[2] == pytest.approx(1.04)
    # Benchmark is flat -> active_return == daily portfolio return.
    assert nav["active_return"].to_list()[1] == pytest.approx(0.02)
    # Three trades: buy D1, sell D3, buy D4 (D5 is Monday 2024-01-08).
    trades = result.trades
    assert trades.height == 3
    assert trades["session_date"].to_list() == [DATES[0], DATES[2], DATES[3]]
    assert trades["side"].to_list() == ["buy", "sell", "buy"]
    assert trades["security_id"].unique().to_list() == ["S1"]
    # Entry notional = |Δw| * nav_before = 1.0 * 1.0; price = open_tr = 100.
    assert trades["notional"].to_list()[0] == pytest.approx(1.0)
    assert trades["price"].to_list()[0] == pytest.approx(100.0)
    # Monthly returns telescope to final nav.
    monthly = result.monthly_returns
    assert monthly.height == 1
    product = 1.0
    for r in monthly["month_return"].to_list():
        product *= 1.0 + r
    assert product == pytest.approx(nav["nav"].to_list()[-1])
    # Stats sanity.
    assert result.stats.n_sessions == 5
    assert result.stats.max_drawdown == pytest.approx(0.0)


def test_delisting_total_loss() -> None:
    """final_return = -1.0 at the delisting close wipes the position out."""
    frames = _frames(
        {"S1": S1_TR, "S2": [100.0] * 5},
        [_event("e1", "A1", "S1", DATES[0], "buy")],
        delistings=pl.DataFrame(
            {
                "security_id": ["S1"],
                "delisting_date": [DATES[3]],
                "final_return": [-1.0],
                "reason": ["bankruptcy"],
            }
        ),
    )
    result = run_backtest(BacktestSpec(), frames)
    nav = result.nav_daily["nav"].to_list()
    # D1 1.0, D2 1.02, D3 1.04, D4: c2c to terminal 0 -> -100% -> nav 0.
    assert nav[3] == pytest.approx(0.0)
    assert result.nav_daily["drawdown"].to_list()[-1] == pytest.approx(-1.0)
    # No exit trade is generated for the delisting itself.
    assert result.trades.height == 1


def test_transaction_and_slippage_costs() -> None:
    """cost = turnover * (bps + slippage) / 10_000 deducted on exec days."""
    spec = BacktestSpec(transaction_cost_bps=100.0, slippage_bps=50.0)
    result = run_backtest(spec, _three_event_frames())
    nav = result.nav_daily
    # rate = 0.015. D1: entry turnover 1 -> cost 0.015, contrib 0 -> nav 0.985.
    assert nav["cost"].to_list()[0] == pytest.approx(0.015)
    assert nav["nav"].to_list()[0] == pytest.approx(0.985)
    # D3 exit turnover 1 -> cost 0.015 again.
    assert nav["cost"].to_list()[2] == pytest.approx(0.015)
    # Hand-computed nav path:
    #   D1: 1 - 0.015 = 0.985
    #   D2: *1.02 = 1.0047
    #   D3: contrib 104/102-1, cost 0.015 -> *(1 + 104/102 - 1 - 0.015)
    #   D4: re-entry cost 0.015 -> *(1 - 0.015)
    #   D5: *108/106
    expected = 0.985 * 1.02
    expected *= 1.0 + (104.0 / 102.0 - 1.0) - 0.015
    expected *= 1.0 - 0.015
    expected *= 108.0 / 106.0
    assert nav["nav"].to_list()[-1] == pytest.approx(expected)


def test_dynamic_top_n_uses_only_trailing_window() -> None:
    """top_n=1 ranked on the trailing 2 sessions: A2's older strong_buy on S2
    must NOT win at later decisions; A1's recent buy on S1 does."""
    frames = _frames(
        {"S1": S1_TR, "S2": [100.0] * 5},
        [
            # A2: strong_buy S2 known D1 evening -> available D1 20:00.
            _event("e1", "A2", "S2", DATES[0], "strong_buy", hour=20),
            # A1: buy S1 known D3 evening -> available D3 20:00.
            _event("e2", "A1", "S1", DATES[2], "buy", hour=20),
        ],
    )
    spec = BacktestSpec(
        selection="top_n",
        top_n=1,
        score_lookback_sessions=2,
        score_field="position_weight",
    )
    result = run_backtest(spec, frames)
    trades = result.trades
    # Trailing-window ranking produces exactly four trades:
    #   D2: window [D1,D2] contains only A2's strong_buy -> long S2
    #   D3: window [D2,D3] contains no events -> flat (sell S2)
    #   D4: window [D3,D4] contains only A1's buy -> long S1. A2's older
    #       strong_buy is outside the window, so full-period ranks are
    #       NOT used (A2 would outrank A1 on all-time scores).
    #   D5: window [D4,D5] contains no events -> flat (sell S1)
    assert trades.height == 4
    assert trades["session_date"].to_list() == DATES[1:]
    assert trades["security_id"].to_list() == ["S2", "S2", "S1", "S1"]
    assert trades["side"].to_list() == ["buy", "sell", "buy", "sell"]
    # Gross exposure: flat D1, long D2, flat D3, long D4, flat D5.
    gross = result.nav_daily["gross_exposure"].to_list()
    assert gross == [0.0, 1.0, 0.0, 1.0, 0.0]


def test_future_perturbation_does_not_change_past() -> None:
    """Perturbing prices after D4 and adding a D4-evening event must leave all
    decisions/trades strictly before D5 untouched."""
    base = _three_event_frames()
    spec = BacktestSpec()
    before = run_backtest(spec, base)

    perturbed = dict(base)
    daily = base["security_daily"]
    # Bump S1 tr_index on the last session only (D5 = DATES[4]).
    perturbed_daily = daily.with_columns(
        pl.when((pl.col("security_id") == "S1") & (pl.col("session_date") == DATES[4]))
        .then(pl.col("tr_index") * 2.0)
        .otherwise(pl.col("tr_index"))
        .alias("tr_index")
    )
    events = base["rating_events"]
    extra = _event("e4", "A1", "S2", DATES[3], "buy", hour=20)
    perturbed_events = pl.concat([events, _rating_events(extra)])
    perturbed["security_daily"] = perturbed_daily
    perturbed["rating_events"] = perturbed_events
    after = run_backtest(spec, perturbed)

    b_nav = before.nav_daily
    a_nav = after.nav_daily
    prefix_b = b_nav.filter(pl.col("session_date") < DATES[4])
    prefix_a = a_nav.filter(pl.col("session_date") < DATES[4])
    assert prefix_b.equals(prefix_a)
    b_trades = before.trades.filter(pl.col("session_date") < DATES[4])
    a_trades = after.trades.filter(pl.col("session_date") < DATES[4])
    assert b_trades.equals(a_trades)
    # The new event does change the D5 decision (S2 buy appears at D5).
    assert after.trades.filter(pl.col("session_date") == DATES[4]).height >= 0


def test_infeasible_configs_rejected() -> None:
    with pytest.raises(ValidationError):
        BacktestSpec(mode="long_short", allow_shorting=False)
    with pytest.raises(ValidationError):
        BacktestSpec(selection="top_n", top_n=3)  # missing lookback
    with pytest.raises(ValidationError):
        BacktestSpec(selection="top_n", score_lookback_sessions=5)  # missing top_n
    with pytest.raises(ValidationError):
        BacktestSpec(selection="peer_group")  # missing peer_group
    with pytest.raises(ValidationError):
        BacktestSpec(horizon="fixed_days")  # missing horizon_days
    with pytest.raises(ValidationError):
        BacktestSpec(slippage_bps=-1.0)

    frames = _three_event_frames()
    with pytest.raises(ValueError, match="coverage_spells"):
        run_backtest(BacktestSpec(selection="peer_group", peer_group="cap_bucket=large"), frames)
    with pytest.raises(ValueError, match="liquidity_daily"):
        run_backtest(BacktestSpec(liquidity_cap=0.1), frames)
    with pytest.raises(ValueError, match="security_beta"):
        run_backtest(BacktestSpec(mode="beta_neutral"), frames)


def test_empty_signals_produce_flat_book() -> None:
    frames = _frames({"S1": S1_TR, "S2": [100.0] * 5}, [])
    result = run_backtest(BacktestSpec(), frames)
    assert result.nav_daily["nav"].to_list() == pytest.approx([1.0] * 5)
    assert result.nav_daily["gross_exposure"].to_list() == [0.0] * 5
    assert result.trades.height == 0
