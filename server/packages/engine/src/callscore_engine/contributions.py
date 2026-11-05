"""Contribution frame: segment-weighted excess returns per interval and horizon.

``to_exit`` sums ``segment_weight x segment_excess_return`` over the interval's
exposure segments (priced by ``returns.compute_legs``, so a single segment equals
``weight x to_exit excess`` exactly). Fixed horizons use the first segment's weight
(the full rating weight) times the horizon excess return from ``RETURN_FRAME``.
"""

import polars as pl

from callscore_core.frames import HORIZON_TO_EXIT, horizon_label
from callscore_core.methodology import Methodology
from callscore_engine.returns import compute_legs

_SCHEMA: dict[str, pl.DataType] = {
    "interval_id": pl.String(),
    "analyst_id": pl.String(),
    "horizon": pl.String(),
    "entry_session": pl.Date(),
    "position_weight": pl.Float64(),
    "excess_return": pl.Float64(),
    "contribution": pl.Float64(),
    "unavailable_reason": pl.String(),
}


def _first_segment_weights(exposures: pl.DataFrame) -> dict[str, float | None]:
    """``interval_id -> position_weight`` of segment 1 (null when absent or null)."""
    first = exposures.filter(pl.col("segment_no") == 1)
    return {
        str(row["interval_id"]): (
            None if row["position_weight"] is None else float(row["position_weight"])
        )
        for row in first.iter_rows(named=True)
    }


def _segment_excess(
    exposures: pl.DataFrame,
    intervals: pl.DataFrame,
    security_daily: pl.DataFrame,
    benchmark_daily: pl.DataFrame,
    beta: pl.DataFrame,
    delistings: pl.DataFrame,
    classifications: pl.DataFrame,
    methodology: Methodology,
) -> dict[tuple[str, int], float | None]:
    """Per-segment excess return for investable intervals, keyed by (interval_id, segment_no)."""
    investable_ids = intervals.filter(pl.col("is_investable")).select("interval_id")
    segments = exposures.join(investable_ids, on="interval_id", how="inner")
    if segments.is_empty():
        return {}
    legs = segments.join(
        intervals.select("interval_id", "security_id", "entry_session"),
        on="interval_id",
        how="inner",
    ).select(
        pl.concat_str(
            pl.col("interval_id"), pl.lit("#"), pl.col("segment_no").cast(pl.String)
        ).alias("leg_id"),
        "interval_id",
        "security_id",
        "entry_session",
        pl.col("start_session"),
        pl.col("start_point"),
        pl.col("end_session"),
        pl.col("end_point"),
        "segment_no",
        "position_weight",
    )
    priced = compute_legs(
        legs,
        security_daily=security_daily,
        benchmark_daily=benchmark_daily,
        beta=beta,
        delistings=delistings,
        classifications=classifications,
        methodology=methodology,
    )
    segment_weights = {
        (str(row["interval_id"]), int(row["segment_no"])): (
            None if row["position_weight"] is None else float(row["position_weight"])
        )
        for row in legs.select("interval_id", "segment_no", "position_weight").iter_rows(named=True)
    }
    out: dict[tuple[str, int], float | None] = {}
    for row in priced.iter_rows(named=True):
        key = (str(row["interval_id"]), int(row["segment_no"]))
        weight = segment_weights[key]
        excess = row["excess_return"]
        out[key] = None if weight is None or excess is None else weight * float(excess)
    return out


def build_contributions(
    intervals: pl.DataFrame,
    exposures: pl.DataFrame,
    returns: pl.DataFrame,
    security_daily: pl.DataFrame,
    benchmark_daily: pl.DataFrame,
    beta: pl.DataFrame,
    delistings: pl.DataFrame,
    classifications: pl.DataFrame,
    methodology: Methodology,
) -> pl.DataFrame:
    """Build ``CONTRIBUTION_FRAME`` rows for every interval and horizon."""
    horizons = [HORIZON_TO_EXIT] + [horizon_label(days) for days in methodology.fixed_horizons_days]

    first_weights = _first_segment_weights(exposures)
    segment_contrib = _segment_excess(
        exposures,
        intervals,
        security_daily,
        benchmark_daily,
        beta,
        delistings,
        classifications,
        methodology,
    )
    ret_lookup = {
        (str(row["interval_id"]), str(row["horizon"])): row for row in returns.iter_rows(named=True)
    }
    segments_by_interval: dict[str, list[tuple[int, float | None]]] = {}
    for row in exposures.iter_rows(named=True):
        weight = row["position_weight"]
        segments_by_interval.setdefault(str(row["interval_id"]), []).append(
            (int(row["segment_no"]), None if weight is None else float(weight))
        )
    for segments in segments_by_interval.values():
        segments.sort()

    out: list[dict[str, object]] = []
    for row in intervals.iter_rows(named=True):
        interval_id = str(row["interval_id"])
        analyst_id = str(row["analyst_id"])
        entry_session = row["entry_session"]
        investable = bool(row["is_investable"])
        weight = first_weights.get(interval_id)
        segments = segments_by_interval.get(interval_id, [])

        for horizon in horizons:
            ret = ret_lookup.get((interval_id, horizon))
            position_weight = weight if investable else None
            excess_return = (
                None if ret is None or ret["excess_return"] is None else float(ret["excess_return"])
            )
            contribution: float | None = None
            reason: str | None = None

            if not investable:
                reason = "non_investable"
                position_weight = None
                excess_return = None
            elif horizon == HORIZON_TO_EXIT:
                if weight is None:
                    reason = "insufficient_consensus"
                else:
                    parts = [
                        segment_contrib.get((interval_id, segment_no)) for segment_no, _ in segments
                    ]
                    if parts and all(part is not None for part in parts):
                        contribution = sum(part for part in parts if part is not None)
            else:
                if ret is not None and ret["unavailable_reason"] is not None:
                    reason = str(ret["unavailable_reason"])
                    excess_return = None
                elif weight is None:
                    reason = "insufficient_consensus"
                elif excess_return is not None:
                    contribution = weight * excess_return

            out.append(
                {
                    "interval_id": interval_id,
                    "analyst_id": analyst_id,
                    "horizon": horizon,
                    "entry_session": entry_session,
                    "position_weight": position_weight,
                    "excess_return": excess_return,
                    "contribution": contribution,
                    "unavailable_reason": reason,
                }
            )

    frame = pl.DataFrame(out, schema=_SCHEMA)
    return frame.sort("interval_id", "horizon")
