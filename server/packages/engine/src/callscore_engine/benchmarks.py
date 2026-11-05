"""Custom benchmark construction (decision 0003 pricing convention).

Builds a ``BENCHMARK_DAILY_FRAME``-shaped total-return index from the
``security_daily`` / ``security_classifications`` / ``sessions`` frames plus an
optional ``liquidity_daily`` aux frame. Pure function of data: no I/O, no
clock, no store dependency.

Conventions implemented here:

* Membership is point-in-time: a security is a candidate at rebalance date
  ``R`` when a classification spell covers ``R`` (``from_date <= R``,
  ``to_date`` inclusive or null) and ``delisting_date`` is null or ``>= R``.
  Universe filters (``sector_id``, ``cap_bucket``, ``region``,
  ``security_ids``) are applied to the spell covering ``R``.
* Weighting is fixed at each rebalance session and drifts daily with
  total-return prices until the next rebalance (``daily`` rebalancing
  therefore never drifts). Weights are normalized over the members that
  actually have price data on each session; a member without a
  ``security_daily`` row on a session is excluded *that day* only.
* ``cap_weighted`` uses ``liquidity_daily.adv_20d`` (dollar ADV) as the size
  proxy: the engine frame contracts carry no market-cap or share-count
  field, and ``cap_bucket`` is categorical. ``free_float`` weighting uses
  ``adv_20d * free_float``. The liquidity value is the latest
  ``liquidity_daily`` row on or before the rebalance date (PIT). Missing
  liquidity data excludes the security for that rebalance period.
* ``cap_matched`` (preset and weighting) splits members into ``cap_bucket``
  strata at the rebalance date; each present stratum receives an equal share
  of the basket and is ``adv_20d``-weighted inside.
* Benchmark levels follow the same open/close recursion shape as
  ``pricing.execution_levels`` but on basket factors. For member ``i`` on
  session ``t`` (its own ``security_daily`` calendar):

  - first listed session: ``f_open = 1.0``, ``f_close = close_tr / open_tr``
    (only the intraday move counts; there is no prior close);
  - later sessions: ``f_open = open_tr(t) / close_tr(prev)``,
    ``f_close = close_tr(t) / open_tr(t)``.

  With start-of-day weights ``w_i`` (normalized over members present that
  day), ``F_open = sum_i w_i f_open_i`` and ``F_close = sum_i w_i f_close_i``.
  Then ``open_level(t) = close_level(t-1) * F_open`` and
  ``close_level(t) = open_level(t) * F_close``, with
  ``close_level(t0 - 1) = 100.0`` conceptually — the first emitted session
  therefore opens near 100 and compounds from there. Because the two legs
  use separately normalized weighted sums, ``close_level`` is *not* exactly
  the weighted sum of member ``tr_index`` levels; it is the documented
  basket recursion above.
* Presets:
  - ``equal_weight_market``: all classified securities, equal weights.
  - ``cap_matched``: all classified securities, ``cap_matched`` weighting.
  - ``sector_neutral``: long leg = members of ``spec.sector_id`` (required),
    short leg = all classified securities equal-weighted, same rebalance
    calendar. The combined daily factor is self-financing:
    ``F = 1 + (F_long - 1) - (F_short - 1) = F_long - F_short + 1`` applied
    separately to the open and close legs. A leg with no members on a day
    contributes factor 1.0 (flat) that day.
* The benchmark calendar is the union of ``sessions.session_date`` (the
  ``global_calendar`` merge), clipped to ``spec.start_date``/``end_date``.
  Members trading on other exchanges simply lack rows on foreign holidays
  and drop out for those days.

Missing-data accounting: every exclusion is counted in
``BenchmarkBuild.exclusion_counts`` — ``no_classification`` (a security with
price data on a rebalance date but no covering classification spell),
``no_liquidity`` (member lacking the liquidity field its weighting needs),
and ``no_price`` (member-day without a ``security_daily`` row). Securities
filtered out by the universe filter are intentional and not counted.
"""

import bisect
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import polars as pl

from callscore_core.frames import BENCHMARK_DAILY_FRAME
from callscore_core.methodology import FrozenModel, Identifier
from callscore_engine.calendar import global_calendar
from callscore_engine.pricing import execution_levels

Weighting = Literal["equal", "cap_weighted", "free_float", "cap_matched"]
Rebalance = Literal["daily", "monthly", "quarterly"]
Preset = Literal["custom", "equal_weight_market", "cap_matched", "sector_neutral"]

_DTYPE_MAP: dict[str, pl.DataType] = {
    "String": pl.String(),
    "Date": pl.Date(),
    "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
    "Float64": pl.Float64(),
    "Int64": pl.Int64(),
    "Boolean": pl.Boolean(),
}
BENCHMARK_SCHEMA = pl.Schema(
    {column.name: _DTYPE_MAP[column.dtype] for column in BENCHMARK_DAILY_FRAME.columns}
)

_BASE_LEVEL = 100.0


class UniverseFilter(FrozenModel):
    """PIT membership filter applied to the classification spell at rebalance."""

    sector_id: str | None = None
    cap_bucket: str | None = None
    region: str | None = None
    security_ids: tuple[str, ...] | None = None
    include_delisted: bool = False
    """When false (default), securities whose ``delisting_date`` precedes the
    rebalance date are excluded even if a spell still covers it."""


class BenchmarkSpec(FrozenModel):
    """Specification for :func:`build_custom_benchmark`."""

    benchmark_id: Identifier
    preset: Preset = "custom"
    universe: UniverseFilter = UniverseFilter()
    weighting: Weighting = "equal"
    rebalance: Rebalance = "monthly"
    sector_id: str | None = None
    """Long-leg sector for ``preset='sector_neutral'`` (required there)."""
    start_date: date | None = None
    end_date: date | None = None


@dataclass(frozen=True)
class BenchmarkFrames:
    """Input frames for benchmark construction.

    ``security_daily`` and ``security_classifications`` follow their frame
    contracts; ``sessions`` follows ``SESSION_FRAME``. ``liquidity_daily`` is
    the store aux shape ``(security_id, session_date, adv_20d, free_float)``
    — it has no frame contract — and is required for ``cap_weighted``,
    ``free_float`` and ``cap_matched`` weightings.
    """

    security_daily: pl.DataFrame
    security_classifications: pl.DataFrame
    sessions: pl.DataFrame
    liquidity_daily: pl.DataFrame | None = None


@dataclass(frozen=True)
class BenchmarkBuild:
    """Result of :func:`build_custom_benchmark`."""

    frame: pl.DataFrame
    """``BENCHMARK_DAILY_FRAME``-conformant frame (id, date, open, close)."""
    exclusion_counts: dict[str, int] = field(default_factory=dict)
    """Counts of PIT exclusions by reason; never silently included members."""


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
class _SecData:
    """Per-security sorted TR levels and liquidity lookups."""

    dates: tuple[date, ...]
    open_tr: dict[date, float]
    close_tr: dict[date, float]
    liquidity: tuple[tuple[date, float | None, float | None], ...]


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
    """Spell covering ``day`` (inclusive ``to_date``); else latest prior; else None.

    Unlike ``intervals._pick_spell`` there is no "earliest future" fallback:
    a security is only a member while a spell actually covers the date, so a
    future spell must not pull it into earlier periods.
    """
    covering = [s for s in spells if s.from_date <= day and (s.to_date is None or s.to_date >= day)]
    if not covering:
        return None
    return max(covering, key=lambda s: s.from_date)


def _security_data(
    security_daily: pl.DataFrame, liquidity_daily: pl.DataFrame | None
) -> dict[str, _SecData]:
    levels = execution_levels(security_daily)
    grouped: dict[str, dict[date, tuple[float, float]]] = {}
    for security_id, session_date, open_tr, close_tr in levels.select(
        "security_id", "session_date", "open_tr", "close_tr"
    ).iter_rows():
        grouped.setdefault(str(security_id), {})[session_date] = (
            float(open_tr),
            float(close_tr),
        )
    liquidity: dict[str, list[tuple[date, float | None, float | None]]] = {}
    if liquidity_daily is not None and liquidity_daily.height:
        for security_id, session_date, adv, ff in liquidity_daily.select(
            "security_id", "session_date", "adv_20d", "free_float"
        ).iter_rows():
            liquidity.setdefault(str(security_id), []).append(
                (
                    session_date,
                    None if adv is None else float(adv),
                    None if ff is None else float(ff),
                )
            )
    for values in liquidity.values():
        values.sort(key=lambda v: v[0])
    return {
        security_id: _SecData(
            dates=tuple(sorted(points)),
            open_tr={d: v[0] for d, v in points.items()},
            close_tr={d: v[1] for d, v in points.items()},
            liquidity=tuple(liquidity.get(security_id, ())),
        )
        for security_id, points in grouped.items()
    }


def _liquidity_at(data: _SecData, day: date) -> tuple[float | None, float | None]:
    """Latest ``(adv_20d, free_float)`` on or before ``day`` (PIT)."""
    result: tuple[float | None, float | None] = (None, None)
    for session_date, adv, ff in data.liquidity:
        if session_date > day:
            break
        result = (adv, ff)
    return result


def _factors(data: _SecData, day: date) -> tuple[float, float] | None:
    """``(f_open, f_close)`` for ``day``; None when the security does not trade it."""
    if day not in data.close_tr:
        return None
    open_tr = data.open_tr[day]
    close_tr = data.close_tr[day]
    idx = bisect.bisect_left(data.dates, day)
    if idx == 0:
        return 1.0, close_tr / open_tr
    prev_close = data.close_tr[data.dates[idx - 1]]
    return open_tr / prev_close, close_tr / open_tr


def _rebalance_dates(dates: list[date], frequency: Rebalance) -> list[date]:
    if frequency == "daily":
        return list(dates)
    picked: list[date] = []
    last_key: tuple[int, int] | None = None
    for day in dates:
        key = (day.year, day.month) if frequency == "monthly" else (day.year, (day.month - 1) // 3)
        if key != last_key:
            picked.append(day)
            last_key = key
    return picked


def _passes_universe(spell: _Spell, security_id: str, universe: UniverseFilter, day: date) -> bool:
    if universe.sector_id is not None and spell.sector_id != universe.sector_id:
        return False
    if universe.cap_bucket is not None and spell.cap_bucket != universe.cap_bucket:
        return False
    if universe.region is not None and spell.region != universe.region:
        return False
    if universe.security_ids is not None and security_id not in universe.security_ids:
        return False
    return not (
        not universe.include_delisted
        and spell.delisting_date is not None
        and spell.delisting_date < day
    )


def _raw_weight(
    weighting: Weighting,
    spell: _Spell,
    security_id: str,
    data: _SecData,
    day: date,
    counts: dict[str, int],
) -> float | None:
    """Unnormalized weight for one member; None when required data is missing."""
    if weighting == "equal":
        return 1.0
    adv, ff = _liquidity_at(data, day)
    if weighting == "cap_weighted":
        if adv is None:
            counts["no_liquidity"] = counts.get("no_liquidity", 0) + 1
            return None
        return adv
    if weighting == "free_float":
        if adv is None or ff is None:
            counts["no_liquidity"] = counts.get("no_liquidity", 0) + 1
            return None
        return adv * ff
    raise ValueError(f"Unknown weighting {weighting!r}")


def _members_at(
    rebalance_day: date,
    sec_data: dict[str, _SecData],
    spells: dict[str, list[_Spell]],
    universe: UniverseFilter,
    weighting: Weighting,
    counts: dict[str, int],
) -> dict[str, float]:
    """Normalized base weights for the basket effective at ``rebalance_day``."""
    raw: dict[str, float] = {}
    for security_id, data in sec_data.items():
        spell = _pick_spell(spells.get(security_id, []), rebalance_day)
        if spell is None:
            # Only count securities that actually traded that day: a delisted
            # or not-yet-listed name is not "missing membership data".
            if rebalance_day in data.close_tr:
                counts["no_classification"] = counts.get("no_classification", 0) + 1
            continue
        if not _passes_universe(spell, security_id, universe, rebalance_day):
            continue
        weight = _raw_weight(weighting, spell, security_id, data, rebalance_day, counts)
        if weight is None or weight <= 0:
            continue
        raw[security_id] = weight
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {security_id: value / total for security_id, value in raw.items()}


def _cap_matched_members(
    rebalance_day: date,
    sec_data: dict[str, _SecData],
    spells: dict[str, list[_Spell]],
    universe: UniverseFilter,
    counts: dict[str, int],
) -> dict[str, float]:
    """Equal weight per ``cap_bucket`` stratum, ``adv_20d``-weighted inside."""
    by_bucket: dict[str, dict[str, float]] = {}
    for security_id, data in sec_data.items():
        spell = _pick_spell(spells.get(security_id, []), rebalance_day)
        if spell is None:
            if rebalance_day in data.close_tr:
                counts["no_classification"] = counts.get("no_classification", 0) + 1
            continue
        if not _passes_universe(spell, security_id, universe, rebalance_day):
            continue
        adv, _ff = _liquidity_at(data, rebalance_day)
        if adv is None or adv <= 0:
            counts["no_liquidity"] = counts.get("no_liquidity", 0) + 1
            continue
        by_bucket.setdefault(spell.cap_bucket, {})[security_id] = adv
    n_buckets = len(by_bucket)
    if n_buckets == 0:
        return {}
    # Normalize strata to equal shares, adv-weighted inside each stratum.
    result: dict[str, float] = {}
    for _bucket, members in by_bucket.items():
        total = sum(members.values())
        share = 1.0 / n_buckets
        for security_id, adv in members.items():
            result[security_id] = share * adv / total
    return result


def _basket_factors(
    weights: dict[str, float],
    anchor: dict[str, float],
    day: date,
    sec_data: dict[str, _SecData],
    counts: dict[str, int],
) -> tuple[float, float]:
    """Drifted ``(F_open, F_close)`` for one basket on ``day``.

    ``anchor`` maps security -> close_tr at its last rebalance (the drift
    reference). Members lacking ``day`` data are excluded for the day.
    """
    f_open = 0.0
    f_close = 0.0
    total_drift = 0.0
    present: list[tuple[str, float, tuple[float, float]]] = []
    for security_id, base_weight in weights.items():
        data = sec_data.get(security_id)
        if data is None:
            counts["no_price"] = counts.get("no_price", 0) + 1
            continue
        factors = _factors(data, day)
        if factors is None:
            counts["no_price"] = counts.get("no_price", 0) + 1
            continue
        drift = data.close_tr[day] / anchor[security_id]
        present.append((security_id, base_weight * drift, factors))
        total_drift += base_weight * drift
    if total_drift <= 0 or not present:
        return 1.0, 1.0
    for _security_id, drifted, (fo, fc) in present:
        f_open += (drifted / total_drift) * fo
        f_close += (drifted / total_drift) * fc
    return f_open, f_close


def _build_basket_levels(
    base_weights_by_rebalance: dict[date, dict[str, float]],
    rebalances: list[date],
    calendar_dates: list[date],
    sec_data: dict[str, _SecData],
    counts: dict[str, int],
) -> tuple[list[tuple[date, float, float]], dict[date, float], dict[date, float]]:
    """Daily ``(F_open, F_close)`` for one basket; also returns anchors.

    Returns ``(rows, open_factors, close_factors)`` where rows are
    ``(session_date, F_open, F_close)``.
    """
    rows: list[tuple[date, float, float]] = []
    open_f: dict[date, float] = {}
    close_f: dict[date, float] = {}
    # anchors: security -> close_tr reference for drift, updated at each
    # rebalance to that security's close_tr on the rebalance date.
    current_weights: dict[str, float] = {}
    anchor: dict[str, float] = {}
    pending = list(rebalances)

    for day in calendar_dates:
        while pending and pending[0] <= day:
            rebal = pending.pop(0)
            weights = base_weights_by_rebalance.get(rebal, {})
            current_weights = weights
            anchor = {}
            for security_id in weights:
                data = sec_data.get(security_id)
                if data is not None and rebal in data.close_tr:
                    anchor[security_id] = data.close_tr[rebal]
            # Members without a price on the rebalance date cannot anchor;
            # they drift from their first available close instead.
            for security_id in weights:
                if security_id not in anchor:
                    data = sec_data.get(security_id)
                    if data is not None and data.dates:
                        first = data.dates[0]
                        if first <= day and first in data.close_tr:
                            anchor[security_id] = data.close_tr[first]
        if not current_weights:
            rows.append((day, 1.0, 1.0))
            open_f[day] = 1.0
            close_f[day] = 1.0
            continue
        # Refresh anchors for members whose anchor date is missing (e.g. a
        # member that joined via a later spell): use latest close <= day.
        fo, fc = _basket_factors(current_weights, anchor, day, sec_data, counts)
        rows.append((day, fo, fc))
        open_f[day] = fo
        close_f[day] = fc
    return rows, open_f, close_f


def _compose_levels(
    factor_rows: list[tuple[date, float, float]],
    calendar_dates: list[date],
    long_open: dict[date, float] | None = None,
    long_close: dict[date, float] | None = None,
    short_open: dict[date, float] | None = None,
    short_close: dict[date, float] | None = None,
) -> list[tuple[date, float, float]]:
    """Turn daily basket factors into ``(date, open_level, close_level)``.

    Single basket: ``open = prev_close * F_open``, ``close = open * F_close``.
    Sector-neutral: ``F = F_long - F_short + 1`` on each leg (missing leg
    factors default to 1.0).
    """
    out: list[tuple[date, float, float]] = []
    prev_close = _BASE_LEVEL
    factor_by_date = {day: (fo, fc) for day, fo, fc in factor_rows}
    for day in calendar_dates:
        if long_open is None:
            fo, fc = factor_by_date.get(day, (1.0, 1.0))
        else:
            lo = long_open.get(day, 1.0) if long_open is not None else 1.0
            lc = long_close.get(day, 1.0) if long_close is not None else 1.0
            so = short_open.get(day, 1.0) if short_open is not None else 1.0
            sc = short_close.get(day, 1.0) if short_close is not None else 1.0
            fo = 1.0 + (lo - 1.0) - (so - 1.0)
            fc = 1.0 + (lc - 1.0) - (sc - 1.0)
        open_level = prev_close * fo
        close_level = open_level * fc
        out.append((day, open_level, close_level))
        prev_close = close_level
    return out


def _preset_overrides(spec: BenchmarkSpec) -> tuple[UniverseFilter, Weighting, Rebalance]:
    """Resolve ``(universe, weighting, rebalance)`` for ``spec.preset``."""
    if spec.preset == "equal_weight_market":
        return spec.universe, "equal", spec.rebalance
    if spec.preset == "cap_matched":
        return spec.universe, "cap_matched", spec.rebalance
    if spec.preset == "sector_neutral":
        if spec.sector_id is None:
            raise ValueError("preset='sector_neutral' requires spec.sector_id")
        return spec.universe, spec.weighting, spec.rebalance
    return spec.universe, spec.weighting, spec.rebalance


def _basket_for(
    universe: UniverseFilter,
    weighting: Weighting,
    rebalance: Rebalance,
    calendar_dates: list[date],
    sec_data: dict[str, _SecData],
    spells: dict[str, list[_Spell]],
    counts: dict[str, int],
) -> tuple[dict[date, float], dict[date, float]]:
    """Full pipeline for one long-only basket: members -> daily leg factors."""
    rebalances = _rebalance_dates(calendar_dates, rebalance)
    base_weights: dict[date, dict[str, float]] = {}
    for day in rebalances:
        if weighting == "cap_matched":
            base_weights[day] = _cap_matched_members(day, sec_data, spells, universe, counts)
        else:
            base_weights[day] = _members_at(day, sec_data, spells, universe, weighting, counts)
    _rows, open_f, close_f = _build_basket_levels(
        base_weights, rebalances, calendar_dates, sec_data, counts
    )
    return open_f, close_f


def _assert_benchmark_frame(df: pl.DataFrame) -> None:
    contract = BENCHMARK_DAILY_FRAME
    assert list(df.columns) == list(contract.column_names)
    assert list(df.schema.items()) == list(BENCHMARK_SCHEMA.items())
    for key in contract.primary_key:
        assert df[key].null_count() == 0, f"primary key column {key} has nulls"
    assert df.equals(df.sort(*contract.sort_by)), "benchmark_daily not sorted by sort_by"


def build_custom_benchmark(spec: BenchmarkSpec, frames: BenchmarkFrames) -> BenchmarkBuild:
    """Build a ``benchmark_daily`` frame from a :class:`BenchmarkSpec`.

    See the module docstring for the exact level recursion, weighting fields
    and preset semantics. Raises ``ValueError`` for infeasible specs (e.g.
    ``sector_neutral`` without ``sector_id``) or empty inputs.
    """
    if frames.sessions.height == 0:
        raise ValueError("sessions frame is empty")
    if frames.security_daily.height == 0:
        raise ValueError("security_daily frame is empty")

    counts: dict[str, int] = {}
    sec_data = _security_data(frames.security_daily, frames.liquidity_daily)
    spells = _group_spells(frames.security_classifications)

    calendar = global_calendar(frames.sessions)
    calendar_dates = [d for d in calendar.session_dates]
    if spec.start_date is not None:
        calendar_dates = [d for d in calendar_dates if d >= spec.start_date]
    if spec.end_date is not None:
        calendar_dates = [d for d in calendar_dates if d <= spec.end_date]
    if not calendar_dates:
        raise ValueError("No sessions inside spec.start_date/end_date range")

    universe, weighting, rebalance = _preset_overrides(spec)

    if spec.preset == "sector_neutral":
        long_universe = UniverseFilter(
            sector_id=spec.sector_id,
            cap_bucket=universe.cap_bucket,
            region=universe.region,
            security_ids=universe.security_ids,
            include_delisted=universe.include_delisted,
        )
        # Short leg: broad market, equal-weighted, same rebalance calendar.
        empty_universe = UniverseFilter(include_delisted=universe.include_delisted)
        long_open, long_close = _basket_for(
            long_universe, weighting, rebalance, calendar_dates, sec_data, spells, counts
        )
        short_open, short_close = _basket_for(
            empty_universe, "equal", rebalance, calendar_dates, sec_data, spells, counts
        )
        levels = _compose_levels([], calendar_dates, long_open, long_close, short_open, short_close)
    else:
        open_f, close_f = _basket_for(
            universe, weighting, rebalance, calendar_dates, sec_data, spells, counts
        )
        levels = _compose_levels(
            [(day, open_f[day], close_f[day]) for day in calendar_dates], calendar_dates
        )

    df = pl.DataFrame(
        {
            "benchmark_id": [spec.benchmark_id] * len(levels),
            "session_date": [day for day, _o, _c in levels],
            "open_level": [o for _d, o, _c in levels],
            "close_level": [c for _d, _o, c in levels],
        },
        schema=BENCHMARK_SCHEMA,
    ).sort(*BENCHMARK_DAILY_FRAME.sort_by)
    _assert_benchmark_frame(df)
    return BenchmarkBuild(frame=df, exclusion_counts=counts)
