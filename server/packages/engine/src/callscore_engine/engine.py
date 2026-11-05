"""Scoring entry point: compose the pure pipeline (``runner.run_pipeline``)."""

from collections.abc import Mapping

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.methodology import Methodology
from callscore_engine.runner import PipelineResult, SnapshotFrames, run_pipeline


def score_frame(
    frames: SnapshotFrames | Mapping[str, pl.DataFrame],
    methodology: Methodology,
    context: RunContext,
) -> PipelineResult:
    """Run the full scoring pipeline; returns every stage output frame.

    Formerly a placeholder returning its input unchanged; it now delegates to
    :func:`callscore_engine.runner.run_pipeline`.
    """
    return run_pipeline(frames, methodology, context)


__all__ = ["PipelineResult", "SnapshotFrames", "score_frame"]
