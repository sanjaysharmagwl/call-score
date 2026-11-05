"""Tests for ``callscore_engine.stats.infer_metrics`` (hand-built contract frames)."""

import time
import zlib
from datetime import date

import numpy as np
import polars as pl
import pytest

from callscore_core.enums import ShrinkageMethod
from callscore_core.frames import METRIC_RESULT_FRAME
from callscore_core.methodology import Methodology
from callscore_core.metrics import METRIC_DEFINITIONS
from callscore_engine.stats import infer_metrics

_DTYPES = {
    "String": pl.String(),
    "Date": pl.Date(),
    "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
    "Float64": pl.Float64(),
    "Int64": pl.Int64(),
    "Boolean": pl.Boolean(),
}

VALUE_SCHEMA = {
    "analyst_id": pl.String(),
    "metric_code": pl.String(),
    "value": pl.Float64(),
    "n_obs": pl.Int64(),
    "unavailable_reason": pl.String(),
}
SAMPLE_SCHEMA = {
    "analyst_id": pl.String(),
    "metric_code": pl.String(),
    "observation_id": pl.String(),
    "observation_date": pl.Date(),
    "value": pl.Float64(),
}
PEER_SCHEMA = {"analyst_id": pl.String(), "peer_group": pl.String()}

BOOT = "quarter_block_bootstrap"
MEAN_CONTRIB = "mean_contribution"  # bootstrap, mean, shrink, null 0.0, higher
IC = "information_coefficient"  # inference none, higher
STALE = "stale_coverage"  # bootstrap, lower, no null_value, no shrink


def _methodology(**overrides: object) -> Methodology:
    kwargs: dict[str, object] = {"min_peer_size": 2, "bootstrap_draws": 500}
    kwargs.update(overrides)
    return Methodology.model_validate(kwargs)


def _values(rows: list[tuple[str, str, float | None, int, str | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "analyst_id": [r[0] for r in rows],
            "metric_code": [r[1] for r in rows],
            "value": [r[2] for r in rows],
            "n_obs": [r[3] for r in rows],
            "unavailable_reason": [r[4] for r in rows],
        },
        schema=VALUE_SCHEMA,
    )


def _samples(
    analyst_id: str,
    metric_code: str,
    by_quarter: list[list[float]],
) -> list[dict[str, object]]:
    """One sample row per observation; quarter ``q`` maps to month ``3q+2``."""
    rows: list[dict[str, object]] = []
    i = 0
    for q, values in enumerate(by_quarter):
        obs_date = date(2021 + q // 4, 3 * (q % 4) + 1, 15)
        for v in values:
            rows.append(
                {
                    "analyst_id": analyst_id,
                    "metric_code": metric_code,
                    "observation_id": f"{analyst_id}-{metric_code}-{i}",
                    "observation_date": obs_date,
                    "value": v,
                }
            )
            i += 1
    return rows


def _sample_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=SAMPLE_SCHEMA)
    return pl.DataFrame(rows).select(pl.col(k).cast(v) for k, v in SAMPLE_SCHEMA.items())


def _peers(pairs: list[tuple[str, str | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"analyst_id": [p[0] for p in pairs], "peer_group": [p[1] for p in pairs]},
        schema=PEER_SCHEMA,
    )


def _row(frame: pl.DataFrame, analyst: str, metric: str) -> dict[str, object]:
    out = frame.filter(
        (pl.col("analyst_id") == analyst) & (pl.col("metric_code") == metric)
    ).to_dicts()
    assert len(out) == 1
    return out[0]


def test_output_matches_metric_result_contract() -> None:
    samples = _sample_frame(_samples("a1", MEAN_CONTRIB, [[0.01, 0.02], [0.03, -0.01]]))
    values = _values([("a1", MEAN_CONTRIB, 0.0125, 4, None)])
    out = infer_metrics(values, samples, _peers([("a1", "g1")]), _methodology())
    expected = [c.name for c in METRIC_RESULT_FRAME.columns]
    assert out.columns == expected
    for column in METRIC_RESULT_FRAME.columns:
        assert out.schema[column.name] == _DTYPES[column.dtype], column.name
    assert out.sort("analyst_id", "metric_code").equals(out)


def test_bootstrap_is_deterministic_per_seed() -> None:
    methodology = _methodology()
    rng = np.random.default_rng(7)
    by_quarter = [list(rng.normal(0.01, 0.05, 8)) for _ in range(6)]
    samples = _sample_frame(_samples("a1", MEAN_CONTRIB, by_quarter))
    values = _values(
        [("a1", MEAN_CONTRIB, float(np.mean([v for q in by_quarter for v in q])), 48, None)]
    )
    peers = _peers([("a1", "g1"), ("a2", "g1")])

    first = infer_metrics(values, samples, peers, methodology)
    second = infer_metrics(values, samples, peers, methodology)
    assert first.equals(second)

    other_seed = _methodology(bootstrap_seed=methodology.bootstrap_seed + 1)
    third = infer_metrics(values, samples, peers, other_seed)
    row_a = _row(first, "a1", MEAN_CONTRIB)
    row_b = _row(third, "a1", MEAN_CONTRIB)
    assert row_a["std_err"] != row_b["std_err"] or row_a["ci_low"] != row_b["ci_low"]


def test_std_err_matches_analytic_standard_error() -> None:
    rng = np.random.default_rng(11)
    by_quarter = [list(rng.normal(0.0, 1.0, 20)) for _ in range(20)]
    flat = [v for q in by_quarter for v in q]
    samples = _sample_frame(_samples("a1", MEAN_CONTRIB, by_quarter))
    values = _values([("a1", MEAN_CONTRIB, float(np.mean(flat)), len(flat), None)])
    out = infer_metrics(values, samples, _peers([("a1", "g1")]), _methodology(bootstrap_draws=2000))
    row = _row(out, "a1", MEAN_CONTRIB)
    analytic = float(np.std(flat, ddof=1)) / np.sqrt(len(flat))
    assert row["std_err"] == pytest.approx(analytic, rel=0.15)
    assert row["value"] == pytest.approx(float(np.mean(flat)))


def test_confidence_interval_and_significance() -> None:
    # All observations identical: degenerate CI at the value, excludes null 0.
    by_quarter = [[1.0] * 4 for _ in range(4)]
    samples = _sample_frame(_samples("a1", MEAN_CONTRIB, by_quarter))
    values = _values([("a1", MEAN_CONTRIB, 1.0, 16, None)])
    peers = _peers([("a1", "g1"), ("a2", "g1")])
    out = infer_metrics(values, samples, peers, _methodology())
    row = _row(out, "a1", MEAN_CONTRIB)
    assert row["std_err"] == 0.0
    assert row["ci_low"] == 1.0 and row["ci_high"] == 1.0
    assert row["is_significant"] is True
    assert row["t_stat"] is None  # std_err == 0

    # Quarter blocks with means +0.04, -0.04, 0, 0: pooled mean is 0 but
    # resampled draw means vary, so the CI is non-degenerate and contains 0.
    by_quarter = [[0.04] * 8, [-0.04] * 8, [0.0] * 8, [0.0] * 8]
    samples = _sample_frame(_samples("a2", MEAN_CONTRIB, by_quarter))
    values = _values([("a1", MEAN_CONTRIB, 1.0, 16, None), ("a2", MEAN_CONTRIB, 0.0, 32, None)])
    all_samples = _sample_frame(
        _samples("a1", MEAN_CONTRIB, [[1.0] * 4 for _ in range(4)])
        + _samples("a2", MEAN_CONTRIB, by_quarter)
    )
    out = infer_metrics(values, all_samples, peers, _methodology())
    row = _row(out, "a2", MEAN_CONTRIB)
    assert row["ci_low"] is not None and row["ci_high"] is not None
    assert float(row["ci_low"]) < 0.0 < float(row["ci_high"])
    assert row["is_significant"] is False
    assert row["t_stat"] == pytest.approx(0.0)


def test_single_quarter_block_nulls_statistics() -> None:
    samples = _sample_frame(_samples("a1", MEAN_CONTRIB, [[0.01, 0.02, 0.03, 0.04]]))
    values = _values([("a1", MEAN_CONTRIB, 0.025, 4, None)])
    out = infer_metrics(values, samples, _peers([("a1", "g1"), ("a2", "g1")]), _methodology())
    row = _row(out, "a1", MEAN_CONTRIB)
    assert row["value"] == 0.025
    for column in ("std_err", "t_stat", "ci_low", "ci_high", "shrunk_value", "is_significant"):
        assert row[column] is None, column


def test_unavailable_value_keeps_reason_and_nulls_statistics() -> None:
    values = _values([("a1", MEAN_CONTRIB, None, 0, "insufficient_observations")])
    out = infer_metrics(values, _sample_frame([]), _peers([("a1", "g1")]), _methodology())
    row = _row(out, "a1", MEAN_CONTRIB)
    assert row["value"] is None
    assert row["unavailable_reason"] == "insufficient_observations"
    for column in ("std_err", "t_stat", "ci_low", "ci_high", "shrunk_value", "peer_percentile"):
        assert row[column] is None, column


def test_inference_none_metric_has_null_statistics() -> None:
    values = _values([("a1", IC, 0.3, 20, None), ("a2", IC, 0.1, 20, None)])
    peers = _peers([("a1", "g1"), ("a2", "g1")])
    out = infer_metrics(
        values,
        _sample_frame(_samples("a1", IC, [[0.1, 0.2], [0.3, 0.4]])),
        peers,
        _methodology(),
    )
    row = _row(out, "a1", IC)
    assert row["value"] == 0.3
    for column in ("std_err", "t_stat", "ci_low", "ci_high", "shrunk_value", "is_significant"):
        assert row[column] is None, column
    # Percentiles still apply to inference-free metrics.
    assert row["peer_percentile"] is not None


def _boot_samples(analyst: str, spread: float) -> list[dict[str, object]]:
    """Four distinct quarter blocks of noisy observations (non-degenerate draws)."""
    rng = np.random.default_rng(zlib.crc32(analyst.encode()))
    return _samples(analyst, MEAN_CONTRIB, [list(rng.normal(0.02, spread, 8)) for _ in range(4)])


def test_shrinkage_bounds_and_tau2_zero() -> None:
    methodology = _methodology()
    analysts = ["a1", "a2", "a3"]
    # Tight values, widely dispersed sampling error -> tau^2 = 0 -> group mean.
    values = [0.01, 0.02, 0.03]
    value_rows = [(a, MEAN_CONTRIB, v, 16, None) for a, v in zip(analysts, values, strict=True)]
    sample_rows = [row for a in analysts for row in _boot_samples(a, spread=5.0)]
    out = infer_metrics(
        _values(value_rows),
        _sample_frame(sample_rows),
        _peers([(a, "g1") for a in analysts]),
        methodology,
    )
    group_mean = sum(values) / len(values)
    for a in analysts:
        row = _row(out, a, MEAN_CONTRIB)
        assert row["shrunk_value"] == pytest.approx(group_mean, abs=1e-12)
        assert row["std_err"] is not None and float(row["std_err"]) > 0


def test_shrinkage_partial_pooling_stays_within_bounds() -> None:
    methodology = _methodology()
    analysts = ["a1", "a2", "a3", "a4"]
    values = [0.20, 0.05, -0.10, 0.02]
    value_rows = [(a, MEAN_CONTRIB, v, 16, None) for a, v in zip(analysts, values, strict=True)]
    sample_rows = []
    for a in analysts:
        sample_rows += _boot_samples(a, spread=0.2)
    out = infer_metrics(
        _values(value_rows),
        _sample_frame(sample_rows),
        _peers([(a, "g1") for a in analysts]),
        methodology,
    )
    group_mean = sum(values) / len(values)
    for a, v in zip(analysts, values, strict=True):
        shrunk = _row(out, a, MEAN_CONTRIB)["shrunk_value"]
        assert shrunk is not None
        lo, hi = min(v, group_mean), max(v, group_mean)
        assert lo - 1e-12 <= float(shrunk) <= hi + 1e-12


def test_shrinkage_requires_min_peer_size_method_and_group() -> None:
    methodology = _methodology(min_peer_size=3)
    analysts = ["a1", "a2"]
    value_rows = [(a, MEAN_CONTRIB, 0.01, 16, None) for a in analysts]
    sample_rows = [row for a in analysts for row in _boot_samples(a, 0.2)]
    peers = _peers([("a1", "g1"), ("a2", "g1"), ("a3", None)])
    value_rows.append(("a3", MEAN_CONTRIB, 0.05, 16, None))
    sample_rows += _boot_samples("a3", 0.2)
    out = infer_metrics(_values(value_rows), _sample_frame(sample_rows), peers, methodology)
    assert _row(out, "a1", MEAN_CONTRIB)["shrunk_value"] is None  # group too small
    assert _row(out, "a2", MEAN_CONTRIB)["shrunk_value"] is None
    assert _row(out, "a3", MEAN_CONTRIB)["shrunk_value"] is None  # null peer group

    none_method = _methodology(shrinkage_method=ShrinkageMethod.NONE)
    out_none = infer_metrics(
        _values(value_rows[:2]),
        _sample_frame(sample_rows[: -len(_boot_samples("a3", 0.2))]),
        _peers([("a1", "g1"), ("a2", "g1")]),
        none_method,
    )
    assert _row(out_none, "a1", MEAN_CONTRIB)["shrunk_value"] is None


def test_peer_percentile_orientation_and_ties() -> None:
    methodology = _methodology()
    # information_coefficient: higher is better; stale_coverage: lower is better.
    analysts = ["a1", "a2", "a3", "a4"]
    values = [0.01, 0.02, 0.02, 0.04]
    value_rows = []
    for a, v in zip(analysts, values, strict=True):
        value_rows.append((a, IC, v, 10, None))
        value_rows.append((a, STALE, v, 10, None))
    peers = _peers([(a, "g1") for a in analysts])
    out = infer_metrics(_values(value_rows), _sample_frame([]), peers, methodology)
    # Ascending average ranks: 0.01 -> 1, 0.02 -> 2.5, 0.02 -> 2.5, 0.04 -> 4.
    # Higher-is-better keeps ascending ranks; lower-is-better inverts them
    # (descending average ranks 4, 2.5, 2.5, 1) — the middle ties land on 50.
    expected_high = {"a1": 12.5, "a2": 50.0, "a3": 50.0, "a4": 87.5}
    expected_low = {"a1": 87.5, "a2": 50.0, "a3": 50.0, "a4": 12.5}
    for a in analysts:
        assert _row(out, a, IC)["peer_percentile"] == pytest.approx(expected_high[a])
        assert _row(out, a, STALE)["peer_percentile"] == pytest.approx(expected_low[a])


def test_peer_percentile_requires_group_size_and_group() -> None:
    methodology = _methodology(min_peer_size=3)
    value_rows = [
        ("a1", IC, 0.1, 10, None),
        ("a2", IC, 0.2, 10, None),
        ("a3", IC, 0.3, 10, None),
    ]
    peers = _peers([("a1", "g1"), ("a2", "g1"), ("a3", None)])
    out = infer_metrics(_values(value_rows), _sample_frame([]), peers, methodology)
    assert _row(out, "a1", IC)["peer_percentile"] is None  # group of 2 < 3
    assert _row(out, "a2", IC)["peer_percentile"] is None
    assert _row(out, "a3", IC)["peer_percentile"] is None  # no peer group


def test_unknown_metric_code_rejected() -> None:
    values = _values([("a1", "not_a_metric", 1.0, 5, None)])
    with pytest.raises(ValueError, match="not_a_metric"):
        infer_metrics(values, _sample_frame([]), _peers([("a1", "g1")]), _methodology())


def test_bootstrap_performance_smoke() -> None:
    rng = np.random.default_rng(3)
    analysts = [f"a{i:03d}" for i in range(180)]
    bootstrap_codes = [d.code for d in METRIC_DEFINITIONS if d.inference == BOOT]
    value_rows: list[tuple[str, str, float | None, int, str | None]] = []
    sample_rows: list[dict[str, object]] = []
    for a in analysts:
        for code in bootstrap_codes:
            by_quarter = [list(rng.normal(0.0, 0.02, 10)) for _ in range(8)]
            flat = [v for q in by_quarter for v in q]
            value_rows.append((a, code, float(np.mean(flat)), len(flat), None))
            sample_rows += _samples(a, code, by_quarter)
    peers = _peers([(a, f"g{i % 6}") for i, a in enumerate(analysts)])
    started = time.perf_counter()
    out = infer_metrics(_values(value_rows), _sample_frame(sample_rows), peers, _methodology())
    elapsed = time.perf_counter() - started
    assert out.height == len(analysts) * len(bootstrap_codes)
    assert elapsed < 20.0, f"infer_metrics took {elapsed:.1f}s"
    print(f"\ninfer_metrics smoke: {out.height} rows in {elapsed:.2f}s")
