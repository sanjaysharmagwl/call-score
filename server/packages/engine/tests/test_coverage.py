from datetime import UTC, date, datetime, timedelta

import polars as pl

from callscore_core.contracts import RunContext
from callscore_engine.coverage import (
    assert_coverage_daily_contract,
    build_coverage_daily,
)

D0 = date(2024, 1, 1)  # Monday
N_DAYS = 40


def _weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DATES = _weekdays(D0, N_DAYS)


def _ts(d: date, hour: int) -> datetime:
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=UTC)


def _sessions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "exchange_id": ["XNYS"] * N_DAYS,
            "session_date": DATES,
            "open_ts": [_ts(d, 13) for d in DATES],
            "close_ts": [_ts(d, 20) for d in DATES],
        }
    )


def _classifications() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "security_id": ["S1", "S1"],
            "from_date": [date(2020, 1, 1), date(2024, 6, 1)],
            "to_date": [date(2024, 5, 31), None],
            "exchange_id": ["XNYS", "XNYS"],
            "currency": ["USD", "USD"],
            "sector_id": ["TECH", "TECH"],
            "region": ["NA", "NA"],
            "cap_bucket": ["large", "large"],
            "listing_date": [date(2020, 1, 1), date(2020, 1, 1)],
            "delisting_date": [None, None],
        }
    )


def _events() -> pl.DataFrame:
    """Analyst A on S1: buy at idx 6 expiring idx 11, reiterated at idx 16
    expiring idx 26 (refreshes staleness). Analyst B rates S2 once."""
    rows = [
        {
            "event_id": "EV1",
            "analyst_id": "A",
            "firm_id": "F1",
            "security_id": "S1",
            "announced_ts": _ts(DATES[6], 12),
            "effective_ts": _ts(DATES[6], 12),
            "knowledge_ts": _ts(DATES[6], 12),
            "action_type": "initiation",
            "native_rating": "buy",
            "canonical_rating": "buy",
            "prior_canonical_rating": None,
            "scale_id": "s1",
            "price_target": None,
            "pt_currency": None,
            "pt_horizon_months": None,
            "expiry_date": DATES[11],
            "supersedes_event_id": None,
            "is_correction": False,
        },
        {
            "event_id": "EV2",
            "analyst_id": "A",
            "firm_id": "F1",
            "security_id": "S1",
            "announced_ts": _ts(DATES[16], 12),
            "effective_ts": _ts(DATES[16], 12),
            "knowledge_ts": _ts(DATES[16], 12),
            "action_type": "reiteration",
            "native_rating": "buy",
            "canonical_rating": "buy",
            "prior_canonical_rating": "buy",
            "scale_id": "s1",
            "price_target": None,
            "pt_currency": None,
            "pt_horizon_months": None,
            "expiry_date": DATES[26],
            "supersedes_event_id": None,
            "is_correction": False,
        },
        {
            "event_id": "EV3",
            "analyst_id": "B",
            "firm_id": "F1",
            "security_id": "S2",
            "announced_ts": _ts(DATES[2], 12),
            "effective_ts": _ts(DATES[2], 12),
            "knowledge_ts": _ts(DATES[2], 12),
            "action_type": "initiation",
            "native_rating": "buy",
            "canonical_rating": "buy",
            "prior_canonical_rating": None,
            "scale_id": "s1",
            "price_target": None,
            "pt_currency": None,
            "pt_horizon_months": None,
            "expiry_date": DATES[8],
            "supersedes_event_id": None,
            "is_correction": False,
        },
        # A correction published after the knowledge cutoff must not count.
        {
            "event_id": "EV4",
            "analyst_id": "A",
            "firm_id": "F1",
            "security_id": "S1",
            "announced_ts": _ts(DATES[20], 12),
            "effective_ts": _ts(DATES[20], 12),
            "knowledge_ts": _ts(date(2025, 1, 1), 12),
            "action_type": "reiteration",
            "native_rating": "buy",
            "canonical_rating": "buy",
            "prior_canonical_rating": "buy",
            "scale_id": "s1",
            "price_target": None,
            "pt_currency": None,
            "pt_horizon_months": None,
            "expiry_date": DATES[35],
            "supersedes_event_id": None,
            "is_correction": False,
        },
    ]
    return pl.DataFrame(rows)


def _coverage_spells() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "analyst_id": ["A", "B"],
            "security_id": ["S1", "S2"],
            "from_date": [DATES[0], DATES[0]],
            "to_date": [DATES[29], None],
            "is_lead": [True, True],
        }
    )


def _classifications_s2() -> pl.DataFrame:
    cls = _classifications()
    return pl.concat(
        [
            cls,
            pl.DataFrame(
                {
                    "security_id": ["S2"],
                    "from_date": [date(2020, 1, 1)],
                    "to_date": [None],
                    "exchange_id": ["XNYS"],
                    "currency": ["USD"],
                    "sector_id": ["HLTH"],
                    "region": ["NA"],
                    "cap_bucket": ["mid"],
                    "listing_date": [date(2020, 1, 1)],
                    "delisting_date": [None],
                }
            ),
        ]
    )


def _intervals() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "interval_id": pl.String(),
            "event_id": pl.String(),
            "analyst_id": pl.String(),
            "firm_id": pl.String(),
            "security_id": pl.String(),
            "native_rating": pl.String(),
            "canonical_rating": pl.String(),
            "action_type": pl.String(),
            "announced_ts": pl.Datetime("us", "UTC"),
            "effective_ts": pl.Datetime("us", "UTC"),
            "knowledge_ts": pl.Datetime("us", "UTC"),
            "available_ts": pl.Datetime("us", "UTC"),
            "entry_session": pl.Date(),
            "entry_point": pl.String(),
            "entry_ts": pl.Datetime("us", "UTC"),
            "exit_session": pl.Date(),
            "exit_point": pl.String(),
            "exit_ts": pl.Datetime("us", "UTC"),
            "exit_reason": pl.String(),
            "expiry_date": pl.Date(),
            "decay_start_session": pl.Date(),
            "is_investable": pl.Boolean(),
            "non_investable_reason": pl.String(),
            "flags": pl.String(),
        }
    )


def _context(as_of: date | None = None) -> RunContext:
    return RunContext(
        as_of_date=as_of or DATES[34],
        knowledge_cutoff=_ts(DATES[34], 23),
        source_revision="test",
        source_fingerprint="0" * 64,
        universe_fingerprint="0" * 64,
        algorithm_version="test",
    )


def _build(as_of: date | None = None) -> pl.DataFrame:
    return build_coverage_daily(
        _events(),
        _intervals(),
        _coverage_spells(),
        _sessions(),
        _classifications_s2(),
        _context(as_of),
    )


def _stale_dates(out: pl.DataFrame, analyst: str, security: str) -> list[date]:
    return [
        r["session_date"]
        for r in out.iter_rows(named=True)
        if r["analyst_id"] == analyst and r["security_id"] == security and r["is_stale"]
    ]


def test_coverage_daily_staleness_and_refresh() -> None:
    out = _build()
    assert_coverage_daily_contract(out)

    a_rows = [r for r in out.iter_rows(named=True) if r["analyst_id"] == "A"]
    # Spell A/S1 ends at idx 29 (to_date inclusive); as_of idx 34 -> clipped to idx 29.
    assert [r["session_date"] for r in a_rows] == DATES[:30]
    assert all(r["security_id"] == "S1" for r in a_rows)

    stale = _stale_dates(out, "A", "S1")
    # EV1 (avail idx6, expiry idx11) makes idx12..idx15 stale; EV2 at idx16
    # (expiry idx26) refreshes -> idx17..idx26 fine; idx27..idx29 stale again.
    # EV4 is beyond the knowledge cutoff and must not refresh anything.
    assert stale == DATES[12:16] + DATES[27:30]


def test_pre_first_rating_sessions_not_stale() -> None:
    out = _build()
    early = [
        r
        for r in out.iter_rows(named=True)
        if r["analyst_id"] == "B" and r["session_date"] <= DATES[2]
    ]
    # B's first rating is available at idx2 noon, before that session's close,
    # but earlier sessions have no prior rating -> never stale.
    assert early and all(r["is_stale"] is False for r in early)
    # B's rating expires idx8 -> idx9 onward stale (open-ended spell to as_of).
    assert _stale_dates(out, "B", "S2") == DATES[9:35]


def test_spell_boundaries_and_as_of_clipping() -> None:
    # as_of before the spell end clips rows; spell to_date is inclusive.
    out = _build(as_of=DATES[20])
    a_dates = [r["session_date"] for r in out.iter_rows(named=True) if r["analyst_id"] == "A"]
    assert a_dates == DATES[:21]
    # Open-ended spell B/S2 also stops at the last session <= as_of.
    b_dates = [r["session_date"] for r in out.iter_rows(named=True) if r["analyst_id"] == "B"]
    assert b_dates == DATES[:21]


def test_no_coverage_no_rows() -> None:
    spells = _coverage_spells().filter(pl.col("analyst_id") == "A")
    out = build_coverage_daily(
        _events(), _intervals(), spells, _sessions(), _classifications_s2(), _context()
    )
    assert out["analyst_id"].unique().to_list() == ["A"]
