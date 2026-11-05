"""Tests for the PIT consensus_daily frame (hand-built inputs)."""

from datetime import UTC, date, datetime, timedelta

import polars as pl

from callscore_synth.consensus import build_consensus_daily

D0 = date(2024, 1, 2)


def _sessions(n: int = 5) -> pl.DataFrame:
    rows = []
    d = D0
    for _i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append(
            {
                "exchange_id": "XNYS",
                "session_date": d,
                "open_ts": datetime(d.year, d.month, d.day, 14, 30, tzinfo=UTC),
                "close_ts": datetime(d.year, d.month, d.day, 21, 0, tzinfo=UTC),
            }
        )
        d += timedelta(days=1)
    return pl.DataFrame(
        rows,
        schema={
            "exchange_id": pl.String,
            "session_date": pl.Date,
            "open_ts": pl.Datetime("us", "UTC"),
            "close_ts": pl.Datetime("us", "UTC"),
        },
    )


def _daily(sec: str, n: int = 5) -> pl.DataFrame:
    sessions = _sessions(n)
    dates = sessions["session_date"].to_list()
    return pl.DataFrame(
        {
            "security_id": [sec] * n,
            "session_date": dates,
            "open": [10.0] * n,
            "close": [10.0] * n,
            "high": [10.5] * n,
            "low": [9.5] * n,
            "adj_factor": [1.0] * n,
            "cash_dividend": [None] * n,
            "tr_index": [100.0 + i for i in range(n)],
            "is_halted": [False] * n,
        },
        schema={
            "security_id": pl.String,
            "session_date": pl.Date,
            "open": pl.Float64,
            "close": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "adj_factor": pl.Float64,
            "cash_dividend": pl.Float64,
            "tr_index": pl.Float64,
            "is_halted": pl.Boolean,
        },
    )


def _event(
    eid: str,
    aid: str,
    sid: str,
    announced: datetime,
    canonical: str,
    *,
    effective: datetime | None = None,
    knowledge: datetime | None = None,
    expiry: date | None = None,
    is_correction: bool = False,
    supersedes: str | None = None,
    pt: float | None = None,
) -> dict[str, object]:
    eff = effective or announced
    kno = knowledge or eff
    return {
        "event_id": eid,
        "analyst_id": aid,
        "firm_id": "F1",
        "security_id": sid,
        "announced_ts": announced,
        "effective_ts": eff,
        "knowledge_ts": kno,
        "action_type": "initiation",
        "native_rating": canonical,
        "canonical_rating": canonical,
        "prior_canonical_rating": None,
        "scale_id": "F1:five",
        "price_target": pt,
        "pt_currency": "USD" if pt is not None else None,
        "pt_horizon_months": 12 if pt is not None else None,
        "expiry_date": expiry,
        "supersedes_event_id": supersedes,
        "is_correction": is_correction,
    }


def _frames(events: list[dict[str, object]], n_sessions: int = 5) -> dict[str, pl.DataFrame]:
    schema = {
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
    return {
        "rating_events": pl.DataFrame(events, schema=schema),
        "sessions": _sessions(n_sessions),
        "security_daily": _daily("S1", n_sessions),
    }


def test_empty_inputs() -> None:
    empty_events = pl.DataFrame(schema={c: pl.String for c in ()})
    out = build_consensus_daily(
        {"rating_events": pl.DataFrame(), "sessions": _sessions(), "security_daily": _daily("S1")}
    )
    assert out.height == 0
    del empty_events


def test_simple_consensus_mean() -> None:
    # Two analysts rate before session 1's close: buy (0.5) + strong_buy (1.0).
    t = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)  # before day-1 close (21:00)
    events = [
        _event("E1", "A1", "S1", t, "buy", expiry=date(2024, 3, 1), pt=120.0),
        _event("E2", "A2", "S1", t, "strong_buy", expiry=date(2024, 3, 1)),
    ]
    out = build_consensus_daily(_frames(events))
    # All 5 sessions active -> 5 rows.
    assert out.height == 5
    row = out.row(0, named=True)
    assert row["security_id"] == "S1"
    assert row["n_ratings"] == 2
    assert row["consensus_score"] == 0.75
    assert row["target_price"] == 120.0
    assert row["target_currency"] == "USD"


def test_pit_knowledge_ts_gates_visibility() -> None:
    sessions = _sessions(5)
    # Event announced day 1 but knowledge only arrives day 3 -> visible from
    # the first session whose close is after knowledge.
    t_ann = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    t_kno = sessions.row(2, named=True)["close_ts"] + timedelta(hours=1)
    events = [_event("E1", "A1", "S1", t_ann, "buy", knowledge=t_kno)]
    out = build_consensus_daily(_frames(events))
    first_date = out["session_date"].min()
    assert first_date > date(2024, 1, 4)  # not visible on days 1-2 close basis


def test_expiry_ends_membership() -> None:
    t = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    # expiry = day2 -> active through day2's date (inclusive), gone day3+.
    events = [_event("E1", "A1", "S1", t, "buy", expiry=date(2024, 1, 3))]
    out = build_consensus_daily(_frames(events))
    dates = out["session_date"].to_list()
    assert max(dates) <= date(2024, 1, 3)


def test_correction_supersedes_original() -> None:
    t = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    t2 = t + timedelta(days=1)
    events = [
        _event("E1", "A1", "S1", t, "buy", expiry=date(2024, 3, 1)),
        _event(
            "E2",
            "A1",
            "S1",
            t2,
            "sell",
            effective=t2,
            knowledge=t2,
            expiry=date(2024, 3, 1),
            is_correction=True,
            supersedes="E1",
        ),
    ]
    out = build_consensus_daily(_frames(events))
    # Before the correction is visible the buy counts; after, the sell does.
    early = out.filter(pl.col("session_date") <= date(2024, 1, 3))
    late = out.filter(pl.col("session_date") > date(2024, 1, 4))
    assert early.height > 0 and late.height > 0
    assert early["consensus_score"].max() == 0.5  # buy weight
    assert late["consensus_score"].max() == -0.5  # sell weight
    assert late["n_ratings"].max() == 1


def test_non_weight_ratings_excluded() -> None:
    t = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    events = [
        _event("E1", "A1", "S1", t, "buy", expiry=date(2024, 3, 1)),
        _event("E2", "A2", "S1", t, "under_review", expiry=date(2024, 3, 1)),
        _event("E3", "A3", "S1", t, "hold", expiry=date(2024, 3, 1)),
    ]
    out = build_consensus_daily(_frames(events))
    # under_review excluded; buy + hold -> n=2, mean (0.5+0)/2 = 0.25
    row = out.row(0, named=True)
    assert row["n_ratings"] == 2
    assert row["consensus_score"] == 0.25


def test_latest_event_per_analyst_wins() -> None:
    t = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    t2 = t + timedelta(days=2)
    events = [
        _event("E1", "A1", "S1", t, "buy", expiry=date(2024, 3, 1)),
        _event("E2", "A1", "S1", t2, "strong_sell", expiry=date(2024, 3, 1)),
    ]
    out = build_consensus_daily(_frames(events))
    late = out.filter(pl.col("session_date") > date(2024, 1, 5))
    assert late.height > 0
    assert (late["consensus_score"] == -1.0).all()
    assert (late["n_ratings"] == 1).all()
