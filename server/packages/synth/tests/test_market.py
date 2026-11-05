"""Tests for market-data generation: TR convention, delistings, benchmarks."""

from datetime import date

import numpy as np
import polars as pl

from callscore_core.frames import MARKET_BENCHMARK_ID
from callscore_synth import market
from callscore_synth.profiles import Profile

# A ~2y profile: long enough for beta windows, small enough to stay fast.
PROFILE = Profile(
    name="testmkt",
    seed=2024,
    securities=40,
    analysts=10,
    firms=3,
    start_date=date(2023, 1, 1),
    end_date=date(2024, 12, 31),
    target_sessions=None,
)


def _naive_tr_index(
    open_: np.ndarray, close: np.ndarray, adj: np.ndarray, div: np.ndarray
) -> np.ndarray:
    """Reference implementation of the frames.py TR recursion, row by row."""
    n = len(close)
    tr = np.empty(n)
    tr[0] = 100.0
    for t in range(1, n):
        open_tr = tr[t - 1] * adj[t] * (open_[t] + div[t]) / (adj[t - 1] * close[t - 1])
        tr[t] = open_tr * close[t] / open_[t]
    return tr


def test_tr_levels_matches_naive_recursion() -> None:
    rng = np.random.default_rng(0)
    n = 300
    close = 50.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = close * (1.0 + rng.normal(0, 0.003, n))
    adj = np.ones(n)
    adj[100:] = 2.0  # a 2:1 split at t=100
    adj[200:] = 3.0  # and a 3:1 at t=200 -> forward-cumulative 6.0
    div = np.zeros(n)
    div[50] = 0.4
    div[150] = 0.2
    open_tr, tr_index = market._tr_levels(open_, close, adj, div)
    expected = _naive_tr_index(open_, close, adj, div)
    np.testing.assert_allclose(tr_index, expected, rtol=1e-10)
    # open_tr(t) * close(t) / open(t) == tr_index(t)
    np.testing.assert_allclose(open_tr * close / open_, tr_index, rtol=1e-10)
    # First session conventions.
    assert tr_index[0] == 100.0
    assert open_tr[0] == 100.0 * open_[0] / close[0]


def test_security_daily_tr_consistency_all_securities() -> None:
    data = market.generate(profile=PROFILE)
    sd = data.frames["security_daily"]
    assert sd.height > 0
    for sid in sd["security_id"].unique().to_list():
        f = sd.filter(pl.col("security_id") == sid).sort("session_date")
        open_ = f["open"].to_numpy()
        close = f["close"].to_numpy()
        adj = f["adj_factor"].to_numpy()
        div = f["cash_dividend"].fill_null(0.0).to_numpy()
        tr = f["tr_index"].to_numpy()
        expected = _naive_tr_index(open_, close, adj, div)
        np.testing.assert_allclose(tr, expected, rtol=1e-8)
        # Positivity and OHLC sanity.
        assert (open_ > 0).all() and (close > 0).all() and (adj > 0).all()
        assert (f["high"].to_numpy() >= np.maximum(open_, close) - 1e-9).all()
        assert (f["low"].to_numpy() <= np.minimum(open_, close) + 1e-9).all()
        assert tr[0] == 100.0


def test_halted_sessions_flat() -> None:
    data = market.generate(profile=PROFILE)
    sd = data.frames["security_daily"]
    halted = sd.filter(pl.col("is_halted"))
    assert halted.height > 0  # ~12% of 40 securities over 2y -> some halts
    for row in halted.iter_rows(named=True):
        assert row["open"] == row["close"]
        prev = (
            sd.filter(
                (pl.col("security_id") == row["security_id"])
                & (pl.col("session_date") < row["session_date"])
            )
            .sort("session_date")
            .tail(1)
        )
        assert prev.height == 1
        assert row["close"] == prev["close"][0]
        assert row["tr_index"] == prev["tr_index"][0]


def test_delistings_end_security_daily() -> None:
    data = market.generate(profile=PROFILE)
    sd = data.frames["security_daily"]
    delist = data.frames["delistings"]
    assert delist.height >= 1
    reasons = set(delist["reason"].to_list())
    assert "bankrupt" in reasons  # guaranteed in every profile
    for row in delist.iter_rows(named=True):
        last = sd.filter(pl.col("security_id") == row["security_id"])["session_date"].max()
        assert last == row["delisting_date"]
        bankrupt = row["reason"] == "bankrupt"
        if bankrupt:
            assert row["final_return"] == -1.0


def test_small_profile_has_bankruptcy() -> None:
    data = market.generate(profile="small")
    reasons = data.frames["delistings"]["reason"].to_list()
    assert "bankrupt" in reasons
    assert data.frames["delistings"].height >= 3


def test_benchmark_levels_positive_and_ordered() -> None:
    data = market.generate(profile=PROFILE)
    bm = data.frames["benchmark_daily"]
    assert bm.height > 0
    assert (bm["open_level"] > 0).all()
    assert (bm["close_level"] > 0).all()
    # IDX_BROAD exists and starts at ~100.
    broad = bm.filter(pl.col("benchmark_id") == MARKET_BENCHMARK_ID).sort("session_date")
    assert broad.height > 0
    assert abs(broad["close_level"][0] - 100.0) < 1e-6
    # Every sector benchmark id referenced by a classification exists in defs.
    defs = data.frames["benchmark_def"]
    ids = set(defs["benchmark_id"].to_list())
    assert MARKET_BENCHMARK_ID in ids
    assert {"BENCH_EQ", "BENCH_LC"} <= ids
    for bid in bm["benchmark_id"].unique().to_list():
        assert bid in ids


def test_security_beta_pit_monthly() -> None:
    data = market.generate(profile=PROFILE)
    beta = data.frames["security_beta"]
    assert beta.height > 0
    assert (beta["window_days"] == 252).all()
    # Monthly-ish spacing: as_of dates are session dates, ~21 sessions apart.
    sessions = set(data.frames["sessions"]["session_date"].to_list())
    assert set(beta["as_of_date"].to_list()) <= sessions
    for _sid, group in beta.group_by("security_id"):
        dates_sorted = sorted(group["as_of_date"].to_list())
        assert len(dates_sorted) == len(set(dates_sorted))
        # Betas within the clipped sane band.
        assert ((group["beta"] > 0) & (group["beta"] < 3)).all()


def test_corporate_actions_match_daily_frame() -> None:
    data = market.generate(profile=PROFILE)
    sd = data.frames["security_daily"]
    ca = data.frames["corporate_actions"]
    splits = ca.filter(pl.col("action_type") == "split")
    divs = ca.filter(pl.col("action_type") == "cash_dividend")
    # Every dividend row has a matching non-null cash_dividend in security_daily.
    daily_div = sd.filter(pl.col("cash_dividend").is_not_null()).select(
        "security_id", "session_date", "cash_dividend"
    )
    assert divs.height == daily_div.height
    joined = divs.join(
        daily_div,
        left_on=["security_id", "session_date"],
        right_on=["security_id", "session_date"],
        how="inner",
    )
    assert joined.height == divs.height
    # Split ex-dates change adj_factor.
    for row in splits.iter_rows(named=True):
        day = sd.filter(
            (pl.col("security_id") == row["security_id"])
            & (pl.col("session_date") == row["session_date"])
        ).row(0, named=True)
        prev = (
            sd.filter(
                (pl.col("security_id") == row["security_id"])
                & (pl.col("session_date") < row["session_date"])
            )
            .sort("session_date")
            .tail(1)
        ).row(0, named=True)
        assert abs(day["adj_factor"] / prev["adj_factor"] - row["ratio"]) < 1e-9


def test_manifest_determinism() -> None:
    m1 = market.synth_manifest(7, PROFILE)
    m2 = market.synth_manifest(7, PROFILE)
    assert m1 == m2
    m3 = market.synth_manifest(8, PROFILE)
    assert m1 != m3
    # Row counts and checksums present for every frame.
    for _name, info in m1["frames"].items():  # type: ignore[union-attr]
        assert isinstance(info["rows"], int) and info["rows"] >= 0
        assert len(info["checksum"]) == 64
    assert m1["session_count"] > 400  # type: ignore[operator]


def test_generate_and_manifest_agree() -> None:
    data = market.generate(seed=7, profile=PROFILE)
    manifest = data.manifest()
    assert manifest["frames"]["security_daily"]["rows"] == data.frames["security_daily"].height  # type: ignore[index]
