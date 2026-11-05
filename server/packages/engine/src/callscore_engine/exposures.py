"""Exposure segments: position weights, consensus deviations and decay tapering.

Implements the weight and segment rules of decision 0003. Interval boundaries are
independent of weights, so a weight-only change only rewrites this frame.
"""

from bisect import bisect_left
from datetime import date

import polars as pl

from callscore_core.enums import CanonicalRating, ExpiryPolicy, ScoringMode
from callscore_core.frames import POINT_CLOSE, POINT_OPEN
from callscore_core.methodology import Methodology

_SCHEMA: dict[str, pl.DataType] = {
    "interval_id": pl.String(),
    "segment_no": pl.Int64(),
    "start_session": pl.Date(),
    "start_point": pl.String(),
    "end_session": pl.Date(),
    "end_point": pl.String(),
    "position_weight": pl.Float64(),
    "weight_source": pl.String(),
}

_POINT_RANK = {POINT_OPEN: 0, POINT_CLOSE: 1}

_INTERVAL_COLUMNS = (
    "interval_id",
    "analyst_id",
    "security_id",
    "canonical_rating",
    "is_investable",
    "available_ts",
    "entry_ts",
    "exit_ts",
    "entry_session",
    "entry_point",
    "exit_session",
    "exit_point",
    "decay_start_session",
)


def _point_after(session_a: date, point_a: str, session_b: date, point_b: str) -> bool:
    """True when execution point (session_a, point_a) is strictly after (session_b, point_b)."""
    if session_a != session_b:
        return session_a > session_b
    return _POINT_RANK[point_a] > _POINT_RANK[point_b]


def _clamp_end(
    end_session: date,
    end_point: str,
    exit_session: date,
    exit_point: str,
) -> tuple[date, str]:
    """Cap a segment end at the interval exit point (exposure is ``[entry, exit)``)."""
    if _point_after(end_session, end_point, exit_session, exit_point):
        return exit_session, exit_point
    return end_session, end_point


def _segment_rows(
    interval: dict[str, object],
    weight: float | None,
    weight_source: str,
    session_dates: list[date],
    decay_days: int,
) -> list[dict[str, object]]:
    """Exposure segments for one interval, ``segment_no`` from 1."""
    entry_session = interval["entry_session"]
    entry_point = interval["entry_point"]
    exit_session = interval["exit_session"]
    exit_point = interval["exit_point"]
    assert isinstance(entry_session, date)
    assert isinstance(exit_session, date)
    assert isinstance(entry_point, str)
    assert isinstance(exit_point, str)

    segments: list[tuple[date, str, date, str, float | None, str]] = []
    decay_start = interval["decay_start_session"]
    use_decay = interval["is_investable"] and isinstance(decay_start, date) and session_dates

    if not use_decay:
        segments.append(
            (entry_session, entry_point, exit_session, exit_point, weight, weight_source)
        )
    else:
        assert isinstance(decay_start, date)
        # Segment 1: entry -> close of the session before decay_start, full weight.
        decay_idx = bisect_left(session_dates, decay_start)
        if decay_idx >= 1:
            prev_session = session_dates[decay_idx - 1]
            if not _point_after(entry_session, entry_point, prev_session, POINT_CLOSE):
                end_session, end_point = _clamp_end(
                    prev_session, POINT_CLOSE, exit_session, exit_point
                )
                if _point_after(end_session, end_point, entry_session, entry_point):
                    segments.append(
                        (
                            entry_session,
                            entry_point,
                            end_session,
                            end_point,
                            weight,
                            weight_source,
                        )
                    )
        # Decay sessions k = 1..decay_days: prev-close -> close, weight w * (1 - k / decay_days).
        for k in range(1, decay_days + 1):
            session_idx = decay_idx + k - 1
            if session_idx >= len(session_dates):
                break
            session = session_dates[session_idx]
            if session_idx >= 1:
                start_session = session_dates[session_idx - 1]
                start_point = POINT_CLOSE
            else:
                start_session = session
                start_point = POINT_OPEN
            # Stop once the segment start reaches the exit point.
            if not _point_after(exit_session, exit_point, start_session, start_point):
                break
            end_session, end_point = _clamp_end(session, POINT_CLOSE, exit_session, exit_point)
            if not _point_after(end_session, end_point, start_session, start_point):
                continue
            taper = weight * (1.0 - k / decay_days) if weight is not None else None
            segments.append((start_session, start_point, end_session, end_point, taper, "decay"))

    return [
        {
            "interval_id": interval["interval_id"],
            "segment_no": segment_no,
            "start_session": start_session,
            "start_point": start_point,
            "end_session": end_session,
            "end_point": end_point,
            "position_weight": segment_weight,
            "weight_source": source,
        }
        for segment_no, (
            start_session,
            start_point,
            end_session,
            end_point,
            segment_weight,
            source,
        ) in enumerate(segments, start=1)
    ]


def build_exposures(
    intervals: pl.DataFrame,
    events: pl.DataFrame,
    sessions: pl.DataFrame,
    methodology: Methodology,
) -> pl.DataFrame:
    """Build ``EXPOSURE_FRAME`` rows for every interval.

    ``events`` (the PIT-visible rating events) is accepted for stage-signature
    parity; weights derive from the intervals frame itself, which already carries
    each interval's canonical rating, analyst and lifetime.
    """
    del events
    if intervals.is_empty():
        return pl.DataFrame(schema=_SCHEMA)

    rating_weights = {
        rating.value: methodology.position_weights.for_rating(rating) for rating in CanonicalRating
    }
    relative = methodology.scoring_mode == ScoringMode.RELATIVE_TO_CONSENSUS
    base_source = "consensus" if relative else "rating"

    rows = [
        {column: row[column] for column in _INTERVAL_COLUMNS}
        for row in intervals.select(_INTERVAL_COLUMNS).to_dicts()
    ]
    investable = [row for row in rows if row["is_investable"]]
    by_security: dict[str, list[dict[str, object]]] = {}
    for row in investable:
        by_security.setdefault(str(row["security_id"]), []).append(row)

    def own_weight(row: dict[str, object]) -> float | None:
        return rating_weights.get(str(row["canonical_rating"]))

    weights: dict[str, float | None] = {}
    for row in rows:
        interval_id = str(row["interval_id"])
        if not row["is_investable"]:
            weights[interval_id] = None
            continue
        w_own = own_weight(row)
        if w_own is None:
            weights[interval_id] = None
        elif not relative:
            weights[interval_id] = w_own
        else:
            available_ts = row["available_ts"]
            others = [
                other_weight
                for other in by_security.get(str(row["security_id"]), [])
                if other["analyst_id"] != row["analyst_id"]
                and other["entry_ts"] <= available_ts < other["exit_ts"]
                for other_weight in [own_weight(other)]
                if other_weight is not None
            ]
            if len(others) < methodology.min_consensus_ratings:
                weights[interval_id] = None
            else:
                deviation = w_own - sum(others) / len(others)
                weights[interval_id] = min(1.0, max(-1.0, deviation))

    session_dates = sorted(
        sessions.select(pl.col("session_date").cast(pl.Date)).unique().to_series().to_list()
    )

    out: list[dict[str, object]] = []
    for row in rows:
        interval_id = str(row["interval_id"])
        weight = weights[interval_id]
        use_decay = (
            methodology.expiry_policy == ExpiryPolicy.DECAY
            and row["is_investable"]
            and row["decay_start_session"] is not None
        )
        out.extend(
            _segment_rows(
                row,
                weight,
                base_source,
                session_dates if use_decay else [],
                methodology.decay_days,
            )
        )

    frame = pl.DataFrame(out, schema=_SCHEMA)
    return frame.sort("interval_id", "segment_no")
