from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from callscore_core.enums import ExpiryPolicy, ScoringMode
from callscore_core.frames import EXPOSURE_FRAME, FrameContract
from callscore_core.methodology import Methodology
from callscore_engine.exposures import build_exposures

_DTYPES: dict[str, pl.DataType] = {
    "String": pl.String(),
    "Date": pl.Date(),
    "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
    "Float64": pl.Float64(),
    "Int64": pl.Int64(),
    "Boolean": pl.Boolean(),
}

DAYS = [
    date(2024, 1, 2) + timedelta(days=i)
    for i in range(14)
    if (date(2024, 1, 2) + timedelta(days=i)).weekday() < 5
][:10]


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
    interval_id: str,
    analyst_id: str,
    canonical: str,
    *,
    security_id: str = "S1",
    investable: bool = True,
    available: date | None = None,
    entry: int = 1,
    exit_: int = 4,
    entry_point: str = "open",
    exit_point: str = "close",
    decay_start: date | None = None,
) -> dict[str, object]:
    avail = available or DAYS[0]
    return {
        "interval_id": interval_id,
        "event_id": f"E_{interval_id}",
        "analyst_id": analyst_id,
        "firm_id": "F",
        "security_id": security_id,
        "native_rating": canonical,
        "canonical_rating": canonical,
        "action_type": "initiation",
        "announced_ts": _ts(avail, 8),
        "effective_ts": _ts(avail, 8),
        "knowledge_ts": _ts(avail, 8),
        "available_ts": _ts(avail, 12),
        "entry_session": DAYS[entry],
        "entry_point": entry_point,
        "entry_ts": _ts(DAYS[entry], 9),
        "exit_session": DAYS[exit_],
        "exit_point": exit_point,
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


def _assert_contract(frame: pl.DataFrame, contract: FrameContract = EXPOSURE_FRAME) -> None:
    assert frame.columns == list(contract.column_names)
    for column in contract.columns:
        assert frame.schema[column.name] == _DTYPES[column.dtype], column.name
    assert frame.equals(frame.sort(list(contract.sort_by)))
    assert not frame.select(list(contract.primary_key)).is_duplicated().any()


def test_absolute_weights_and_non_investable_single_segment() -> None:
    intervals = _intervals(
        [
            _interval("I1", "A", "buy", entry=1, exit_=4),
            _interval("I2", "B", "strong_sell", entry=1, exit_=4),
            _interval("I3", "C", "not_rated", investable=False, entry=1, exit_=4),
        ]
    )
    out = build_exposures(intervals, pl.DataFrame(), _sessions(), Methodology())
    _assert_contract(out)
    rows = {row["interval_id"]: row for row in out.iter_rows(named=True)}
    assert rows["I1"]["position_weight"] == pytest.approx(0.5)
    assert rows["I1"]["weight_source"] == "rating"
    assert (rows["I1"]["start_session"], rows["I1"]["start_point"]) == (DAYS[1], "open")
    assert (rows["I1"]["end_session"], rows["I1"]["end_point"]) == (DAYS[4], "close")
    assert rows["I2"]["position_weight"] == pytest.approx(-1.0)
    assert rows["I3"]["position_weight"] is None
    assert rows["I3"]["weight_source"] == "rating"
    assert out.height == 3


def test_relative_consensus_weight_and_insufficient_consensus() -> None:
    methodology = Methodology(
        scoring_mode=ScoringMode.RELATIVE_TO_CONSENSUS, min_consensus_ratings=3
    )
    others = [
        _interval(f"O{i}", analyst, "strong_buy", entry=0, exit_=8)
        for i, analyst in enumerate(["B", "C", "D"])
    ]
    # Others entered on DAYS[0] open; analyst A's rating becomes available on DAYS[1],
    # while all three other intervals are still open (entry_ts <= available_ts < exit_ts).
    for row in others:
        row["available_ts"] = _ts(DAYS[0], 7)
        row["entry_ts"] = _ts(DAYS[0], 9)
        row["exit_ts"] = _ts(DAYS[8], 16)
        row["exit_session"] = DAYS[8]
    own = _interval("I1", "A", "buy", available=DAYS[1], entry=2, exit_=6)
    own["available_ts"] = _ts(DAYS[1], 12)
    own["entry_ts"] = _ts(DAYS[2], 9)
    sparse = _interval("I2", "E", "buy", security_id="S2", available=DAYS[1], entry=2, exit_=6)
    sparse["available_ts"] = _ts(DAYS[1], 12)
    sparse["entry_ts"] = _ts(DAYS[2], 9)
    intervals = _intervals(others + [own, sparse])
    out = build_exposures(intervals, pl.DataFrame(), _sessions(), methodology)
    _assert_contract(out)
    rows = {row["interval_id"]: row for row in out.iter_rows(named=True)}
    # w_own = 0.5, mean(w_others) = 1.0 -> clip(0.5 - 1.0) = -0.5
    assert rows["I1"]["position_weight"] == pytest.approx(-0.5)
    assert rows["I1"]["weight_source"] == "consensus"
    # Analyst E has no other active ratings -> below min_consensus_ratings.
    assert rows["I2"]["position_weight"] is None
    assert rows["I2"]["weight_source"] == "consensus"


def test_decay_segments_taper_and_cap_at_exit() -> None:
    methodology = Methodology(expiry_policy=ExpiryPolicy.DECAY, decay_days=3)
    intervals = _intervals(
        [
            _interval(
                "I1",
                "A",
                "buy",
                entry=1,
                exit_=6,
                decay_start=DAYS[3],
            )
        ]
    )
    out = build_exposures(intervals, pl.DataFrame(), _sessions(), methodology)
    _assert_contract(out)
    rows = out.sort("segment_no").iter_rows(named=True)
    got = [
        (
            r["start_session"],
            r["start_point"],
            r["end_session"],
            r["end_point"],
            r["position_weight"],
            r["weight_source"],
        )
        for r in rows
    ]
    assert got == [
        (DAYS[1], "open", DAYS[2], "close", pytest.approx(0.5), "rating"),
        (DAYS[2], "close", DAYS[3], "close", pytest.approx(0.5 * 2 / 3), "decay"),
        (DAYS[3], "close", DAYS[4], "close", pytest.approx(0.5 * 1 / 3), "decay"),
        (DAYS[4], "close", DAYS[5], "close", pytest.approx(0.0), "decay"),
    ]


def test_decay_capped_at_interval_exit() -> None:
    methodology = Methodology(expiry_policy=ExpiryPolicy.DECAY, decay_days=5)
    intervals = _intervals(
        [
            _interval(
                "I1",
                "A",
                "buy",
                entry=1,
                exit_=4,
                exit_point="open",
                decay_start=DAYS[3],
            )
        ]
    )
    out = build_exposures(intervals, pl.DataFrame(), _sessions(), methodology)
    _assert_contract(out)
    rows = list(out.sort("segment_no").iter_rows(named=True))
    # Exit is the open of DAYS[4]: the k=2 decay session truncates at that open and
    # later sessions have no remaining exposure.
    assert [(r["end_session"], r["end_point"], r["weight_source"]) for r in rows] == [
        (DAYS[2], "close", "rating"),
        (DAYS[3], "close", "decay"),
        (DAYS[4], "open", "decay"),
    ]
    assert rows[1]["position_weight"] == pytest.approx(0.5 * 4 / 5)
    assert rows[2]["position_weight"] == pytest.approx(0.5 * 3 / 5)


def test_empty_intervals() -> None:
    empty = pl.DataFrame(
        schema={
            name: _DTYPES[col.dtype] for name, col in ((c.name, c) for c in EXPOSURE_FRAME.columns)
        }
    )
    out = build_exposures(empty, pl.DataFrame(), _sessions(), Methodology())
    _assert_contract(out)
    assert out.height == 0
