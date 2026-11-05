"""Strategy backtest engine (decision 0003 conventions).

``run_backtest`` turns a frozen :class:`BacktestSpec` plus contract frames into
a daily NAV simulation. Pure function of data: no I/O, no clock, no store
dependency. All signal, weight and constituent decisions at time ``t`` use
only data knowable at ``t`` (``knowledge_ts <=`` the decision timestamp,
classification spells covering the decision date, ``security_beta`` with
``as_of_date`` strictly before it, trailing windows ending at it).

Conventions implemented here:

* Decisions. ``rebalance='event_driven'`` decides once per global-calendar
  session: knowledge cutoff = that session's ``open_ts`` and execution at the
  same session's open — exactly "next open after knowledge_ts" for every
  signal, because any event's first eligible open is some session's open.
  ``weekly``/``monthly`` decide at the close of the last global session of
  each ISO week / calendar month; execution is the first open strictly after
  that close on each security's own exchange calendar.
* Weight timeline. Each decision produces a complete target-weight map
  (securities absent from it are flat). Per security, weight changes execute
  at the security's next own session with price data; several decisions can
  collapse onto one execution session (latest decision wins). Between
  execution sessions the target weight is constant — the same
  "target weights, never price drift" convention as
  ``portfolio.simulate_portfolios``.
* Daily P&L. On security ``i``'s own session ``t`` with previous own session
  ``p``: an unchanged weight earns the exact point-to-point return
  ``w * (disp(t)/close_tr(p) - 1)``; on an execution session (weight changes
  at the open) the day splits into an overnight leg under the old weight
  ``w_on * (open_tr(t)/close_tr(p) - 1)`` plus an intraday leg under the new
  weight ``w_in * (disp(t)/open_tr(t) - 1)`` — the same segment convention
  as ``portfolio.py``.
  ``disp(t) = close_tr(t)`` normally and ``close_tr(t) * (1 + final_return)``
  on the delisting date when the position is still held (an exit executing
  at the delisting open earns only the overnight leg — same rule as
  ``portfolio.py``). On a security's first session the overnight leg is 0.
  Union-calendar days where the security does not trade contribute 0.
* Costs. ``turnover(t) = sum_i |w_i(t) - w_i(prev own session)|`` over
  execution events only; ``cost(t) = turnover(t) * (transaction_cost_bps +
  slippage_bps) / 10_000`` is deducted from the day's return. ``nav`` and
  ``benchmark_nav`` compound from 1.0; ``drawdown = nav / running_max - 1``;
  ``active_return(t)`` is the portfolio daily return minus the benchmark
  daily return (total-return basis, not hedged).
* Benchmark leg. ``benchmark_nav`` compounds the chosen
  ``spec.benchmark_id``'s ``close_level`` returns on its own session dates
  (first session measures ``close/open - 1``); union days without a
  benchmark session accrue 0.
* Signals. The latest knowable rating per ``(analyst, security)`` at the
  decision timestamp drives ``position_weights.for_rating`` scores; DROP
  actions and non-investable canonical ratings contribute nothing. Event
  supersession is resolved per decision cutoff exactly like
  ``point_in_time.visible_events``: an event is superseded when a correction
  with ``knowledge_ts <= cutoff`` names it (one hop; intermediate
  corrections still count).
* ``selection`` chooses the analyst cohort: ``all``, ``analyst_ids``
  filter, ``peer_group`` (via ``peers.assign_peer_groups`` — requires
  ``coverage_spells``), or dynamic ``top_n``/``top_quartile`` ranked on
  ``score_lookback_sessions`` trailing global sessions only (never
  full-period ranks). ``score_field``: ``position_weight`` (default),
  ``rating_ordinal`` (strong_buy=5 … strong_sell=1), or ``trailing_return``
  (mean TR return of the analyst's in-window events' securities over the
  window).
* Sizing. Per-security score = sum of selected analysts' position weights.
  ``sizing='equal'`` gives every nonzero-score name an equal share;
  ``conviction`` weights by |score|; ``inverse_vol`` weights by
  1/stdev of daily TR returns over ``vol_lookback_sessions`` own sessions
  (names without enough history get the cross-sectional median vol).
  Caps applied in order: sign filter by ``mode``, ``max_position_weight``,
  ``sector_caps`` (sector spell at the decision date), ``liquidity_cap``
  (``weight <= liquidity_cap * adv_20d`` — ``adv_20d`` and NAV share the
  same currency unit by convention since NAV starts at 1.0), then a final
  ``gross_exposure_cap`` rescale. Modes: ``long_only`` drops negative
  weights; ``long_short`` keeps signs; ``market_neutral`` scales each side
  to ``gross/2``; ``beta_neutral`` additionally solves the short-side scale
  ``k = -sum_long(w*beta)/sum_short(w*beta)`` for portfolio beta ~ 0 using
  PIT ``security_beta`` (``as_of_date`` strictly before the decision);
  candidates lacking beta data are excluded in beta modes.
* Shorts. ``allow_shorting=False`` with a short-requiring mode raises
  ``ValueError``; otherwise every security is assumed shortable
  (synthetic convention — documented, no borrow data exists in contracts).
* Feasibility. Spec-level violations (missing lookback, missing peer group
  or coverage spells, liquidity cap without ``liquidity_daily``, beta mode
  without ``security_beta`` data, beta-neutralization impossible) raise
  ``ValueError``. A degenerate signal set (all scores zero) is *not* an
  error: the book goes flat — unallocated weight is simply cash earning 0.
* ``horizon='fixed_days'``: a position is force-closed once it has been
  held ``horizon_days`` global sessions and cannot re-enter until
  ``horizon_days`` further sessions have passed (cooldown prevents
  enter/exit churn on an unchanged signal).

Output frames (no contract exists for backtests; schemas are defined here
and documented):

* ``nav_daily`` — ``(session_date, nav, benchmark_nav, active_return,
  gross_exposure, turnover, cost, drawdown)``: the ``portfolio_nav`` shape
  minus ``analyst_id`` (a strategy is not an analyst), matching the store's
  ``backtest_nav`` table.
* ``trades`` — ``(trade_seq, session_date, security_id, side, quantity,
  price, notional, fee, realized_pnl)`` mirroring ``backtest_trade``;
  ``price`` is the TR ``open_tr`` level at execution, ``quantity =
  |Δw| * nav_before_day / price``, ``fee = notional * rate``,
  ``realized_pnl`` is null (weight-based accounting does not track lots).
* ``monthly_returns`` — ``(year, month, month_return, nav_end)`` where
  ``month_return`` compounds the daily portfolio returns inside the
  calendar month, so ``prod(1 + month_return) == final nav``.
* ``attribution`` — ``(dimension, group_key, contribution)`` with
  ``dimension`` in ``{sector, analyst, rating_action}`; each security's
  daily gross contribution (``w_on*r_on + w_in*r_in``) is credited to the
  dominant signal's metadata (max |position weight| among selected
  analysts, ties by ``analyst_id``) and the day's cost is allocated
  pro-rata to each security's turnover.
"""

import bisect
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal

import polars as pl
from pydantic import Field, model_validator

from callscore_core.enums import CanonicalRating, PeerDimension, RatingAction
from callscore_core.frames import MARKET_BENCHMARK_ID
from callscore_core.methodology import (
    FrozenModel,
    Identifier,
    Number,
    PositionWeights,
    PositiveInt,
)
from callscore_engine.calendar import (
    ExchangeCalendar,
    build_calendars,
    global_calendar,
)
from callscore_engine.peers import assign_peer_groups
from callscore_engine.point_in_time import with_available_ts
from callscore_engine.pricing import MissingBenchmarkData, execution_levels

SelectionMode = Literal["all", "top_n", "top_quartile", "peer_group"]
ScoreField = Literal["position_weight", "rating_ordinal", "trailing_return"]
SizingMode = Literal["equal", "conviction", "inverse_vol"]
PortfolioMode = Literal["long_only", "long_short", "market_neutral", "beta_neutral"]
RebalanceFreq = Literal["event_driven", "weekly", "monthly"]
HorizonMode = Literal["until_change", "fixed_days"]

_COST_DIVISOR = 10_000.0
_TRADING_DAYS_PER_YEAR = 252.0
_RATING_ORDINAL: dict[str, float] = {
    CanonicalRating.STRONG_BUY.value: 5.0,
    CanonicalRating.BUY.value: 4.0,
    CanonicalRating.HOLD.value: 3.0,
    CanonicalRating.SELL.value: 2.0,
    CanonicalRating.STRONG_SELL.value: 1.0,
}
_BETA_MODES = frozenset({"market_neutral", "beta_neutral"})
_SHORT_MODES = frozenset({"long_short", "market_neutral", "beta_neutral"})

_NAV_SCHEMA: dict[str, pl.DataType] = {
    "session_date": pl.Date(),
    "nav": pl.Float64(),
    "benchmark_nav": pl.Float64(),
    "active_return": pl.Float64(),
    "gross_exposure": pl.Float64(),
    "turnover": pl.Float64(),
    "cost": pl.Float64(),
    "drawdown": pl.Float64(),
}
_TRADES_SCHEMA: dict[str, pl.DataType] = {
    "trade_seq": pl.Int64(),
    "session_date": pl.Date(),
    "security_id": pl.String(),
    "side": pl.String(),
    "quantity": pl.Float64(),
    "price": pl.Float64(),
    "notional": pl.Float64(),
    "fee": pl.Float64(),
    "realized_pnl": pl.Float64(),
}
_MONTHLY_SCHEMA: dict[str, pl.DataType] = {
    "year": pl.Int64(),
    "month": pl.Int64(),
    "month_return": pl.Float64(),
    "nav_end": pl.Float64(),
}
_ATTRIBUTION_SCHEMA: dict[str, pl.DataType] = {
    "dimension": pl.String(),
    "group_key": pl.String(),
    "contribution": pl.Float64(),
}

_REQUIRED_FRAMES = (
    "rating_events",
    "security_daily",
    "security_classifications",
    "sessions",
    "benchmark_daily",
)


class BacktestSpec(FrozenModel):
    """Frozen specification for :func:`run_backtest`."""

    selection: SelectionMode = "all"
    analyst_ids: tuple[Identifier, ...] | None = None
    top_n: PositiveInt | None = None
    """Required when ``selection='top_n'``."""
    score_lookback_sessions: PositiveInt | None = None
    """Required for ``top_n``/``top_quartile``: ranking window in global
    sessions. Ranks are computed on this trailing window only."""
    peer_group: Identifier | None = None
    """Required when ``selection='peer_group'``; must equal the
    ``assign_peer_groups`` key format ``"dim=value|..."``."""
    peer_group_dims: tuple[PeerDimension, ...] = (
        PeerDimension.CAP_BUCKET,
        PeerDimension.REGION,
        PeerDimension.SECTOR,
    )
    score_field: ScoreField = "position_weight"
    sizing: SizingMode = "equal"
    position_weights: PositionWeights = PositionWeights()
    mode: PortfolioMode = "long_only"
    gross_exposure_cap: Annotated[Number, Field(gt=0, le=2)] = 1.0
    max_position_weight: Annotated[Number, Field(gt=0, le=1)] = 1.0
    sector_caps: dict[Identifier, Annotated[Number, Field(gt=0)]] | None = None
    """Max total |weight| per sector_id (classification at decision date)."""
    liquidity_cap: Annotated[Number, Field(ge=0)] | None = None
    """Max weight per name as a fraction of ``adv_20d`` (requires
    ``liquidity_daily``); None disables the cap."""
    allow_shorting: bool = True
    """Synthetic convention: when True (default) every security is assumed
    shortable; when False any short-requiring mode is rejected."""
    vol_lookback_sessions: PositiveInt = 60
    rebalance: RebalanceFreq = "event_driven"
    horizon: HorizonMode = "until_change"
    horizon_days: PositiveInt | None = None
    """Required when ``horizon='fixed_days'``."""
    transaction_cost_bps: Annotated[Number, Field(ge=0, le=10_000)] = 0.0
    slippage_bps: Annotated[Number, Field(ge=0, le=10_000)] | None = None
    """Optional extra cost in bps, added to ``transaction_cost_bps``."""
    benchmark_id: Identifier = MARKET_BENCHMARK_ID
    risk_free_annual: Annotated[Number, Field(ge=0)] = 0.0
    start_date: date | None = None
    """First decision-eligible global session (earlier sessions only warm up
    trailing windows)."""
    end_date: date | None = None
    """Last global session included in the NAV series."""

    @model_validator(mode="after")
    def coherent(self) -> "BacktestSpec":
        if self.selection in ("top_n", "top_quartile") and (self.score_lookback_sessions is None):
            raise ValueError(f"selection={self.selection!r} requires score_lookback_sessions")
        if self.selection == "top_n" and self.top_n is None:
            raise ValueError("selection='top_n' requires top_n")
        if self.selection == "peer_group" and self.peer_group is None:
            raise ValueError("selection='peer_group' requires peer_group")
        if self.horizon == "fixed_days" and self.horizon_days is None:
            raise ValueError("horizon='fixed_days' requires horizon_days")
        if self.mode in _SHORT_MODES and not self.allow_shorting:
            raise ValueError(f"mode={self.mode!r} requires shorting but allow_shorting=False")
        if self.mode == "long_only" and self.gross_exposure_cap > 1.0:
            raise ValueError("long_only mode cannot exceed gross_exposure_cap 1.0")
        return self


@dataclass(frozen=True)
class BacktestStats:
    annualized_return: float | None
    annualized_volatility: float | None
    sharpe_ratio: float | None
    information_ratio: float | None
    max_drawdown: float | None
    turnover: float | None
    """Mean daily one-sided turnover ``sum_i |Δw_i|`` (not annualized)."""
    hit_rate: float | None
    """Fraction of sessions with positive portfolio daily return."""
    n_sessions: int


@dataclass(frozen=True)
class BacktestResult:
    nav_daily: pl.DataFrame
    trades: pl.DataFrame
    monthly_returns: pl.DataFrame
    attribution: pl.DataFrame
    stats: BacktestStats


@dataclass(frozen=True)
class _SecLevels:
    """Sorted own-calendar TR levels for one security."""

    dates: tuple[date, ...]
    open_tr: dict[date, float]
    close_tr: dict[date, float]


@dataclass(frozen=True)
class _WeightEvent:
    """One target-weight change executing at a security's session open."""

    session: date
    weight: float
    seq: int
    analyst_id: str | None = None
    action_type: str | None = None
    sector_id: str | None = None


@dataclass(frozen=True)
class _Spell:
    from_date: date
    to_date: date | None
    exchange_id: str
    sector_id: str
    cap_bucket: str
    region: str
    delisting_date: date | None


@dataclass(frozen=True)
class _Decision:
    """One rebalance decision point."""

    ts: datetime
    day: date
    idx: int
    """Index into the full (unclipped) global calendar — for horizon ages."""
    period_end: bool
    """True for weekly/monthly (execute next open after close); False for
    event-driven (execute at this session's open)."""


@dataclass(frozen=True)
class _SignalMeta:
    analyst_id: str | None
    action_type: str | None
    sector_id: str | None


@dataclass
class _Context:
    """Precomputed lookups shared by all decisions and the daily loop."""

    spec: BacktestSpec
    calendars: dict[str, ExchangeCalendar]
    global_cal: ExchangeCalendar
    all_dates: list[date]
    decision_dates: list[date]
    date_idx: dict[date, int]
    sec_levels: dict[str, _SecLevels]
    classifications: pl.DataFrame
    spells: dict[str, list[_Spell]]
    coverage_spells: pl.DataFrame | None
    delistings: dict[str, tuple[date, float]]
    betas: dict[str, tuple[tuple[date, ...], tuple[float, ...]]]
    liquidity: dict[str, list[tuple[date, float | None]]]
    pair_events: dict[tuple[str, str], list[dict[str, Any]]]
    analyst_pairs: dict[str, list[tuple[str, str]]]
    corr_knowledge: dict[str, list[datetime]]
    benchmark_ret: dict[date, float]
    peer_groups_cache: dict[date, dict[str, str | None]]


def _group_spells(classifications: pl.DataFrame) -> dict[str, list[_Spell]]:
    spells: dict[str, list[_Spell]] = {}
    for row in classifications.iter_rows(named=True):
        spells.setdefault(str(row["security_id"]), []).append(
            _Spell(
                from_date=row["from_date"],
                to_date=row["to_date"],
                exchange_id=str(row["exchange_id"]),
                sector_id=str(row["sector_id"]),
                cap_bucket=str(row["cap_bucket"]),
                region=str(row["region"]),
                delisting_date=row["delisting_date"],
            )
        )
    for values in spells.values():
        values.sort(key=lambda s: s.from_date)
    return spells


def _pick_spell(spells: list[_Spell], day: date) -> _Spell | None:
    covering = [s for s in spells if s.from_date <= day and (s.to_date is None or s.to_date >= day)]
    if not covering:
        return None
    return max(covering, key=lambda s: s.from_date)


def _sec_levels(security_daily: pl.DataFrame) -> dict[str, _SecLevels]:
    levels = execution_levels(security_daily)
    grouped: dict[str, dict[date, tuple[float, float]]] = {}
    for security_id, session_date, open_tr, close_tr in levels.select(
        "security_id", "session_date", "open_tr", "close_tr"
    ).iter_rows():
        grouped.setdefault(str(security_id), {})[session_date] = (
            float(open_tr),
            float(close_tr),
        )
    return {
        security_id: _SecLevels(
            dates=tuple(sorted(points)),
            open_tr={d: v[0] for d, v in points.items()},
            close_tr={d: v[1] for d, v in points.items()},
        )
        for security_id, points in grouped.items()
    }


def _benchmark_returns(
    benchmark_daily: pl.DataFrame, benchmark_id: str
) -> tuple[dict[date, float], dict[date, float]]:
    """``(close_level, daily_return)`` maps for one benchmark id."""
    sub = benchmark_daily.filter(pl.col("benchmark_id") == benchmark_id).sort("session_date")
    if sub.height == 0:
        raise MissingBenchmarkData(f"benchmark_daily has no rows for {benchmark_id!r}")
    closes: dict[date, float] = {}
    rets: dict[date, float] = {}
    prev_close: float | None = None
    for session_date, open_level, close_level in sub.select(
        "session_date", "open_level", "close_level"
    ).iter_rows():
        day = session_date
        close = float(close_level)
        closes[day] = close
        if prev_close is None:
            rets[day] = close / float(open_level) - 1.0
        else:
            rets[day] = close / prev_close - 1.0
        prev_close = close
    return closes, rets


def _build_context(spec: BacktestSpec, frames: Mapping[str, pl.DataFrame]) -> _Context:
    missing = [name for name in _REQUIRED_FRAMES if name not in frames]
    if missing:
        raise ValueError(f"run_backtest missing required frames: {sorted(missing)}")
    sessions = frames["sessions"]
    if sessions.height == 0:
        raise ValueError("sessions frame is empty")
    if spec.selection == "peer_group" and "coverage_spells" not in frames:
        raise ValueError("selection='peer_group' requires a coverage_spells frame")
    if spec.liquidity_cap is not None and "liquidity_daily" not in frames:
        raise ValueError("liquidity_cap requires a liquidity_daily frame")
    if spec.mode == "beta_neutral" and (
        "security_beta" not in frames or frames["security_beta"].height == 0
    ):
        raise ValueError("mode='beta_neutral' requires a non-empty security_beta frame")

    calendars = build_calendars(sessions)
    global_cal = global_calendar(sessions)
    all_dates = list(global_cal.session_dates)
    date_idx = {d: i for i, d in enumerate(all_dates)}

    decision_dates = [d for d in all_dates]
    if spec.start_date is not None:
        decision_dates = [d for d in decision_dates if d >= spec.start_date]
    if spec.end_date is not None:
        decision_dates = [d for d in decision_dates if d <= spec.end_date]

    _closes, benchmark_ret = _benchmark_returns(frames["benchmark_daily"], spec.benchmark_id)

    delistings: dict[str, tuple[date, float]] = {}
    if "delistings" in frames and frames["delistings"].height:
        for security_id, delist_date, final_return in (
            frames["delistings"].select("security_id", "delisting_date", "final_return").iter_rows()
        ):
            delistings[str(security_id)] = (delist_date, float(final_return))

    betas: dict[str, tuple[tuple[date, ...], tuple[float, ...]]] = {}
    if "security_beta" in frames and frames["security_beta"].height:
        beta_rows: dict[str, list[tuple[date, float]]] = defaultdict(list)
        for security_id, as_of, value in (
            frames["security_beta"].select("security_id", "as_of_date", "beta").iter_rows()
        ):
            beta_rows[str(security_id)].append((as_of, float(value)))
        for security_id, values in beta_rows.items():
            values.sort()
            betas[security_id] = (
                tuple(d for d, _ in values),
                tuple(b for _, b in values),
            )

    liquidity: dict[str, list[tuple[date, float | None]]] = defaultdict(list)
    if "liquidity_daily" in frames and frames["liquidity_daily"].height:
        for security_id, session_date, adv in (
            frames["liquidity_daily"].select("security_id", "session_date", "adv_20d").iter_rows()
        ):
            liquidity[str(security_id)].append((session_date, None if adv is None else float(adv)))
    for liq_rows in liquidity.values():
        liq_rows.sort()

    # All events with available_ts; supersession resolved per decision cutoff.
    events = with_available_ts(frames["rating_events"])
    pair_events: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    corr_knowledge: dict[str, list[datetime]] = defaultdict(list)
    for row in events.iter_rows(named=True):
        if row["is_correction"] and row["supersedes_event_id"] is not None:
            corr_knowledge[str(row["supersedes_event_id"])].append(row["knowledge_ts"])
        pair_events[(str(row["analyst_id"]), str(row["security_id"]))].append(row)
    for rows in pair_events.values():
        rows.sort(
            key=lambda r: (
                r["available_ts"],
                r["announced_ts"],
                str(r["event_id"]),
            )
        )
    for knowledge in corr_knowledge.values():
        knowledge.sort()

    coverage = frames.get("coverage_spells")
    return _Context(
        spec=spec,
        calendars=calendars,
        global_cal=global_cal,
        all_dates=all_dates,
        decision_dates=decision_dates,
        date_idx=date_idx,
        sec_levels=_sec_levels(frames["security_daily"]),
        classifications=frames["security_classifications"],
        spells=_group_spells(frames["security_classifications"]),
        coverage_spells=coverage,
        delistings=delistings,
        betas=betas,
        liquidity=dict(liquidity),
        pair_events=dict(pair_events),
        analyst_pairs=_analyst_pairs(pair_events),
        corr_knowledge=dict(corr_knowledge),
        benchmark_ret=benchmark_ret,
        peer_groups_cache={},
    )


def _analyst_pairs(
    pair_events: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, list[tuple[str, str]]]:
    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for analyst_id, security_id in pair_events:
        grouped[analyst_id].append((analyst_id, security_id))
    return dict(grouped)


def _state_at(
    ctx: _Context,
    analyst_id: str,
    security_id: str,
    cutoff_ts: datetime,
) -> tuple[str, str] | None:
    """Latest knowable ``(canonical_rating, action_type)`` at ``cutoff_ts``.

    An event is eligible when ``available_ts <= cutoff_ts`` (which implies
    ``knowledge_ts <= cutoff_ts``) and it is not superseded by a correction
    whose ``knowledge_ts <= cutoff_ts``. A DROP action clears the state.
    """
    rows = ctx.pair_events.get((analyst_id, security_id))
    if not rows:
        return None
    avail = [r["available_ts"] for r in rows]
    pos = bisect.bisect_right(avail, cutoff_ts)
    corrections = ctx.corr_knowledge
    for idx in range(pos - 1, -1, -1):
        row = rows[idx]
        event_id = str(row["event_id"])
        superseded = any(k <= cutoff_ts for k in corrections.get(event_id, ()))
        if superseded:
            continue
        action = str(row["action_type"])
        if action == RatingAction.DROP.value:
            return None
        return str(row["canonical_rating"]), action
    return None


def _window_sessions(ctx: _Context, decision: _Decision) -> tuple[list[date], date | None]:
    """Last ``score_lookback_sessions`` global dates <= decision day, plus the
    reference date just before the window (for trailing returns)."""
    n = ctx.spec.score_lookback_sessions
    assert n is not None
    pos = bisect.bisect_right(ctx.all_dates, decision.day)
    if pos < n:
        return [], None
    window = ctx.all_dates[pos - n : pos]
    ref = ctx.all_dates[pos - n - 1] if pos - n - 1 >= 0 else None
    return window, ref


def _trailing_return(
    ctx: _Context, security_id: str, ref_date: date | None, end_date: date
) -> float | None:
    """TR return of ``security_id`` from its last close <= ref_date to its
    last close <= end_date; None when either side is missing."""
    levels = ctx.sec_levels.get(security_id)
    if levels is None or not levels.dates or ref_date is None:
        return None
    end_idx = bisect.bisect_right(levels.dates, end_date) - 1
    ref_idx = bisect.bisect_right(levels.dates, ref_date) - 1
    if end_idx < 0 or ref_idx < 0 or end_idx <= ref_idx:
        return None
    base = levels.close_tr[levels.dates[ref_idx]]
    if base <= 0:
        return None
    return levels.close_tr[levels.dates[end_idx]] / base - 1.0


def _analyst_pool(ctx: _Context) -> list[str]:
    pool = {analyst for analyst, _sec in ctx.pair_events}
    if ctx.spec.analyst_ids is not None:
        allowed = set(ctx.spec.analyst_ids)
        pool &= allowed
    return sorted(pool)


def _peer_groups(ctx: _Context, day: date) -> dict[str, str | None]:
    cached = ctx.peer_groups_cache.get(day)
    if cached is not None:
        return cached
    assert ctx.coverage_spells is not None
    result = assign_peer_groups(
        ctx.coverage_spells, ctx.classifications, day, ctx.spec.peer_group_dims
    )
    mapping = {
        str(a): (None if g is None else str(g))
        for a, g in result.select("analyst_id", "peer_group").iter_rows()
    }
    ctx.peer_groups_cache[day] = mapping
    return mapping


def _analyst_score(
    ctx: _Context,
    analyst_id: str,
    decision: _Decision,
    window: list[date],
    ref_date: date | None,
) -> float | None:
    """Trailing-window score for one analyst; None when no qualifying events."""
    spec = ctx.spec
    window_open = ctx.global_cal.open_ts_for(window[0]) if window else None
    if window_open is None:
        return None
    scores: list[float] = []
    for analyst, security_id in ctx.analyst_pairs.get(analyst_id, []):
        for row in ctx.pair_events[(analyst, security_id)]:
            available = row["available_ts"]
            if not (window_open <= available <= decision.ts):
                continue
            if spec.score_field == "position_weight":
                try:
                    rating = CanonicalRating(str(row["canonical_rating"]))
                except ValueError:
                    continue
                value = spec.position_weights.for_rating(rating)
                if value is None:
                    continue
                scores.append(value)
            elif spec.score_field == "rating_ordinal":
                value = _RATING_ORDINAL.get(str(row["canonical_rating"]))
                if value is None:
                    continue
                scores.append(value)
            else:  # trailing_return
                value = _trailing_return(ctx, security_id, ref_date, decision.day)
                if value is None:
                    continue
                scores.append(value)
    if not scores:
        return None
    return sum(scores) / len(scores)


def _select_analysts(ctx: _Context, decision: _Decision) -> list[str]:
    spec = ctx.spec
    pool = _analyst_pool(ctx)
    if spec.selection == "all":
        return pool
    if spec.selection == "peer_group":
        groups = _peer_groups(ctx, decision.day)
        return sorted(a for a in pool if groups.get(a) == spec.peer_group)
    # top_n / top_quartile: rank on the trailing window only.
    window, ref_date = _window_sessions(ctx, decision)
    if not window:
        return []
    ranked: list[tuple[float, str]] = []
    for analyst_id in pool:
        score = _analyst_score(ctx, analyst_id, decision, window, ref_date)
        if score is not None:
            ranked.append((score, analyst_id))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if spec.selection == "top_n":
        assert spec.top_n is not None
        return sorted(a for _s, a in ranked[: spec.top_n])
    k = max(1, math.ceil(len(ranked) / 4))
    return sorted(a for _s, a in ranked[:k])


def _beta_at(ctx: _Context, security_id: str, day: date) -> float | None:
    entry = ctx.betas.get(security_id)
    if entry is None:
        return None
    dates, values = entry
    idx = bisect.bisect_left(dates, day) - 1
    return values[idx] if idx >= 0 else None


def _volatility(ctx: _Context, security_id: str, day: date) -> float | None:
    """Sample stdev of daily close-to-close TR returns over the trailing
    ``vol_lookback_sessions`` own sessions ending on/before ``day``."""
    levels = ctx.sec_levels.get(security_id)
    if levels is None:
        return None
    n = ctx.spec.vol_lookback_sessions
    end_idx = bisect.bisect_right(levels.dates, day)
    if end_idx < 2:
        return None
    start_idx = max(0, end_idx - n - 1)
    returns: list[float] = []
    for i in range(start_idx + 1, end_idx):
        prev = levels.close_tr[levels.dates[i - 1]]
        cur = levels.close_tr[levels.dates[i]]
        if prev > 0:
            returns.append(cur / prev - 1.0)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var)


def _raw_scores(
    ctx: _Context, decision: _Decision, analysts: list[str]
) -> dict[str, tuple[float, str, str]]:
    """``security_id -> (score, dominant_analyst, action_type)`` at decision."""
    spec = ctx.spec
    totals: dict[str, float] = defaultdict(float)
    dominant: dict[str, tuple[float, str, str]] = {}
    for analyst_id in analysts:
        for _analyst, security_id in ctx.analyst_pairs.get(analyst_id, []):
            state = _state_at(ctx, analyst_id, security_id, decision.ts)
            if state is None:
                continue
            canonical, action = state
            if action == RatingAction.DROP.value:
                continue
            try:
                rating = CanonicalRating(canonical)
            except ValueError:
                continue
            weight = spec.position_weights.for_rating(rating)
            if weight is None:
                continue
            totals[security_id] += weight
            current = dominant.get(security_id)
            if (
                current is None
                or abs(weight) > abs(current[0])
                or (abs(weight) == abs(current[0]) and analyst_id < current[1])
            ):
                dominant[security_id] = (weight, analyst_id, action)
    return {
        security_id: (totals[security_id], meta[1], meta[2])
        for security_id, meta in dominant.items()
        if totals[security_id] != 0.0
    }


def _sector_at(ctx: _Context, security_id: str, day: date) -> str | None:
    spells = ctx.spells.get(security_id)
    if not spells:
        return None
    spell = _pick_spell(spells, day)
    return spell.sector_id if spell is not None else None


def _size_weights(
    ctx: _Context,
    scores: dict[str, tuple[float, str, str]],
    decision: _Decision,
) -> dict[str, tuple[float, _SignalMeta]]:
    """Turn raw scores into capped target weights for one decision."""
    spec = ctx.spec
    mode = spec.mode
    day = decision.day

    # Sign filter + beta-data requirement.
    candidates: dict[str, float] = {}
    metas: dict[str, _SignalMeta] = {}
    for security_id, (score, analyst_id, action) in scores.items():
        if mode == "long_only" and score <= 0:
            continue
        if mode in _BETA_MODES and _beta_at(ctx, security_id, day) is None:
            continue  # documented: beta modes exclude names without beta
        candidates[security_id] = score
        sector = _sector_at(ctx, security_id, day)
        metas[security_id] = _SignalMeta(analyst_id, action, sector)
    if mode == "beta_neutral" and scores and not candidates:
        raise ValueError("mode='beta_neutral': every candidate security lacks PIT security_beta")
    if not candidates:
        return {}

    # Base weights.
    n = len(candidates)
    raw: dict[str, float] = {}
    if spec.sizing == "equal":
        for security_id, score in candidates.items():
            raw[security_id] = (1.0 if score > 0 else -1.0) / n
    elif spec.sizing == "conviction":
        total = sum(abs(s) for s in candidates.values())
        if total <= 0:
            return {}
        for security_id, score in candidates.items():
            raw[security_id] = score / total
    else:  # inverse_vol
        inv: dict[str, float] = {}
        vols: list[float] = []
        for security_id in candidates:
            vol = _volatility(ctx, security_id, day)
            if vol is not None and vol > 0:
                vols.append(vol)
        median_vol = sorted(vols)[len(vols) // 2] if vols else None
        for security_id, score in candidates.items():
            vol = _volatility(ctx, security_id, day)
            if vol is None or vol <= 0:
                vol = median_vol
            if vol is None or vol <= 0:
                continue
            inv[security_id] = (1.0 if score > 0 else -1.0) / vol
        if not inv:
            return {}
        total = sum(inv.values())
        raw = {k: v / total for k, v in inv.items()}

    # Normalize to unit gross, then apply caps.
    gross = sum(abs(w) for w in raw.values())
    if gross <= 0:
        return {}
    weights = {k: v / gross for k, v in raw.items()}

    for _ in range(4):
        changed = False
        # max_position_weight
        for security_id in list(weights):
            if abs(weights[security_id]) > spec.max_position_weight:
                weights[security_id] = math.copysign(spec.max_position_weight, weights[security_id])
                changed = True
        # sector caps
        if spec.sector_caps:
            by_sector: dict[str | None, list[str]] = defaultdict(list)
            for security_id in weights:
                by_sector[metas[security_id].sector_id].append(security_id)
            for sector, members in by_sector.items():
                cap = spec.sector_caps.get(sector or "")
                if cap is None:
                    continue
                total = sum(abs(weights[m]) for m in members)
                if total > cap and total > 0:
                    scale = cap / total
                    for m in members:
                        weights[m] *= scale
                    changed = True
        # liquidity cap
        if spec.liquidity_cap is not None:
            for security_id in list(weights):
                adv = _liquidity_at(ctx.liquidity.get(security_id) or [], day)
                cap = spec.liquidity_cap * (adv or 0.0)
                if abs(weights[security_id]) > cap:
                    weights[security_id] = math.copysign(cap, weights[security_id])
                    changed = True
        if not changed:
            break

    # Mode transforms.
    if mode == "market_neutral" or mode == "beta_neutral":
        long_gross = sum(w for w in weights.values() if w > 0)
        short_gross = -sum(w for w in weights.values() if w < 0)
        target = spec.gross_exposure_cap / 2.0
        if long_gross > 0 and short_gross > 0:
            for security_id in list(weights):
                if weights[security_id] > 0:
                    weights[security_id] *= target / long_gross
                else:
                    weights[security_id] *= target / short_gross
        else:
            # One-sided book: cannot neutralize; scale to full gross cap.
            total = sum(abs(w) for w in weights.values())
            if total > 0:
                scale = spec.gross_exposure_cap / total
                weights = {k: v * scale for k, v in weights.items()}
    elif mode == "long_short":
        total = sum(abs(w) for w in weights.values())
        if total > spec.gross_exposure_cap and total > 0:
            scale = spec.gross_exposure_cap / total
            weights = {k: v * scale for k, v in weights.items()}
    else:  # long_only
        total = sum(weights.values())
        if total > spec.gross_exposure_cap and total > 0:
            scale = spec.gross_exposure_cap / total
            weights = {k: v * scale for k, v in weights.items()}

    if mode == "beta_neutral":
        long_beta = sum(w * (_beta_at(ctx, s, day) or 0.0) for s, w in weights.items() if w > 0)
        short_beta = sum(w * (_beta_at(ctx, s, day) or 0.0) for s, w in weights.items() if w < 0)
        if short_beta == 0.0:
            if long_beta != 0.0:
                raise ValueError(
                    "mode='beta_neutral': short side has zero aggregate beta; cannot neutralize"
                )
        else:
            k = -long_beta / short_beta
            if not math.isfinite(k) or k <= 0:
                raise ValueError("mode='beta_neutral': beta-neutralizing scale is not positive")
            weights = {s: (w * k if w < 0 else w) for s, w in weights.items()}
            total = sum(abs(w) for w in weights.values())
            if total > spec.gross_exposure_cap and total > 0:
                scale = spec.gross_exposure_cap / total
                weights = {s: v * scale for s, v in weights.items()}

    return {
        security_id: (weights[security_id], metas[security_id])
        for security_id in weights
        if abs(weights[security_id]) > 1e-12
    }


def _exec_session(ctx: _Context, security_id: str, decision: _Decision) -> date | None:
    """First own session with price data where the decision's weight applies."""
    levels = ctx.sec_levels.get(security_id)
    if levels is None or not levels.dates:
        return None
    if not decision.period_end:
        # event_driven: execute at the decision session's open -> first own
        # session on/after the decision date.
        idx = bisect.bisect_left(levels.dates, decision.day)
    else:
        # weekly/monthly: first open strictly after the period-end close on
        # the security's exchange calendar, then shifted to a trading day.
        spell = _pick_spell(ctx.spells.get(security_id, []), decision.day)
        exchange_id = spell.exchange_id if spell is not None else "*"
        cal = ctx.calendars.get(exchange_id, ctx.global_cal)
        point = cal.first_open_after(decision.ts)
        if point is None:
            return None
        idx = bisect.bisect_left(levels.dates, point.session_date)
    if idx >= len(levels.dates):
        return None
    return levels.dates[idx]


def _decision_points(ctx: _Context) -> list[_Decision]:
    spec = ctx.spec
    dates = ctx.decision_dates
    if not dates:
        return []
    open_ts = {d: ctx.global_cal.open_ts_for(d) for d in ctx.all_dates}
    close_ts = {d: ctx.global_cal.close_ts_for(d) for d in ctx.all_dates}
    points: list[_Decision] = []
    if spec.rebalance == "event_driven":
        for day in dates:
            ts = open_ts.get(day)
            if ts is None:
                continue
            points.append(_Decision(ts=ts, day=day, idx=ctx.date_idx[day], period_end=False))
        return points

    # weekly / monthly: last global session of each ISO week / calendar month.
    freq = spec.rebalance
    last_of_period: dict[tuple[int, ...], date] = {}
    order: list[tuple[int, ...]] = []
    for day in ctx.all_dates:
        if freq == "weekly":
            iso = day.isocalendar()
            key: tuple[int, ...] = (iso.year, iso.week)
        else:
            key = (day.year, day.month)
        if key not in last_of_period:
            order.append(key)
        last_of_period[key] = day
    for key in order:
        day = last_of_period[key]
        if day not in dates:
            continue
        ts = close_ts.get(day)
        if ts is None:
            continue
        points.append(_Decision(ts=ts, day=day, idx=ctx.date_idx[day], period_end=True))
    return points


def _weight_at(
    events: list[_WeightEvent], sessions: list[date], day: date
) -> tuple[float, _SignalMeta | None]:
    """Weight and metadata of the latest event on/before ``day``."""
    idx = bisect.bisect_right(sessions, day) - 1
    if idx < 0:
        return 0.0, None
    event = events[idx]
    meta = _SignalMeta(event.analyst_id, event.action_type, event.sector_id)
    return event.weight, meta


def _run_decisions(
    ctx: _Context,
) -> tuple[list[_Decision], list[dict[str, tuple[float, _SignalMeta]]]]:
    """All decision weight maps, honoring fixed-horizon exits/cooldowns."""
    spec = ctx.spec
    decisions = _decision_points(ctx)
    maps: list[dict[str, tuple[float, _SignalMeta]]] = []
    held_since: dict[str, int] = {}
    blocked_until: dict[str, int] = {}
    current: dict[str, float] = {}
    for decision in decisions:
        idx = decision.idx
        # Fixed-horizon enforcement on currently held names.
        forced_out: set[str] = set()
        if spec.horizon == "fixed_days" and spec.horizon_days is not None:
            for security_id, entry_idx in list(held_since.items()):
                if current.get(security_id, 0.0) == 0.0:
                    continue
                if idx - entry_idx >= spec.horizon_days:
                    forced_out.add(security_id)
                    blocked_until[security_id] = idx + spec.horizon_days
                    held_since.pop(security_id, None)
        analysts = _select_analysts(ctx, decision)
        scores = _raw_scores(ctx, decision, analysts)
        weights = _size_weights(ctx, scores, decision)
        # Apply horizon blocks and forced exits.
        final: dict[str, tuple[float, _SignalMeta]] = {}
        for security_id, (weight, meta) in weights.items():
            if security_id in forced_out or (
                security_id in blocked_until and idx < blocked_until[security_id]
            ):
                continue
            final[security_id] = (weight, meta)
        # Track entries/exits for horizon accounting.
        for security_id in set(current) | set(final):
            was = current.get(security_id, 0.0) != 0.0
            now = final.get(security_id, (0.0, None))[0] != 0.0
            if now and not was and security_id not in held_since:
                held_since[security_id] = idx
            elif was and not now:
                held_since.pop(security_id, None)
        current = {s: w for s, (w, _m) in final.items()}
        maps.append(final)
    return decisions, maps


def _build_events(
    ctx: _Context,
    decisions: list[_Decision],
    maps: list[dict[str, tuple[float, _SignalMeta]]],
) -> dict[str, list[_WeightEvent]]:
    """Per-security sorted weight events with coalesced execution sessions."""
    per_sec: dict[str, dict[date, _WeightEvent]] = defaultdict(dict)
    previous: dict[str, float] = {}
    for seq, (decision, weight_map) in enumerate(zip(decisions, maps, strict=True)):
        touched = set(previous) | set(weight_map)
        for security_id in touched:
            new_weight = weight_map.get(security_id, (0.0, _SignalMeta(None, None, None)))[0]
            meta = weight_map.get(security_id, (0.0, _SignalMeta(None, None, None)))[1]
            if new_weight == previous.get(security_id, 0.0):
                continue
            exec_day = _exec_session(ctx, security_id, decision)
            if exec_day is None:
                continue  # no future session: change can never execute
            existing = per_sec[security_id].get(exec_day)
            if existing is None or seq > existing.seq:
                per_sec[security_id][exec_day] = _WeightEvent(
                    session=exec_day,
                    weight=new_weight,
                    seq=seq,
                    analyst_id=meta.analyst_id if meta else None,
                    action_type=meta.action_type if meta else None,
                    sector_id=meta.sector_id if meta else None,
                )
            previous[security_id] = new_weight
    events: dict[str, list[_WeightEvent]] = {}
    for security_id, by_day in per_sec.items():
        ordered = sorted(by_day.values(), key=lambda e: (e.session, e.seq))
        # Coalesce duplicate sessions keeping the latest seq.
        deduped: dict[date, _WeightEvent] = {}
        for event in ordered:
            current = deduped.get(event.session)
            if current is None or event.seq > current.seq:
                deduped[event.session] = event
        events[security_id] = [deduped[d] for d in sorted(deduped)]
    return events


def _liquidity_at(rows: list[tuple[date, float | None]], day: date) -> float | None:
    result: float | None = None
    for session_date, adv in rows:
        if session_date > day:
            break
        result = adv
    return result


def _simulate(
    ctx: _Context,
    events: dict[str, list[_WeightEvent]],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    dict[tuple[str, str], float],
    BacktestStats,
]:
    """Daily NAV loop; returns nav rows, trade rows, attribution, stats."""
    spec = ctx.spec
    rate = (spec.transaction_cost_bps + (spec.slippage_bps or 0.0)) / _COST_DIVISOR

    # Union calendar: global decision dates plus benchmark sessions in range.
    lo = ctx.decision_dates[0] if ctx.decision_dates else ctx.all_dates[0]
    hi = ctx.decision_dates[-1] if ctx.decision_dates else ctx.all_dates[-1]
    nav_dates = sorted(
        {d for d in ctx.decision_dates} | {d for d in ctx.benchmark_ret if lo <= d <= hi}
    )
    if not nav_dates:
        return [], [], {}, _empty_stats()

    event_sessions: dict[str, list[date]] = {
        security_id: [e.session for e in evs] for security_id, evs in events.items()
    }
    securities = sorted(events)

    nav_rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []
    attribution: dict[tuple[str, str], float] = defaultdict(float)
    daily_returns: list[float] = []
    daily_active: list[float] = []
    daily_turnover: list[float] = []

    nav = 1.0
    benchmark_nav = 1.0
    peak = 1.0
    trade_seq = 0

    for day in nav_dates:
        nav_before = nav
        port_ret = 0.0
        turnover = 0.0
        day_legs: list[tuple[_SignalMeta | None, float, float]] = []
        # (meta, gross_contribution, turnover_i) per trading security
        for security_id in securities:
            levels = ctx.sec_levels.get(security_id)
            evs = events[security_id]
            sess = event_sessions[security_id]
            if levels is None:
                continue
            idx = bisect.bisect_left(levels.dates, day)
            if idx >= len(levels.dates) or levels.dates[idx] != day:
                continue
            w_in, meta_in = _weight_at(evs, sess, day)
            p = levels.dates[idx - 1] if idx > 0 else None
            w_on, meta_on = _weight_at(evs, sess, p) if p is not None else (0.0, None)
            open_tr = levels.open_tr[day]
            close_tr = levels.close_tr[day]
            disp = close_tr
            delist = ctx.delistings.get(security_id)
            if delist is not None and delist[0] == day and w_in != 0.0:
                disp = close_tr * (1.0 + delist[1])
            if p is not None and w_on == w_in:
                # Unchanged weight: exact close-to-close (or terminal) return,
                # matching portfolio.py's single point-to-point convention.
                contrib = w_in * (disp / levels.close_tr[p] - 1.0)
            else:
                # Execution boundary at today's open: overnight leg under the
                # old weight, open-to-close leg under the new weight.
                r_on = open_tr / levels.close_tr[p] - 1.0 if p is not None else 0.0
                contrib = w_on * r_on + w_in * (disp / open_tr - 1.0)
            port_ret += contrib
            ev_pos = bisect.bisect_left(sess, day)
            has_event = ev_pos < len(sess) and sess[ev_pos] == day
            turn_i = abs(w_in - w_on) if has_event else 0.0
            turnover += turn_i
            meta = meta_in if w_in != 0.0 else meta_on
            day_legs.append((meta, contrib, turn_i))
            if has_event and turn_i > 0:
                trade_seq += 1
                price = open_tr
                notional = turn_i * nav_before
                trade_rows.append(
                    {
                        "trade_seq": trade_seq,
                        "session_date": day,
                        "security_id": security_id,
                        "side": "buy" if w_in > w_on else "sell",
                        "quantity": notional / price if price > 0 else 0.0,
                        "price": price,
                        "notional": notional,
                        "fee": notional * rate,
                        "realized_pnl": None,
                    }
                )
        cost = turnover * rate
        port_ret -= cost
        # Cost allocation pro-rata to turnover for attribution.
        if turnover > 0 and day_legs:
            for meta, contrib, turn_i in day_legs:
                if meta is None:
                    continue
                alloc = contrib - cost * (turn_i / turnover if turnover else 0.0)
                for dim, key in (
                    ("sector", meta.sector_id or "unknown"),
                    ("analyst", meta.analyst_id or "none"),
                    ("rating_action", meta.action_type or "none"),
                ):
                    attribution[(dim, key)] += alloc
        elif day_legs:
            for meta, contrib, _turn_i in day_legs:
                if meta is None:
                    continue
                for dim, key in (
                    ("sector", meta.sector_id or "unknown"),
                    ("analyst", meta.analyst_id or "none"),
                    ("rating_action", meta.action_type or "none"),
                ):
                    attribution[(dim, key)] += contrib

        gross = 0.0
        for security_id in securities:
            w, _meta = _weight_at(events[security_id], event_sessions[security_id], day)
            gross += abs(w)

        nav *= 1.0 + port_ret
        bench_ret = ctx.benchmark_ret.get(day, 0.0)
        benchmark_nav *= 1.0 + bench_ret
        peak = max(peak, nav)
        daily_returns.append(port_ret)
        daily_active.append(port_ret - bench_ret)
        daily_turnover.append(turnover)
        nav_rows.append(
            {
                "session_date": day,
                "nav": nav,
                "benchmark_nav": benchmark_nav,
                "active_return": port_ret - bench_ret,
                "gross_exposure": gross,
                "turnover": turnover,
                "cost": cost,
                "drawdown": nav / peak - 1.0,
            }
        )

    stats = _compute_stats(
        daily_returns, daily_active, daily_turnover, nav_rows, spec.risk_free_annual
    )
    return nav_rows, trade_rows, dict(attribution), stats


def _empty_stats() -> BacktestStats:
    return BacktestStats(
        annualized_return=None,
        annualized_volatility=None,
        sharpe_ratio=None,
        information_ratio=None,
        max_drawdown=None,
        turnover=None,
        hit_rate=None,
        n_sessions=0,
    )


def _compute_stats(
    returns: list[float],
    active: list[float],
    turnovers: list[float],
    nav_rows: list[dict[str, object]],
    risk_free_annual: float,
) -> BacktestStats:
    n = len(returns)
    if n == 0:
        return _empty_stats()
    nav_end = float(nav_rows[-1]["nav"])  # type: ignore[arg-type]
    ann_ret = nav_end ** (_TRADING_DAYS_PER_YEAR / n) - 1.0 if nav_end > 0 else None
    if n >= 2:
        mean = sum(returns) / n
        var = sum((r - mean) ** 2 for r in returns) / (n - 1)
        vol = math.sqrt(var) * math.sqrt(_TRADING_DAYS_PER_YEAR)
        std = math.sqrt(var)
    else:
        vol = None
        std = 0.0
    ann_vol = vol
    sharpe = None
    if std is not None and std > 0:
        rf_daily = (1.0 + risk_free_annual) ** (1.0 / _TRADING_DAYS_PER_YEAR) - 1.0
        sharpe = (sum(returns) / n - rf_daily) / std * math.sqrt(_TRADING_DAYS_PER_YEAR)
    # Information ratio vs benchmark on active returns.
    ir = None
    if n >= 2:
        a_mean = sum(active) / n
        a_var = sum((a - a_mean) ** 2 for a in active) / (n - 1)
        a_std = math.sqrt(a_var)
        if a_std > 0:
            ir = a_mean / a_std * math.sqrt(_TRADING_DAYS_PER_YEAR)
    drawdowns = [float(r["drawdown"]) for r in nav_rows]  # type: ignore[arg-type]
    max_dd = min(drawdowns) if drawdowns else None
    turnover = sum(turnovers) / n
    hit = sum(1 for r in returns if r > 0) / n
    return BacktestStats(
        annualized_return=ann_ret,
        annualized_volatility=ann_vol,
        sharpe_ratio=sharpe,
        information_ratio=ir,
        max_drawdown=max_dd,
        turnover=turnover,
        hit_rate=hit,
        n_sessions=n,
    )


def _monthly_returns(nav_rows: list[dict[str, object]]) -> pl.DataFrame:
    """Compound daily returns per calendar month."""
    buckets: dict[tuple[int, int], tuple[float, float]] = {}
    # Recompute daily returns from consecutive nav rows.
    prev_nav = 1.0
    for row in nav_rows:
        day = row["session_date"]
        assert isinstance(day, date)
        nav = float(row["nav"])  # type: ignore[arg-type]
        key = (day.year, day.month)
        factor, _end = buckets.get(key, (1.0, nav))
        # prev_nav == 0 (e.g. a -100% delisting) leaves the book at 0; the
        # daily factor is undefined, so treat it as flat to keep the month
        # telescoping product finite.
        daily_factor = nav / prev_nav if prev_nav > 0 else 1.0
        buckets[key] = (factor * daily_factor, nav)
        prev_nav = nav
    rows = [
        {
            "year": year,
            "month": month,
            "month_return": factor - 1.0,
            "nav_end": end,
        }
        for (year, month), (factor, end) in sorted(buckets.items())
    ]
    return pl.DataFrame(rows, schema=_MONTHLY_SCHEMA)


def run_backtest(spec: BacktestSpec, frames: Mapping[str, pl.DataFrame]) -> BacktestResult:
    """Run a strategy backtest; see the module docstring for conventions."""
    ctx = _build_context(spec, frames)
    decisions, maps = _run_decisions(ctx)
    events = _build_events(ctx, decisions, maps)
    nav_rows, trade_rows, attribution_map, stats = _simulate(ctx, events)

    nav_daily = pl.DataFrame(nav_rows, schema=_NAV_SCHEMA).sort("session_date")
    trades = pl.DataFrame(trade_rows, schema=_TRADES_SCHEMA).sort("trade_seq")
    monthly = _monthly_returns(nav_rows)
    attr_rows = [
        {"dimension": dim, "group_key": key, "contribution": value}
        for (dim, key), value in sorted(attribution_map.items())
    ]
    attribution = pl.DataFrame(attr_rows, schema=_ATTRIBUTION_SCHEMA).sort("dimension", "group_key")
    return BacktestResult(
        nav_daily=nav_daily,
        trades=trades,
        monthly_returns=monthly,
        attribution=attribution,
        stats=stats,
    )
