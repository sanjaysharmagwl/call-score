"""Tests for ``callscore_api.services.cache.StageCache``.

Hand-built frames only; no DB or engine needed. The invalidation-matrix
tests pin the ``contracts.stage_fingerprint`` / ``STAGE_METHODOLOGY_FIELDS``
semantics the cache keys rely on.
"""

import json
from datetime import UTC, date, datetime

import polars as pl
import pytest

from callscore_api.services.cache import StageCache
from callscore_core.contracts import STAGE_ORDER, RunContext, stage_fingerprint
from callscore_core.enums import Stage
from callscore_core.methodology import Methodology

FP_A = "a" * 64
FP_B = "b" * 64


def _frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "analyst_id": ["A1", "A2"],
            "day": [date(2025, 1, 6), date(2025, 1, 7)],
            "ts": [
                datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
                datetime(2025, 1, 7, 14, 30, tzinfo=UTC),
            ],
            "value": [1.5, None],
            "flag": [True, False],
        }
    )


@pytest.fixture()
def cache(tmp_path) -> StageCache:
    return StageCache(tmp_path / "cache")


def test_put_get_roundtrip(cache: StageCache) -> None:
    frame = _frame()
    cache.put_frame(Stage.RETURNS, FP_A, "returns", frame)
    loaded = cache.get_frame(Stage.RETURNS, FP_A, "returns")
    assert loaded is not None
    assert loaded.equals(frame)
    assert loaded.schema == frame.schema


def test_get_miss_on_absent(cache: StageCache) -> None:
    assert cache.get_frame(Stage.RETURNS, FP_A, "returns") is None
    assert cache.load_stage(Stage.RETURNS, FP_A, ["returns"]) is None


def test_get_miss_on_fingerprint_mismatch(cache: StageCache) -> None:
    cache.put_frame(Stage.RETURNS, FP_A, "returns", _frame())
    assert cache.get_frame(Stage.RETURNS, FP_B, "returns") is None


def test_corrupt_parquet_is_miss(cache: StageCache) -> None:
    cache.put_frame(Stage.RETURNS, FP_A, "returns", _frame())
    parquet = cache.stage_dir(Stage.RETURNS, FP_A) / "returns.parquet"
    parquet.write_bytes(b"this is not parquet")
    assert cache.get_frame(Stage.RETURNS, FP_A, "returns") is None


def test_truncated_parquet_is_miss(cache: StageCache) -> None:
    cache.put_frame(Stage.RETURNS, FP_A, "returns", _frame())
    parquet = cache.stage_dir(Stage.RETURNS, FP_A) / "returns.parquet"
    data = parquet.read_bytes()
    parquet.write_bytes(data[: len(data) // 3])
    assert cache.get_frame(Stage.RETURNS, FP_A, "returns") is None


def test_missing_meta_is_miss(cache: StageCache) -> None:
    cache.put_frame(Stage.RETURNS, FP_A, "returns", _frame())
    (cache.stage_dir(Stage.RETURNS, FP_A) / "returns.json").unlink()
    assert cache.get_frame(Stage.RETURNS, FP_A, "returns") is None


def test_tampered_meta_row_count_is_miss(cache: StageCache) -> None:
    cache.put_frame(Stage.RETURNS, FP_A, "returns", _frame())
    meta_path = cache.stage_dir(Stage.RETURNS, FP_A) / "returns.json"
    meta = json.loads(meta_path.read_text())
    meta["row_count"] = meta["row_count"] + 1
    meta_path.write_text(json.dumps(meta))
    assert cache.get_frame(Stage.RETURNS, FP_A, "returns") is None


def test_tampered_meta_schema_is_miss(cache: StageCache) -> None:
    cache.put_frame(Stage.RETURNS, FP_A, "returns", _frame())
    meta_path = cache.stage_dir(Stage.RETURNS, FP_A) / "returns.json"
    meta = json.loads(meta_path.read_text())
    for pair in meta["columns"]:
        if pair[0] == "value":
            pair[1] = "Int64"  # dtype drift
    meta_path.write_text(json.dumps(meta))
    assert cache.get_frame(Stage.RETURNS, FP_A, "returns") is None


def test_atomic_write_leaves_no_tmp_files(cache: StageCache) -> None:
    cache.put_frame(Stage.RETURNS, FP_A, "returns", _frame())
    leftovers = list(cache.root.rglob("*.tmp"))
    assert leftovers == []


def test_cached_or_compute_computes_once(cache: StageCache) -> None:
    calls = 0

    def compute() -> pl.DataFrame:
        nonlocal calls
        calls += 1
        return _frame()

    first = cache.cached_or_compute(Stage.METRICS, FP_A, "metric_values", compute)
    second = cache.cached_or_compute(Stage.METRICS, FP_A, "metric_values", compute)
    assert calls == 1
    assert first.equals(second)


def test_load_stage_requires_all_frames(cache: StageCache) -> None:
    frame = _frame()
    cache.put_frame(Stage.EXPOSURES, FP_A, "exposures", frame)
    cache.put_frame(Stage.EXPOSURES, FP_A, "contributions", frame)
    # event_windows missing -> whole stage is a miss
    assert (
        cache.load_stage(Stage.EXPOSURES, FP_A, ["exposures", "contributions", "event_windows"])
        is None
    )
    assert cache.load_stage(Stage.EXPOSURES, FP_A, ["exposures", "contributions"]) is not None


def test_store_stage_roundtrip(cache: StageCache) -> None:
    frames = {"a": _frame(), "b": _frame().with_columns(pl.lit(1).alias("extra"))}
    # "b" has an extra column — cache stores schemas verbatim, no contract check.
    cache.store_stage(Stage.STATISTICS, FP_A, frames)
    loaded = cache.load_stage(Stage.STATISTICS, FP_A, ["a", "b"])
    assert loaded is not None
    assert loaded["a"].equals(frames["a"])
    assert loaded["b"].equals(frames["b"])


# ---------------------------------------------------------------------------
# Invalidation matrix: which stage fingerprints change per methodology field
# ---------------------------------------------------------------------------


def _context() -> RunContext:
    return RunContext(
        as_of_date=date(2025, 1, 17),
        knowledge_cutoff=datetime(2025, 1, 18, tzinfo=UTC),
        source_revision="test",
        source_fingerprint="0" * 64,
        universe_fingerprint="0" * 64,
        algorithm_version="test",
    )


def _fingerprints(methodology: Methodology, context: RunContext | None = None) -> dict[Stage, str]:
    ctx = context or _context()
    fps: dict[Stage, str] = {}
    upstream: tuple[str, ...] = ()
    for stage in STAGE_ORDER:
        fps[stage] = stage_fingerprint(stage, methodology, ctx, upstream)
        upstream = (*upstream, fps[stage])
    return fps


def _changed(base: Methodology, other: Methodology) -> set[Stage]:
    a, b = _fingerprints(base), _fingerprints(other)
    return {stage for stage in STAGE_ORDER if a[stage] != b[stage]}


def test_metrics_only_field_invalidates_metrics_and_statistics() -> None:
    changed = _changed(Methodology(), Methodology(transaction_cost_bps=5.0))
    assert changed == {Stage.METRICS, Stage.STATISTICS}


def test_statistics_only_field_invalidates_statistics() -> None:
    changed = _changed(Methodology(), Methodology(bootstrap_seed=12345))
    assert changed == {Stage.STATISTICS}


def test_peer_group_dims_invalidates_statistics_only() -> None:
    changed = _changed(
        Methodology(),
        Methodology(peer_group_dims=(Methodology().peer_group_dims[0],)),
    )
    assert changed == {Stage.STATISTICS}


def test_returns_field_cascades_downstream_only() -> None:
    # benchmark_id is a RETURNS-stage methodology field; INTERVALS is upstream
    # and unchanged, everything downstream of RETURNS picks up the new fp.
    changed = _changed(Methodology(), Methodology(benchmark_id="IDX_OTHER"))
    assert changed == {Stage.RETURNS, Stage.EXPOSURES, Stage.METRICS, Stage.STATISTICS}


def test_intervals_field_invalidates_everything() -> None:
    changed = _changed(Methodology(), Methodology(entry_convention="same_close"))
    assert changed == set(STAGE_ORDER)


def test_context_change_invalidates_everything() -> None:
    base = _fingerprints(Methodology())
    other_ctx = _context().model_copy(update={"as_of_date": date(2025, 1, 18)})
    other = _fingerprints(Methodology(), other_ctx)
    assert {s for s in STAGE_ORDER if base[s] != other[s]} == set(STAGE_ORDER)


def test_unchanged_stages_keep_cache_entries() -> None:
    """A METRICS-only change leaves earlier stage fingerprints (cache keys) intact."""
    base = _fingerprints(Methodology())
    other = _fingerprints(Methodology(transaction_cost_bps=5.0))
    for stage in (Stage.INTERVALS, Stage.RETURNS, Stage.EXPOSURES):
        assert base[stage] == other[stage]
