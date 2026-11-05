"""Rating-event, rating-scale and oracle-analyst generation.

Produces the ``RATING_EVENT_FRAME`` plus the store-shaped ``rating_scale`` /
``rating_scale_map`` reference frames the CLI persists, and augments the
reference frames (``analysts``, ``employment_spells``, ``coverage_spells``)
with a fixed set of oracle analysts whose skill is planted deterministically.

Skill horizon (documented choice): the **subsequent ~63-session total-return
excess** over ``IDX_BROAD`` measured from the event's session close
(``tr_index`` ratio minus benchmark ``close_level`` ratio over the same
session span). ``A_ORACLE`` buys positive-excess names and sells
negative-excess names; ``A_INVERSE`` mirrors it; ``A_NOISE`` is a coin flip;
``A_LUCKY`` issues a handful of buys on the sample's best full-period
performers (fewer than ``min_ratings_for_rank`` observations);
``A_HERDER`` copies the contemporaneous consensus of the other analysts;
``A_SLEEPER`` covers a subset of names very sparsely with long-dated
expiries; ``A_BANKRUPT`` holds a bankruptcy name into the -100% delisting.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

import numpy as np
import polars as pl

from callscore_core.frames import MARKET_BENCHMARK_ID, RATING_EVENT_FRAME
from callscore_synth import reference
from callscore_synth.profiles import Profile

# RNG stream indices (added to the per-profile offset; market.py uses 1-16).
_STREAM = {
    "scales": 20,
    "regular": 21,
    "oracle": 22,
    "special": 23,
    "herder": 24,
    "corrections": 25,
}

SKILL_HORIZON_SESSIONS = 63

ORACLE_ANALYST_IDS = (
    "A_ORACLE",
    "A_INVERSE",
    "A_NOISE",
    "A_LUCKY",
    "A_HERDER",
    "A_SLEEPER",
    "A_BANKRUPT",
)

_ORACLE_NAMES = {
    "A_ORACLE": "Oracle Skilled",
    "A_INVERSE": "Oracle Inverse",
    "A_NOISE": "Oracle Noise",
    "A_LUCKY": "Oracle Lucky",
    "A_HERDER": "Oracle Herder",
    "A_SLEEPER": "Oracle Sleeper",
    "A_BANKRUPT": "Oracle Bankrupt",
}

# Broad-market names covered intensively by the four "active" oracles.
_ACTIVE_ORACLES = ("A_ORACLE", "A_INVERSE", "A_NOISE", "A_HERDER")

# Canonical rating helpers ---------------------------------------------------- #

_INVEST_RANK = {"strong_buy": 0, "buy": 1, "hold": 2, "sell": 3, "strong_sell": 4}
_RANK_INVEST = {v: k for k, v in _INVEST_RANK.items()}
_INVEST_CANONICALS = tuple(_RANK_INVEST[i] for i in range(5))
_ADMIN_CANONICALS = ("under_review", "restricted", "not_rated")

# Default PositionWeights (methodology defaults) used for consensus math.
_CANONICAL_WEIGHT = {
    "strong_buy": 1.0,
    "buy": 0.5,
    "hold": 0.0,
    "sell": -0.5,
    "strong_sell": -1.0,
}

_INITIAL_WEIGHTS = np.array([0.14, 0.22, 0.34, 0.16, 0.14])  # sb,b,h,s,ss
_MOVE_DELTAS = np.array([-2, -1, 1, 2])
_MOVE_WEIGHTS = np.array([0.20, 0.30, 0.30, 0.20])

# Implied price-target return per canonical rating (pre-jitter).
_PT_IMPLIED = {
    "strong_buy": 0.28,
    "buy": 0.15,
    "hold": 0.03,
    "sell": -0.12,
    "strong_sell": -0.26,
}
_PT_HORIZONS = (3, 6, 12)

_EVENT_SCHEMA: dict[str, Any] = {
    "event_id": pl.String,
    "analyst_id": pl.String,
    "firm_id": pl.String,
    "security_id": pl.String,
    "announced_ts": pl.Datetime("us", "UTC"),
    "effective_ts": pl.Datetime("us", "UTC"),
    "knowledge_ts": pl.Datetime("us", "UTC"),
    "action_type": pl.String,
    "native_rating": pl.String,
    "canonical_rating": pl.String,
    "prior_canonical_rating": pl.String,
    "scale_id": pl.String,
    "price_target": pl.Float64,
    "pt_currency": pl.String,
    "pt_horizon_months": pl.Int64,
    "expiry_date": pl.Date,
    "supersedes_event_id": pl.String,
    "is_correction": pl.Boolean,
}


# --------------------------------------------------------------------------- #
# Rating scales
# --------------------------------------------------------------------------- #

# (native_label, canonical_rating, rank) triples per family. Every family also
# carries the three administrative states so suspend/restrict/drop events have
# a native label on any scale.
_FIVE_TIER = (
    ("Strong Buy", "strong_buy", 1),
    ("Buy", "buy", 2),
    ("Hold", "hold", 3),
    ("Sell", "sell", 4),
    ("Strong Sell", "strong_sell", 5),
)
_FIVE_TIER_ALT = (
    ("Outperform", "strong_buy", 1),
    ("Market Perform", "buy", 2),
    ("Neutral", "hold", 3),
    ("Underperform", "sell", 4),
    ("Strong Underperform", "strong_sell", 5),
)
_THREE_TIER = (
    ("Buy", "buy", 1),
    ("Hold", "hold", 2),
    ("Sell", "sell", 3),
)
_THREE_TIER_ALT = (
    ("Overweight", "buy", 1),
    ("Equal Weight", "hold", 2),
    ("Underweight", "sell", 3),
)

_SCALE_KINDS = ("five_tier", "three_tier")
_SCALE_PROBS = np.array([0.65, 0.35])
_SCALE_ALT_PROB = 0.5
# ~12% of firms get a mid-sample scale redefinition (label variant only).
_SCALE_REDEFINE_PROB = 0.12


@dataclass
class _ScaleVersion:
    effective_from: date
    effective_to: date | None
    labels: dict[str, str]  # canonical_rating -> native_label
    n_tiers: int


@dataclass
class _FirmScale:
    scale_id: str
    firm_id: str
    versions: list[_ScaleVersion] = field(default_factory=list)


def _admin_labels(n_invest: int) -> tuple[tuple[str, str, int], ...]:
    start = n_invest + 1
    return (
        ("Under Review", "under_review", start),
        ("Restricted", "restricted", start + 1),
        ("Not Rated", "not_rated", start + 2),
    )


def _labels_for(kind: str, alt: bool) -> tuple[tuple[str, str, int], ...]:
    base: tuple[tuple[str, str, int], ...]
    if kind == "five_tier":
        base = _FIVE_TIER_ALT if alt else _FIVE_TIER
    else:
        base = _THREE_TIER_ALT if alt else _THREE_TIER
    return base + _admin_labels(len(base))


def _version_from_rows(
    rows: tuple[tuple[str, str, int], ...], effective_from: date, effective_to: date | None
) -> _ScaleVersion:
    labels = {canonical: label for label, canonical, _rank in rows}
    n_invest = sum(1 for _l, c, _r in rows if c in _INVEST_RANK)
    return _ScaleVersion(
        effective_from=effective_from,
        effective_to=effective_to,
        labels=labels,
        n_tiers=n_invest,
    )


def generate_scales(
    profile: Profile, firms: pl.DataFrame, r: np.random.Generator
) -> tuple[dict[str, _FirmScale], pl.DataFrame, pl.DataFrame]:
    """Per-firm rating scales (one effective-dated version, ~12% redefined).

    Returns ``(firm_id -> _FirmScale, rating_scales frame, scale_maps frame)``.
    """
    firm_ids = firms["firm_id"].to_list()
    base_from = profile.start_date - timedelta(days=560)
    mid = profile.start_date + timedelta(days=profile.span_days // 2)

    scales: dict[str, _FirmScale] = {}
    scale_rows: list[dict[str, object]] = []
    map_rows: list[dict[str, object]] = []

    for fid in firm_ids:
        kind = _SCALE_KINDS[int(r.choice(len(_SCALE_KINDS), p=_SCALE_PROBS))]
        alt = bool(r.random() < _SCALE_ALT_PROB)
        scale_id = f"{fid}:{kind}"
        fs = _FirmScale(scale_id=scale_id, firm_id=str(fid))

        rows_v1 = _labels_for(kind, alt)
        redefine = bool(r.random() < _SCALE_REDEFINE_PROB)
        if redefine:
            fs.versions.append(_version_from_rows(rows_v1, base_from, mid - timedelta(days=1)))
            rows_v2 = _labels_for(kind, not alt)
            fs.versions.append(_version_from_rows(rows_v2, mid, None))
        else:
            fs.versions.append(_version_from_rows(rows_v1, base_from, None))

        for version in fs.versions:
            scale_rows.append(
                {
                    "scale_id": scale_id,
                    "firm_id": fid,
                    "effective_from": version.effective_from,
                    "effective_to": version.effective_to,
                    "n_tiers": version.n_tiers,
                }
            )
            # Rebuild rank rows: labels dict lost rank order; recompute from
            # the template rows matching this version's label set.
            template = next(
                t
                for t in (_labels_for(kind, alt), _labels_for(kind, not alt))
                if {c: lab for lab, c, _r in t} == version.labels
            )
            for label, canonical, rank in template:
                map_rows.append(
                    {
                        "scale_id": scale_id,
                        "effective_from": version.effective_from,
                        "native_label": label,
                        "canonical_rating": canonical,
                        "rank_within_scale": rank,
                    }
                )
        scales[str(fid)] = fs

    scales_frame = pl.DataFrame(
        scale_rows,
        schema={
            "scale_id": pl.String,
            "firm_id": pl.String,
            "effective_from": pl.Date,
            "effective_to": pl.Date,
            "n_tiers": pl.Int64,
        },
    ).sort("scale_id", "effective_from")
    maps_frame = pl.DataFrame(
        map_rows,
        schema={
            "scale_id": pl.String,
            "effective_from": pl.Date,
            "native_label": pl.String,
            "canonical_rating": pl.String,
            "rank_within_scale": pl.Int64,
        },
    ).sort("scale_id", "effective_from", "native_label")
    return scales, scales_frame, maps_frame


def _native_label(fs: _FirmScale, canonical: str, on: date) -> str:
    """Native label for ``canonical`` under the scale version effective on ``on``."""
    version = fs.versions[0]
    for v in fs.versions:
        if v.effective_from <= on and (v.effective_to is None or on <= v.effective_to):
            version = v
    return version.labels[canonical]


# --------------------------------------------------------------------------- #
# Market context: per-security sessions, prices, forward excess returns
# --------------------------------------------------------------------------- #


class _SecSeries(NamedTuple):
    security_id: str
    dates: list[date]
    pos: dict[date, int]
    close: list[float]
    tr_index: list[float]


class _MarketCtx:
    def __init__(self, frames: dict[str, pl.DataFrame], profile: Profile) -> None:
        sessions = frames["sessions"].sort("session_date")
        self.close_ts_by_date: dict[date, datetime] = {
            r[0]: r[1] for r in sessions.select("session_date", "close_ts").iter_rows()
        }
        self.open_ts_by_date: dict[date, datetime] = {
            r[0]: r[1] for r in sessions.select("session_date", "open_ts").iter_rows()
        }
        self.all_dates = sessions["session_date"].to_list()

        bench = frames["benchmark_daily"].filter(pl.col("benchmark_id") == MARKET_BENCHMARK_ID)
        self.bench_close: dict[date, float] = {
            r[0]: float(r[1]) for r in bench.select("session_date", "close_level").iter_rows()
        }

        daily = frames["security_daily"].sort("security_id", "session_date")
        self.secs: dict[str, _SecSeries] = {}
        for sid, grp in daily.partition_by("security_id", as_dict=True).items():
            key = str(sid[0]) if isinstance(sid, tuple) else str(sid)
            g = grp.sort("session_date")
            dates = g["session_date"].to_list()
            self.secs[key] = _SecSeries(
                security_id=key,
                dates=dates,
                pos={d: i for i, d in enumerate(dates)},
                close=[float(v) for v in g["close"].to_list()],
                tr_index=[float(v) for v in g["tr_index"].to_list()],
            )

        self.end_date = profile.end_date

    def fwd_excess(
        self, sec: _SecSeries, idx: int, horizon: int = SKILL_HORIZON_SESSIONS
    ) -> float | None:
        """Forward ``horizon``-session TR excess vs IDX_BROAD from session ``idx``."""
        j = min(idx + horizon, len(sec.dates) - 1)
        if j <= idx:
            return None
        d0, d1 = sec.dates[idx], sec.dates[j]
        b0, b1 = self.bench_close.get(d0), self.bench_close.get(d1)
        if b0 is None or b1 is None or b0 <= 0:
            return None
        stock = sec.tr_index[j] / sec.tr_index[idx] - 1.0
        bench = b1 / b0 - 1.0
        return stock - bench

    def total_excess(self, sec: _SecSeries) -> float | None:
        """Full-sample TR excess vs IDX_BROAD (first -> last session)."""
        if len(sec.dates) < 2:
            return None
        return self.fwd_excess(sec, 0, horizon=len(sec.dates) - 1)


# --------------------------------------------------------------------------- #
# Event construction
# --------------------------------------------------------------------------- #


class _PairState(NamedTuple):
    canonical: str | None
    mode: str  # "open" | "suspended" | "restricted" | "dropped"
    prior_investment: str | None


def _timestamps(
    r: np.random.Generator, session_date: date, ctx: _MarketCtx
) -> tuple[datetime, datetime, datetime]:
    """(announced, effective, knowledge) UTC timestamps for a session date."""
    open_ts = ctx.open_ts_by_date[session_date]
    announced = open_ts + timedelta(hours=float(r.uniform(1.0, 6.5)))
    effective = announced + timedelta(hours=float(r.uniform(0.0, 24.0)))
    # ~20% of events carry an ingestion lag of 1-72h after effective.
    lag = timedelta(0) if r.random() < 0.8 else timedelta(hours=float(r.uniform(1.0, 72.0)))
    knowledge = effective + lag
    return announced, effective, knowledge


def _expiry(r: np.random.Generator, effective: datetime, lo: int = 90, hi: int = 365) -> date:
    return effective.date() + timedelta(days=int(r.integers(lo, hi + 1)))


def _price_target(
    r: np.random.Generator,
    canonical: str,
    close: float | None,
    p: float,
) -> tuple[float | None, str | None, int | None]:
    if close is None or close <= 0 or r.random() >= p:
        return None, None, None
    implied = _PT_IMPLIED.get(canonical, 0.0) * float(r.uniform(0.75, 1.35))
    pt = close * (1.0 + implied)
    horizon = int(r.choice(_PT_HORIZONS))
    return pt, "USD", horizon


def _draw_canonical(r: np.random.Generator) -> str:
    return _INVEST_CANONICALS[int(r.choice(5, p=_INITIAL_WEIGHTS))]


def _move_canonical(r: np.random.Generator, current: str) -> str:
    old = _INVEST_RANK[current]
    new = min(max(old + int(r.choice(_MOVE_DELTAS, p=_MOVE_WEIGHTS)), 0), 4)
    return _RANK_INVEST[new]


@dataclass
class _EventDraft:
    analyst_id: str
    firm_id: str
    security_id: str
    announced: datetime
    effective: datetime
    knowledge: datetime
    action_type: str
    canonical: str
    prior: str | None
    scale_id: str
    price_target: float | None
    pt_currency: str | None
    pt_horizon_months: int | None
    expiry: date | None
    is_correction: bool = False
    supersedes: str | None = None

    def finalize(self, event_id: str, native: str) -> dict[str, object]:
        return {
            "event_id": event_id,
            "analyst_id": self.analyst_id,
            "firm_id": self.firm_id,
            "security_id": self.security_id,
            "announced_ts": self.announced,
            "effective_ts": self.effective,
            "knowledge_ts": self.knowledge,
            "action_type": self.action_type,
            "native_rating": native,
            "canonical_rating": self.canonical,
            "prior_canonical_rating": self.prior,
            "scale_id": self.scale_id,
            "price_target": self.price_target,
            "pt_currency": self.pt_currency,
            "pt_horizon_months": self.pt_horizon_months,
            "expiry_date": self.expiry,
            "supersedes_event_id": self.supersedes,
            "is_correction": self.is_correction,
        }


def _clamp_invest(canonical: str, allowed: frozenset[str]) -> str:
    """Collapse strong ratings when the firm's scale lacks those tiers."""
    if canonical in allowed:
        return canonical
    return {"strong_buy": "buy", "strong_sell": "sell"}.get(canonical, canonical)


def _regular_action(
    r: np.random.Generator,
    state: _PairState,
    ctx_sec: _SecSeries,
    idx: int,
    market: _MarketCtx,
    scale_id: str,
    allowed: frozenset[str],
) -> tuple[_EventDraft, _PairState]:
    """One regular-analyst event at session ``idx``; ``None`` = stop the spell."""
    session_date = ctx_sec.dates[idx]
    announced, effective, knowledge = _timestamps(r, session_date, market)
    close = ctx_sec.close[idx] if idx < len(ctx_sec.close) else None
    prior = state.canonical

    if state.canonical is None or state.mode == "dropped":
        canonical = _clamp_invest(_draw_canonical(r), allowed)
        pt, ccy, hor = _price_target(r, canonical, close, 0.40)
        draft = _EventDraft(
            analyst_id="",  # filled by caller
            firm_id="",
            security_id=ctx_sec.security_id,
            announced=announced,
            effective=effective,
            knowledge=knowledge,
            action_type="initiation",
            canonical=canonical,
            prior=None,
            scale_id=scale_id,
            price_target=pt,
            pt_currency=ccy,
            pt_horizon_months=hor,
            expiry=_expiry(r, effective),
        )
        return draft, _PairState(canonical, "open", None)

    u = float(r.random())
    mode = state.mode
    if mode == "suspended":
        if u < 0.70:
            canonical = state.prior_investment or "hold"
            action = "resume"
            new_mode, new_prior = "open", None
        elif u < 0.88:
            canonical, action = "not_rated", "drop"
            new_mode, new_prior = "dropped", state.prior_investment
        else:
            canonical, action = "under_review", "reiteration"
            new_mode, new_prior = "suspended", state.prior_investment
    elif mode == "restricted":
        if u < 0.70:
            canonical = state.prior_investment or "hold"
            action = "resume"
            new_mode, new_prior = "open", None
        elif u < 0.85:
            canonical, action = "not_rated", "drop"
            new_mode, new_prior = "dropped", state.prior_investment
        else:
            canonical, action = "restricted", "reiteration"
            new_mode, new_prior = "restricted", state.prior_investment
    else:
        if u < 0.05:
            canonical, action = "under_review", "suspend"
            new_mode, new_prior = "suspended", prior
        elif u < 0.09:
            canonical, action = "restricted", "restrict"
            new_mode, new_prior = "restricted", prior
        elif u < 0.15:
            canonical, action = "not_rated", "drop"
            new_mode, new_prior = "dropped", state.prior_investment
        else:
            if float(r.random()) < 0.45:
                canonical = prior if prior in _INVEST_RANK else _draw_canonical(r)
                action = "reiteration" if r.random() < 0.62 else "pt_change"
            else:
                canonical = _move_canonical(r, prior if prior in _INVEST_RANK else "hold")
                canonical = _clamp_invest(canonical, allowed)
                old_rank = _INVEST_RANK.get(prior or "hold", 2)
                action = "upgrade" if _INVEST_RANK[canonical] < old_rank else "downgrade"
                if canonical == prior:
                    action = "reiteration"
            new_mode, new_prior = "open", None

    # Price targets appear on initiations (~40%) and always on pt_change.
    if action in ("initiation", "pt_change"):
        pt_p = 1.0 if action == "pt_change" else 0.40
        pt, ccy, hor = _price_target(r, canonical, close, pt_p)
    else:
        pt, ccy, hor = None, None, None
    expiry = None if canonical == "not_rated" else _expiry(r, effective)
    draft = _EventDraft(
        analyst_id="",
        firm_id="",
        security_id=ctx_sec.security_id,
        announced=announced,
        effective=effective,
        knowledge=knowledge,
        action_type=action,
        canonical=canonical,
        prior=prior,
        scale_id=scale_id,
        price_target=pt,
        pt_currency=ccy,
        pt_horizon_months=hor,
        expiry=expiry,
    )
    return draft, _PairState(canonical, new_mode, new_prior)


def _guided_action(
    r: np.random.Generator,
    state: _PairState,
    desired: str,
    ctx_sec: _SecSeries,
    idx: int,
    market: _MarketCtx,
    scale_id: str,
) -> tuple[_EventDraft, _PairState]:
    """One oracle-style event steering toward ``desired`` canonical rating."""
    session_date = ctx_sec.dates[idx]
    announced, effective, knowledge = _timestamps(r, session_date, market)
    close = ctx_sec.close[idx] if idx < len(ctx_sec.close) else None
    prior = state.canonical

    if prior is None or state.mode == "dropped":
        action = "initiation"
        canonical = desired
        new_state = _PairState(canonical, "open", None)
    elif state.mode != "open":
        action = "resume"
        canonical = desired
        new_state = _PairState(canonical, "open", None)
    elif desired == prior:
        action = "reiteration" if r.random() < 0.72 else "pt_change"
        canonical = desired
        new_state = state
    else:
        canonical = desired
        old_rank = _INVEST_RANK.get(prior, 2)
        action = "upgrade" if _INVEST_RANK[canonical] < old_rank else "downgrade"
        new_state = _PairState(canonical, "open", None)

    if action in ("initiation", "pt_change"):
        pt_p = 1.0 if action == "pt_change" else 0.40
        pt, ccy, hor = _price_target(r, canonical, close, pt_p)
    else:
        pt, ccy, hor = None, None, None
    draft = _EventDraft(
        analyst_id="",
        firm_id="",
        security_id=ctx_sec.security_id,
        announced=announced,
        effective=effective,
        knowledge=knowledge,
        action_type=action,
        canonical=canonical,
        prior=prior,
        scale_id=scale_id,
        price_target=pt,
        pt_currency=ccy,
        pt_horizon_months=hor,
        expiry=_expiry(r, effective),
    )
    return draft, new_state


# --------------------------------------------------------------------------- #
# Scheduling helpers
# --------------------------------------------------------------------------- #


def _session_idx_range(
    sec: _SecSeries, cov_from: date | None, cov_to: date | None, end: date
) -> tuple[int, int]:
    """Inclusive session-index window of ``sec`` inside the coverage spell."""
    lo_d = cov_from or sec.dates[0]
    hi_d = min(cov_to or end, end)
    lo = 0
    while lo < len(sec.dates) and sec.dates[lo] < lo_d:
        lo += 1
    hi = len(sec.dates) - 1
    while hi >= 0 and sec.dates[hi] > hi_d:
        hi -= 1
    return lo, hi


def _spread_indices(
    r: np.random.Generator, lo: int, hi: int, k: int, min_gap: int = 20
) -> list[int]:
    """``k`` sorted distinct indices in ``[lo, hi]`` spaced >= ``min_gap``."""
    span = hi - lo + 1
    if span <= 0 or k <= 0:
        return []
    k = min(k, max(1, span // max(min_gap, 1)))
    for _ in range(50):
        idxs = sorted(int(x) for x in r.choice(span, size=k, replace=False) + lo)
        if k == 1 or min(b - a for a, b in zip(idxs, idxs[1:], strict=False)) >= min_gap:
            return idxs
    return idxs


# --------------------------------------------------------------------------- #
# Oracle desired-rating pickers
# --------------------------------------------------------------------------- #


def _oracle_desired(
    kind: str,
    r: np.random.Generator,
    excess: float | None,
) -> str:
    if kind == "A_NOISE":
        return _INVEST_CANONICALS[int(r.integers(0, 5))]
    if excess is None:
        return "hold"
    if kind == "A_INVERSE":
        excess = -excess
    if excess > 0.05:
        return "strong_buy"
    if excess > 0.0:
        return "buy"
    if excess < -0.05:
        return "strong_sell"
    if excess < 0.0:
        return "sell"
    return "hold"


def _consensus_canonical(mean_weight: float) -> str:
    """Nearest canonical rating to a mean position weight."""
    best = "hold"
    best_d = abs(mean_weight)
    for canonical, w in _CANONICAL_WEIGHT.items():
        d = abs(mean_weight - w)
        if d < best_d:
            best, best_d = canonical, d
    return best


# --------------------------------------------------------------------------- #
# Top-level generation
# --------------------------------------------------------------------------- #


def generate(
    frames: dict[str, pl.DataFrame], profile: Profile, seed: int
) -> dict[str, pl.DataFrame]:
    """Generate rating events, scales and oracle analysts for ``(seed, profile)``.

    ``frames`` must contain the reference/market frames produced by
    ``market.generate`` (``sessions``, ``security_daily``, ``benchmark_daily``,
    ``delistings``, ``coverage_spells``, ``employment_spells``, ``analysts``,
    ``firms``). Returns a dict with the new frames ``rating_events``,
    ``rating_scales``, ``scale_maps`` and the *updated* ``analysts``,
    ``employment_spells`` and ``coverage_spells`` frames (oracle rows added).
    """
    offset = reference.profile_seed_offset(profile.name)

    def rng_for(tag: str) -> np.random.Generator:
        return reference.rng(seed, _STREAM[tag] + offset)

    market = _MarketCtx(frames, profile)
    firms = frames["firms"]
    analysts = frames["analysts"]
    coverage = frames["coverage_spells"]
    employment = frames["employment_spells"]

    firm_scales, scales_frame, maps_frame = generate_scales(profile, firms, rng_for("scales"))
    analyst_firm = {r[0]: str(r[1]) for r in analysts.select("analyst_id", "firm_id").iter_rows()}

    drafts: list[_EventDraft] = []
    # security_id -> finalized event dicts so far (for herder consensus).
    sec_events: dict[str, list[dict[str, object]]] = {}
    pair_states: dict[tuple[str, str], _PairState] = {}
    event_counter = 0

    def scale_id_of(analyst_id: str) -> str:
        fid = analyst_firm.get(analyst_id, "")
        fs = firm_scales.get(fid)
        return fs.scale_id if fs is not None else next(iter(firm_scales.values())).scale_id

    def native_of(analyst_id: str, canonical: str, on: date) -> str:
        fid = analyst_firm.get(analyst_id, "")
        fs = firm_scales.get(fid)
        if fs is None:
            fs = next(iter(firm_scales.values()))
        return _native_label(fs, canonical, on)

    def clamp_desired(analyst_id: str, canonical: str) -> str:
        """Coarse scales cannot express strong ratings: collapse them."""
        fs = firm_scales.get(analyst_firm.get(analyst_id, ""))
        if fs is None or canonical in fs.versions[0].labels:
            return canonical
        return {"strong_buy": "buy", "strong_sell": "sell"}.get(canonical, canonical)

    def allowed_for(analyst_id: str) -> frozenset[str]:
        fs = firm_scales.get(analyst_firm.get(analyst_id, ""))
        if fs is None:
            fs = next(iter(firm_scales.values()))
        return frozenset(fs.versions[0].labels)

    def emit(
        analyst_id: str,
        firm_id: str,
        sec: _SecSeries,
        sched: Iterable[tuple[int, str | None]],
        r: np.random.Generator,
        guided: bool,
    ) -> None:
        """Generate events for one (analyst, security) schedule.

        ``sched`` is ``(session_index, desired_canonical | None)``; ``None``
        means the regular state machine picks the rating itself.
        """
        nonlocal event_counter
        state = pair_states.get((analyst_id, sec.security_id), _PairState(None, "open", None))
        scale_id = scale_id_of(analyst_id)
        allowed = allowed_for(analyst_id)
        last_announced: datetime | None = None
        for idx, desired in sched:
            if idx < 0 or idx >= len(sec.dates):
                continue
            if desired is None:
                draft, state = _regular_action(r, state, sec, idx, market, scale_id, allowed)
            else:
                draft, state = _guided_action(
                    r, state, clamp_desired(analyst_id, desired), sec, idx, market, scale_id
                )
            # analyst_id/firm_id are filled by the caller context.
            draft.analyst_id = analyst_id
            draft.firm_id = firm_id
            # Keep announced_ts strictly increasing per (analyst, security).
            if last_announced is not None and draft.announced <= last_announced:
                draft.announced = last_announced + timedelta(hours=1)
                draft.effective = max(draft.effective, draft.announced)
                draft.knowledge = max(draft.knowledge, draft.effective)
            last_announced = draft.announced
            pair_states[(analyst_id, sec.security_id)] = state
            drafts.append(draft)
            event_counter += 1
            # A drop ends this spell's event stream (a later coverage spell for
            # the same pair may still re-initiate via the persisted state).
            if not guided and state.mode == "dropped":
                break

    # --- regular analysts --------------------------------------------------- #
    r_regular = rng_for("regular")
    cov_rows = (
        coverage.sort("analyst_id", "security_id", "from_date")
        .select("analyst_id", "security_id", "from_date", "to_date")
        .iter_rows()
    )
    for aid, sid, cov_from, cov_to in cov_rows:
        sec = market.secs.get(str(sid))
        if sec is None:
            continue
        lo, hi = _session_idx_range(sec, cov_from, cov_to, market.end_date)
        if hi - lo < 15:
            continue
        fid = analyst_firm.get(str(aid), "")
        # ~20% of spells lapse early: stop updating months before coverage ends.
        eff_hi = hi
        if r_regular.random() < 0.20 and hi - lo > 140:
            eff_hi = hi - int(r_regular.integers(60, 141))
        span = eff_hi - lo + 1
        k = int(max(2, round(span / float(r_regular.uniform(70, 150)))))
        k = min(k, 14)
        idxs = _spread_indices(r_regular, lo, eff_hi, k, min_gap=25)
        sched: list[tuple[int, str | None]] = [(i, None) for i in idxs]
        emit(str(aid), fid, sec, sched, r_regular, guided=False)

    # --- oracle analysts: coverage + employment ----------------------------- #
    firm_ids = firms["firm_id"].to_list()
    oracle_firm = {
        aid: str(firm_ids[i % len(firm_ids)]) for i, aid in enumerate(ORACLE_ANALYST_IDS)
    }
    for aid in ORACLE_ANALYST_IDS:
        analyst_firm[aid] = oracle_firm[aid]

    emp_start = profile.start_date - timedelta(days=450)
    cov_from_oracle = profile.start_date - timedelta(days=45)
    delist_to = {
        str(r[0]): r[1]
        for r in frames["delistings"].select("security_id", "delisting_date").iter_rows()
    }
    sec_ids_sorted = sorted(market.secs)
    bankrupt_ids = [
        str(r[0])
        for r in frames["delistings"]
        .filter(pl.col("reason") == "bankrupt")
        .select("security_id")
        .iter_rows()
    ]

    oracle_coverage: dict[str, list[str]] = {}
    for aid in _ACTIVE_ORACLES:
        oracle_coverage[aid] = list(sec_ids_sorted)
    oracle_coverage["A_SLEEPER"] = sec_ids_sorted[::4]
    oracle_coverage["A_LUCKY"] = []  # filled below
    oracle_coverage["A_BANKRUPT"] = bankrupt_ids

    # --- A_LUCKY: top full-period performers, one initiation each ------------ #
    scored: list[tuple[float, str]] = []
    for sid, sec in market.secs.items():
        if len(sec.dates) < 126:
            continue
        ex = market.total_excess(sec)
        if ex is not None:
            scored.append((ex, sid))
    scored.sort(key=lambda t: (-t[0], t[1]))
    n_lucky = min(12, max(4, len(sec_ids_sorted) // 6), len(scored))
    lucky_ids = [sid for _ex, sid in scored[:n_lucky]]
    oracle_coverage["A_LUCKY"] = lucky_ids

    # --- oracle event schedules ---------------------------------------------- #
    r_oracle = rng_for("oracle")
    r_special = rng_for("special")

    for aid in _ACTIVE_ORACLES:
        kind = aid
        for sid in oracle_coverage[aid]:
            sec = market.secs[sid]
            lo, hi = _session_idx_range(sec, cov_from_oracle, None, market.end_date)
            if hi - lo < 40:
                continue
            # Leave ~25 sessions of forward data where possible.
            hi_fwd = min(hi, len(sec.dates) - 1 - 25)
            eff_hi = hi_fwd if hi_fwd > lo else hi
            k = int(r_oracle.integers(4, 8))
            idxs = _spread_indices(r_oracle, lo, eff_hi, k, min_gap=30)
            sched = []
            for i in idxs:
                ex = market.fwd_excess(sec, i)
                sched.append((i, _oracle_desired(kind, r_oracle, ex)))
            fid = oracle_firm[aid]
            emit(aid, fid, sec, sched, r_oracle, guided=True)

    # --- A_LUCKY events -------------------------------------------------------- #
    for sid in lucky_ids:
        sec = market.secs[sid]
        lo, hi = _session_idx_range(sec, cov_from_oracle, None, market.end_date)
        if hi - lo < 30:
            continue
        idx = lo + int((hi - lo) * 0.4)
        ex = market.total_excess(sec)
        desired = "strong_buy" if (ex is not None and ex > 0.15) else "buy"
        emit("A_LUCKY", oracle_firm["A_LUCKY"], sec, [(idx, desired)], r_special, guided=True)

    # --- A_SLEEPER: sparse regular-style events, long-dated expiry ------------ #
    for sid in oracle_coverage["A_SLEEPER"]:
        sec = market.secs[sid]
        lo, hi = _session_idx_range(sec, cov_from_oracle, None, market.end_date)
        if hi - lo < 60:
            continue
        k = int(r_special.integers(1, 4))
        idxs = _spread_indices(r_special, lo, hi, k, min_gap=120)
        sched = [(i, None) for i in idxs]
        emit("A_SLEEPER", oracle_firm["A_SLEEPER"], sec, sched, r_special, guided=False)

    # --- A_BANKRUPT: buy-and-hold into the delisting --------------------------- #
    for sid in bankrupt_ids:
        sec = market.secs.get(sid)
        if sec is None or len(sec.dates) < 30:
            continue
        last = len(sec.dates) - 1
        first = max(0, last - int(r_special.integers(150, 221)))
        if first >= last - 10:
            first = max(0, last - 120)
        desired = "strong_buy" if r_special.random() < 0.5 else "buy"
        sched = [(first, desired)]
        if last - first > 80:
            sched.append((first + (last - first) // 2, desired))
        emit("A_BANKRUPT", oracle_firm["A_BANKRUPT"], sec, sched, r_special, guided=True)

    # --- A_HERDER: copy the contemporaneous consensus -------------------------- #
    r_herd = rng_for("herder")
    herder_drafts: list[_EventDraft] = []
    for sid in oracle_coverage["A_HERDER"]:
        sec = market.secs[sid]
        lo, hi = _session_idx_range(sec, cov_from_oracle, None, market.end_date)
        if hi - lo < 40:
            continue
        hi_fwd = min(hi, len(sec.dates) - 1 - 25)
        eff_hi = hi_fwd if hi_fwd > lo else hi
        k = int(r_herd.integers(4, 8))
        idxs = _spread_indices(r_herd, lo, eff_hi, k, min_gap=30)
        sched = []
        sec_evts = sec_events.get(sid, [])
        for i in idxs:
            desired = _herd_desired(sec_evts, sec, i, market, sid, r_herd)
            sched.append((i, desired))
        # Herder events are emitted after all others so the consensus is final.
        state = pair_states.get(("A_HERDER", sid), _PairState(None, "open", None))
        scale_id = scale_id_of("A_HERDER")
        fid = oracle_firm["A_HERDER"]
        last_announced: datetime | None = None
        for i, h_desired in sched:
            assert h_desired is not None
            draft, state = _guided_action(
                r_herd, state, clamp_desired("A_HERDER", h_desired), sec, i, market, scale_id
            )
            draft.analyst_id = "A_HERDER"
            draft.firm_id = fid
            if last_announced is not None and draft.announced <= last_announced:
                draft.announced = last_announced + timedelta(hours=1)
                draft.effective = max(draft.effective, draft.announced)
                draft.knowledge = max(draft.knowledge, draft.effective)
            last_announced = draft.announced
            pair_states[("A_HERDER", sid)] = state
            herder_drafts.append(draft)
            event_counter += 1
    drafts.extend(herder_drafts)

    # --- finalize ids + native labels ------------------------------------------ #
    # Sort drafts deterministically, then number events. Correction chains are
    # appended afterwards and reference these base ids directly.
    drafts.sort(key=lambda d: (d.announced, d.analyst_id, d.security_id))
    base_rows: list[dict[str, object]] = []
    for i, d in enumerate(drafts):
        eid = f"EV_{i + 1:06d}"
        native = native_of(d.analyst_id, d.canonical, d.announced.date())
        base_rows.append(d.finalize(eid, native))
        sec_events.setdefault(d.security_id, []).append(base_rows[-1])

    # --- correction chains (~1.5% of base events) ------------------------------- #
    r_corr = rng_for("corrections")
    n_corr = int(round(len(base_rows) * 0.015)) if len(base_rows) >= 30 else 0
    if n_corr > 0:
        chosen = sorted(
            r_corr.choice(len(base_rows), size=min(n_corr, len(base_rows)), replace=False).tolist()
        )
        corr_rows: list[dict[str, object]] = []
        for j, pos in enumerate(chosen):
            src = base_rows[pos]
            src_announced = src["announced_ts"]
            assert isinstance(src_announced, datetime)
            announced = src_announced + timedelta(hours=float(r_corr.uniform(2.0, 72.0)))
            effective = announced + timedelta(hours=float(r_corr.uniform(0.0, 12.0)))
            knowledge = effective + timedelta(hours=float(r_corr.uniform(1.0, 48.0)))
            canon = str(src["canonical_rating"])
            if canon in _INVEST_RANK and r_corr.random() < 0.5:
                canon = _move_canonical(r_corr, canon)
            analyst_id = str(src["analyst_id"])
            canon = clamp_desired(analyst_id, canon)
            native = native_of(analyst_id, canon, announced.date())
            corr_rows.append(
                {
                    "event_id": f"EV_{len(base_rows) + j + 1:06d}",
                    "analyst_id": analyst_id,
                    "firm_id": src["firm_id"],
                    "security_id": src["security_id"],
                    "announced_ts": announced,
                    "effective_ts": effective,
                    "knowledge_ts": knowledge,
                    "action_type": src["action_type"],
                    "native_rating": native,
                    "canonical_rating": canon,
                    "prior_canonical_rating": src["canonical_rating"],
                    "scale_id": src["scale_id"],
                    "price_target": src["price_target"],
                    "pt_currency": src["pt_currency"],
                    "pt_horizon_months": src["pt_horizon_months"],
                    "expiry_date": src["expiry_date"],
                    "supersedes_event_id": src["event_id"],
                    "is_correction": True,
                }
            )
        rows = base_rows + corr_rows
    else:
        rows = base_rows

    events_frame = pl.DataFrame(rows, schema=_EVENT_SCHEMA).select(
        [c.name for c in RATING_EVENT_FRAME.columns]
    )
    events_frame = events_frame.sort("analyst_id", "security_id", "announced_ts", "event_id")

    # A delisted security cannot be (re-)rated: drop events whose availability
    # (max(effective_ts, knowledge_ts) — the engine's entry basis) falls on or
    # after its delisting_date, since their entry session would be post-delist.
    # Events available before the delisting date are kept so A_BANKRUPT's
    # into-delisting call survives.
    if frames["delistings"].height:
        delist_df = (
            frames["delistings"]
            .select("security_id", "delisting_date")
            .unique(subset=["security_id"], keep="first")
        )
        events_frame = (
            events_frame.join(delist_df, on="security_id", how="left")
            .filter(
                pl.col("delisting_date").is_null()
                | (
                    pl.max_horizontal("effective_ts", "knowledge_ts").dt.date()
                    < pl.col("delisting_date")
                )
            )
            .drop("delisting_date")
        )

    # --- oracle analyst/ employment / coverage augmentation --------------------- #
    oracle_rows = [
        {"analyst_id": aid, "name": _ORACLE_NAMES[aid], "firm_id": oracle_firm[aid]}
        for aid in ORACLE_ANALYST_IDS
    ]
    analysts_out = pl.concat(
        [analysts, pl.DataFrame(oracle_rows, schema=analysts.schema)], how="vertical"
    ).sort("analyst_id")

    emp_rows = [
        {
            "analyst_id": aid,
            "firm_id": oracle_firm[aid],
            "from_date": emp_start,
            "to_date": None,
        }
        for aid in ORACLE_ANALYST_IDS
    ]
    employment_out = pl.concat(
        [employment, pl.DataFrame(emp_rows, schema=employment.schema)], how="vertical"
    ).sort("analyst_id", "firm_id", "from_date")

    cov_rows_out: list[dict[str, object]] = []
    for aid, sids in oracle_coverage.items():
        for sid in sids:
            sec = market.secs.get(sid)
            if sec is None:
                continue
            cov_to = delist_to.get(sid) or market.end_date
            cov_rows_out.append(
                {
                    "analyst_id": aid,
                    "security_id": sid,
                    "from_date": cov_from_oracle,
                    "to_date": cov_to,
                    "is_lead": aid in _ACTIVE_ORACLES,
                }
            )
    coverage_out = pl.concat(
        [
            coverage,
            pl.DataFrame(
                cov_rows_out,
                schema={
                    "analyst_id": pl.String,
                    "security_id": pl.String,
                    "from_date": pl.Date,
                    "to_date": pl.Date,
                    "is_lead": pl.Boolean,
                },
            ),
        ],
        how="vertical",
    ).sort("analyst_id", "security_id", "from_date")

    return {
        "rating_events": events_frame,
        "rating_scales": scales_frame,
        "scale_maps": maps_frame,
        "analysts": analysts_out,
        "employment_spells": employment_out,
        "coverage_spells": coverage_out,
    }


# --------------------------------------------------------------------------- #
# Herder consensus lookup
# --------------------------------------------------------------------------- #


def _herd_desired(
    sec_events: list[dict[str, object]],
    sec: _SecSeries,
    idx: int,
    market: _MarketCtx,
    sid: str,
    r: np.random.Generator,
) -> str:
    """Canonical rating copying the other analysts' visible consensus.

    Considers events with ``available_ts <`` the herder event's announced
    time — approximated by the session's close minus a small intraday offset,
    matching the engine's ``available_ts < close_ts`` visibility rule closely
    enough for synthetic data. The latest event per analyst wins; expired
    ratings and non-weight canonicals are excluded.
    """
    session_date = sec.dates[idx]
    cutoff = market.close_ts_by_date.get(session_date)
    if cutoff is None:
        return "hold"
    # Latest visible event per analyst at this session's close. ``best`` maps
    # analyst_id -> ((avail, event_id), canonical, expiry); the tuple key gives
    # the engine's (available_ts, event_id) ordering.
    best: dict[str, tuple[tuple[datetime, str], str, date | None]] = {}
    for e in sec_events:
        if e["analyst_id"] == "A_HERDER":
            continue
        eff = e["effective_ts"]
        kno = e["knowledge_ts"]
        assert isinstance(eff, datetime) and isinstance(kno, datetime)
        avail = max(eff, kno)
        if avail >= cutoff:
            continue
        canon = str(e["canonical_rating"])
        key = (avail, str(e["event_id"]))
        prev = best.get(str(e["analyst_id"]))
        if prev is None or key > prev[0]:
            expiry = e["expiry_date"]
            best[str(e["analyst_id"])] = (
                key,
                canon,
                expiry if isinstance(expiry, date) else None,
            )
    weights: list[float] = []
    for _avail, canon, expiry in best.values():
        if expiry is not None and expiry < session_date:
            continue
        w = _CANONICAL_WEIGHT.get(canon)
        if w is not None:
            weights.append(w)
    if not weights:
        return "hold"
    mean_w = float(np.mean(weights))
    # "Almost exactly": 90% exact nearest, 10% one-step jitter.
    if r.random() < 0.10:
        mean_w += float(r.normal(0.0, 0.15))
    return _consensus_canonical(mean_w)
