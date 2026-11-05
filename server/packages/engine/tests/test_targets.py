from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from callscore_core.contracts import RunContext
from callscore_engine.targets import (
    assert_target_outcomes_contract,
    build_target_outcomes,
)

D0 = date(2024, 1, 1)  # Monday
N_DATA = 60  # security_daily / price data through the as-of date
N_SESSIONS = 90  # exchange calendar extends beyond as_of for censoring tests
AS_OF_IDX = N_DATA - 1


def _weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


ALL_DATES = _weekdays(D0, N_SESSIONS)
DATA_DATES = ALL_DATES[:N_DATA]


def _ts(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC)


def _sessions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "exchange_id": ["XNYS"] * N_SESSIONS,
            "session_date": ALL_DATES,
            "open_ts": [_ts(d, 13, 30) for d in ALL_DATES],
            "close_ts": [_ts(d, 20, 0) for d in ALL_DATES],
        }
    )


def _security_daily() -> pl.DataFrame:
    """Raw open/close 50, high 70, low 35; 2:1 split at idx 5 (adj_factor 1 -> 2).

    px levels are raw * adj_factor, so from idx 5: open_px = close_px = 100,
    high_px = 140, low_px = 70.
    """
    adj = [1.0 if i < 5 else 2.0 for i in range(N_DATA)]
    return pl.DataFrame(
        {
            "security_id": ["S1"] * N_DATA,
            "session_date": DATA_DATES,
            "open": [50.0] * N_DATA,
            "close": [50.0] * N_DATA,
            "high": [70.0] * N_DATA,
            "low": [35.0] * N_DATA,
            "adj_factor": adj,
            "cash_dividend": [None] * N_DATA,
            "tr_index": [100.0] * N_DATA,
            "is_halted": [False] * N_DATA,
        }
    )


def _event(
    event_id: str,
    analyst: str,
    avail_idx: int,
    target: float | None,
    months: int | None = 1,
) -> dict:
    avail = _ts(DATA_DATES[avail_idx], 14, 0)
    return {
        "event_id": event_id,
        "analyst_id": analyst,
        "firm_id": "F1",
        "security_id": "S1",
        "announced_ts": avail,
        "effective_ts": avail,
        "knowledge_ts": avail,
        "action_type": "pt_change",
        "native_rating": "buy",
        "canonical_rating": "buy",
        "prior_canonical_rating": "buy",
        "scale_id": "scale1",
        "price_target": target,
        "pt_currency": "USD",
        "pt_horizon_months": months,
        "expiry_date": None,
        "supersedes_event_id": None,
        "is_correction": False,
    }


def _events() -> pl.DataFrame:
    rows = [
        _event("E1", "A", 10, 60.0),  # next open d11, target hit
        _event("E2", "B", 10, 80.0),  # target above the high -> miss
        _event("E3", "C", 10, 40.0),  # downside target, low reaches it
        _event("E4", "D", 58, 60.0),  # horizon beyond the data -> immature
        _event("E5", "A", 20, 60.0),  # interval opened at d20 close
        _event("E6", "E", 25, 60.0),  # same-rating: open interval at avail
        _event("E7", "F", 10, 0.0),  # non-positive target -> omitted
        _event("E8", "G", 10, 60.0, months=None),  # no horizon -> omitted
        _event("E9", "H", 10, -10.0),  # negative target -> omitted
    ]
    return pl.DataFrame(rows)


def _intervals() -> pl.DataFrame:
    def row(
        interval_id: str,
        analyst: str,
        entry_idx: int,
        entry_point: str,
        entry_hour: int,
        exit_idx: int,
    ) -> dict:
        entry_session = DATA_DATES[entry_idx]
        return {
            "interval_id": interval_id,
            "event_id": interval_id,
            "analyst_id": analyst,
            "firm_id": "F1",
            "security_id": "S1",
            "native_rating": "buy",
            "canonical_rating": "buy",
            "action_type": "initiation",
            "announced_ts": _ts(entry_session, 12, 0),
            "effective_ts": _ts(entry_session, 12, 0),
            "knowledge_ts": _ts(entry_session, 12, 0),
            "available_ts": _ts(entry_session, 12, 0),
            "entry_session": entry_session,
            "entry_point": entry_point,
            "entry_ts": _ts(entry_session, entry_hour, 30 if entry_point == "open" else 0),
            "exit_session": DATA_DATES[exit_idx],
            "exit_point": "close",
            "exit_ts": _ts(DATA_DATES[exit_idx], 20, 0),
            "exit_reason": "run_end",
            "expiry_date": None,
            "decay_start_session": None,
            "is_investable": True,
            "non_investable_reason": None,
            "flags": None,
        }

    return pl.DataFrame(
        [
            # E5 opened an interval at the d20 close.
            row("E5", "A", 20, "close", 20, 50),
            # Analyst E already holds S1 from the d15 open; E6 does not open one.
            row("IX", "E", 15, "open", 13, 50),
        ]
    )


def _context() -> RunContext:
    return RunContext(
        as_of_date=DATA_DATES[AS_OF_IDX],
        knowledge_cutoff=_ts(DATA_DATES[AS_OF_IDX], 23, 59),
        source_revision="test",
        source_fingerprint="0" * 64,
        universe_fingerprint="0" * 64,
        algorithm_version="test",
    )


def _build() -> pl.DataFrame:
    return build_target_outcomes(
        _events(), _intervals(), _security_daily(), _sessions(), _context()
    )


def test_target_outcomes_hand_computed() -> None:
    out = _build()
    assert_target_outcomes_contract(out)
    assert out["event_id"].to_list() == ["E1", "E2", "E3", "E4", "E5", "E6"]
    by_id = {r["event_id"]: r for r in out.iter_rows(named=True)}

    # E1: avail d10 14:00 -> next open d11; adj 2 -> entry_px 100, target_px 120.
    e1 = by_id["E1"]
    assert e1["entry_session"] == DATA_DATES[11]
    assert e1["entry_price"] == pytest.approx(100.0)
    assert e1["target_price"] == pytest.approx(120.0)
    assert e1["implied_return"] == pytest.approx(0.2)
    assert e1["is_matured"] is True
    assert e1["horizon_end_session"] == date(2024, 2, 16)  # entry + 1 month
    assert e1["realized_return"] == pytest.approx(0.0)  # close_px 100 / 100 - 1
    assert e1["hit"] is True  # high_px 140 >= 120

    # E2: same entry, target_px 160 > high_px 140 -> miss.
    e2 = by_id["E2"]
    assert e2["target_price"] == pytest.approx(160.0)
    assert e2["implied_return"] == pytest.approx(0.6)
    assert e2["hit"] is False

    # E3: target_px 80 < entry 100 -> implied -0.2; low_px 70 <= 80 -> hit.
    e3 = by_id["E3"]
    assert e3["target_price"] == pytest.approx(80.0)
    assert e3["implied_return"] == pytest.approx(-0.2)
    assert e3["hit"] is True

    # E4: entry d59 (last data session); +1 month lands beyond the as-of boundary.
    e4 = by_id["E4"]
    assert e4["entry_session"] == DATA_DATES[59]
    assert e4["is_matured"] is False
    assert e4["realized_return"] is None
    assert e4["hit"] is None

    # E5: the event opened an interval -> d20 close, not the next open (d21).
    e5 = by_id["E5"]
    assert e5["entry_session"] == DATA_DATES[20]
    assert e5["entry_price"] == pytest.approx(100.0)  # close_px at d20
    assert e5["is_matured"] is True
    assert e5["hit"] is True

    # E6: same-rating event -> the open interval's d15 open entry.
    e6 = by_id["E6"]
    assert e6["entry_session"] == DATA_DATES[15]
    assert e6["entry_price"] == pytest.approx(100.0)  # open_px at d15
    assert e6["is_matured"] is True


def test_split_adjustment_uses_entry_session_adj_factor() -> None:
    # Move the split before the entry: adj_factor 2 already at d11 -> target_px 120.
    # Here adj is 1 at idx < 5 and the event enters at d4 -> adj 1 -> target_px 60.
    events = pl.DataFrame([_event("S1", "A", 3, 60.0)])
    out = build_target_outcomes(events, _intervals(), _security_daily(), _sessions(), _context())
    row = out.row(0, named=True)
    assert row["entry_session"] == DATA_DATES[4]  # next open after avail d3 14:00
    assert row["entry_price"] == pytest.approx(50.0)  # open_px with adj 1
    assert row["target_price"] == pytest.approx(60.0)  # 60 * adj 1
    assert row["implied_return"] == pytest.approx(0.2)


def test_row_order_invariance() -> None:
    a = _build()
    b = build_target_outcomes(
        _events().sample(fraction=1.0, seed=3),
        _intervals(),
        _security_daily(),
        _sessions(),
        _context(),
    )
    assert a.equals(b)
