"""Smoke test: ``score_frame`` runs the real pipeline (was a placeholder)."""

from datetime import UTC, date, datetime

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.methodology import Methodology
from callscore_engine import score_frame
from callscore_engine.runner import PipelineResult, SnapshotFrames


def _context() -> RunContext:
    return RunContext(
        as_of_date=date(2025, 1, 2),
        knowledge_cutoff=datetime(2025, 1, 3, tzinfo=UTC),
        source_revision="test",
        source_fingerprint="0" * 64,
        universe_fingerprint="0" * 64,
        algorithm_version="test",
    )


def test_score_frame_runs_pipeline_on_empty_inputs() -> None:
    sessions = pl.DataFrame(
        {
            "exchange_id": ["XNYS"],
            "session_date": [date(2025, 1, 2)],
            "open_ts": [datetime(2025, 1, 2, 14, 30, tzinfo=UTC)],
            "close_ts": [datetime(2025, 1, 2, 21, 0, tzinfo=UTC)],
        }
    )
    result = score_frame(SnapshotFrames(sessions=sessions), Methodology(), _context())
    assert isinstance(result, PipelineResult)
    assert result.intervals.height == 0
    assert result.metric_results.height == 0
    assert set(result.stage_fingerprints) == {
        "intervals",
        "returns",
        "exposures",
        "metrics",
        "statistics",
    }
