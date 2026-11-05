"""Daily consensus frame: mean canonical weight of visible ratings.

Produces the store-shaped ``consensus_daily`` table
(``security_id, session_date, n_ratings, consensus_score, target_price,
target_currency``) from the generated ``rating_events``.

Point-in-time rules (mirroring ``callscore_engine.point_in_time`` /
``coverage``):

* An event is visible at a session close ``c`` when
  ``available_ts = max(effective_ts, knowledge_ts) < c``.
* An event superseded by a *visible* correction
  (``is_correction`` with ``supersedes_event_id``) no longer counts.
* Per (analyst, security) only the latest visible event by
  ``(available_ts, event_id)`` counts.
* A rating counts only while ``expiry_date`` is null or ``>= session_date``
  and its canonical rating carries a position weight (``not_rated``,
  ``under_review`` and ``restricted`` are excluded from both the mean and
  ``n_ratings``).

Implementation: per security, the active set can only change at session
indices where an event becomes visible or where some rating's ``expiry_date``
passes. Between consecutive change points the consensus is constant, so the
mean is computed once per segment and emitted for every session in it. Rows
are emitted for every listed session with at least one active rating;
sessions with zero active ratings are omitted (documented choice — the table
is a snapshot of *existing* consensus).
"""

from datetime import date, datetime
from typing import NamedTuple

import polars as pl

from callscore_core.enums import CanonicalRating
from callscore_core.methodology import PositionWeights

_CONSENSUS_SCHEMA = {
    "security_id": pl.String,
    "session_date": pl.Date,
    "n_ratings": pl.Int64,
    "consensus_score": pl.Float64,
    "target_price": pl.Float64,
    "target_currency": pl.String,
}


class _LiveRating(NamedTuple):
    avail: datetime
    event_id: str
    weight: float | None  # None -> canonical carries no position weight
    expiry: date | None
    price_target: float | None


def _event_weight(canonical: str, weights: PositionWeights) -> float | None:
    try:
        return weights.for_rating(CanonicalRating(canonical))
    except ValueError:
        return None


def build_consensus_daily(frames: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Build ``consensus_daily`` rows from ``rating_events`` + market frames."""
    events_frame = frames.get("rating_events")
    sessions_frame = frames.get("sessions")
    daily_frame = frames.get("security_daily")
    weights = PositionWeights()

    empty = pl.DataFrame(schema=_CONSENSUS_SCHEMA)
    if (
        events_frame is None
        or sessions_frame is None
        or daily_frame is None
        or events_frame.height == 0
    ):
        return empty

    close_ts_by_date = {
        r[0]: r[1] for r in sessions_frame.select("session_date", "close_ts").iter_rows()
    }

    ev_by_sec: dict[str, list[dict[str, object]]] = {}
    for row in events_frame.iter_rows(named=True):
        ev_by_sec.setdefault(str(row["security_id"]), []).append(row)

    sec_dates: dict[str, list[date]] = {}
    for raw_key, grp in (
        daily_frame.sort("security_id", "session_date")
        .partition_by("security_id", as_dict=True)
        .items()
    ):
        key = str(raw_key[0]) if isinstance(raw_key, tuple) else str(raw_key)
        sec_dates[key] = grp["session_date"].to_list()

    out_rows: list[dict[str, object]] = []
    for sid, dates in sec_dates.items():
        evs = ev_by_sec.get(sid)
        if evs:
            _emit_security_consensus(sid, dates, evs, close_ts_by_date, weights, out_rows)

    if not out_rows:
        return empty
    return pl.DataFrame(out_rows, schema=_CONSENSUS_SCHEMA).sort("security_id", "session_date")


def _emit_security_consensus(
    sid: str,
    dates: list[date],
    evs: list[dict[str, object]],
    close_ts_by_date: dict[date, datetime],
    weights: PositionWeights,
    out_rows: list[dict[str, object]],
) -> None:
    n = len(dates)
    closes = [close_ts_by_date.get(d) for d in dates]

    # Sort events by (available_ts, event_id); availability order == visibility order.
    def _avail(e: dict[str, object]) -> tuple[datetime, str]:
        eff, kno = e["effective_ts"], e["knowledge_ts"]
        assert isinstance(eff, datetime) and isinstance(kno, datetime)
        return max(eff, kno), str(e["event_id"])

    evs = sorted(evs, key=_avail)

    # Change points: session indices where the active set may change.
    change: set[int] = {0, n}
    parsed: list[tuple[datetime, str, str, str, date | None, float | None]] = []
    for e in evs:
        avail, eid = _avail(e)
        expiry = e["expiry_date"]
        exp = expiry if isinstance(expiry, date) else None
        pt = e["price_target"]
        parsed.append(
            (
                avail,
                eid,
                str(e["analyst_id"]),
                str(e["canonical_rating"]),
                exp,
                float(pt) if isinstance(pt, int | float) else None,
            )
        )
        # First session index whose close is strictly after avail.
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            c = closes[mid]
            if c is not None and c > avail:
                hi = mid
            else:
                lo = mid + 1
        change.add(lo)
        if exp is not None:
            # First session index whose date is strictly after expiry_date.
            lo2, hi2 = 0, n
            while lo2 < hi2:
                mid = (lo2 + hi2) // 2
                if dates[mid] > exp:
                    hi2 = mid
                else:
                    lo2 = mid + 1
            change.add(lo2)

    bounds = sorted(change)
    ev_pos = 0
    stacks: dict[str, list[_LiveRating]] = {}
    superseded: set[str] = set()
    is_correction = [bool(e["is_correction"]) for e in evs]
    supersedes_ids = [
        str(e["supersedes_event_id"]) if e["supersedes_event_id"] is not None else None for e in evs
    ]

    for seg_start, seg_end in zip(bounds, bounds[1:], strict=False):
        # Activate every event visible by the segment's first session.
        seg_close = closes[seg_start]
        while ev_pos < len(parsed):
            avail, eid, aid, canon, exp, pt = parsed[ev_pos]
            c = seg_close
            if c is None or avail >= c:
                break
            ev_pos += 1
            if is_correction[ev_pos - 1] and supersedes_ids[ev_pos - 1] is not None:
                superseded.add(supersedes_ids[ev_pos - 1] or "")
            stacks.setdefault(aid, []).append(
                _LiveRating(
                    avail=avail,
                    event_id=eid,
                    weight=_event_weight(canon, weights),
                    expiry=exp,
                    price_target=pt,
                )
            )
        if seg_end <= seg_start:
            continue
        # Within [seg_start, seg_end) no visibility or expiry boundary exists,
        # so a rating is active for the whole segment iff its expiry is None
        # or >= the segment's last session date.
        last_date = dates[seg_end - 1]
        n_ratings = 0
        w_sum = 0.0
        pt_sum = 0.0
        n_pt = 0
        for stack in stacks.values():
            top = _current_rating(stack, superseded)
            if top is None or top.weight is None:
                continue
            if top.expiry is not None and top.expiry < last_date:
                continue
            n_ratings += 1
            w_sum += top.weight
            if top.price_target is not None:
                pt_sum += top.price_target
                n_pt += 1
        if n_ratings == 0:
            continue
        score = w_sum / n_ratings
        mean_pt = pt_sum / n_pt if n_pt else None
        for k in range(seg_start, seg_end):
            out_rows.append(
                {
                    "security_id": sid,
                    "session_date": dates[k],
                    "n_ratings": n_ratings,
                    "consensus_score": score,
                    "target_price": mean_pt,
                    "target_currency": "USD" if n_pt else None,
                }
            )


def _current_rating(stack: list[_LiveRating], superseded: set[str]) -> _LiveRating | None:
    """Latest non-superseded rating in an analyst's stack (avail order)."""
    for rating in reversed(stack):
        if rating.event_id not in superseded:
            return rating
    return None
