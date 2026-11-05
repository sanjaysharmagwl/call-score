"""Independent reference generator for the golden fixture (task T02).

Stdlib only (datetime/zoneinfo, csv, json). It NEVER imports callscore_engine or
callscore_core: it re-implements the decision-0003 semantics (sessions, pricing
convention, PIT event resolution, interval construction, return legs and
decay-weighted contributions) from scratch so the expected outputs are an
independent numerical check, not a copy of the engine.

Run from anywhere:  python3 server/packages/engine/tests/golden/reference.py
Writes data/fixtures/golden/{inputs,expected}/*.csv|*.json.
"""

from __future__ import annotations

import csv
import json
from bisect import bisect_left, bisect_right
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[5]
OUT = ROOT / "data" / "fixtures" / "golden"

EXCHANGE = "XNYS"
TZ = ZoneInfo("America/New_York")
SESSION_START = date(2025, 2, 24)  # Monday
SESSION_END = date(2025, 5, 23)  # Friday; spans the 2025-03-09 US DST switch
HOLIDAYS = {date(2025, 3, 17), date(2025, 4, 18), date(2025, 5, 2)}
AS_OF_DATE = SESSION_END
KNOWLEDGE_CUTOFF = datetime(2025, 5, 24, tzinfo=UTC)

# --- methodology variants -----------------------------------------------------
# Overrides applied on top of callscore_core.methodology.Methodology defaults.
METHODOLOGIES = {
    "default": {},
    "decay": {"expiry_policy": "decay", "decay_days": 5},
    "fixed": {"fixed_horizons_days": [5, 10], "primary_fixed_horizon_days": 10},
}
DEFAULT_HORIZONS = (63, 126, 252)
FIXED_HORIZONS = (5, 10)

POSITION_WEIGHTS = {
    "strong_buy": 1.0,
    "buy": 0.5,
    "hold": 0.0,
    "sell": -0.5,
    "strong_sell": -1.0,
}
INVESTABLE = {"strong_buy", "buy", "hold", "sell", "strong_sell"}
NON_INVESTABLE = {"not_rated", "under_review", "restricted"}

# Event announcement times (UTC). 12:00Z is before both EST (14:30Z) and EDT
# (13:30Z) opens; 15:00Z is inside both sessions; 21:30Z is after both closes
# (EST 21:00Z, EDT 20:00Z).
PRE, DAY, POST = "12:00:00+00:00", "15:00:00+00:00", "21:30:00+00:00"


def iso(d: date) -> str:
    return d.isoformat()


def ts(d: date, tod: str) -> str:
    return f"{iso(d)}T{tod}"


# --- calendar -----------------------------------------------------------------
def build_sessions() -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    d = SESSION_START
    while d <= SESSION_END:
        if d.weekday() < 5 and d not in HOLIDAYS:
            open_local = datetime.combine(d, time(9, 30), tzinfo=TZ)
            close_local = datetime.combine(d, time(16, 0), tzinfo=TZ)
            sessions.append(
                {
                    "exchange_id": EXCHANGE,
                    "session_date": d,
                    "open_ts": open_local.astimezone(UTC),
                    "close_ts": close_local.astimezone(UTC),
                }
            )
        d += timedelta(days=1)
    return sessions


# --- prices -------------------------------------------------------------------
BASE_PRICE = {"S1": 100.0, "S2": 50.0, "S3": 80.0, "S4": 30.0, "S5": 20.0}
SPLIT = {"S2": 30}  # session index of the 2:1 split ex-date
HALT = {"S4": set(range(20, 25))}  # halted session indices
DIVIDEND = {"S3": (40, 0.30)}  # (session index, cash_dividend per share)
DELIST = {"S5": 45}  # last traded session index
SECTORS = {"S1": "TECH", "S2": "TECH", "S3": "HLTH", "S4": "HLTH", "S5": "FIN"}
CAPS = {"S1": "large", "S2": "mid", "S3": "large", "S4": "small", "S5": "mid"}
BETAS = {"S1": 1.1, "S2": 0.9, "S3": 1.2, "S4": 0.8, "S5": 1.3}
BENCH_SECTOR_FACTOR = {"TECH": 0.0006, "HLTH": 0.0004, "FIN": 0.0008}


def adj_at(sec: str, i: int) -> float:
    split_idx = SPLIT.get(sec)
    return 2.0 if split_idx is not None and i >= split_idx else 1.0


def build_security_daily(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Raw OHLC obeying the frames.py pricing convention; tr_index derived by it."""
    n = len(sessions)
    rows: list[dict[str, Any]] = []
    for sec in ("S1", "S2", "S3", "S4", "S5"):
        last_idx = DELIST.get(sec, n - 1)
        econ_close = BASE_PRICE[sec]
        prev_raw_close = BASE_PRICE[sec]
        tr_prev: Decimal | None = None
        for i in range(last_idx + 1):
            drift = 0.0008 + 0.0006 * (((i * 13 + len(sec)) % 7) - 3) / 3.0
            econ_close = econ_close * (1.0 + drift)
            adj = adj_at(sec, i)
            raw_close = econ_close / adj
            div = DIVIDEND.get(sec, (None, 0.0))[1] if DIVIDEND.get(sec, (None, 0))[0] == i else 0.0
            if i in HALT.get(sec, set()):
                raw_open = raw_close_px = prev_raw_close
                raw_high = raw_low = prev_raw_close
                halted = True
            else:
                overnight = 0.0004 * (((i * 5 + len(sec)) % 5) - 2) / 2.0
                raw_open = prev_raw_close * (1.0 + overnight) if i > 0 else raw_close * 0.999
                raw_close_px = raw_close
                raw_high = max(raw_open, raw_close) * 1.004
                raw_low = min(raw_open, raw_close) * 0.996
                halted = False
            # tr_index per frames.py convention (Decimal for exactness).
            o, c = Decimal(str(round(raw_open, 6))), Decimal(str(round(raw_close_px, 6)))
            a, ap = Decimal(str(adj)), Decimal(str(adj_at(sec, i - 1))) if i > 0 else Decimal("1")
            dv = Decimal(str(div))
            if i == 0 or tr_prev is None:
                open_tr = Decimal("100") * o / c
            else:
                pc = Decimal(str(round(prev_raw_close, 6)))
                open_tr = tr_prev * a * (o + dv) / (ap * pc)
            tr = open_tr * c / o
            tr_prev = tr
            prev_raw_close = raw_close_px
            div_val = div if div else None
            rows.append(
                {
                    "security_id": sec,
                    "session_date": sessions[i]["session_date"],
                    "open": round(raw_open, 6),
                    "close": round(raw_close_px, 6),
                    "high": round(raw_high, 6),
                    "low": round(raw_low, 6),
                    "adj_factor": adj,
                    "cash_dividend": div_val,
                    "tr_index": float(tr.quantize(Decimal("0.0000000001"))),
                    "is_halted": halted,
                }
            )
    return rows


def build_benchmark_daily(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bid, factor in (
        ("IDX_BROAD", 0.0005),
        ("SECTOR_TECH", BENCH_SECTOR_FACTOR["TECH"]),
        ("SECTOR_HLTH", BENCH_SECTOR_FACTOR["HLTH"]),
        ("SECTOR_FIN", BENCH_SECTOR_FACTOR["FIN"]),
    ):
        close = 1000.0
        for i, s in enumerate(sessions):
            open_level = close * (1.0 + 0.0002) if i > 0 else 999.5
            close = open_level * (1.0 + factor)
            rows.append(
                {
                    "benchmark_id": bid,
                    "session_date": s["session_date"],
                    "open_level": round(open_level, 6),
                    "close_level": round(close, 6),
                }
            )
    return rows


# --- rating events ------------------------------------------------------------
# (event_id, analyst, action, canonical, ann_idx, tod, expiry_idx|None,
#  extra: price_target/horizon/pt_ccy, supersedes, is_correction, knowledge_tod)
NATIVE = {
    "strong_buy": "Strong Buy",
    "buy": "Buy",
    "hold": "Hold",
    "sell": "Sell",
    "strong_sell": "Strong Sell",
    "under_review": "Under Review",
    "restricted": "Restricted",
    "not_rated": "Not Rated",
}
PRIOR = {
    ("A1", "S1"): [None, "buy", "strong_buy", "buy", "buy"],
    ("A1", "S2"): [None, "hold"],
    ("A1", "S3"): [None, "strong_buy", "under_review", "buy"],
    ("A1", "S4"): [None, "sell"],
    ("A1", "S5"): [None, "strong_buy"],
    ("A2", "S1"): [None, "buy", "hold", "sell"],
    ("A2", "S2"): [None, "strong_sell", "restricted", "strong_sell"],
    ("A2", "S3"): [None, "buy"],
    ("A2", "S4"): [None, "hold"],
    ("A3", "S3"): [None, "buy", "sell", "hold", "sell"],
    ("A3", "S4"): [None, "strong_buy", "sell", "buy"],
    ("A3", "S5"): [None, "buy"],
}

# event tuples: id, analyst, security, action, canonical, idx, tod, expiry_idx,
#               supersedes, is_correction, knowledge_override|None
RAW_EVENTS = [
    # A1 / S1: initiation -> upgrade -> reiteration(ext) -> downgrade -> drop
    ("e01", "A1", "S1", "initiation", "buy", 2, PRE, 12, None, False, None),
    ("e02", "A1", "S1", "upgrade", "strong_buy", 5, PRE, 15, None, False, None),
    ("e03", "A1", "S1", "reiteration", "strong_buy", 8, PRE, 20, None, False, None),
    ("e04", "A1", "S1", "downgrade", "buy", 15, DAY, 30, None, False, None),
    ("e05", "A1", "S1", "drop", "not_rated", 25, PRE, None, None, False, None),
    # A1 / S2: initiation hold, pt_change extends expiry
    ("e06", "A1", "S2", "initiation", "hold", 3, PRE, 18, None, False, None),
    ("e07", "A1", "S2", "pt_change", "hold", 10, PRE, 25, None, False, None),
    # A1 / S3: initiation -> suspend -> resume
    ("e08", "A1", "S3", "initiation", "strong_buy", 4, PRE, 30, None, False, None),
    ("e09", "A1", "S3", "suspend", "under_review", 14, PRE, 32, None, False, None),
    ("e10", "A1", "S3", "resume", "buy", 20, PRE, 35, None, False, None),
    # A1 / S4: initiation sell (+ invisible correction e26)
    ("e11", "A1", "S4", "initiation", "sell", 6, PRE, 40, None, False, None),
    ("e26", "A1", "S4", "upgrade", "buy", 35, DAY, 58, "e11", True, "2025-05-26T00:00:00+00:00"),
    # A1 / S5: initiation, later delisted (bankrupt)
    ("e12", "A1", "S5", "initiation", "strong_buy", 8, PRE, 50, None, False, None),
    # A2 / S1: buy -> downgrade hold -> downgrade sell
    ("e13", "A2", "S1", "initiation", "buy", 4, PRE, 35, None, False, None),
    ("e14", "A2", "S1", "downgrade", "hold", 12, DAY, 38, None, False, None),
    ("e15", "A2", "S1", "downgrade", "sell", 22, PRE, 48, None, False, None),
    # A2 / S2: strong_sell -> restrict -> resume
    ("e16", "A2", "S2", "initiation", "strong_sell", 5, PRE, 28, None, False, None),
    ("e17", "A2", "S2", "restrict", "restricted", 18, PRE, 33, None, False, None),
    ("e18", "A2", "S2", "resume", "strong_sell", 30, PRE, 52, None, False, None),
    # A2 / S3, S4
    ("e19", "A2", "S3", "initiation", "buy", 7, PRE, 44, None, False, None),
    ("e20", "A2", "S4", "initiation", "hold", 9, PRE, 33, None, False, None),
    # A3 / S3: buy, then a 2-deep correction chain on the downgrade
    ("e21", "A3", "S3", "initiation", "buy", 6, PRE, 38, None, False, None),
    ("e22", "A3", "S3", "downgrade", "sell", 16, PRE, 41, None, False, None),
    ("e23", "A3", "S3", "downgrade", "hold", 21, PRE, 42, "e22", True, None),
    ("e24", "A3", "S3", "downgrade", "sell", 22, PRE, 44, "e23", True, None),
    # A3 / S4: strong_buy -> downgrade(sell) -> upgrade(buy); the sell interval is
    # zero-length because e29 (post-close) and e30 (next pre-market) share an entry.
    ("e27", "A3", "S4", "initiation", "strong_buy", 10, PRE, 36, None, False, None),
    ("e29", "A3", "S4", "downgrade", "sell", 11, POST, 37, None, False, None),
    ("e30", "A3", "S4", "upgrade", "buy", 12, PRE, None, None, False, None),
    # A3 / S5: coverage spell ends mid-interval -> coverage_ended
    ("e28", "A3", "S5", "initiation", "buy", 9, PRE, 55, None, False, None),
]

PRICE_TARGETS = {
    "e01": (125.0, 12),
    "e08": (110.0, 12),
    "e12": (30.0, 12),
    "e21": (60.0, 12),
}
FIRMS = {"A1": "F1", "A2": "F1", "A3": "F2"}


def build_events(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    prior_seen: dict[tuple[str, str], int] = {}
    for eid, analyst, sec, action, canon, k, tod, exp_k, sup, corr, know in RAW_EVENTS:
        d = sessions[k]["session_date"]
        announced = ts(d, tod)
        knowledge = know if know is not None else announced
        pt = PRICE_TARGETS.get(eid)
        key = (analyst, sec)
        idx = prior_seen.get(key, 0)
        prior_list = PRIOR.get(key, [])
        prior = prior_list[idx] if idx < len(prior_list) else None
        prior_seen[key] = idx + 1
        events.append(
            {
                "event_id": eid,
                "analyst_id": analyst,
                "firm_id": FIRMS[analyst],
                "security_id": sec,
                "announced_ts": announced,
                "effective_ts": announced,
                "knowledge_ts": knowledge,
                "action_type": action,
                "native_rating": NATIVE[canon],
                "canonical_rating": canon,
                "prior_canonical_rating": prior,
                "scale_id": "five_tier",
                "price_target": pt[0] if pt else None,
                "pt_currency": "USD" if pt else None,
                "pt_horizon_months": pt[1] if pt else None,
                "expiry_date": iso(sessions[exp_k]["session_date"]) if exp_k is not None else None,
                "supersedes_event_id": sup,
                "is_correction": corr,
                "_ann_idx": k,
            }
        )
    return events


# --- spell / reference frames --------------------------------------------------
def build_coverage(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for sec in ("S1", "S2", "S3", "S4", "S5"):
        rows.append(
            {
                "analyst_id": "A1",
                "security_id": sec,
                "from_date": date(2025, 1, 1),
                "to_date": None,
                "is_lead": sec in ("S1", "S3"),
            }
        )
    for sec in ("S1", "S2", "S3", "S4"):
        rows.append(
            {
                "analyst_id": "A2",
                "security_id": sec,
                "from_date": date(2025, 1, 1),
                "to_date": None,
                "is_lead": sec == "S2",
            }
        )
    # A3's S5 coverage spell ends mid-run -> coverage_ended exit for e28.
    for sec, to in (("S3", None), ("S4", None), ("S5", sessions[40]["session_date"])):
        rows.append(
            {
                "analyst_id": "A3",
                "security_id": sec,
                "from_date": date(2025, 1, 1),
                "to_date": to,
                "is_lead": sec == "S4",
            }
        )
    return rows


def build_employment(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"analyst_id": "A1", "firm_id": "F1", "from_date": date(2025, 1, 1), "to_date": None},
        {
            "analyst_id": "A2",
            "firm_id": "F1",
            "from_date": date(2025, 1, 1),
            "to_date": sessions[50]["session_date"],
        },
        {"analyst_id": "A3", "firm_id": "F2", "from_date": date(2025, 1, 1), "to_date": None},
    ]


def build_classifications(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for sec in ("S1", "S2", "S3", "S4", "S5"):
        to = sessions[DELIST[sec]]["session_date"] if sec in DELIST else None
        rows.append(
            {
                "security_id": sec,
                "from_date": date(2025, 1, 1),
                "to_date": to,
                "exchange_id": EXCHANGE,
                "currency": "USD",
                "sector_id": SECTORS[sec],
                "region": "NA",
                "cap_bucket": CAPS[sec],
                "listing_date": date(2024, 6, 3),
                "delisting_date": to,
            }
        )
    return rows


def build_betas() -> list[dict[str, Any]]:
    return [
        {
            "security_id": sec,
            "as_of_date": date(2025, 1, 2),
            "beta": BETAS[sec],
            "window_days": 252,
        }
        for sec in ("S1", "S2", "S3", "S4", "S5")
    ]


def build_delistings(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "security_id": "S5",
            "delisting_date": sessions[DELIST["S5"]]["session_date"],
            "final_return": -1.0,
            "reason": "bankrupt",
        }
    ]


# --- interval engine (reference) -------------------------------------------------
def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def visible_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = KNOWLEDGE_CUTOFF
    vis = [e for e in events if parse_ts(str(e["knowledge_ts"])) <= cutoff]
    superseded = {str(e["supersedes_event_id"]) for e in vis if e["supersedes_event_id"]}
    return [e for e in vis if str(e["event_id"]) not in superseded]


class Interval:
    def __init__(self, ev: dict[str, Any], entry: tuple[int, str, datetime]) -> None:
        self.event = ev
        self.entry = entry  # (session_idx, "open", open_ts)
        self.expiry: date | None = (
            date.fromisoformat(str(ev["expiry_date"])) if ev["expiry_date"] else None
        )
        self.candidates: list[tuple[str, int, str, datetime]] = []  # reason, idx, point, ts
        self.exit_candidate: tuple[str, int, str, datetime] | None = None

    @property
    def canonical(self) -> str:
        return str(self.event["canonical_rating"])


def last_session_on_or_before(sessions: list[dict[str, Any]], d: date) -> int | None:
    dates = [s["session_date"] for s in sessions]
    i = bisect_right(dates, d) - 1
    return i if i >= 0 else None


def entry_point(
    sessions: list[dict[str, Any]], avail: datetime
) -> tuple[int, str, datetime] | None:
    for i, s in enumerate(sessions):
        if s["open_ts"] > avail:
            return (i, "open", s["open_ts"])
    return None


POINT_RANK = {"open": 0, "close": 1}
TIE_PRECEDENCE = [
    "delisted",
    "analyst_left",
    "coverage_ended",
    "dropped",
    "superseded",
    "expired",
    "run_end",
]


def build_intervals(
    events: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    employment: list[dict[str, Any]],
    delistings: list[dict[str, Any]],
    expiry_policy: str,
    decay_days: int,
) -> list[Interval]:
    vis = sorted(visible_events(events), key=lambda e: (str(e["announced_ts"]), str(e["event_id"])))
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in vis:
        by_key.setdefault((str(e["analyst_id"]), str(e["security_id"])), []).append(e)

    intervals: list[Interval] = []
    last_idx = len(sessions) - 1

    for (_analyst, _sec), evs in by_key.items():
        current: Interval | None = None
        for ev in evs:
            avail = max(parse_ts(str(ev["effective_ts"])), parse_ts(str(ev["knowledge_ts"])))
            entry = entry_point(sessions, avail)
            assert entry is not None, f"no entry session for {ev['event_id']}"
            action = str(ev["action_type"])
            canon = str(ev["canonical_rating"])
            if action == "drop":
                if current is not None:
                    current.candidates.append(("dropped", *entry))
                    intervals.append(current)
                    current = None
                continue
            if current is not None and canon == current.canonical:
                new_exp = date.fromisoformat(str(ev["expiry_date"])) if ev["expiry_date"] else None
                if new_exp is not None and (current.expiry is None or new_exp > current.expiry):
                    current.expiry = new_exp
                continue
            if current is not None:
                current.candidates.append(("superseded", *entry))
                intervals.append(current)
            current = Interval(ev, entry)
        # run-end candidate handled below for the still-open interval
        if current is not None:
            intervals.append(current)

    # second pass: exits
    for iv in intervals:
        ev = iv.event
        analyst, sec = str(ev["analyst_id"]), str(ev["security_id"])
        eidx = iv.entry[0]
        # expiry candidate
        if iv.expiry is not None:
            exp_idx = last_session_on_or_before(sessions, iv.expiry)
            assert exp_idx is not None
            if expiry_policy == "hard_close":
                iv.candidates.append(("expired", exp_idx, "close", sessions[exp_idx]["close_ts"]))
            elif expiry_policy == "decay":
                tail = exp_idx + decay_days
                if tail <= last_idx:
                    iv.candidates.append(("expired", tail, "close", sessions[tail]["close_ts"]))
        # coverage spell containing entry date
        edate = sessions[eidx]["session_date"]
        for sp in coverage:
            if (
                str(sp["analyst_id"]) == analyst
                and str(sp["security_id"]) == sec
                and date.fromisoformat(str(sp["from_date"])) <= edate
                and (sp["to_date"] is None or edate <= sp["to_date"])
            ):
                if sp["to_date"] is not None:
                    cidx = last_session_on_or_before(sessions, sp["to_date"])
                    if cidx is not None:
                        iv.candidates.append(
                            ("coverage_ended", cidx, "close", sessions[cidx]["close_ts"])
                        )
                break
        # employment spell containing entry date
        for sp in employment:
            if (
                str(sp["analyst_id"]) == analyst
                and date.fromisoformat(str(sp["from_date"])) <= edate
                and (sp["to_date"] is None or edate <= sp["to_date"])
            ):
                if sp["to_date"] is not None:
                    aidx = last_session_on_or_before(sessions, sp["to_date"])
                    if aidx is not None:
                        iv.candidates.append(
                            ("analyst_left", aidx, "close", sessions[aidx]["close_ts"])
                        )
                break
        # delisting
        for dl in delistings:
            if str(dl["security_id"]) == sec:
                didx = last_session_on_or_before(
                    sessions, date.fromisoformat(str(dl["delisting_date"]))
                )
                if didx is not None:
                    iv.candidates.append(("delisted", didx, "close", sessions[didx]["close_ts"]))
        # run end
        iv.candidates.append(("run_end", last_idx, "close", sessions[last_idx]["close_ts"]))
        # earliest point; ties by precedence
        best_pt = min((c[1], POINT_RANK[c[2]]) for c in iv.candidates)
        tied = [c for c in iv.candidates if (c[1], POINT_RANK[c[2]]) == best_pt]
        tied.sort(key=lambda c: TIE_PRECEDENCE.index(c[0]))
        iv.exit_candidate = tied[0]
    return intervals


def interval_rows(
    intervals: list[Interval],
    sessions: list[dict[str, Any]],
    expiry_policy: str,
    decay_days: int,
) -> list[dict[str, Any]]:
    rows = []
    for iv in sorted(
        intervals,
        key=lambda x: (
            str(x.event["analyst_id"]),
            str(x.event["security_id"]),
            str(x.entry[2]),
            str(x.event["event_id"]),
        ),
    ):
        ev = iv.event
        eidx, _, ets = iv.entry
        assert iv.exit_candidate is not None
        xreason, xidx, xpoint, xts = iv.exit_candidate
        canon = str(ev["canonical_rating"])
        nonpositive = (xidx, POINT_RANK[xpoint]) <= (eidx, POINT_RANK["open"])
        # non_investable_reason precedence (mirrors the engine):
        # before_listing > non-positive structural > non-investable canonical rating.
        reason: str | None = None
        if nonpositive:
            structural = {
                "coverage_ended": "after_coverage_end",
                "delisted": "after_delisting",
                "run_end": "after_run_end",
            }
            reason = structural.get(xreason, "shorter_than_one_session")
        elif canon in NON_INVESTABLE:
            reason = canon
        investable = reason is None
        flags: list[str] = []
        if nonpositive:
            flags.append("non_positive_length")
        if canon in NON_INVESTABLE:
            flags.append("non_investable_rating")
        decay_start = None
        if expiry_policy == "decay" and iv.expiry is not None:
            exp_idx = last_session_on_or_before(sessions, iv.expiry)
            if exp_idx is not None and exp_idx + 1 <= len(sessions) - 1:
                decay_start = sessions[exp_idx + 1]["session_date"]
        rows.append(
            {
                "interval_id": str(ev["event_id"]),
                "event_id": str(ev["event_id"]),
                "analyst_id": str(ev["analyst_id"]),
                "firm_id": str(ev["firm_id"]),
                "security_id": str(ev["security_id"]),
                "native_rating": str(ev["native_rating"]),
                "canonical_rating": canon,
                "action_type": str(ev["action_type"]),
                "announced_ts": str(ev["announced_ts"]),
                "effective_ts": str(ev["effective_ts"]),
                "knowledge_ts": str(ev["knowledge_ts"]),
                "available_ts": max(str(ev["effective_ts"]), str(ev["knowledge_ts"])),
                "entry_session": iso(sessions[eidx]["session_date"]),
                "entry_point": "open",
                "entry_ts": ets.isoformat(),
                "exit_session": iso(sessions[xidx]["session_date"]),
                "exit_point": xpoint,
                "exit_ts": xts.isoformat(),
                "exit_reason": xreason,
                "expiry_date": iso(iv.expiry) if iv.expiry else None,
                "decay_start_session": iso(decay_start) if decay_start else None,
                "is_investable": investable,
                "non_investable_reason": reason,
                "flags": ",".join(flags) if flags else None,
            }
        )
    return rows


# --- returns / contributions -----------------------------------------------------
def tr_levels(security_daily: list[dict[str, Any]]) -> dict[tuple[str, date], dict[str, float]]:
    out = {}
    for r in security_daily:
        key = (str(r["security_id"]), r["session_date"])
        out[key] = {
            "open_tr": float(r["tr_index"]) * float(r["open"]) / float(r["close"]),
            "close_tr": float(r["tr_index"]),
            "open_px": float(r["open"]) * float(r["adj_factor"]),
            "close_px": float(r["close"]) * float(r["adj_factor"]),
        }
    return out


def bench_levels(
    benchmark_daily: list[dict[str, Any]],
) -> dict[tuple[str, date], dict[str, float]]:
    return {
        (str(r["benchmark_id"]), r["session_date"]): {
            "open": float(r["open_level"]),
            "close": float(r["close_level"]),
        }
        for r in benchmark_daily
    }


def sector_of(sec: str, entry_date: date) -> str:
    return SECTORS[sec]


def leg_value(
    levels: dict[tuple[str, date], dict[str, float]],
    sec: str,
    sdate: date,
    spoint: str,
    edate: date,
    epoint: str,
    terminal: float | None = None,
) -> tuple[float, float, float, float] | None:
    """Return (entry_px, exit_px, entry_tr, exit_tr) or None if a level is missing."""
    s = levels.get((sec, sdate))
    e = levels.get((sec, edate))
    if s is None or e is None:
        return None
    entry_tr = s["open_tr"] if spoint == "open" else s["close_tr"]
    entry_px = s["open_px"] if spoint == "open" else s["close_px"]
    if terminal is not None:
        exit_tr = e["close_tr"] * (1.0 + terminal)
        exit_px = e["close_px"] * (1.0 + terminal)
    else:
        exit_tr = e["open_tr"] if epoint == "open" else e["close_tr"]
        exit_px = e["open_px"] if epoint == "open" else e["close_px"]
    return entry_px, exit_px, entry_tr, exit_tr


def bench_leg(
    benches: dict[tuple[str, date], dict[str, float]],
    bid: str,
    sdate: date,
    spoint: str,
    edate: date,
    epoint: str,
) -> tuple[float, float] | None:
    s = benches.get((bid, sdate))
    e = benches.get((bid, edate))
    if s is None or e is None:
        return None
    return (
        s["open"] if spoint == "open" else s["close"],
        e["open"] if epoint == "open" else e["close"],
    )


def build_returns_and_contributions(
    iv_rows: list[dict[str, Any]],
    intervals: list[Interval],
    sessions: list[dict[str, Any]],
    sec_levels: dict[tuple[str, date], dict[str, float]],
    benches: dict[tuple[str, date], dict[str, float]],
    delistings: list[dict[str, Any]],
    expiry_policy: str,
    decay_days: int,
    horizons: tuple[int, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_id = {str(iv.event["event_id"]): iv for iv in intervals}
    delist = {str(d["security_id"]): d for d in delistings}
    dates = [s["session_date"] for s in sessions]
    last_idx = len(sessions) - 1

    returns: list[dict[str, Any]] = []
    contribs: list[dict[str, Any]] = []
    weight_of = {k: v for k, v in POSITION_WEIGHTS.items()}

    def stock_bench_leg(
        sec: str,
        bid: str,
        sidx: int,
        spoint: str,
        eidx: int,
        epoint: str,
        terminal: float | None = None,
    ) -> dict[str, float] | None:
        sdate, edate = dates[sidx], dates[eidx]
        sv = leg_value(sec_levels, sec, sdate, spoint, edate, epoint, terminal)
        bv = bench_leg(benches, bid, sdate, spoint, edate, epoint)
        if sv is None or bv is None:
            return None
        entry_px, exit_px, entry_tr, exit_tr = sv
        b0, b1 = bv
        stock = exit_tr / entry_tr - 1.0
        bench = b1 / b0 - 1.0
        return {
            "entry_price": entry_px,
            "exit_price": exit_px,
            "stock_return": stock,
            "benchmark_return": bench,
            "excess_return": stock - bench,
        }

    for row in iv_rows:
        iid = str(row["interval_id"])
        iv = by_id[iid]
        ev = iv.event
        sec = str(ev["security_id"])
        analyst = str(ev["analyst_id"])
        canon = str(ev["canonical_rating"])
        investable = bool(row["is_investable"])
        eidx = dates.index(date.fromisoformat(str(row["entry_session"])))
        xidx = dates.index(date.fromisoformat(str(row["exit_session"])))
        xpoint = str(row["exit_point"])
        bid = f"SECTOR_{sector_of(sec, date.fromisoformat(str(row['entry_session'])))}"
        weight = weight_of.get(canon) if investable else None
        dl_idx = None
        if sec in delist:
            dl_idx = dates.index(date.fromisoformat(str(delist[sec]["delisting_date"])))

        def truncate(
            ei: int,
            ep: str,
            *,
            dl_idx: int | None = dl_idx,
            sec: str = sec,
            delist: dict[str, Any] = delist,
        ) -> tuple[int, str, float | None, bool]:
            """Delisting truncation shared by horizon and decay-segment legs."""
            if dl_idx is not None and (ei > dl_idx or (ei == dl_idx and ep == "close")):
                return dl_idx, "close", float(delist[sec]["final_return"]), True
            return ei, ep, None, False

        # Decay exposure segments for the to-exit contribution, mirroring
        # exposures._segment_rows: segment 1 entry -> close before decay_start at
        # full weight, then one prev-close -> close segment per decay session.
        decay_start = row["decay_start_session"]
        seg_parts: list[tuple[float, float]] = []  # (segment weight, segment excess)
        if expiry_policy == "decay" and investable and decay_start is not None:
            assert weight is not None
            decay_idx = bisect_left(dates, date.fromisoformat(str(decay_start)))
            raw_segs: list[tuple[int, str, int, str, float]] = []
            if decay_idx >= 1:
                prev_idx = decay_idx - 1
                if (eidx, POINT_RANK["open"]) <= (prev_idx, POINT_RANK["close"]):
                    ee, ep = prev_idx, "close"
                    if (ee, POINT_RANK[ep]) > (xidx, POINT_RANK[xpoint]):
                        ee, ep = xidx, xpoint
                    if (ee, POINT_RANK[ep]) > (eidx, POINT_RANK["open"]):
                        raw_segs.append((eidx, "open", ee, ep, weight))
            for k in range(1, decay_days + 1):
                si = decay_idx + k - 1
                if si >= len(dates):
                    break
                if si >= 1:
                    ss_i, sp = si - 1, "close"
                else:
                    ss_i, sp = si, "open"
                if not (xidx, POINT_RANK[xpoint]) > (ss_i, POINT_RANK[sp]):
                    break
                ee, ep = si, "close"
                if (ee, POINT_RANK[ep]) > (xidx, POINT_RANK[xpoint]):
                    ee, ep = xidx, xpoint
                if not (ee, POINT_RANK[ep]) > (ss_i, POINT_RANK[sp]):
                    continue
                raw_segs.append((ss_i, sp, ee, ep, weight * (1.0 - k / decay_days)))
            for ss_i, sp, ee_i, ep, w_seg in raw_segs:
                te, tp, terminal, _ = truncate(ee_i, ep)
                seg_leg = stock_bench_leg(sec, bid, ss_i, sp, te, tp, terminal)
                if seg_leg is not None:
                    seg_parts.append((w_seg, seg_leg["excess_return"]))

        horizon_specs: list[tuple[str, int | None]] = [("to_exit", None)] + [
            (f"d{n}", n) for n in horizons
        ]
        for hlabel, hn in horizon_specs:
            unavailable: str | None = None
            leg: dict[str, float] | None = None
            hend_idx: int | None = xidx
            hpoint: str | None = xpoint
            term_flag = False
            if not investable:
                unavailable = "non_investable"
                hend_idx, hpoint = None, None
            elif hn is None:
                te, tp, terminal, term_flag = truncate(xidx, xpoint)
                leg = stock_bench_leg(sec, bid, eidx, "open", te, tp, terminal)
                assert leg is not None, f"missing leg for {iid} to_exit"
            else:
                target = eidx + hn
                if target > last_idx:
                    unavailable = "horizon_incomplete"
                    hend_idx, hpoint = None, None
                else:
                    te, tp, terminal, term_flag = truncate(target, "close")
                    hend_idx, hpoint = te, tp
                    leg = stock_bench_leg(sec, bid, eidx, "open", te, tp, terminal)
                    assert leg is not None, f"missing leg for {iid} {hlabel}"
            end_known = hend_idx is not None and hpoint is not None
            returns.append(
                {
                    "interval_id": iid,
                    "horizon": hlabel,
                    "start_session": iso(dates[eidx]),
                    "start_point": "open",
                    "end_session": iso(dates[hend_idx]) if hend_idx is not None else None,
                    "end_point": hpoint if end_known else None,
                    "entry_price": leg["entry_price"] if leg else None,
                    "exit_price": leg["exit_price"] if leg else None,
                    "stock_return": leg["stock_return"] if leg else None,
                    "benchmark_id": bid if investable else None,
                    "benchmark_return": leg["benchmark_return"] if leg else None,
                    "beta": None,
                    "excess_return": leg["excess_return"] if leg else None,
                    "terminated_by_delisting": term_flag,
                    "unavailable_reason": unavailable,
                }
            )
            # contribution
            if leg is None:
                assert unavailable is not None
                contrib_val, creason = None, unavailable
            elif hn is None and seg_parts:
                contrib_val = sum(w * ex for w, ex in seg_parts)
                creason = None
            else:
                contrib_val = (weight if weight is not None else 0.0) * leg["excess_return"]
                creason = None
            contribs.append(
                {
                    "interval_id": iid,
                    "analyst_id": analyst,
                    "horizon": hlabel,
                    "entry_session": iso(dates[eidx]),
                    "position_weight": weight,
                    "excess_return": leg["excess_return"] if leg else None,
                    "contribution": contrib_val,
                    "unavailable_reason": creason,
                }
            )
    return returns, contribs


# --- IO -------------------------------------------------------------------------
FRAME_COLUMNS: dict[str, tuple[str, ...]] = {
    "sessions": ("exchange_id", "session_date", "open_ts", "close_ts"),
    "rating_events": (
        "event_id",
        "analyst_id",
        "firm_id",
        "security_id",
        "announced_ts",
        "effective_ts",
        "knowledge_ts",
        "action_type",
        "native_rating",
        "canonical_rating",
        "prior_canonical_rating",
        "scale_id",
        "price_target",
        "pt_currency",
        "pt_horizon_months",
        "expiry_date",
        "supersedes_event_id",
        "is_correction",
    ),
    "coverage_spells": ("analyst_id", "security_id", "from_date", "to_date", "is_lead"),
    "employment_spells": ("analyst_id", "firm_id", "from_date", "to_date"),
    "security_classifications": (
        "security_id",
        "from_date",
        "to_date",
        "exchange_id",
        "currency",
        "sector_id",
        "region",
        "cap_bucket",
        "listing_date",
        "delisting_date",
    ),
    "security_daily": (
        "security_id",
        "session_date",
        "open",
        "close",
        "high",
        "low",
        "adj_factor",
        "cash_dividend",
        "tr_index",
        "is_halted",
    ),
    "benchmark_daily": ("benchmark_id", "session_date", "open_level", "close_level"),
    "security_beta": ("security_id", "as_of_date", "beta", "window_days"),
    "delistings": ("security_id", "delisting_date", "final_return", "reason"),
    "intervals": (
        "interval_id",
        "event_id",
        "analyst_id",
        "firm_id",
        "security_id",
        "native_rating",
        "canonical_rating",
        "action_type",
        "announced_ts",
        "effective_ts",
        "knowledge_ts",
        "available_ts",
        "entry_session",
        "entry_point",
        "entry_ts",
        "exit_session",
        "exit_point",
        "exit_ts",
        "exit_reason",
        "expiry_date",
        "decay_start_session",
        "is_investable",
        "non_investable_reason",
        "flags",
    ),
    "returns": (
        "interval_id",
        "horizon",
        "start_session",
        "start_point",
        "end_session",
        "end_point",
        "entry_price",
        "exit_price",
        "stock_return",
        "benchmark_id",
        "benchmark_return",
        "beta",
        "excess_return",
        "terminated_by_delisting",
        "unavailable_reason",
    ),
    "contributions": (
        "interval_id",
        "analyst_id",
        "horizon",
        "entry_session",
        "position_weight",
        "excess_return",
        "contribution",
        "unavailable_reason",
    ),
}


def fmt(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    return str(v)


# Input-frame sort keys, mirroring callscore_core.frames FrameContract.sort_by.
SORT_KEYS: dict[str, tuple[str, ...]] = {
    "sessions": ("exchange_id", "session_date"),
    "rating_events": ("analyst_id", "security_id", "announced_ts", "event_id"),
    "coverage_spells": ("analyst_id", "security_id", "from_date"),
    "employment_spells": ("analyst_id", "from_date"),
    "security_classifications": ("security_id", "from_date"),
    "security_daily": ("security_id", "session_date"),
    "benchmark_daily": ("benchmark_id", "session_date"),
    "security_beta": ("security_id", "as_of_date"),
    "delistings": ("security_id",),
}


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = SORT_KEYS.get(path.stem) or SORT_KEYS.get(path.stem.rsplit("_missing_day", 1)[0])
    if keys:
        rows = sorted(rows, key=lambda r: tuple(str(r.get(k) or "") for k in keys))
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow([fmt(r.get(c)) for c in columns])


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, sort_keys=False) + "\n")


def main() -> None:
    sessions = build_sessions()
    print(
        f"sessions: {len(sessions)} "
        f"({sessions[0]['session_date']} .. {sessions[-1]['session_date']})"
    )

    sec_daily = build_security_daily(sessions)
    bench_daily = build_benchmark_daily(sessions)
    bench_daily.sort(key=lambda r: (str(r["benchmark_id"]), str(r["session_date"])))
    events = build_events(sessions)
    coverage = build_coverage(sessions)
    employment = build_employment(sessions)
    classes = build_classifications(sessions)
    betas = build_betas()
    delistings = build_delistings(sessions)

    # strip private helper key before writing
    event_rows = [{k: v for k, v in e.items() if not k.startswith("_")} for e in events]
    # sort events by contract sort_by: analyst_id, security_id, announced_ts, event_id
    event_rows.sort(
        key=lambda e: (
            str(e["analyst_id"]),
            str(e["security_id"]),
            str(e["announced_ts"]),
            str(e["event_id"]),
        )
    )

    inp = OUT / "inputs"
    exp = OUT / "expected"
    write_csv(inp / "sessions.csv", FRAME_COLUMNS["sessions"], sessions)
    write_csv(inp / "rating_events.csv", FRAME_COLUMNS["rating_events"], event_rows)
    write_csv(inp / "coverage_spells.csv", FRAME_COLUMNS["coverage_spells"], coverage)
    write_csv(inp / "employment_spells.csv", FRAME_COLUMNS["employment_spells"], employment)
    write_csv(
        inp / "security_classifications.csv",
        FRAME_COLUMNS["security_classifications"],
        classes,
    )
    write_csv(inp / "security_daily.csv", FRAME_COLUMNS["security_daily"], sec_daily)
    write_csv(inp / "benchmark_daily.csv", FRAME_COLUMNS["benchmark_daily"], bench_daily)
    # optional failure variant: SECTOR_TECH missing on the date many intervals exit
    gap_date = sessions[25]["session_date"]
    gap_rows = [
        r
        for r in bench_daily
        if not (r["benchmark_id"] == "SECTOR_TECH" and r["session_date"] == gap_date)
    ]
    write_csv(inp / "benchmark_daily_missing_day.csv", FRAME_COLUMNS["benchmark_daily"], gap_rows)
    write_csv(inp / "security_beta.csv", FRAME_COLUMNS["security_beta"], betas)
    write_csv(inp / "delistings.csv", FRAME_COLUMNS["delistings"], delistings)

    sec_levels = tr_levels(sec_daily)
    benches = bench_levels(bench_daily)

    for name, (policy, decay_days, horizons) in {
        "default": ("hard_close", 90, DEFAULT_HORIZONS),
        "decay": ("decay", 5, DEFAULT_HORIZONS),
        "fixed": ("hard_close", 90, FIXED_HORIZONS),
    }.items():
        intervals = build_intervals(
            events, sessions, coverage, employment, delistings, policy, decay_days
        )
        iv_rows = interval_rows(intervals, sessions, policy, decay_days)
        returns, contribs = build_returns_and_contributions(
            iv_rows,
            intervals,
            sessions,
            sec_levels,
            benches,
            delistings,
            policy,
            decay_days,
            horizons,
        )
        returns.sort(key=lambda r: (str(r["interval_id"]), str(r["horizon"])))
        contribs.sort(key=lambda r: (str(r["interval_id"]), str(r["horizon"])))
        write_json(exp / f"intervals_{name}.json", iv_rows)
        write_json(exp / f"returns_{name}.json", returns)
        write_json(exp / f"contributions_{name}.json", contribs)
        reasons = sorted({str(r["exit_reason"]) for r in iv_rows})
        print(
            f"{name}: {len(iv_rows)} intervals, {len(returns)} return rows, "
            f"{len(contribs)} contribution rows; exit reasons: {reasons}"
        )


if __name__ == "__main__":
    main()
