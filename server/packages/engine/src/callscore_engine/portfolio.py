"""Analyst portfolio simulation: one NAV book per analyst (decision 0003).

Conventions implemented here:

* One book per ``analyst_id``. Every interval is hedged against the benchmark
  chosen by ``pricing.benchmark_assignment``. In ``beta_adjusted`` mode the
  hedge leg is ``beta`` times the market benchmark return, with beta taken
  from the latest ``security_beta`` row whose ``as_of_date`` is strictly
  before the interval's ``entry_session``. A missing beta raises
  ``ValueError`` — the run fails rather than assuming a number.
* Target weights are the active exposure segments' ``position_weight`` values
  (null weights are ignored), summed per security per session and rescaled so
  ``sum(|w_i|) = 1`` — gross exposure 1 — whenever anything is held. When no
  segment is active the book is all cash: ``active_return`` and
  ``gross_exposure`` are 0. Weights are target weights rebalanced every
  session; price drift never moves them.
* The session calendar of an analyst is the union of its held securities'
  session dates: the ``sessions`` rows of the exchange(s) found in the
  security's classification spells, clipped to each spell's inclusive
  ``[from_date, to_date]`` (null ``to_date`` is open-ended). A security with
  no classification falls back to its own ``security_daily`` dates. Rows run
  from the first entry session through the last calendar session
  ``<= context.as_of_date``; idle sessions are included with
  ``active_return = 0``. A security with no ``security_daily`` row on a union
  session contributes a stock return of 0 (its hedge leg still accrues).
* The position return on session ``t`` uses total-return levels (``px``
  levels when ``methodology.return_basis`` is ``price``): the entry session
  measures from the entry point to the close (0 when entering at the close),
  the exit session from the previous session's close to the exit point, and
  every other session close-to-close on the security's own calendar. On the
  delisting date the position realizes the terminal value
  ``close_level * (1 + final_return)`` and closes; segments reaching past the
  delisting date are truncated to it (an exit at the delisting open still
  happens before the terminal value).
* ``active_return(t) = sum_i w_i(t) * (r_i(t) - b_i(t)) - cost(t)`` where the
  benchmark leg ``b_i`` spans exactly the same points as the stock leg and
  ``cost(t) = turnover(t) * transaction_cost_bps / 10_000`` with
  ``turnover(t) = sum_i |w_i(t) - w_i(t-1)|`` over target-weight changes only
  (entry, exit, decay, rating change, renormalization — never price drift).
  ``nav`` and ``benchmark_nav`` (the hedge leg ``sum_i w_i b_i``, without
  cost) compound from 1.0 and ``drawdown`` is ``nav / running_max - 1``.
  Because turnover is measured between consecutive sessions' weights, the
  cost of an exit at a session's close lands on the following session.
* Missing benchmark levels on a held span raise ``MissingBenchmarkData``.
"""

import bisect
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.enums import ReturnBasis
from callscore_core.frames import POINT_CLOSE, POINT_OPEN
from callscore_core.methodology import Methodology
from callscore_engine.pricing import (
    MissingBenchmarkData,
    benchmark_assignment,
    benchmark_levels,
    execution_levels,
)

_NAV_SCHEMA: dict[str, pl.DataType] = {
    "analyst_id": pl.String(),
    "session_date": pl.Date(),
    "nav": pl.Float64(),
    "benchmark_nav": pl.Float64(),
    "active_return": pl.Float64(),
    "gross_exposure": pl.Float64(),
    "turnover": pl.Float64(),
    "cost": pl.Float64(),
    "drawdown": pl.Float64(),
}

_POSITIONS_SCHEMA: dict[str, pl.DataType] = {
    "analyst_id": pl.String(),
    "session_date": pl.Date(),
    "security_id": pl.String(),
    "interval_id": pl.String(),
    "weight": pl.Float64(),
}

_COST_RATE_DIVISOR = 10_000.0


@dataclass(frozen=True)
class _Segment:
    """One exposure segment with its market data and delisting-clipped span."""

    analyst_id: str
    interval_id: str
    security_id: str
    entry_session: date
    start_session: date
    start_point: str
    raw_weight: float
    benchmark_id: str | None
    beta_adjusted: bool
    beta: float | None
    eff_start: date
    eff_end: date
    eff_end_point: str
    terminal_return: float


@dataclass(frozen=True)
class _Levels:
    """Sorted session dates and ``date -> (open_level, close_level)`` for one id."""

    dates: tuple[date, ...]
    open_close: dict[date, tuple[float, float]]


class _BetaLookup:
    """Latest beta per security strictly before a session (``security_beta``)."""

    def __init__(self, beta: pl.DataFrame) -> None:
        rows: dict[str, list[tuple[date, float]]] = defaultdict(list)
        for security_id, as_of, value in beta.select(
            "security_id", "as_of_date", "beta"
        ).iter_rows():
            rows[security_id].append((as_of, float(value)))
        self._by_security = {
            key: (
                tuple(d for d, _ in sorted(values)),
                tuple(b for _, b in sorted(values)),
            )
            for key, values in rows.items()
        }

    def get(self, security_id: str, entry_session: date) -> float | None:
        entry = self._by_security.get(security_id)
        if entry is None:
            return None
        dates, betas = entry
        idx = bisect.bisect_left(dates, entry_session) - 1
        return betas[idx] if idx >= 0 else None


def _levels(frame: pl.DataFrame, id_col: str, open_col: str, close_col: str) -> dict[str, _Levels]:
    grouped: dict[str, dict[date, tuple[float, float]]] = defaultdict(dict)
    for key, session_date, open_level, close_level in frame.select(
        id_col, "session_date", open_col, close_col
    ).iter_rows():
        grouped[key][session_date] = (float(open_level), float(close_level))
    return {key: _Levels(tuple(sorted(points)), points) for key, points in grouped.items()}


def _build_segments(
    intervals: pl.DataFrame,
    exposures: pl.DataFrame,
    delistings: pl.DataFrame,
    methodology: Methodology | None,
    classifications: pl.DataFrame,
    beta: pl.DataFrame | None,
) -> dict[str, list[_Segment]]:
    """Exposure segments joined to intervals, benchmark-assigned and delisting-clipped.

    ``methodology=None`` skips benchmark/beta resolution (used by
    ``portfolio_positions``, which only needs weights).
    """
    active = exposures.filter(pl.col("position_weight").is_not_null())
    if active.is_empty() or intervals.is_empty():
        return {}
    joined = active.join(
        intervals.select("interval_id", "analyst_id", "security_id", "entry_session"),
        on="interval_id",
        how="inner",
    )
    if joined.is_empty():
        return {}

    beta_lookup: _BetaLookup | None = None
    if methodology is not None:
        assignment = benchmark_assignment(intervals, classifications, methodology)
        joined = joined.join(assignment, on="interval_id", how="left")
        if beta is not None and not beta.is_empty():
            beta_lookup = _BetaLookup(beta)
    else:
        joined = joined.with_columns(
            pl.lit(None, dtype=pl.String).alias("benchmark_id"),
            pl.lit(False).alias("beta_adjusted"),
        )

    delist_map = {
        security_id: (delist_date, float(final_return))
        for security_id, delist_date, final_return in delistings.select(
            "security_id", "delisting_date", "final_return"
        ).iter_rows()
    }

    by_analyst: dict[str, list[_Segment]] = defaultdict(list)
    for row in joined.iter_rows(named=True):
        security_id = row["security_id"]
        delist_date, final_return = delist_map.get(security_id, (None, 0.0))
        end_session = row["end_session"]
        end_point = row["end_point"]
        terminal_return = 0.0
        if delist_date is not None and delist_date <= end_session:
            # The position cannot outlive its security: it realizes the terminal
            # value at the delisting close unless it already exits at the open.
            eff_end = delist_date
            if end_session > delist_date or end_point == POINT_CLOSE:
                eff_end_point = POINT_CLOSE
                terminal_return = final_return
            else:
                eff_end_point = end_point
        else:
            eff_end = end_session
            eff_end_point = end_point
        eff_start = row["start_session"]
        if eff_end < eff_start:
            continue

        beta_adjusted = bool(row["beta_adjusted"])
        beta_value: float | None = None
        if beta_adjusted:
            if beta_lookup is None:
                raise ValueError(
                    f"beta_adjusted interval {row['interval_id']} but no beta frame given"
                )
            beta_value = beta_lookup.get(security_id, row["entry_session"])
            if beta_value is None:
                raise ValueError(
                    f"No beta for {security_id} strictly before {row['entry_session']} "
                    f"(interval {row['interval_id']})"
                )

        by_analyst[row["analyst_id"]].append(
            _Segment(
                analyst_id=row["analyst_id"],
                interval_id=row["interval_id"],
                security_id=security_id,
                entry_session=row["entry_session"],
                start_session=eff_start,
                start_point=row["start_point"],
                raw_weight=float(row["position_weight"]),
                benchmark_id=row["benchmark_id"],
                beta_adjusted=beta_adjusted,
                beta=beta_value,
                eff_start=eff_start,
                eff_end=eff_end,
                eff_end_point=eff_end_point,
                terminal_return=terminal_return,
            )
        )

    for segments in by_analyst.values():
        segments.sort(key=lambda s: (s.eff_start, s.interval_id, s.security_id))
    return dict(by_analyst)


def _exchange_calendars(sessions: pl.DataFrame) -> dict[str, tuple[date, ...]]:
    ordered = sessions.sort("exchange_id", "session_date")
    return {
        exchange_id: tuple(dates)
        for exchange_id, dates in ordered.group_by("exchange_id", maintain_order=True)
        .agg(pl.col("session_date"))
        .iter_rows()
    }


def _daily_dates(security_daily: pl.DataFrame) -> dict[str, tuple[date, ...]]:
    ordered = security_daily.sort("security_id", "session_date")
    return {
        security_id: tuple(dates)
        for security_id, dates in ordered.group_by("security_id", maintain_order=True)
        .agg(pl.col("session_date"))
        .iter_rows()
    }


def _security_calendars(
    needed: set[str],
    classifications: pl.DataFrame,
    exchange_calendars: dict[str, tuple[date, ...]],
    daily_dates: dict[str, tuple[date, ...]],
) -> dict[str, tuple[date, ...]]:
    """Session dates per held security: exchange calendar clipped to spells."""
    spells: dict[str, list[tuple[str, date, date | None]]] = defaultdict(list)
    if not classifications.is_empty():
        for security_id, exchange_id, from_date, to_date in classifications.select(
            "security_id", "exchange_id", "from_date", "to_date"
        ).iter_rows():
            spells[security_id].append((exchange_id, from_date, to_date))
    calendars: dict[str, tuple[date, ...]] = {}
    for security_id in needed:
        if security_id not in spells:
            calendars[security_id] = daily_dates.get(security_id, ())
            continue
        dates: set[date] = set()
        for exchange_id, from_date, to_date in spells[security_id]:
            calendar = exchange_calendars.get(exchange_id)
            if not calendar:
                continue
            lo = bisect.bisect_left(calendar, from_date)
            hi = bisect.bisect_right(calendar, to_date) if to_date is not None else len(calendar)
            dates.update(calendar[lo:hi])
        calendars[security_id] = tuple(sorted(dates))
    return calendars


def _daily_weights(
    segments: list[_Segment],
    calendars: dict[str, tuple[date, ...]],
    as_of: date,
) -> tuple[list[date], dict[date, dict[str, float]], dict[tuple[date, str], _Segment]]:
    """Row dates, scaled target weights per date and the segment behind each weight."""
    held = sorted({s.security_id for s in segments})
    calendar_set: set[date] = set()
    for security_id in held:
        calendar_set.update(calendars.get(security_id, ()))
    calendar = sorted(calendar_set)
    if not calendar:
        return [], {}, {}
    upto = bisect.bisect_right(calendar, as_of)
    if upto == 0:
        return [], {}, {}
    last = calendar[upto - 1]
    first = min(s.eff_start for s in segments)
    lo = bisect.bisect_left(calendar, first)
    hi = bisect.bisect_right(calendar, last)
    row_dates = calendar[lo:hi]
    if not row_dates:
        return [], {}, {}

    range_lo, range_hi = row_dates[0], row_dates[-1]
    raw: dict[date, dict[str, float]] = {}
    segment_at: dict[tuple[date, str], _Segment] = {}
    for segment in segments:
        a = bisect.bisect_left(calendar, max(segment.eff_start, range_lo))
        b = bisect.bisect_right(calendar, min(segment.eff_end, range_hi))
        for session_date in calendar[a:b]:
            bucket = raw.setdefault(session_date, {})
            bucket[segment.security_id] = bucket.get(segment.security_id, 0.0) + segment.raw_weight
            segment_at.setdefault((session_date, segment.security_id), segment)

    scaled: dict[date, dict[str, float]] = {}
    for session_date, bucket in raw.items():
        gross = sum(abs(value) for value in bucket.values())
        scaled[session_date] = {
            security_id: (value / gross if gross > 0 else 0.0)
            for security_id, value in bucket.items()
        }
    return row_dates, scaled, segment_at


def _stock_return(segment: _Segment, session_date: date, levels: _Levels | None) -> float:
    """Position return on ``session_date`` between its execution points."""
    if levels is None or session_date not in levels.open_close:
        return 0.0  # not trading that day: contributes 0
    open_level, close_level = levels.open_close[session_date]
    if session_date == segment.eff_start:
        base = open_level if segment.start_point == POINT_OPEN else close_level
    else:
        idx = bisect.bisect_left(levels.dates, session_date)
        if idx == 0:
            return 0.0
        base = levels.open_close[levels.dates[idx - 1]][1]
    if session_date == segment.eff_end:
        if segment.eff_end_point == POINT_OPEN:
            disposition = open_level
        else:
            disposition = close_level * (1.0 + segment.terminal_return)
    else:
        disposition = close_level
    return disposition / base - 1.0


def _bench_return(
    segment: _Segment,
    session_date: date,
    benchmark_levels_map: dict[str, _Levels],
    methodology: Methodology,
) -> float:
    """Benchmark return over the same points as the stock leg."""
    benchmark_id = methodology.benchmark_id if segment.beta_adjusted else segment.benchmark_id
    levels = benchmark_levels_map.get(benchmark_id) if benchmark_id is not None else None
    label = f"{segment.interval_id} on {session_date}"
    if levels is None or session_date not in levels.open_close:
        raise MissingBenchmarkData(f"No benchmark level for {label} (benchmark {benchmark_id})")
    open_level, close_level = levels.open_close[session_date]
    if session_date == segment.eff_start:
        base = open_level if segment.start_point == POINT_OPEN else close_level
    else:
        idx = bisect.bisect_left(levels.dates, session_date)
        if idx == 0:
            raise MissingBenchmarkData(f"No benchmark session before {session_date} for {label}")
        base = levels.open_close[levels.dates[idx - 1]][1]
    if session_date == segment.eff_end:
        disposition = open_level if segment.eff_end_point == POINT_OPEN else close_level
    else:
        disposition = close_level
    bench_return = disposition / base - 1.0
    if segment.beta_adjusted:
        assert segment.beta is not None
        bench_return *= segment.beta
    return bench_return


def _prepare(
    intervals: pl.DataFrame,
    exposures: pl.DataFrame,
    security_daily: pl.DataFrame,
    delistings: pl.DataFrame,
    classifications: pl.DataFrame,
    sessions: pl.DataFrame,
    methodology: Methodology | None,
    beta: pl.DataFrame | None,
) -> tuple[
    dict[str, list[_Segment]],
    dict[str, tuple[date, ...]],
    dict[str, _Levels] | None,
]:
    """Shared plumbing: segments per analyst and per-security session calendars."""
    by_analyst = _build_segments(
        intervals, exposures, delistings, methodology, classifications, beta
    )
    if not by_analyst:
        return {}, {}, None
    needed = {s.security_id for segments in by_analyst.values() for s in segments}
    calendars = _security_calendars(
        needed, classifications, _exchange_calendars(sessions), _daily_dates(security_daily)
    )
    levels = None
    if methodology is not None:
        basis = "px" if methodology.return_basis == ReturnBasis.PRICE else "tr"
        levels = _levels(
            execution_levels(security_daily), "security_id", f"open_{basis}", f"close_{basis}"
        )
    return by_analyst, calendars, levels


def simulate_portfolios(
    intervals: pl.DataFrame,
    exposures: pl.DataFrame,
    security_daily: pl.DataFrame,
    benchmark_daily: pl.DataFrame,
    beta: pl.DataFrame,
    delistings: pl.DataFrame,
    classifications: pl.DataFrame,
    sessions: pl.DataFrame,
    methodology: Methodology,
    context: RunContext,
) -> pl.DataFrame:
    """Daily NAV, hedge NAV, active return, turnover, cost and drawdown per analyst.

    Returns a ``PORTFOLIO_NAV_FRAME`` sorted by ``(analyst_id, session_date)``.
    """
    by_analyst, calendars, security_levels = _prepare(
        intervals,
        exposures,
        security_daily,
        delistings,
        classifications,
        sessions,
        methodology,
        beta,
    )
    if not by_analyst:
        return pl.DataFrame(schema=_NAV_SCHEMA)

    bench_levels = _levels(benchmark_levels(benchmark_daily), "benchmark_id", "open_tr", "close_tr")
    cost_rate = methodology.transaction_cost_bps / _COST_RATE_DIVISOR

    rows: list[dict[str, object]] = []
    for analyst_id in sorted(by_analyst):
        row_dates, weights, segment_at = _daily_weights(
            by_analyst[analyst_id], calendars, context.as_of_date
        )
        nav = 1.0
        benchmark_nav = 1.0
        peak = 1.0
        previous: dict[str, float] = {}
        for session_date in row_dates:
            day_weights = weights.get(session_date, {})
            excess = 0.0
            hedge = 0.0
            for security_id, weight in day_weights.items():
                if weight == 0.0:
                    continue
                segment = segment_at[(session_date, security_id)]
                stock = _stock_return(
                    segment, session_date, (security_levels or {}).get(security_id)
                )
                bench = _bench_return(segment, session_date, bench_levels, methodology)
                excess += weight * (stock - bench)
                hedge += weight * bench
            keys = set(day_weights) | set(previous)
            turnover = sum(abs(day_weights.get(key, 0.0) - previous.get(key, 0.0)) for key in keys)
            cost = turnover * cost_rate
            active_return = excess - cost
            nav *= 1.0 + active_return
            benchmark_nav *= 1.0 + hedge
            peak = max(peak, nav)
            rows.append(
                {
                    "analyst_id": analyst_id,
                    "session_date": session_date,
                    "nav": nav,
                    "benchmark_nav": benchmark_nav,
                    "active_return": active_return,
                    "gross_exposure": sum(abs(w) for w in day_weights.values()),
                    "turnover": turnover,
                    "cost": cost,
                    "drawdown": nav / peak - 1.0,
                }
            )
            previous = day_weights
    return pl.DataFrame(rows, schema=_NAV_SCHEMA).sort("analyst_id", "session_date")


def portfolio_positions(
    intervals: pl.DataFrame,
    exposures: pl.DataFrame,
    security_daily: pl.DataFrame,
    delistings: pl.DataFrame,
    classifications: pl.DataFrame,
    sessions: pl.DataFrame,
    context: RunContext,
) -> pl.DataFrame:
    """Scaled target weights per ``(analyst_id, session_date, security_id, interval_id)``.

    Same weight timeline ``simulate_portfolios`` uses, restricted to sessions
    where a segment is active. Sorted by ``(analyst_id, session_date,
    security_id, interval_id)``.
    """
    by_analyst, calendars, _ = _prepare(
        intervals,
        exposures,
        security_daily,
        delistings,
        classifications,
        sessions,
        None,
        None,
    )
    if not by_analyst:
        return pl.DataFrame(schema=_POSITIONS_SCHEMA)

    rows: list[dict[str, object]] = []
    for analyst_id in sorted(by_analyst):
        row_dates, weights, segment_at = _daily_weights(
            by_analyst[analyst_id], calendars, context.as_of_date
        )
        for session_date in row_dates:
            for security_id in sorted(weights.get(session_date, {})):
                segment = segment_at[(session_date, security_id)]
                rows.append(
                    {
                        "analyst_id": analyst_id,
                        "session_date": session_date,
                        "security_id": security_id,
                        "interval_id": segment.interval_id,
                        "weight": weights[session_date][security_id],
                    }
                )
    return pl.DataFrame(rows, schema=_POSITIONS_SCHEMA).sort(
        "analyst_id", "session_date", "security_id", "interval_id"
    )
