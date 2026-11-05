"""Unit tests for ``callscore_engine.runner`` (hand-built minimal frames)."""

from datetime import UTC, date, datetime

import polars as pl
import pytest

from callscore_core.contracts import RunContext, StageArtifact, stage_fingerprint
from callscore_core.enums import Stage
from callscore_core.frames import FRAME_CONTRACTS, INTERVAL_FRAME
from callscore_core.methodology import Methodology
from callscore_engine.runner import (
    PipelineResult,
    SnapshotFrames,
    assert_frame_contract,
    run_pipeline,
)


def _context() -> RunContext:
    return RunContext(
        as_of_date=date(2025, 1, 2),
        knowledge_cutoff=datetime(2025, 1, 3, tzinfo=UTC),
        source_revision="test",
        source_fingerprint="0" * 64,
        universe_fingerprint="0" * 64,
        algorithm_version="test",
    )


def _sessions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "exchange_id": ["XNYS", "XNYS"],
            "session_date": [date(2025, 1, 2), date(2025, 1, 3)],
            "open_ts": [
                datetime(2025, 1, 2, 14, 30, tzinfo=UTC),
                datetime(2025, 1, 3, 14, 30, tzinfo=UTC),
            ],
            "close_ts": [
                datetime(2025, 1, 2, 21, 0, tzinfo=UTC),
                datetime(2025, 1, 3, 21, 0, tzinfo=UTC),
            ],
        }
    )


def _run(methodology: Methodology | None = None) -> PipelineResult:
    return run_pipeline(
        SnapshotFrames(sessions=_sessions()), methodology or Methodology(), _context()
    )


def test_empty_inputs_produce_contract_outputs() -> None:
    result = _run()
    assert isinstance(result, PipelineResult)
    for name in (
        "intervals",
        "exposures",
        "returns",
        "contributions",
        "event_windows",
        "target_outcomes",
        "coverage_daily",
        "portfolio_nav",
        "portfolio_positions",
        "metric_samples",
        "metric_results",
        "composites",
    ):
        frame = getattr(result, name)
        assert_frame_contract(frame, FRAME_CONTRACTS[name])
        assert frame.height == 0


def test_accepts_plain_mapping_input() -> None:
    result = run_pipeline({"sessions": _sessions()}, Methodology(), _context())
    assert result.intervals.height == 0


def test_stage_fingerprints_cover_all_stages_and_are_deterministic() -> None:
    methodology = Methodology()
    context = _context()
    first = run_pipeline(SnapshotFrames(sessions=_sessions()), methodology, context)
    second = run_pipeline(SnapshotFrames(sessions=_sessions()), methodology, context)
    assert set(first.stage_fingerprints) == set(Stage)
    assert first.stage_fingerprints == second.stage_fingerprints
    # Fingerprints match contracts.stage_fingerprint chained in STAGE_ORDER.
    upstream: tuple[str, ...] = ()
    for stage in (Stage.INTERVALS, Stage.RETURNS, Stage.EXPOSURES, Stage.METRICS, Stage.STATISTICS):
        expected = stage_fingerprint(stage, methodology, context, upstream)
        assert first.stage_fingerprints[stage] == expected
        upstream = (*upstream, expected)


def test_methodology_change_invalidates_owning_stage() -> None:
    base = _run()
    decay = _run(Methodology(decay_days=30))
    cost = _run(Methodology(transaction_cost_bps=10.0))
    # decay_days is an INTERVALS field: later stages see it via upstreams.
    assert decay.stage_fingerprints[Stage.INTERVALS] != base.stage_fingerprints[Stage.INTERVALS]
    assert decay.stage_fingerprints[Stage.RETURNS] != base.stage_fingerprints[Stage.RETURNS]
    # transaction_cost_bps is a METRICS field: statistics sees it via upstream.
    assert cost.stage_fingerprints[Stage.METRICS] != base.stage_fingerprints[Stage.METRICS]
    assert cost.stage_fingerprints[Stage.STATISTICS] != base.stage_fingerprints[Stage.STATISTICS]
    # ...but it must not leak into earlier stages.
    assert cost.stage_fingerprints[Stage.INTERVALS] == base.stage_fingerprints[Stage.INTERVALS]
    assert cost.stage_fingerprints[Stage.RETURNS] == base.stage_fingerprints[Stage.RETURNS]
    assert cost.stage_fingerprints[Stage.EXPOSURES] == base.stage_fingerprints[Stage.EXPOSURES]


def test_stage_artifacts_reference_stage_fingerprints() -> None:
    result = _run()
    assert len(result.stage_artifacts) == 13
    assert all(isinstance(a, StageArtifact) for a in result.stage_artifacts)
    by_stage = {a.stage for a in result.stage_artifacts}
    assert by_stage == set(Stage)
    for artifact in result.stage_artifacts:
        assert artifact.cache_fingerprint == result.stage_fingerprints[artifact.stage]
        assert artifact.row_count == 0
    # EXPOSURES-stage artifacts carry (INTERVALS, RETURNS) upstream fingerprints.
    exposures_artifacts = [a for a in result.stage_artifacts if a.stage == Stage.EXPOSURES]
    expected_upstream = (
        result.stage_fingerprints[Stage.INTERVALS],
        result.stage_fingerprints[Stage.RETURNS],
    )
    assert all(a.upstream_fingerprints == expected_upstream for a in exposures_artifacts)


def test_assert_frame_contract_rejects_violations() -> None:
    good = pl.DataFrame(
        schema={
            c.name: {
                "String": pl.String,
                "Date": pl.Date,
                "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
                "Float64": pl.Float64,
                "Int64": pl.Int64,
                "Boolean": pl.Boolean,
            }[c.dtype]
            for c in INTERVAL_FRAME.columns
        }
    )
    assert_frame_contract(good, INTERVAL_FRAME)
    bad_dtype = good.with_columns(pl.lit(1).alias("interval_id"))
    with pytest.raises(ValueError, match="dtype"):
        assert_frame_contract(bad_dtype, INTERVAL_FRAME)
