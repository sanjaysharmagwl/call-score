from datetime import date

import polars as pl
import pytest

from callscore_core.enums import PeerDimension
from callscore_core.methodology import Methodology
from callscore_engine.peers import ALL_PEERS, assign_peer_groups
from callscore_engine.pricing import (
    MissingBenchmarkData,
    benchmark_assignment,
    benchmark_levels,
    execution_levels,
    point_level,
)

D1, D2 = date(2024, 1, 2), date(2024, 1, 3)


def _daily() -> pl.DataFrame:
    # Session 2: 2:1 split (adj 1 -> 2) and a 0.5 dividend; raw prices halve.
    # open_tr(2) = 100 * 2 * (25 + 0.5) / (1 * 50) = 102; tr(2) = 102 * 26 / 25 = 106.08
    return pl.DataFrame(
        {
            "security_id": ["S1", "S1"],
            "session_date": [D1, D2],
            "open": [49.0, 25.0],
            "close": [50.0, 26.0],
            "high": [51.0, 27.0],
            "low": [48.0, 24.0],
            "adj_factor": [1.0, 2.0],
            "cash_dividend": [None, 0.5],
            "tr_index": [100.0, 106.08],
            "is_halted": [False, False],
        }
    )


def test_execution_levels_follow_the_pricing_convention() -> None:
    levels = execution_levels(_daily())
    assert levels["open_tr"].to_list() == pytest.approx([98.0, 102.0])
    assert levels["close_tr"].to_list() == pytest.approx([100.0, 106.08])
    assert levels["close_px"].to_list() == pytest.approx([50.0, 52.0])
    assert levels["high_px"].to_list() == pytest.approx([51.0, 54.0])


def test_execution_levels_reject_non_positive_close() -> None:
    with pytest.raises(ValueError):
        execution_levels(_daily().with_columns(pl.lit(0.0).alias("close")))


def test_point_level_selects_open_or_close_and_leaves_missing_null() -> None:
    frame = pl.DataFrame(
        {
            "security_id": ["S1", "S1", "S1"],
            "s": [D1, D2, date(2024, 1, 4)],
            "p": ["open", "close", "close"],
        }
    )
    out = point_level(
        frame,
        execution_levels(_daily()),
        id_col="security_id",
        session_col="s",
        point_col="p",
        out="lvl",
    )
    assert out["lvl"].to_list()[:2] == pytest.approx([98.0, 106.08])
    assert out["lvl"][2] is None


def test_benchmark_levels_shape() -> None:
    bench = pl.DataFrame(
        {"benchmark_id": ["B"], "session_date": [D1], "open_level": [1.0], "close_level": [2.0]}
    )
    assert benchmark_levels(bench).columns == [
        "benchmark_id",
        "session_date",
        "open_tr",
        "close_tr",
    ]


def _intervals() -> pl.DataFrame:
    return pl.DataFrame(
        {"interval_id": ["I1", "I2"], "security_id": ["S1", "S1"], "entry_session": [D1, D2]}
    )


def _classes() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "security_id": ["S1", "S1"],
            "from_date": [date(2023, 1, 1), D2],
            "to_date": [D1, None],
            "sector_id": ["TECH", "HLTH"],
        }
    )


def test_sector_benchmark_follows_reclassification_with_inclusive_end() -> None:
    out = benchmark_assignment(_intervals(), _classes(), Methodology())
    assert out["benchmark_id"].to_list() == ["SECTOR_TECH", "SECTOR_HLTH"]
    assert out["beta_adjusted"].to_list() == [False, False]


def test_market_and_beta_modes_use_methodology_benchmark() -> None:
    market = benchmark_assignment(_intervals(), _classes(), Methodology(benchmark_mode="market"))
    assert market["benchmark_id"].to_list() == ["IDX_BROAD"] * 2
    beta = benchmark_assignment(
        _intervals(), _classes(), Methodology(benchmark_mode="beta_adjusted")
    )
    assert beta["beta_adjusted"].to_list() == [True, True]


def test_missing_sector_fails_loudly() -> None:
    with pytest.raises(MissingBenchmarkData):
        benchmark_assignment(
            _intervals(),
            _classes().head(1).with_columns(pl.lit(date(2023, 6, 1)).alias("to_date")),
            Methodology(),
        )


def test_peer_groups_use_dominant_dimension_values() -> None:
    coverage = pl.DataFrame(
        {
            "analyst_id": ["A", "A", "B"],
            "security_id": ["S1", "S2", "S2"],
            "from_date": [date(2024, 1, 1), date(2024, 6, 1), date(2023, 1, 1)],
            "to_date": [None, None, date(2023, 12, 31)],
            "is_lead": [True, True, True],
        }
    )
    classes = pl.DataFrame(
        {
            "security_id": ["S1", "S2"],
            "from_date": [date(2020, 1, 1)] * 2,
            "to_date": [None, None],
            "sector_id": ["TECH", "HLTH"],
            "region": ["NA", "NA"],
            "cap_bucket": ["large", "small"],
        }
    )
    out = assign_peer_groups(
        coverage, classes, date(2024, 12, 31), (PeerDimension.SECTOR, PeerDimension.CAP_BUCKET)
    ).sort("analyst_id")
    assert out.to_dicts() == [
        {"analyst_id": "A", "peer_group": "cap_bucket=large|sector=TECH"},
        {"analyst_id": "B", "peer_group": "cap_bucket=small|sector=HLTH"},
    ]
    assert assign_peer_groups(coverage, classes, date(2024, 12, 31), ())[
        "peer_group"
    ].to_list() == [ALL_PEERS, ALL_PEERS]
