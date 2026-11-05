"""Integrity checks for the golden fixture (task T02).

These tests validate the fixture itself — contract conformance of every input
frame, the pricing-convention recursion in security_daily, calendar sanity,
coverage of expected outputs — plus a few hand-checkable behavioural anchors.
They never import callscore_engine.
"""

import json
from datetime import UTC, date, datetime

import polars as pl
import pytest
from load import (
    FIXTURE_DIR,
    INPUT_DIR,
    check_contract_frame,
    expected_frame,
    load_benchmark_with_gap,
    load_expected,
    load_frame,
    load_inputs,
)

from callscore_core.frames import FRAME_CONTRACTS

VARIANTS = ("default", "decay", "fixed")


@pytest.fixture(scope="module")
def inputs() -> dict[str, pl.DataFrame]:
    return load_inputs()


# --- contract conformance -------------------------------------------------------


def test_every_input_frame_matches_its_contract(inputs: dict[str, pl.DataFrame]) -> None:
    for name, frame in inputs.items():
        check_contract_frame(frame, FRAME_CONTRACTS[name])


def test_expected_frames_match_contracts() -> None:
    for kind in ("intervals", "returns", "contributions"):
        for variant in VARIANTS:
            frame, contract = expected_frame(kind, variant)
            check_contract_frame(frame, contract)


# --- calendar -------------------------------------------------------------------


def test_sessions_calendar(inputs: dict[str, pl.DataFrame]) -> None:
    sessions = inputs["sessions"]
    assert sessions.height == 62
    assert sessions["exchange_id"].unique().to_list() == ["XNYS"]
    dates = sessions["session_date"].to_list()
    assert dates == sorted(dates) and len(set(dates)) == len(dates)
    opens = sessions["open_ts"].to_list()
    assert opens == sorted(opens)
    # DST switch inside the range: both EST (14:30Z) and EDT (13:30Z) opens occur.
    assert {ts.hour for ts in opens} == {13, 14}
    # all opens precede their closes on the same date
    for row in sessions.iter_rows(named=True):
        assert row["open_ts"] < row["close_ts"]


# --- security_daily pricing convention ------------------------------------------


def _levels(frame: pl.DataFrame) -> dict[tuple[str, date], dict[str, float]]:
    out: dict[tuple[str, date], dict[str, float]] = {}
    for r in frame.iter_rows(named=True):
        out[(r["security_id"], r["session_date"])] = {
            "open_tr": r["tr_index"] * r["open"] / r["close"],
            "close_tr": r["tr_index"],
            "open_px": r["open"] * r["adj_factor"],
            "close_px": r["close"] * r["adj_factor"],
        }
    return out


def test_security_daily_prices_positive_and_halts_flat(inputs: dict[str, pl.DataFrame]) -> None:
    daily = inputs["security_daily"]
    for col in ("open", "close", "high", "low", "tr_index"):
        assert daily.filter(pl.col(col) <= 0).height == 0, col
    for r in daily.iter_rows(named=True):
        assert r["low"] <= min(r["open"], r["close"]) <= max(r["open"], r["close"]) <= r["high"]
        if r["is_halted"]:
            assert r["open"] == r["close"]
    # S4 halt stretch: five consecutive halted sessions
    s4 = daily.filter(pl.col("security_id") == "S4")
    halted = s4.filter(pl.col("is_halted"))
    assert halted.height == 5
    assert halted["session_date"].to_list() == sorted(halted["session_date"].to_list())


def test_tr_index_recursion_satisfies_pricing_convention(
    inputs: dict[str, pl.DataFrame],
) -> None:
    daily = inputs["security_daily"].sort("security_id", "session_date")
    rows = daily.iter_rows(named=True)
    prev: dict[str, dict[str, float]] = {}
    first_seen: set[str] = set()
    for r in rows:
        sec = r["security_id"]
        tr = r["tr_index"]
        o, c, a = r["open"], r["close"], r["adj_factor"]
        div = r["cash_dividend"] or 0.0
        if sec not in first_seen:
            first_seen.add(sec)
            assert tr == pytest.approx(100.0, rel=1e-9)
            open_tr = 100.0 * o / c
        else:
            p = prev[sec]
            ap, pc = p["adj"], p["close"]
            ptr = p["tr"]
            open_tr = ptr * a * (o + div) / (ap * pc)
            expected_tr = open_tr * c / o
            assert tr == pytest.approx(expected_tr, rel=1e-9), (sec, r["session_date"])
        prev[sec] = {"tr": tr, "adj": a, "close": c}
    # spot check: split day keeps tr continuous for S2 (adj 1 -> 2)
    s2 = daily.filter(pl.col("security_id") == "S2")
    adj = s2["adj_factor"].to_list()
    assert adj.count(2.0) > 0 and adj.count(1.0) > 0
    # S5 stops at its delisting session
    s5 = daily.filter(pl.col("security_id") == "S5")
    assert s5["session_date"].max() == date(2025, 4, 30)


# --- benchmark ------------------------------------------------------------------


def test_benchmark_daily_complete_and_gap_variant(inputs: dict[str, pl.DataFrame]) -> None:
    bench = inputs["benchmark_daily"]
    assert bench.height == 4 * 62
    for col in ("open_level", "close_level"):
        assert bench.filter(pl.col(col) <= 0).height == 0
    gap = load_benchmark_with_gap()
    assert gap.height == bench.height - 1
    missing = bench.join(gap, on=["benchmark_id", "session_date"], how="anti")
    assert missing.select("benchmark_id", "session_date").to_dicts() == [
        {"benchmark_id": "SECTOR_TECH", "session_date": date(2025, 4, 1)}
    ]


# --- expected coverage ------------------------------------------------------------


def test_expected_intervals_cover_all_cases() -> None:
    for variant in VARIANTS:
        rows = load_expected("intervals", variant)
        assert len(rows) == 23
        reasons = {r["exit_reason"] for r in rows}
        assert reasons == {
            "superseded",
            "expired",
            "dropped",
            "delisted",
            "coverage_ended",
            "analyst_left",
            "run_end",
        }
        non_inv = {r["non_investable_reason"] for r in rows if not r["is_investable"]}
        assert {"under_review", "restricted", "shorter_than_one_session"} <= non_inv
    # default and fixed share identical intervals (only horizons differ)
    assert load_expected("intervals", "default") == load_expected("intervals", "fixed")
    # decay extends expired exits and sets decay_start_session
    default = {r["interval_id"]: r for r in load_expected("intervals", "default")}
    decay = {r["interval_id"]: r for r in load_expected("intervals", "decay")}
    assert decay["e06"]["exit_session"] == date(2025, 4, 8)
    assert decay["e06"]["decay_start_session"] == date(2025, 4, 2)
    assert default["e06"]["exit_session"] == date(2025, 4, 1)


def test_expected_returns_and_contributions_cover_every_interval() -> None:
    for variant, horizons in (
        ("default", {"to_exit", "d63", "d126", "d252"}),
        ("decay", {"to_exit", "d63", "d126", "d252"}),
        ("fixed", {"to_exit", "d5", "d10"}),
    ):
        iv_ids = {r["interval_id"] for r in load_expected("intervals", variant)}
        returns = load_expected("returns", variant)
        contribs = load_expected("contributions", variant)
        assert {r["interval_id"] for r in returns} == iv_ids
        assert {r["interval_id"] for r in contribs} == iv_ids
        for row in returns:
            assert row["horizon"] in horizons
            legs_null = row["stock_return"] is None
            assert legs_null == (row["unavailable_reason"] is not None)
            if row["unavailable_reason"] is not None:
                assert row["entry_price"] is None and row["exit_price"] is None
        for row in contribs:
            assert row["horizon"] in horizons
            assert (row["contribution"] is None) == (row["unavailable_reason"] is not None)
        assert len(returns) == len(contribs)


# --- behavioural anchors ---------------------------------------------------------


def test_delisted_interval_is_a_total_loss(inputs: dict[str, pl.DataFrame]) -> None:
    returns = load_expected("returns", "default")
    e12 = [r for r in returns if r["interval_id"] == "e12"]
    assert len(e12) == 4
    to_exit = [r for r in e12 if r["horizon"] == "to_exit"][0]
    assert to_exit["terminated_by_delisting"] is True
    assert to_exit["stock_return"] == pytest.approx(-1.0, abs=1e-12)
    assert to_exit["exit_price"] == pytest.approx(0.0, abs=1e-12)
    assert to_exit["end_session"] == date(2025, 4, 30)
    assert to_exit["benchmark_id"] == "SECTOR_FIN"
    # fixed horizons that would extend past the as-of date are censored before the
    # delisting truncation is considered: horizon_incomplete, not terminated.
    for row in e12:
        if row["horizon"] != "to_exit":
            assert row["unavailable_reason"] == "horizon_incomplete"
            assert row["terminated_by_delisting"] is False
    # in the fixed-horizons variant d5/d10 land before the delisting and are plain legs
    fixed = [r for r in load_expected("returns", "fixed") if r["interval_id"] == "e12"]
    for row in fixed:
        if row["horizon"] == "to_exit":
            assert row["terminated_by_delisting"] is True
        else:
            assert row["unavailable_reason"] is None
            assert row["terminated_by_delisting"] is False


def test_short_interval_is_non_investable() -> None:
    rows = {r["interval_id"]: r for r in load_expected("intervals", "default")}
    e29 = rows["e29"]
    assert e29["is_investable"] is False
    assert e29["non_investable_reason"] == "shorter_than_one_session"
    assert e29["entry_session"] == e29["exit_session"] == date(2025, 3, 12)
    assert e29["exit_reason"] == "superseded"


def test_correction_chain_and_invisible_correction() -> None:
    events = load_frame("rating_events")
    by_id = {r["event_id"]: r for r in events.iter_rows(named=True)}
    # 2-deep chain: e23 corrects e22, e24 corrects e23
    assert by_id["e23"]["supersedes_event_id"] == "e22"
    assert by_id["e23"]["is_correction"] is True
    assert by_id["e24"]["supersedes_event_id"] == "e23"
    assert by_id["e24"]["is_correction"] is True
    # invisible correction: knowledge after the run cutoff
    assert by_id["e26"]["supersedes_event_id"] == "e11"
    assert by_id["e26"]["knowledge_ts"] > datetime(2025, 5, 24, tzinfo=UTC)
    # e22/e23 never appear as intervals; e24 does
    ids = {r["interval_id"] for r in load_expected("intervals", "default")}
    assert "e24" in ids and "e22" not in ids and "e23" not in ids


def test_return_legs_recompute_from_inputs(inputs: dict[str, pl.DataFrame]) -> None:
    """Every available to_exit leg must equal exit_tr/start_tr - 1 from the CSVs."""
    sec = _levels(inputs["security_daily"])
    bench = inputs["benchmark_daily"]
    bl = {
        (r["benchmark_id"], r["session_date"]): (r["open_level"], r["close_level"])
        for r in bench.iter_rows(named=True)
    }
    delist = {
        r["security_id"]: r["final_return"] for r in inputs["delistings"].iter_rows(named=True)
    }
    classes = inputs["security_classifications"]
    sectors = {r["security_id"]: r["sector_id"] for r in classes.iter_rows(named=True)}
    intervals = {r["interval_id"]: r for r in load_expected("intervals", "default")}
    for row in load_expected("returns", "default"):
        if row["horizon"] != "to_exit" or row["stock_return"] is None:
            continue
        iv = intervals[row["interval_id"]]
        sec_id = iv["security_id"]
        s, e = sec[(sec_id, row["start_session"])], sec[(sec_id, row["end_session"])]
        entry_tr = s["open_tr"] if row["start_point"] == "open" else s["close_tr"]
        if row["terminated_by_delisting"]:
            exit_tr = e["close_tr"] * (1.0 + delist[sec_id])
        else:
            exit_tr = e["open_tr"] if row["end_point"] == "open" else e["close_tr"]
        stock = exit_tr / entry_tr - 1.0
        assert row["stock_return"] == pytest.approx(stock, rel=1e-9)
        bid = row["benchmark_id"]
        assert bid == f"SECTOR_{sectors[sec_id]}"
        b_start = bl[(bid, row["start_session"])]
        b_end = bl[(bid, row["end_session"])]
        b0 = b_start[0] if row["start_point"] == "open" else b_start[1]
        b1 = b_end[1] if row["end_point"] == "close" else b_end[0]
        assert row["benchmark_return"] == pytest.approx(b1 / b0 - 1.0, rel=1e-9)
        assert row["excess_return"] == pytest.approx(
            row["stock_return"] - row["benchmark_return"], rel=1e-9
        )
        assert row["entry_price"] == pytest.approx(s["open_px"], rel=1e-9)


def test_decay_to_exit_contribution_is_segment_weighted(
    inputs: dict[str, pl.DataFrame],
) -> None:
    """e11 (sell, weight -0.5) under decay: sum of 6 segment-weighted excess legs."""
    sec = _levels(inputs["security_daily"])
    sessions = inputs["sessions"].sort("session_date")
    dates = sessions["session_date"].to_list()
    bench = inputs["benchmark_daily"]
    bl = {
        (r["benchmark_id"], r["session_date"]): (r["open_level"], r["close_level"])
        for r in bench.iter_rows(named=True)
    }
    iv = {r["interval_id"]: r for r in load_expected("intervals", "decay")}["e11"]
    expiry = iv["expiry_date"]
    exp_idx = max(i for i, d in enumerate(dates) if d <= expiry)
    eidx = dates.index(iv["entry_session"])
    xidx = dates.index(iv["exit_session"])
    assert xidx == exp_idx + 5
    bid = "SECTOR_HLTH"

    def excess(si: int, sp: str, ei: int, ep: str) -> float:
        s_d, e_d = dates[si], dates[ei]
        s, e = sec[("S4", s_d)], sec[("S4", e_d)]
        st = (e["open_tr"] if ep == "open" else e["close_tr"]) / (
            s["open_tr"] if sp == "open" else s["close_tr"]
        ) - 1.0
        bm = (
            bl[(bid, e_d)][1 if ep == "close" else 0] / bl[(bid, s_d)][1 if sp == "close" else 0]
            - 1.0
        )
        return float(st - bm)

    w = -0.5
    total = w * excess(eidx, "open", exp_idx, "close")
    for k in range(1, 6):
        total += w * (1.0 - k / 5.0) * excess(exp_idx + k - 1, "close", exp_idx + k, "close")
    contrib = [
        r
        for r in load_expected("contributions", "decay")
        if r["interval_id"] == "e11" and r["horizon"] == "to_exit"
    ][0]
    assert contrib["contribution"] == pytest.approx(total, rel=1e-9)
    # and it must differ from the flat weight x to_exit excess used under hard_close
    flat = [
        r
        for r in load_expected("contributions", "default")
        if r["interval_id"] == "e11" and r["horizon"] == "to_exit"
    ][0]
    assert contrib["contribution"] != pytest.approx(flat["contribution"], rel=1e-6)


def test_manifest_and_methodologies_present() -> None:
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
    assert manifest["run_context"]["as_of_date"] == "2025-05-23"
    assert (INPUT_DIR / "benchmark_daily_missing_day.csv").exists()
    assert set(json.loads((FIXTURE_DIR / "methodologies.json").read_text())) == set(VARIANTS)
