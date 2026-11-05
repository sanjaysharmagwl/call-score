"""Market-data generation: prices, benchmarks, corporate actions, betas, etc.

Generates every engine input frame that is *not* reference data, obeying the
pricing convention documented in ``callscore_core.frames`` next to
``SECURITY_DAILY_FRAME``:

* ``adj_factor`` is forward-cumulative (1.0 at listing, multiplied by the
  split ratio on each split's ex-date).
* ``cash_dividend`` is per share on the ex-date, reinvested at that open.
* ``tr_index`` is the close total-return level, 100.0 at the first close;
  ``open_tr(t) = tr_index(p) * adj(t) * (open(t) + div(t)) /
  (adj(p) * close(p))`` and ``tr_index(t) = open_tr(t) * close(t) / open(t)``.
* Halted sessions carry ``open = close =`` previous close, ``is_halted``.

The shared ``_tr_levels`` helper implements those formulas vectorized; the
per-security price paths are built so the convention holds by construction.
"""

import hashlib
import io
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import NamedTuple

import numpy as np
import polars as pl

from callscore_core.frames import MARKET_BENCHMARK_ID, sector_benchmark_id
from callscore_synth import reference
from callscore_synth.profiles import Profile, load_profile
from callscore_synth.reference import SecuritySpec

# RNG stream indices (added to the per-profile offset before seeding).
_STREAM = {
    "specs": 10,
    "firms": 1,
    "analysts": 2,
    "employment": 3,
    "coverage": 4,
    "classifications": 5,
    "delistings": 15,
    "factors": 11,
    "security_params": 12,
    "actions": 13,
    "halts": 14,
    "liquidity": 16,
}

STUDENT_T_DF = 6.0
TRADING_DAYS_YEAR = 252
BETA_WINDOW = 252
BETA_STEP = 21

CUSTOM_BENCHMARKS = ("BENCH_EQ", "BENCH_LC")
_LARGE_CAP_BUCKETS = ("mega", "large")

DELISTING_REASONS = ("bankrupt", "acquired", "voluntary", "merged")

_CAP_BUCKET_ORDER = {b: i for i, b in enumerate(reference.CAP_BUCKETS)}
_SECTOR_ORDER = {s: i for i, s in enumerate(reference.SECTOR_IDS)}

# Annualized idiosyncratic vol base by cap bucket.
_IDIO_VOL_BASE = {"mega": 0.30, "large": 0.34, "mid": 0.42, "small": 0.52, "micro": 0.64}
# Rough daily dollar-volume (ADV) base by cap bucket, USD.
_ADV_BASE = {"mega": 5.0e8, "large": 1.5e8, "mid": 4.0e7, "small": 8.0e6, "micro": 1.0e6}
_BASE_PRICE = {"mega": 420.0, "large": 140.0, "mid": 48.0, "small": 14.0, "micro": 4.0}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _t_shocks(r: np.random.Generator, shape: int | tuple[int, ...]) -> np.ndarray:
    """Student-t shocks scaled to unit variance (fat tails)."""
    scale = math.sqrt(STUDENT_T_DF / (STUDENT_T_DF - 2.0))
    return r.standard_t(STUDENT_T_DF, size=shape) / scale


def _tr_levels(
    open_: np.ndarray,
    close: np.ndarray,
    adj: np.ndarray,
    div: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """``(open_tr, tr_index)`` per the pricing convention in ``frames.py``.

    ``tr_index[0] = 100`` and ``open_tr[0] = 100 * open / close``; for t >= 1::

        open_tr(t) = tr_index(t-1) * adj(t) * (open(t) + div(t))
                     / (adj(t-1) * close(t-1))
        tr_index(t) = open_tr(t) * close(t) / open(t)

    which makes ``tr_index(t) = tr_index(t-1) * K(t)`` with
    ``K(t) = adj(t) * (open(t) + div(t)) * close(t) /
    (adj(t-1) * close(t-1) * open(t))`` — computed via ``cumsum(log K)``.
    """
    n = len(close)
    open_tr = np.empty(n, dtype=np.float64)
    tr_index = np.empty(n, dtype=np.float64)
    open_tr[0] = 100.0 * open_[0] / close[0]
    tr_index[0] = 100.0
    if n > 1:
        prev_close = close[:-1]
        prev_adj = adj[:-1]
        a = adj[1:] * (open_[1:] + div[1:]) / (prev_adj * prev_close)
        k = a * close[1:] / open_[1:]
        tr_index[1:] = 100.0 * np.exp(np.cumsum(np.log(k)))
        open_tr[1:] = tr_index[:-1] * a
    return open_tr, tr_index


# --------------------------------------------------------------------------- #
# Delistings
# --------------------------------------------------------------------------- #


class _DelistPlan(NamedTuple):
    frame: pl.DataFrame
    last_idx: dict[str, int]  # security_id -> last session index (inclusive)


def _session_index(dates64: np.ndarray, d: date) -> int:
    """First session index with ``session_date >= d``."""
    return int(np.searchsorted(dates64, np.datetime64(d)))


def _last_session_index(dates64: np.ndarray, d: date) -> int:
    """Last session index with ``session_date <= d``."""
    return int(np.searchsorted(dates64, np.datetime64(d), side="right") - 1)


def plan_delistings(
    profile: Profile,
    specs: list[SecuritySpec],
    dates: list[date],
    r: np.random.Generator,
) -> _DelistPlan:
    """Assign terminal delistings; guarantees >= 1 bankruptcy per profile."""
    n = len(specs)
    n_sessions = len(dates)
    dates64 = np.array(dates, dtype="datetime64[D]")

    def list_idx(spec: SecuritySpec) -> int:
        if spec.listing_date <= dates[0]:
            return 0
        return min(_session_index(dates64, spec.listing_date), n_sessions - 1)

    # Target counts: ~2% bankrupt, ~4% acquired, ~3% voluntary, ~1% merged.
    counts = {
        "bankrupt": max(1, int(round(n * 0.02))),
        "acquired": max(1, int(round(n * 0.04))),
        "voluntary": max(1, int(round(n * 0.03))),
        "merged": int(round(n * 0.01)),
    }
    reasons: list[str] = []
    for reason, count in counts.items():
        reasons += [reason] * count
    reasons = list(r.permutation(np.array(reasons, dtype=object)))

    order = r.permutation(n)
    rows: list[tuple[str, date, float, str]] = []
    last_idx: dict[str, int] = {}
    reason_iter = iter(reasons)
    for i in order:
        spec = specs[int(i)]
        a = list_idx(spec)
        lo = min(a + 60, n_sessions - 1)
        if lo >= n_sessions - 1:
            continue
        try:
            reason = str(next(reason_iter))
        except StopIteration:
            break
        j = int(r.integers(lo, n_sessions))
        if reason == "bankrupt":
            final_return = -1.0
        elif reason == "acquired":
            final_return = float(r.uniform(0.05, 0.30))
        elif reason == "voluntary":
            final_return = float(r.uniform(-0.15, -0.02))
        else:  # merged
            final_return = float(r.uniform(-0.05, 0.15))
        rows.append((spec.security_id, dates[j], final_return, reason))
        last_idx[spec.security_id] = j

    frame = pl.DataFrame(
        rows,
        schema={
            "security_id": pl.String,
            "delisting_date": pl.Date,
            "final_return": pl.Float64,
            "reason": pl.String,
        },
        orient="row",
    ).sort("security_id")
    return _DelistPlan(frame=frame, last_idx=last_idx)


# --------------------------------------------------------------------------- #
# Market factor simulation
# --------------------------------------------------------------------------- #


def _garch_market_returns(r: np.random.Generator, n: int) -> np.ndarray:
    """Daily market factor returns: GARCH(1,1) variance with t shocks."""
    alpha, beta = 0.08, 0.90
    target_var = 0.010**2
    omega = target_var * (1.0 - alpha - beta)
    z = _t_shocks(r, n)
    var = np.empty(n)
    var[0] = target_var
    eps = np.empty(n)
    for t in range(n):
        eps[t] = math.sqrt(var[t]) * z[t]
        if t + 1 < n:
            var[t + 1] = omega + alpha * eps[t] ** 2 + beta * var[t]
    return eps


def _sector_idio(r: np.random.Generator, n_sectors: int, n: int) -> np.ndarray:
    """Sector idiosyncratic returns with mild AR(1) persistence, shape (S, T)."""
    phi, sd = 0.20, 0.007
    z = _t_shocks(r, (n_sectors, n))
    out = np.empty((n_sectors, n))
    out[:, 0] = sd * z[:, 0]
    innov = sd * math.sqrt(1 - phi * phi)
    for t in range(1, n):
        out[:, t] = phi * out[:, t - 1] + innov * z[:, t]
    return out


def _style_factors(r: np.random.Generator, n_factors: int, n: int) -> np.ndarray:
    """Size/value/momentum style factor daily returns, shape (F, T)."""
    return 0.005 * _t_shocks(r, (n_factors, n))


# --------------------------------------------------------------------------- #
# Corporate-action planning (per security)
# --------------------------------------------------------------------------- #


class _Actions(NamedTuple):
    splits: list[tuple[int, float]]  # (session idx, adj multiplier)
    dividends: list[tuple[int, float]]  # (session idx, yield per event)
    halts: set[int]  # session indices (absolute)


def _plan_actions(
    r: np.random.Generator,
    a: int,
    b: int,
    cap_bucket: str,
) -> _Actions:
    """Splits (2:1, 3:1, 1:2), quarterly-ish dividends, occasional halt stretches."""
    splits: list[tuple[int, float]] = []
    dividends: list[tuple[int, float]] = []
    halts: set[int] = set()
    span = b - a
    if span < 40:
        return _Actions(splits, dividends, halts)

    # Halts: ~12% of securities, one stretch of 1-5 sessions.
    if r.random() < 0.12:
        length = int(r.integers(1, 6))
        start = int(r.integers(a + 20, max(a + 21, b - length - 10)))
        halts = set(range(start, min(start + length, b)))

    def free(j: int) -> bool:
        return a + 10 <= j <= b - 10 and j not in halts and all(abs(j - s) > 3 for s, _ in splits)

    # Splits: ~18% of securities, 1-2 events.
    if r.random() < 0.18:
        n_splits = 1 if r.random() < 0.75 else 2
        for _ in range(n_splits):
            for _attempt in range(20):
                j = int(r.integers(a + 30, b - 10))
                if free(j):
                    ratio = float(r.choice([2.0, 3.0, 0.5], p=[0.45, 0.25, 0.30]))
                    splits.append((j, ratio))
                    break

    # Dividends: annual yield 0-3%, ~quarterly frequency.
    annual_yield = float(r.uniform(0.0, 0.03))
    if annual_yield > 0.002 and span > 80:
        period = 63
        offset = int(r.integers(0, period))
        j = a + 10 + offset
        while j <= b - 10:
            if free(j):
                dividends.append((j, annual_yield / 4.0 * float(r.uniform(0.8, 1.25))))
            j += period

    return _Actions(splits, dividends, halts)


# --------------------------------------------------------------------------- #
# Per-security price path
# --------------------------------------------------------------------------- #


class _SecData(NamedTuple):
    spec: SecuritySpec
    a: int
    b: int
    open_: np.ndarray
    close: np.ndarray
    high: np.ndarray
    low: np.ndarray
    adj: np.ndarray
    div: np.ndarray
    tr_index: np.ndarray
    halted: np.ndarray
    open_factor: np.ndarray  # A[t]: overnight TR factor vs previous close
    close_factor: np.ndarray  # K[t]: close TR factor vs previous close
    cap_rel: np.ndarray  # market_cap * q/q0 (PIT weight numerator)
    segments: list[tuple[int, int, int, int]]  # (start, end, sector_idx, cap_idx) abs idx


def _first_session(spec: SecuritySpec, dates: list[date], dates64: np.ndarray) -> int:
    if spec.listing_date <= dates[0]:
        return 0
    return min(_session_index(dates64, spec.listing_date), len(dates) - 1)


def _build_security_path(
    spec: SecuritySpec,
    a: int,
    b: int,
    rets: np.ndarray,  # R[i, a:b+1] total simple returns
    actions: _Actions,
    base_price: float,
    r: np.random.Generator,
) -> _SecData:
    """Build raw OHLC + adj/div/tr_index for one security."""
    length = b - a + 1
    # adj_factor: forward-cumulative, 1.0 at listing.
    adj = np.ones(length)
    for j, ratio in actions.splits:
        adj[j - a :] *= ratio
    split_factor = np.ones(length)
    split_factor[1:] = adj[1:] / adj[:-1]

    div_yield = {j - a: y for j, y in actions.dividends}
    halt_rel = {j - a for j in actions.halts}

    q = np.empty(length)  # split-continuous close (raw * adj)
    q[0] = base_price * float(np.exp(r.normal(0.0, 0.35)))
    div_arr = np.zeros(length)
    clipped = np.clip(rets, -0.5, 0.5)
    # Halted sessions do not trade: flat price, no drift.
    for h in halt_rel:
        if 0 < h < length:
            clipped[h] = 0.0
    for t in range(1, length):
        q[t] = q[t - 1] * (1.0 + clipped[t])
        if t in div_yield:
            raw_prev_close = q[t - 1] / adj[t - 1]
            div_cash = raw_prev_close * div_yield[t]
            div_arr[t] = div_cash
            q[t] -= div_cash * adj[t]
    q = np.maximum(q, 1e-6)

    close = q / adj
    open_ = np.empty(length)
    gaps = r.normal(0.0, 0.004, size=length)
    open_[0] = close[0] * (1.0 + float(r.normal(0.0, 0.01)))
    for t in range(1, length):
        open_[t] = close[t - 1] * (1.0 + gaps[t]) / split_factor[t] - div_arr[t]
    open_ = np.maximum(open_, 1e-6)

    high = np.maximum(open_, close) * (1.0 + r.random(length) * 0.01)
    low = np.minimum(open_, close) * (1.0 - r.random(length) * 0.01)

    halted = np.zeros(length, dtype=bool)
    for h in halt_rel:
        if h == 0:
            continue
        halted[h] = True
        prev_close = close[h - 1]
        open_[h] = prev_close
        close[h] = prev_close
        # q must stay consistent for later sessions: q[h] = prev_close * adj[h].
        q[h] = prev_close * adj[h]
        high[h] = prev_close
        low[h] = prev_close
    # NOTE: for multi-day halts each day re-reads close[h-1] which is already
    # the flat price, so the stretch stays flat automatically.

    _, tr_index = _tr_levels(open_, close, adj, div_arr)

    # Benchmark factors: A = overnight TR factor, K = close TR factor.
    open_factor = np.ones(length)
    close_factor = np.empty(length)
    close_factor[0] = close[0] / open_[0]
    if length > 1:
        open_factor[1:] = adj[1:] * (open_[1:] + div_arr[1:]) / (adj[:-1] * close[:-1])
        close_factor[1:] = open_factor[1:] * close[1:] / open_[1:]

    cap_rel = spec.market_cap * (q / q[0])

    data = _SecData(
        spec=spec,
        a=a,
        b=b,
        open_=open_,
        close=close,
        high=high,
        low=low,
        adj=adj,
        div=div_arr,
        tr_index=tr_index,
        halted=halted,
        open_factor=open_factor,
        close_factor=close_factor,
        cap_rel=cap_rel,
        segments=[],
    )
    return data


# --------------------------------------------------------------------------- #
# PIT membership segments from classification spells
# --------------------------------------------------------------------------- #


def _membership_segments(
    classification: pl.DataFrame,
    specs: list[SecuritySpec],
    a_by_sec: dict[str, int],
    b_by_sec: dict[str, int],
    dates64: np.ndarray,
) -> dict[str, list[tuple[int, int, int, int]]]:
    """Per security: ``(start, end, sector_idx, cap_idx)`` absolute session spans."""
    out: dict[str, list[tuple[int, int, int, int]]] = {s.security_id: [] for s in specs}
    rows = (
        classification.sort("security_id", "from_date")
        .select("security_id", "from_date", "to_date", "sector_id", "cap_bucket")
        .iter_rows()
    )
    for sec_id, from_d, to_d, sector_id, cap_bucket in rows:
        if sec_id not in out:
            continue
        a = a_by_sec[sec_id]
        b = b_by_sec[sec_id]
        start = max(_session_index(dates64, from_d), a)
        end = b if to_d is None else min(_last_session_index(dates64, to_d), b)
        end = min(end, b)
        if end < start:
            continue
        out[sec_id].append(
            (start, end, _SECTOR_ORDER[str(sector_id)], _CAP_BUCKET_ORDER[str(cap_bucket)])
        )
    return out


_date_min = date.min


# --------------------------------------------------------------------------- #
# Frame assembly helpers
# --------------------------------------------------------------------------- #


def frame_checksum(frame: pl.DataFrame) -> str:
    """Canonical sha256 over a frame's sorted rows (CSV serialization)."""
    ordered = frame.sort(by=list(frame.columns))
    buf = io.BytesIO()
    ordered.write_csv(buf)
    return hashlib.sha256(buf.getvalue()).hexdigest()


def _empty_frame(columns: Mapping[str, "pl.DataType | type[pl.DataType]"]) -> pl.DataFrame:
    return pl.DataFrame(schema=dict(columns))


# --------------------------------------------------------------------------- #
# Top-level generation
# --------------------------------------------------------------------------- #


@dataclass
class GeneratedData:
    """All generated frames plus generation metadata."""

    profile: Profile
    seed: int
    dates: list[date]
    frames: dict[str, pl.DataFrame] = field(default_factory=dict)

    def manifest(self) -> dict[str, object]:
        """Row counts, session counts and per-frame checksums."""
        return {
            "profile": self.profile.name,
            "seed": self.seed,
            "session_count": len(self.dates),
            "target_sessions": self.profile.target_sessions,
            "frames": {
                name: {"rows": frame.height, "checksum": frame_checksum(frame)}
                for name, frame in sorted(self.frames.items())
            },
        }


def generate(seed: int | None = None, profile: str | Profile = "small") -> GeneratedData:
    """Generate the full synthetic dataset for ``(seed, profile)``."""
    prof = load_profile(profile) if isinstance(profile, str | Path) else profile
    effective_seed = prof.seed if seed is None else seed
    offset = reference.profile_seed_offset(prof.name)

    def rng_for(tag: str) -> np.random.Generator:
        return reference.rng(effective_seed, _STREAM[tag] + offset)

    dates = reference.session_dates(prof)
    n_sessions = len(dates)
    dates64 = np.array(dates, dtype="datetime64[D]")

    # --- reference data ---------------------------------------------------- #
    specs = reference.security_specs(prof, rng_for("specs"), dates)
    firms = reference.firm_frame(prof, rng_for("firms"))
    analysts = reference.analyst_frame(prof, rng_for("analysts"), firms["firm_id"].to_list())
    employment = reference.employment_frame(prof, analysts, firms, rng_for("employment"))
    # analyst.firm_id = final employer (last employment spell).
    final_firm = (
        employment.sort("analyst_id", "from_date")
        .group_by("analyst_id", maintain_order=True)
        .agg(pl.col("firm_id").last().alias("firm_id"))
    )
    analysts = analysts.drop("firm_id").join(final_firm, on="analyst_id")
    delist_plan = plan_delistings(prof, specs, dates, rng_for("delistings"))
    delistings = delist_plan.frame
    classification = reference.classification_frame(
        prof, specs, delistings, rng_for("classifications")
    )
    identifiers = reference.identifier_frame(specs, delistings)
    coverage = reference.coverage_frame(
        prof, specs, analysts, employment, delistings, rng_for("coverage")
    )

    # --- market factors ---------------------------------------------------- #
    rf = rng_for("factors")
    market_ret = _garch_market_returns(rf, n_sessions)
    sector_idio = _sector_idio(rf, len(reference.SECTOR_IDS), n_sessions)
    style_ret = _style_factors(rf, 3, n_sessions)

    # --- per-security parameters ------------------------------------------- #
    rp = rng_for("security_params")
    n_sec = len(specs)
    sector_idx_of = (
        np.array([_SECTOR_ORDER[s.sector_id] for s in specs]) if specs else np.array([], dtype=int)
    )
    beta_true = rp.uniform(0.6, 1.6, size=n_sec)
    sector_load = rp.uniform(0.3, 0.8, size=n_sec)
    cap_arr = np.array([_CAP_BUCKET_ORDER[s.cap_bucket] for s in specs])
    idio_vol = np.array(
        [_IDIO_VOL_BASE[s.cap_bucket] + float(rp.uniform(0.0, 0.15)) for s in specs]
    ) / math.sqrt(TRADING_DAYS_YEAR)
    tilts = rp.uniform(-0.30, 0.30, size=(n_sec, 3))
    # Small caps carry a positive size-factor tilt.
    tilts[cap_arr >= _CAP_BUCKET_ORDER["small"], 0] += 0.15
    idio = idio_vol[:, None] * _t_shocks(rp, (n_sec, n_sessions))

    rets = (
        beta_true[:, None] * market_ret[None, :]
        + sector_load[:, None] * sector_idio[sector_idx_of, :]
        + tilts @ style_ret
        + idio
    )

    # --- per-security paths ------------------------------------------------- #
    ra = rng_for("actions")
    rh = rng_for("halts")
    sec_data: list[_SecData] = []
    corporate_rows: list[dict[str, object]] = []
    ca_counter = 0
    a_by_sec: dict[str, int] = {}
    b_by_sec: dict[str, int] = {}

    for i, spec in enumerate(specs):
        a = _first_session(spec, dates, dates64)
        b = delist_plan.last_idx.get(spec.security_id, n_sessions - 1)
        b = min(b, n_sessions - 1)
        if b <= a:
            b = min(a + 1, n_sessions - 1)
        a_by_sec[spec.security_id] = a
        b_by_sec[spec.security_id] = b
        # Draw actions from dedicated streams (order fixed per security).
        actions = _plan_actions(ra, a, b, spec.cap_bucket)
        base_price = _BASE_PRICE[spec.cap_bucket] * float(np.exp(ra.normal(0.0, 0.3)))
        path_rng = rh  # gaps/high-low noise
        data = _build_security_path(spec, a, b, rets[i, a : b + 1], actions, base_price, path_rng)
        # Corporate action audit rows.
        for j, ratio in actions.splits:
            ca_counter += 1
            label = (
                f"{ratio:g}:1 split" if ratio >= 1 else f"1:{int(round(1 / ratio))} reverse split"
            )
            corporate_rows.append(
                {
                    "action_id": f"CA_{ca_counter:06d}",
                    "security_id": spec.security_id,
                    "session_date": dates[j],
                    "action_type": "split",
                    "ratio": ratio,
                    "cash_amount": None,
                    "notes": label,
                }
            )
        for j, _y in actions.dividends:
            ca_counter += 1
            corporate_rows.append(
                {
                    "action_id": f"CA_{ca_counter:06d}",
                    "security_id": spec.security_id,
                    "session_date": dates[j],
                    "action_type": "cash_dividend",
                    "ratio": None,
                    "cash_amount": float(data.div[j - a]),
                    "notes": None,
                }
            )
        sec_data.append(data)

    # --- PIT membership ------------------------------------------------------ #
    segments_by_sec = _membership_segments(classification, specs, a_by_sec, b_by_sec, dates64)
    sec_data = [
        _SecData(**{**d._asdict(), "segments": segments_by_sec[d.spec.security_id]})
        for d in sec_data
    ]

    # --- security_daily ------------------------------------------------------ #
    sd = _assemble_security_daily(sec_data, dates64)

    # --- benchmarks ------------------------------------------------------------ #
    benchmark_daily, benchmark_defs, broad_close_factor = _assemble_benchmarks(
        sec_data, dates, n_sessions
    )

    # --- security_beta --------------------------------------------------------- #
    beta_frame = _assemble_betas(sec_data, broad_close_factor, dates)

    # --- auxiliary market frames ----------------------------------------------- #
    factor_frame = _factor_frame(market_ret, sector_idio, dates)
    fx_frame = _fx_frame(dates)
    liquidity_frame = _liquidity_frame(sec_data, dates, rng_for("liquidity"))
    consensus_frame = _empty_frame(
        {
            "security_id": pl.String,
            "session_date": pl.Date,
            "n_ratings": pl.Int64,
            "consensus_score": pl.Float64,
            "target_price": pl.Float64,
            "target_currency": pl.String,
        }
    )
    corporate_schema = {
        "action_id": pl.String,
        "security_id": pl.String,
        "session_date": pl.Date,
        "action_type": pl.String,
        "ratio": pl.Float64,
        "cash_amount": pl.Float64,
        "notes": pl.String,
    }
    corporate_frame = (
        pl.DataFrame(corporate_rows, schema=corporate_schema).sort("action_id")
        if corporate_rows
        else _empty_frame(corporate_schema)
    )

    frames: dict[str, pl.DataFrame] = {
        # Contract frames.
        "sessions": reference.session_frame(prof),
        "coverage_spells": coverage,
        "employment_spells": employment,
        "security_classifications": classification,
        "security_daily": sd,
        "benchmark_daily": benchmark_daily,
        "security_beta": beta_frame,
        "delistings": delistings,
        "fx_daily": fx_frame,
        # Aux / store-shaped frames.
        "exchange": reference.exchange_frame(),
        "sectors": reference.sector_frame(),
        "firms": firms,
        "analysts": analysts,
        "securities": reference.security_frame(specs),
        "security_identifiers": identifiers,
        "security_attributes": reference.security_attribute_frame(specs),
        "corporate_actions": corporate_frame,
        "benchmark_def": benchmark_defs,
        "factor_return_daily": factor_frame,
        "liquidity_daily": liquidity_frame,
        "consensus_daily": consensus_frame,
    }
    return GeneratedData(profile=prof, seed=effective_seed, dates=dates, frames=frames)


def synth_manifest(seed: int, profile: str | Profile = "small") -> dict[str, object]:
    """``synth_manifest(seed, profile)``: row counts + checksums for a run."""
    return generate(seed=seed, profile=profile).manifest()


# --------------------------------------------------------------------------- #
# Frame assembly
# --------------------------------------------------------------------------- #


def _assemble_security_daily(sec_data: list[_SecData], dates64: np.ndarray) -> pl.DataFrame:
    """``SECURITY_DAILY_FRAME`` sorted by (security_id, session_date)."""
    sec_ids: list[np.ndarray] = []
    sess: list[np.ndarray] = []
    cols: dict[str, list[np.ndarray]] = {
        "open": [],
        "close": [],
        "high": [],
        "low": [],
        "adj_factor": [],
        "cash_dividend": [],
        "tr_index": [],
        "is_halted": [],
    }
    for d in sec_data:
        length = d.b - d.a + 1
        sec_ids.append(np.full(length, d.spec.security_id))
        sess.append(dates64[d.a : d.b + 1])
        cols["open"].append(d.open_)
        cols["close"].append(d.close)
        cols["high"].append(d.high)
        cols["low"].append(d.low)
        cols["adj_factor"].append(d.adj)
        cols["cash_dividend"].append(d.div)
        cols["tr_index"].append(d.tr_index)
        cols["is_halted"].append(d.halted)

    frame = pl.DataFrame(
        {
            "security_id": pl.Series(np.concatenate(sec_ids)),
            "session_date": pl.Series(np.concatenate(sess)),
            "open": pl.Series(np.concatenate(cols["open"])),
            "close": pl.Series(np.concatenate(cols["close"])),
            "high": pl.Series(np.concatenate(cols["high"])),
            "low": pl.Series(np.concatenate(cols["low"])),
            "adj_factor": pl.Series(np.concatenate(cols["adj_factor"])),
            "cash_dividend": pl.Series(np.concatenate(cols["cash_dividend"])),
            "tr_index": pl.Series(np.concatenate(cols["tr_index"])),
            "is_halted": pl.Series(np.concatenate(cols["is_halted"])),
        }
    )
    # cash_dividend is null (not 0.0) on non-ex-dates.
    return frame.with_columns(
        pl.when(pl.col("cash_dividend") == 0.0)
        .then(None)
        .otherwise(pl.col("cash_dividend"))
        .alias("cash_dividend")
    )


def _benchmark_levels(
    open_factor: np.ndarray,
    close_factor: np.ndarray,
    valid: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Recursion shared by all benchmarks.

    ``open_factor``/``close_factor`` are the per-session weighted TR factors
    (already normalized by weight). Returns ``(open_level, close_level,
    close_factor_series)`` aligned to all sessions; invalid sessions carry the
    previous level flat.
    """
    n = len(valid)
    ot = np.where(valid, open_factor, 1.0)
    gt = np.where(valid, close_factor, 1.0)
    open_level = np.ones(n)
    close_level = np.ones(n)
    first = int(np.argmax(valid)) if valid.any() else n
    if first >= n:
        return open_level, close_level, gt
    close_level[first] = 100.0
    open_level[first] = 100.0 * ot[first] / gt[first] if gt[first] != 0 else 100.0
    for t in range(first + 1, n):
        open_level[t] = close_level[t - 1] * ot[t]
        close_level[t] = close_level[t - 1] * gt[t]
    return open_level, close_level, gt


def _assemble_benchmarks(
    sec_data: list[_SecData],
    dates: list[date],
    n_sessions: int,
) -> tuple[pl.DataFrame, pl.DataFrame, np.ndarray]:
    """``BENCHMARK_DAILY_FRAME`` + ``benchmark_def`` aux + broad close factors."""
    benchmark_ids = (
        [MARKET_BENCHMARK_ID]
        + [sector_benchmark_id(s) for s in reference.SECTOR_IDS]
        + list(CUSTOM_BENCHMARKS)
    )
    acc = {
        bid: {
            "w": np.zeros(n_sessions),
            "o": np.zeros(n_sessions),
            "g": np.zeros(n_sessions),
        }
        for bid in benchmark_ids
    }

    for d in sec_data:
        for start, end, sector_i, cap_i in d.segments:
            # Local indices into the per-security arrays, clipped to range.
            ls = max(start - d.a, 0)
            le = min(end - d.a, len(d.cap_rel) - 1)
            if le < ls:
                continue
            local = slice(ls, le + 1)
            abs_start = d.a + ls
            abs_end = d.a + le
            sess = slice(abs_start, abs_end + 1)
            w_cap = d.cap_rel[local]
            w_eq = np.ones(le - ls + 1)
            contribs: list[tuple[str, np.ndarray]] = [
                (MARKET_BENCHMARK_ID, w_cap),
                ("BENCH_EQ", w_eq),
            ]
            sector_bid = sector_benchmark_id(reference.SECTOR_IDS[sector_i])
            contribs.append((sector_bid, w_cap))
            if cap_i <= _CAP_BUCKET_ORDER["large"]:
                contribs.append(("BENCH_LC", w_eq))
            for bid, w in contribs:
                a_ = acc[bid]
                a_["w"][sess] += w
                a_["o"][sess] += w * d.open_factor[local]
                a_["g"][sess] += w * d.close_factor[local]

    rows_bid: list[str] = []
    rows_date: list[date] = []
    rows_open: list[float] = []
    rows_close: list[float] = []
    broad_close_factor = np.ones(n_sessions)
    defs: list[dict[str, object]] = []

    for bid in benchmark_ids:
        a_ = acc[bid]
        valid = a_["w"] > 1e-12
        with np.errstate(divide="ignore", invalid="ignore"):
            open_factor = np.where(valid, a_["o"] / np.where(valid, a_["w"], 1.0), 1.0)
            close_factor = np.where(valid, a_["g"] / np.where(valid, a_["w"], 1.0), 1.0)
        open_level, close_level, gt = _benchmark_levels(open_factor, close_factor, valid)
        first = int(np.argmax(valid)) if valid.any() else n_sessions
        for t in range(first, n_sessions):
            rows_bid.append(bid)
            rows_date.append(dates[t])
            rows_open.append(float(open_level[t]))
            rows_close.append(float(close_level[t]))
        if bid == MARKET_BENCHMARK_ID:
            broad_close_factor = gt

    emitted = set(rows_bid)
    for bid in benchmark_ids:
        if bid not in emitted:
            continue
        if bid == MARKET_BENCHMARK_ID:
            defs.append({"benchmark_id": bid, "kind": "market", "rule_json": None})
        elif bid in CUSTOM_BENCHMARKS:
            rule = "equal_weight" if bid == "BENCH_EQ" else "equal_weight_large_cap"
            defs.append(
                {"benchmark_id": bid, "kind": "custom", "rule_json": json.dumps({"rule": rule})}
            )
        else:
            sector_id = bid.removeprefix("SECTOR_")
            defs.append(
                {
                    "benchmark_id": bid,
                    "kind": "sector",
                    "rule_json": json.dumps({"sector_id": sector_id}),
                }
            )

    daily = pl.DataFrame(
        {
            "benchmark_id": rows_bid,
            "session_date": rows_date,
            "open_level": rows_open,
            "close_level": rows_close,
        },
        schema={
            "benchmark_id": pl.String,
            "session_date": pl.Date,
            "open_level": pl.Float64,
            "close_level": pl.Float64,
        },
    ).sort("benchmark_id", "session_date")
    defs_frame = pl.DataFrame(
        defs,
        schema={"benchmark_id": pl.String, "kind": pl.String, "rule_json": pl.String},
    ).sort("benchmark_id")
    return daily, defs_frame, broad_close_factor


def _assemble_betas(
    sec_data: list[_SecData],
    broad_close_factor: np.ndarray,
    dates: list[date],
) -> pl.DataFrame:
    """``BETA_FRAME``: trailing ~1y beta vs IDX_BROAD, monthly snapshots."""
    bench_ret = broad_close_factor - 1.0
    rows_sec: list[str] = []
    rows_date: list[date] = []
    rows_beta: list[float] = []
    for d in sec_data:
        sec_ret_local = d.close_factor - 1.0  # aligned to sessions a..b
        j = d.a + BETA_WINDOW
        while j <= d.b:
            # Window = the BETA_WINDOW sessions ending at (and including) j.
            ls = j - (BETA_WINDOW - 1) - d.a  # local index of window start
            le = j - d.a  # local index of as_of session
            if ls >= 1:  # skip the first-day intraday factor at local 0
                x = sec_ret_local[ls : le + 1]
                y = bench_ret[j - (BETA_WINDOW - 1) : j + 1]
                if len(x) == len(y) == BETA_WINDOW:
                    y_var = float(np.dot(y - y.mean(), y - y.mean()))
                    if y_var > 1e-12:
                        beta = float(np.dot(x - x.mean(), y - y.mean()) / y_var)
                        beta = min(max(beta, 0.05), 2.5)
                        rows_sec.append(d.spec.security_id)
                        rows_date.append(dates[j])
                        rows_beta.append(beta)
            j += BETA_STEP
    return pl.DataFrame(
        {
            "security_id": rows_sec,
            "as_of_date": rows_date,
            "beta": rows_beta,
            "window_days": [BETA_WINDOW] * len(rows_beta),
        },
        schema={
            "security_id": pl.String,
            "as_of_date": pl.Date,
            "beta": pl.Float64,
            "window_days": pl.Int64,
        },
    ).sort("security_id", "as_of_date")


def _factor_frame(
    market_ret: np.ndarray, sector_idio: np.ndarray, dates: list[date]
) -> pl.DataFrame:
    """``factor_return_daily`` aux: MKT + one composite return per sector."""
    rho = 0.55
    factor_ids: list[str] = ["MKT"] + list(reference.SECTOR_IDS)
    factor_rets = [market_ret] + [
        rho * market_ret + math.sqrt(1 - rho * rho) * sector_idio[i]
        for i in range(len(reference.SECTOR_IDS))
    ]
    n = len(dates)
    ids = np.repeat(np.array(factor_ids), n)
    sess = np.tile(np.array(dates, dtype="datetime64[D]"), len(factor_ids))
    vals = np.concatenate(factor_rets)
    return pl.DataFrame(
        {
            "factor_id": pl.Series(ids),
            "session_date": pl.Series(sess),
            "factor_return": pl.Series(vals),
        },
        schema={"factor_id": pl.String, "session_date": pl.Date, "factor_return": pl.Float64},
    ).sort("factor_id", "session_date")


def _fx_frame(dates: list[date]) -> pl.DataFrame:
    """``FX_DAILY_FRAME``: single USD base -> USD/USD identity rows."""
    return pl.DataFrame(
        {
            "from_ccy": ["USD"] * len(dates),
            "to_ccy": ["USD"] * len(dates),
            "rate_date": dates,
            "rate": [1.0] * len(dates),
        },
        schema={
            "from_ccy": pl.String,
            "to_ccy": pl.String,
            "rate_date": pl.Date,
            "rate": pl.Float64,
        },
    )


_FREE_FLOAT_RANGE = {
    "mega": (0.60, 0.95),
    "large": (0.45, 0.90),
    "mid": (0.35, 0.85),
    "small": (0.25, 0.75),
    "micro": (0.20, 0.60),
}


def _liquidity_frame(
    sec_data: list[_SecData], dates: list[date], r: np.random.Generator
) -> pl.DataFrame:
    """``liquidity_daily`` aux: ADV(20) proxy + free float per session."""
    dates64 = np.array(dates, dtype="datetime64[D]")
    rows_sec: list[np.ndarray] = []
    rows_date: list[np.ndarray] = []
    rows_adv: list[np.ndarray] = []
    rows_ff: list[np.ndarray] = []
    for d in sec_data:
        length = d.b - d.a + 1
        daily = _ADV_BASE[d.spec.cap_bucket] * np.exp(r.normal(0.0, 0.35, size=length))
        cs = np.concatenate([[0.0], np.cumsum(daily)])
        idx = np.arange(length)
        lo = np.maximum(0, idx - 19)
        adv20 = (cs[idx + 1] - cs[lo]) / (idx + 1 - lo)
        ff_lo, ff_hi = _FREE_FLOAT_RANGE[d.spec.cap_bucket]
        free_float = float(r.uniform(ff_lo, ff_hi))
        rows_sec.append(np.full(length, d.spec.security_id))
        rows_date.append(dates64[d.a : d.b + 1])
        rows_adv.append(adv20)
        rows_ff.append(np.full(length, free_float))
    return pl.DataFrame(
        {
            "security_id": pl.Series(np.concatenate(rows_sec)),
            "session_date": pl.Series(np.concatenate(rows_date)),
            "adv_20d": pl.Series(np.concatenate(rows_adv)),
            "free_float": pl.Series(np.concatenate(rows_ff)),
        },
        schema={
            "security_id": pl.String,
            "session_date": pl.Date,
            "adv_20d": pl.Float64,
            "free_float": pl.Float64,
        },
    ).sort("security_id", "session_date")
