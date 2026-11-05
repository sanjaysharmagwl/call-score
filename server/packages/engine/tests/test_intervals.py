"""Interval construction: entry conventions, exits, expiry policies, PIT."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.enums import EntryConvention, ExpiryPolicy
from callscore_core.methodology import Methodology
from callscore_engine.intervals import build_intervals

XNYS = "XNYS"
AS_OF = date(2024, 1, 23)
CUTOFF = datetime(2024, 12, 31, tzinfo=UTC)


def _ts(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _weekdays(start: date, n: int, holidays: set[date] | None = None) -> list[date]:
    holidays = holidays or set()
    out: list[date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5 and day not in holidays:
            out.append(day)
        day += timedelta(days=1)
    return out


# 15 sessions: Tue 2024-01-02 .. Tue 2024-01-22 (weekdays only).
DATES = _weekdays(date(2024, 1, 2), 15)


def _sessions(
    dates: list[date] | None = None,
    open_hm: tuple[int, int] = (14, 30),
    close_hm: tuple[int, int] = (21, 0),
) -> pl.DataFrame:
    dates = DATES if dates is None else dates
    return pl.DataFrame(
        {
            "exchange_id": [XNYS] * len(dates),
            "session_date": dates,
            "open_ts": [_ts(d, *open_hm) for d in dates],
            "close_ts": [_ts(d, *close_hm) for d in dates],
        }
    )


def _event(
    event_id: str,
    day: date,
    hour: int = 12,
    minute: int = 0,
    *,
    knowledge: datetime | None = None,
    announced: datetime | None = None,
    effective: datetime | None = None,
    action: str = "initiation",
    native: str = "Buy",
    canonical: str = "buy",
    expiry: date | None = None,
    supersedes: str | None = None,
    is_correction: bool = False,
    analyst: str = "A1",
    security: str = "S1",
    firm: str = "F1",
) -> dict[str, object]:
    base = _ts(day, hour, minute)
    return {
        "event_id": event_id,
        "analyst_id": analyst,
        "firm_id": firm,
        "security_id": security,
        "announced_ts": announced if announced is not None else base,
        "effective_ts": effective if effective is not None else base,
        "knowledge_ts": knowledge if knowledge is not None else base,
        "action_type": action,
        "native_rating": native,
        "canonical_rating": canonical,
        "prior_canonical_rating": None,
        "scale_id": "scale1",
        "price_target": None,
        "pt_currency": None,
        "pt_horizon_months": None,
        "expiry_date": expiry,
        "supersedes_event_id": supersedes,
        "is_correction": is_correction,
    }


def _context(as_of: date = AS_OF, cutoff: datetime = CUTOFF) -> RunContext:
    return RunContext(
        as_of_date=as_of,
        knowledge_cutoff=cutoff,
        source_revision="rev1",
        source_fingerprint="0" * 64,
        universe_fingerprint="1" * 64,
        algorithm_version="v1",
    )


def _methodology(**overrides: Any) -> Methodology:
    return Methodology(**overrides)


def _coverage(
    analyst: str = "A1",
    security: str = "S1",
    start: date = date(2024, 1, 1),
    end: date | None = None,
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "analyst_id": [analyst],
            "security_id": [security],
            "from_date": [start],
            "to_date": [end],
            "is_lead": [True],
        }
    )


def _employment(
    analyst: str = "A1",
    firm: str = "F1",
    start: date = date(2024, 1, 1),
    end: date | None = None,
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "analyst_id": [analyst],
            "firm_id": [firm],
            "from_date": [start],
            "to_date": [end],
        }
    )


def _classification(
    security: str = "S1",
    start: date = date(2024, 1, 1),
    end: date | None = None,
    listing: date = date(2024, 1, 1),
    exchange: str = XNYS,
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "security_id": [security],
            "from_date": [start],
            "to_date": [end],
            "exchange_id": [exchange],
            "currency": ["USD"],
            "sector_id": ["TECH"],
            "region": ["NA"],
            "cap_bucket": ["large"],
            "listing_date": [listing],
            "delisting_date": [None],
        }
    )


def _delisting(security: str, day: date) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "security_id": [security],
            "delisting_date": [day],
            "final_return": [-1.0],
            "reason": ["bankrupt"],
        }
    )


_EVENT_SCHEMA: dict[str, pl.DataType] = {
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


def _build(
    events: list[dict[str, object]],
    *,
    sessions: pl.DataFrame | None = None,
    coverage: pl.DataFrame | None = None,
    employment: pl.DataFrame | None = None,
    classifications: pl.DataFrame | None = None,
    delistings: pl.DataFrame | None = None,
    methodology: Methodology | None = None,
    as_of: date = AS_OF,
    cutoff: datetime = CUTOFF,
) -> pl.DataFrame:
    return build_intervals(
        events=pl.DataFrame(events, schema=_EVENT_SCHEMA),
        sessions=sessions if sessions is not None else _sessions(),
        coverage_spells=coverage if coverage is not None else _coverage(),
        employment_spells=employment if employment is not None else _employment(),
        classifications=classifications if classifications is not None else _classification(),
        delistings=delistings if delistings is not None else pl.DataFrame(),
        methodology=methodology if methodology is not None else _methodology(),
        context=_context(as_of, cutoff),
    )


def _one(event: dict[str, object], **kwargs: Any) -> dict[str, Any]:
    df = _build([event], **kwargs)
    assert df.height == 1
    return df.to_dicts()[0]


# --- entry conventions ---------------------------------------------------------


def test_next_open_premarket_intraday_postclose() -> None:
    day = DATES[0]
    pre = _one(_event("E1", day, 12))
    assert (pre["entry_session"], pre["entry_point"]) == (day, "open")
    assert pre["entry_ts"] == _ts(day, 14, 30)

    intra = _one(_event("E2", day, 15))
    assert intra["entry_session"] == DATES[1]
    assert intra["entry_ts"] == _ts(DATES[1], 14, 30)

    post = _one(_event("E3", day, 22))
    assert post["entry_session"] == DATES[1]


def test_next_open_skips_weekend_and_holiday() -> None:
    # Friday Jan 5 post-close -> Monday Jan 8 open; drop Jan 8 as a holiday
    # -> Tuesday Jan 9.
    fri = DATES[3]
    assert fri == date(2024, 1, 5)
    row = _one(_event("E1", fri, 22))
    assert row["entry_session"] == DATES[4]  # Monday Jan 8

    sessions = _sessions(_weekdays(date(2024, 1, 2), 15, holidays={DATES[4]}))
    row = _one(_event("E2", fri, 22), sessions=sessions)
    assert row["entry_session"] == DATES[5]  # Tuesday Jan 9


def test_same_close_convention() -> None:
    meth = _methodology(entry_convention=EntryConvention.SAME_CLOSE)
    day = DATES[0]
    pre = _one(_event("E1", day, 12), methodology=meth)
    assert (pre["entry_session"], pre["entry_point"]) == (day, "close")
    assert pre["entry_ts"] == _ts(day, 21, 0)

    # Intraday before the close still enters that day's close.
    intra = _one(_event("E2", day, 15), methodology=meth)
    assert (intra["entry_session"], intra["entry_point"]) == (day, "close")

    # Post-close -> next session's close.
    post = _one(_event("E3", day, 22), methodology=meth)
    assert post["entry_session"] == DATES[1]
    assert post["entry_point"] == "close"


def test_t_plus_2_open_convention() -> None:
    meth = _methodology(entry_convention=EntryConvention.T_PLUS_2_OPEN)
    day = DATES[0]
    row = _one(_event("E1", day, 12), methodology=meth)
    # next_open would be day0; t+2 open is the session after that.
    assert row["entry_session"] == DATES[1]
    assert row["entry_point"] == "open"
    assert row["entry_ts"] == _ts(DATES[1], 14, 30)


def test_entry_uses_dst_shifted_utc_timestamps() -> None:
    # DST begins 2024-03-10: opens move 14:30 -> 13:30 UTC.
    dates = [date(2024, 3, 7), date(2024, 3, 8), date(2024, 3, 11)]
    opens = [_ts(d, 14, 30) if d < date(2024, 3, 10) else _ts(d, 13, 30) for d in dates]
    closes = [_ts(d, 21, 0) if d < date(2024, 3, 10) else _ts(d, 20, 0) for d in dates]
    sessions = pl.DataFrame(
        {
            "exchange_id": [XNYS] * 3,
            "session_date": dates,
            "open_ts": opens,
            "close_ts": closes,
        }
    )
    as_of = date(2024, 3, 12)
    row = _one(
        _event("E1", dates[1], 22),
        sessions=sessions,
        as_of=as_of,
        cutoff=datetime(2024, 12, 31, tzinfo=UTC),
    )
    # Friday post-close -> Monday open at 13:30 UTC.
    assert row["entry_session"] == dates[2]
    assert row["entry_ts"] == _ts(dates[2], 13, 30)


# --- interval chaining ---------------------------------------------------------


def test_rating_change_supersedes_previous_interval() -> None:
    df = _build(
        [
            _event("E1", DATES[0], 12, canonical="buy"),
            _event("E2", DATES[1], 12, action="downgrade", canonical="hold"),
        ]
    )
    assert df.height == 2
    first, second = df.sort("entry_ts").to_dicts()
    assert first["interval_id"] == "E1"
    assert first["exit_reason"] == "superseded"
    # Exit is the superseding interval's entry point (E2 next_open).
    assert first["exit_session"] == DATES[1]
    assert first["exit_point"] == "open"
    assert first["exit_ts"] == _ts(DATES[1], 14, 30)
    assert second["exit_reason"] == "run_end"
    assert second["exit_session"] == DATES[-1]
    assert second["exit_ts"] == _ts(DATES[-1], 21, 0)


def test_same_rating_actions_extend_expiry_without_new_interval() -> None:
    df = _build(
        [
            _event("E1", DATES[0], 12, canonical="buy", expiry=date(2024, 1, 5)),
            _event(
                "E2",
                DATES[1],
                12,
                action="reiteration",
                canonical="buy",
                expiry=date(2024, 1, 10),
            ),
            _event(
                "E3",
                DATES[1],
                15,
                action="pt_change",
                canonical="buy",
                expiry=None,
            ),
        ]
    )
    # Only one interval: E2/E3 are same-rating actions.
    assert df.height == 1
    row = df.to_dicts()[0]
    assert row["interval_id"] == "E1"
    # Expiry extended to the newest non-null expiry in the run (Jan 10).
    assert row["expiry_date"] == date(2024, 1, 10)
    # hard_close: exit at close of last session on/before Jan 10 = Jan 10.
    assert row["exit_reason"] == "expired"
    assert row["exit_session"] == date(2024, 1, 10)
    assert row["exit_point"] == "close"


def test_reiteration_with_null_expiry_keeps_prior_expiry() -> None:
    df = _build(
        [
            _event("E1", DATES[0], 12, canonical="buy", expiry=date(2024, 1, 5)),
            _event("E2", DATES[1], 12, action="reiteration", canonical="buy"),
        ]
    )
    row = df.to_dicts()[0]
    assert row["expiry_date"] == date(2024, 1, 5)


def test_drop_ends_interval_and_opens_none() -> None:
    df = _build(
        [
            _event("E1", DATES[0], 12, canonical="buy"),
            _event("E2", DATES[1], 12, action="drop", canonical="not_rated"),
            _event("E3", DATES[2], 12, action="initiation", canonical="sell"),
        ]
    )
    assert df.height == 2
    rows = {r["interval_id"]: r for r in df.to_dicts()}
    # E1 exits at the drop event's entry point.
    assert rows["E1"]["exit_reason"] == "dropped"
    assert rows["E1"]["exit_session"] == DATES[1]
    assert rows["E1"]["exit_point"] == "open"
    assert rows["E1"]["exit_ts"] == _ts(DATES[1], 14, 30)
    # E3 opens a fresh interval (drop reset the rating state).
    assert rows["E3"]["canonical_rating"] == "sell"
    assert rows["E3"]["exit_reason"] == "run_end"


def test_non_investable_canonical_maps_through() -> None:
    for action, canonical in [
        ("suspend", "under_review"),
        ("restrict", "restricted"),
        ("initiation", "not_rated"),
    ]:
        row = _one(_event("E1", DATES[0], 12, action=action, canonical=canonical))
        assert row["is_investable"] is False
        assert row["non_investable_reason"] == canonical
        assert row["canonical_rating"] == canonical
        assert row["action_type"] == action


def test_resume_reopens_investable_interval() -> None:
    df = _build(
        [
            _event("E1", DATES[0], 12, canonical="buy"),
            _event("E2", DATES[1], 12, action="suspend", canonical="under_review"),
            _event("E3", DATES[2], 12, action="resume", canonical="buy"),
        ]
    )
    rows = {r["interval_id"]: r for r in df.to_dicts()}
    assert rows["E2"]["is_investable"] is False
    assert rows["E2"]["non_investable_reason"] == "under_review"
    assert rows["E3"]["is_investable"] is True
    assert rows["E1"]["exit_reason"] == "superseded"
    assert rows["E2"]["exit_reason"] == "superseded"


# --- expiry policies -----------------------------------------------------------


def test_hard_close_expiry() -> None:
    row = _one(_event("E1", DATES[0], 12, canonical="buy", expiry=date(2024, 1, 5)))
    assert row["exit_reason"] == "expired"
    assert row["exit_session"] == date(2024, 1, 5)  # Friday is a session
    assert row["exit_point"] == "close"
    assert row["exit_ts"] == _ts(date(2024, 1, 5), 21, 0)
    assert row["decay_start_session"] is None


def test_expiry_uses_last_session_on_or_before_expiry_date() -> None:
    # Expiry on Saturday Jan 6 -> last session on/before is Friday Jan 5.
    row = _one(_event("E1", DATES[0], 12, canonical="buy", expiry=date(2024, 1, 6)))
    assert row["exit_session"] == date(2024, 1, 5)


def test_carry_forward_ignores_expiry() -> None:
    meth = _methodology(expiry_policy=ExpiryPolicy.CARRY_FORWARD)
    row = _one(
        _event("E1", DATES[0], 12, canonical="buy", expiry=date(2024, 1, 5)),
        methodology=meth,
    )
    assert row["exit_reason"] == "run_end"
    assert row["expiry_date"] == date(2024, 1, 5)  # still recorded


def test_decay_policy_exit_and_decay_start() -> None:
    meth = _methodology(expiry_policy=ExpiryPolicy.DECAY, decay_days=2)
    row = _one(
        _event("E1", DATES[0], 12, canonical="buy", expiry=date(2024, 1, 5)),
        methodology=meth,
    )
    # expiry session = Jan 5 (Fri); decay_start = next session Jan 8 (Mon);
    # exit = close of session 2 after expiry session = Jan 9.
    assert row["decay_start_session"] == date(2024, 1, 8)
    assert row["exit_reason"] == "expired"
    assert row["exit_session"] == date(2024, 1, 9)
    assert row["exit_ts"] == _ts(date(2024, 1, 9), 21, 0)


def test_decay_beyond_calendar_falls_back_to_run_end() -> None:
    meth = _methodology(expiry_policy=ExpiryPolicy.DECAY, decay_days=500)
    row = _one(
        _event("E1", DATES[0], 12, canonical="buy", expiry=date(2024, 1, 5)),
        methodology=meth,
    )
    # No session 500 after Jan 5 -> only run_end bounds the interval.
    assert row["exit_reason"] == "run_end"
    assert row["decay_start_session"] == date(2024, 1, 8)


# --- exit reasons ---------------------------------------------------------------


def test_coverage_end_exit() -> None:
    coverage = _coverage(end=date(2024, 1, 4))
    row = _one(_event("E1", DATES[0], 12), coverage=coverage)
    assert row["exit_reason"] == "coverage_ended"
    assert row["exit_session"] == date(2024, 1, 4)
    assert row["exit_point"] == "close"


def test_employment_end_exit() -> None:
    employment = _employment(end=date(2024, 1, 4))
    row = _one(_event("E1", DATES[0], 12), employment=employment)
    assert row["exit_reason"] == "analyst_left"
    assert row["exit_session"] == date(2024, 1, 4)


def test_delisting_exit() -> None:
    row = _one(
        _event("E1", DATES[0], 12),
        delistings=_delisting("S1", date(2024, 1, 9)),
    )
    assert row["exit_reason"] == "delisted"
    assert row["exit_session"] == date(2024, 1, 9)
    assert row["exit_point"] == "close"


def test_tie_precedence_delisted_beats_coverage() -> None:
    # Coverage ends and the security delists on the same session close:
    # delisted has higher precedence.
    coverage = _coverage(end=date(2024, 1, 9))
    row = _one(
        _event("E1", DATES[0], 12),
        coverage=coverage,
        delistings=_delisting("S1", date(2024, 1, 9)),
    )
    assert row["exit_reason"] == "delisted"
    assert row["exit_session"] == date(2024, 1, 9)


def test_tie_precedence_coverage_beats_expired() -> None:
    coverage = _coverage(end=date(2024, 1, 9))
    row = _one(
        _event("E1", DATES[0], 12, expiry=date(2024, 1, 9)),
        coverage=coverage,
    )
    # Both exit candidates land on Jan 9 close; coverage_ended wins.
    assert row["exit_reason"] == "coverage_ended"


def test_earliest_exit_wins_over_run_end() -> None:
    # Expiry Jan 5 < run_end Jan 22 -> expired (covered), sanity on ordering.
    row = _one(_event("E1", DATES[0], 12, expiry=date(2024, 1, 5)))
    assert row["exit_ts"] < _ts(DATES[-1], 21, 0)


# --- non-positive intervals ------------------------------------------------------


def test_after_coverage_end_is_non_investable() -> None:
    # Coverage ended Jan 3; event arrives post-close Jan 3 -> entry Jan 4 open,
    # but the coverage exit (Jan 3 close) precedes it.
    coverage = _coverage(end=date(2024, 1, 3))
    row = _one(_event("E1", DATES[1], 22), coverage=coverage)
    assert row["entry_session"] == DATES[2]
    assert row["exit_reason"] == "coverage_ended"
    assert row["exit_session"] == DATES[1]
    assert row["is_investable"] is False
    assert row["non_investable_reason"] == "after_coverage_end"
    assert "non_positive_length" in (row["flags"] or "")


def test_after_delisting_is_non_investable() -> None:
    row = _one(
        _event("E1", DATES[1], 22),
        delistings=_delisting("S1", DATES[1]),
    )
    assert row["exit_reason"] == "delisted"
    assert row["is_investable"] is False
    assert row["non_investable_reason"] == "after_delisting"


def test_after_run_end_is_non_investable() -> None:
    # Event after the last session: entry falls back to run-end close,
    # producing a zero-length interval flagged after_run_end.
    row = _one(_event("E1", AS_OF, 12))
    assert row["exit_reason"] == "run_end"
    assert row["is_investable"] is False
    assert row["non_investable_reason"] == "after_run_end"
    assert row["entry_ts"] == row["exit_ts"]


def test_before_listing_is_non_investable() -> None:
    classifications = _classification(listing=date(2024, 1, 10))
    row = _one(_event("E1", DATES[0], 12), classifications=classifications)
    assert row["entry_session"] == DATES[0]
    assert row["is_investable"] is False
    assert row["non_investable_reason"] == "before_listing"
    assert "before_listing" in (row["flags"] or "")


def test_shorter_than_one_session() -> None:
    # same_close: E1 enters Jan 2 close; E2 (Jan 2 15:00, still before the
    # 21:00 close) also enters Jan 2 close -> E1's exit equals its entry.
    meth = _methodology(entry_convention=EntryConvention.SAME_CLOSE)
    df = _build(
        [
            _event("E1", DATES[0], 12, canonical="buy"),
            _event("E2", DATES[0], 15, action="downgrade", canonical="hold"),
        ],
        methodology=meth,
    )
    rows = {r["interval_id"]: r for r in df.to_dicts()}
    first = rows["E1"]
    assert first["exit_reason"] == "superseded"
    assert first["exit_ts"] == first["entry_ts"]
    assert first["is_investable"] is False
    assert first["non_investable_reason"] == "shorter_than_one_session"
    assert "non_positive_length" in (first["flags"] or "")


# --- point-in-time integration ----------------------------------------------------


def test_correction_chain_visible_at_cutoff() -> None:
    cutoff = _ts(DATES[4], 12)  # Wednesday Jan 10 noon
    df = _build(
        [
            _event("E1", DATES[0], 12, canonical="buy"),
            _event(
                "C1",
                DATES[1],
                12,
                canonical="hold",
                supersedes="E1",
                is_correction=True,
            ),
            _event(
                "C2",
                DATES[2],
                12,
                canonical="sell",
                supersedes="C1",
                is_correction=True,
                knowledge=_ts(DATES[6], 12),  # after cutoff -> invisible
            ),
        ],
        cutoff=cutoff,
    )
    # E1 superseded by visible C1; C1 survives (C2 not yet visible).
    assert df.height == 1
    row = df.to_dicts()[0]
    assert row["interval_id"] == "C1"
    assert row["canonical_rating"] == "hold"
    assert row["event_id"] == "C1"


def test_event_after_knowledge_cutoff_produces_no_interval() -> None:
    cutoff = _ts(DATES[1], 12)  # Jan 3 noon
    df = _build(
        [
            _event("E1", DATES[0], 12, canonical="buy"),
            _event(
                "E2",
                DATES[2],
                12,
                canonical="hold",
                knowledge=_ts(DATES[5], 12),
            ),
        ],
        cutoff=cutoff,
    )
    assert df.height == 1
    assert df.to_dicts()[0]["interval_id"] == "E1"
    # E1 is not superseded (E2 invisible), so it runs to run_end.
    assert df.to_dicts()[0]["exit_reason"] == "run_end"


def test_output_conforms_to_contract_shape() -> None:
    df = _build(
        [
            _event("E1", DATES[0], 12, canonical="buy", expiry=date(2024, 1, 5)),
            _event("E2", DATES[1], 12, action="downgrade", canonical="sell"),
        ]
    )
    from callscore_core.frames import INTERVAL_FRAME

    assert df.columns == list(INTERVAL_FRAME.column_names)
    assert df.height == 2
    # sort_by order
    assert df.equals(df.sort(*INTERVAL_FRAME.sort_by))


def test_empty_events_produce_empty_contract_frame() -> None:
    df = _build([])
    assert df.height == 0
    from callscore_core.frames import INTERVAL_FRAME

    assert df.columns == list(INTERVAL_FRAME.column_names)
