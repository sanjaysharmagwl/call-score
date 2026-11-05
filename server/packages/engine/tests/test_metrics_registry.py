"""Registry completeness and ``compute_metrics`` output-contract tests."""

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from callscore_core.contracts import RunContext
from callscore_core.frames import FRAME_CONTRACTS, METRIC_SAMPLE_FRAME
from callscore_core.methodology import Methodology
from callscore_core.metrics import METRIC_DEFINITIONS, metric_definition
from callscore_engine.metrics import (
    METRIC_SAMPLE_SCHEMA,
    METRIC_VALUE_SCHEMA,
    REGISTRY,
    MetricInputs,
    check_registry,
    compute_metrics,
)

AS_OF = date(2024, 12, 31)


def _context() -> RunContext:
    return RunContext(
        as_of_date=AS_OF,
        knowledge_cutoff=datetime(2024, 12, 31, 23, 59, tzinfo=UTC),
        source_revision="r",
        source_fingerprint="0" * 64,
        universe_fingerprint="0" * 64,
        algorithm_version="v",
    )


def _contributions() -> pl.DataFrame:
    rows = []
    for i in range(10):
        rows.append(
            {
                "interval_id": f"ic{i}",
                "analyst_id": "a1",
                "horizon": "d126",
                "entry_session": date(2024, 1, 2) + timedelta(days=i),
                "position_weight": 0.1 * (i + 1),
                "excess_return": 0.01 * (i + 1),
                "contribution": None,
                "unavailable_reason": None,
            }
        )
    rows.append(
        {
            "interval_id": "t1",
            "analyst_id": "a1",
            "horizon": "to_exit",
            "entry_session": date(2024, 1, 15),
            "position_weight": 1.0,
            "excess_return": 0.10,
            "contribution": 0.10,
            "unavailable_reason": None,
        }
    )
    rows.append(
        {
            "interval_id": "t2",
            "analyst_id": "a1",
            "horizon": "to_exit",
            "entry_session": date(2024, 2, 10),
            "position_weight": -0.5,
            "excess_return": 0.10,
            "contribution": -0.05,
            "unavailable_reason": None,
        }
    )
    return pl.DataFrame(rows)


def _event_windows() -> pl.DataFrame:
    rows = []
    signed = [
        (1.0, 0.10),
        (-0.5, 0.20),
        (0.5, -0.30),
        (-1.0, None),
        (0.0, 0.50),
        (1.0, 0.40),
        (-1.0, -0.20),
    ]
    for i, (wc, post) in enumerate(signed):
        rows.append(
            {
                "event_id": f"e{i}",
                "analyst_id": "a1",
                "security_id": "S1",
                "event_session": date(2024, 3, 1) + timedelta(days=i),
                "weight_change": wc,
                "consensus_deviation": None,
                "car_pm1": 0.01 * (i + 1),
                "pre_event_excess_20d": None,
                "post_event_excess_20d": post,
                "unavailable_reason": None,
            }
        )
    return pl.DataFrame(rows)


def _target_outcomes() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "event_id": "t1",
                "analyst_id": "a1",
                "entry_session": date(2024, 1, 2),
                "horizon_end_session": date(2024, 7, 1),
                "entry_price": 100.0,
                "target_price": 120.0,
                "implied_return": 0.20,
                "realized_return": 0.10,
                "hit": True,
                "is_matured": True,
            },
            {
                "event_id": "t2",
                "analyst_id": "a1",
                "entry_session": date(2024, 2, 1),
                "horizon_end_session": date(2024, 8, 1),
                "entry_price": 100.0,
                "target_price": 110.0,
                "implied_return": 0.10,
                "realized_return": 0.30,
                "hit": True,
                "is_matured": True,
            },
        ]
    )


def _portfolio_nav() -> pl.DataFrame:
    rows = []
    for i in range(64):
        ar = 0.002 + (0.001 if i % 2 == 0 else -0.001)
        rows.append(
            {
                "analyst_id": "a1",
                "session_date": date(2024, 1, 2) + timedelta(days=i),
                "nav": 1.0,
                "benchmark_nav": 1.0,
                "active_return": ar,
                "gross_exposure": 1.0,
                "turnover": 0.0,
                "cost": 0.0,
                "drawdown": 0.0,
            }
        )
    return pl.DataFrame(rows)


def _coverage_daily() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "analyst_id": "a1",
                "security_id": "S1",
                "session_date": date(2024, 1, 2),
                "is_stale": False,
            },
            {
                "analyst_id": "a1",
                "security_id": "S1",
                "session_date": date(2024, 1, 3),
                "is_stale": True,
            },
            {
                "analyst_id": "a1",
                "security_id": "S2",
                "session_date": date(2024, 1, 4),
                "is_stale": False,
            },
        ]
    )


def _inputs() -> MetricInputs:
    return MetricInputs(
        contributions=_contributions(),
        event_windows=_event_windows(),
        target_outcomes=_target_outcomes(),
        portfolio_nav=_portfolio_nav(),
        coverage_daily=_coverage_daily(),
    )


def test_registry_matches_catalog_exactly() -> None:
    check_registry()
    assert set(REGISTRY) == {d.code for d in METRIC_DEFINITIONS}
    assert len(REGISTRY) == len(METRIC_DEFINITIONS)
    for definition in METRIC_DEFINITIONS:
        assert REGISTRY[definition.code] is not None
        assert metric_definition(definition.code) is definition


def test_empty_inputs_produce_unavailable_rows_for_every_metric() -> None:
    out = compute_metrics(MetricInputs(), ["a1", "a2"], Methodology(), _context())
    assert out.values.height == 2 * len(METRIC_DEFINITIONS)
    assert out.samples.height == 0
    assert out.samples.schema == METRIC_SAMPLE_SCHEMA
    assert out.values.schema == METRIC_VALUE_SCHEMA
    for row in out.values.iter_rows(named=True):
        assert row["value"] is None
        assert row["n_obs"] == 0
        assert row["unavailable_reason"] is not None
    # Sorted by (analyst_id, metric_code).
    keys = list(zip(out.values["analyst_id"], out.values["metric_code"], strict=True))
    assert keys == sorted(keys)


def test_value_null_iff_reason_set() -> None:
    out = compute_metrics(_inputs(), ["a1"], Methodology(), _context())
    for row in out.values.iter_rows(named=True):
        assert (row["value"] is None) == (row["unavailable_reason"] is not None), row


def test_row_order_invariance() -> None:
    inputs = _inputs()
    analysts = ["a1"]
    base = compute_metrics(inputs, analysts, Methodology(), _context())
    shuffled = MetricInputs(
        contributions=inputs["contributions"].reverse(),
        event_windows=inputs["event_windows"].reverse(),
        target_outcomes=inputs["target_outcomes"].reverse(),
        portfolio_nav=inputs["portfolio_nav"].reverse(),
        coverage_daily=inputs["coverage_daily"].reverse(),
    )
    again = compute_metrics(shuffled, analysts, Methodology(), _context())
    assert base.values.equals(again.values)
    assert base.samples.equals(again.samples)


def _aggregate(definition_code: str, values: list[float]) -> float:
    import math
    import statistics

    definition = metric_definition(definition_code)
    match definition.aggregate:
        case "mean":
            return statistics.fmean(values)
        case "annualized_mean":
            return 252.0 * statistics.fmean(values)
        case "annualized_sharpe":
            return math.sqrt(252.0) * statistics.fmean(values) / statistics.stdev(values)
        case _:
            raise AssertionError(definition.aggregate)


def test_bootstrapped_metric_aggregates_reproduce_values() -> None:
    out = compute_metrics(_inputs(), ["a1"], Methodology(), _context())
    values = {
        (r["analyst_id"], r["metric_code"]): r["value"] for r in out.values.iter_rows(named=True)
    }
    grouped: dict[tuple[str, str], list[float]] = {}
    for aid, code, _oid, _date, val in out.samples.iter_rows():
        grouped.setdefault((str(aid), str(code)), []).append(float(val))
    bootstrapped = [d for d in METRIC_DEFINITIONS if d.inference == "quarter_block_bootstrap"]
    assert {code for _, code in grouped} == {d.code for d in bootstrapped}
    for (aid, code), obs in grouped.items():
        expected = _aggregate(code, obs)
        actual = values[(aid, code)]
        assert actual == pytest.approx(expected, rel=1e-12, abs=1e-12), (aid, code)
    # Samples only exist for metrics whose value is available.
    available = {
        r["metric_code"] for r in out.values.iter_rows(named=True) if r["value"] is not None
    }
    for _aid, code, *_ in out.samples.iter_rows():
        assert code in available


def test_samples_match_metric_sample_contract() -> None:
    out = compute_metrics(_inputs(), ["a1"], Methodology(), _context())
    contract = FRAME_CONTRACTS[METRIC_SAMPLE_FRAME.name]
    assert tuple(out.samples.columns) == contract.column_names
    for column, dtype in METRIC_SAMPLE_SCHEMA.items():
        assert out.samples.schema[column] == dtype
    # observation_id is unique per (analyst_id, metric_code).
    keys = out.samples.select("analyst_id", "metric_code", "observation_id")
    assert keys.height == keys.unique().height
