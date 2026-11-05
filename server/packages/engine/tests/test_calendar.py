"""Trading-calendar helpers: strictly-after timestamp lookups on UTC sessions."""

from datetime import UTC, date, datetime

import polars as pl
import pytest

from callscore_engine.calendar import (
    GLOBAL_EXCHANGE,
    ExchangeCalendar,
    SessionPoint,
    build_calendars,
    global_calendar,
)

# Winter week: Mon 2024-01-01 is a holiday; sessions Jan 2-5, 8-12 (UTC times).
DATES = [
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
    date(2024, 1, 9),
    date(2024, 1, 10),
    date(2024, 1, 11),
    date(2024, 1, 12),
]


def _ts(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _sessions(
    dates: list[date],
    open_hm: tuple[int, int] = (14, 30),
    close_hm: tuple[int, int] = (21, 0),
    exchange: str = "XNYS",
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "exchange_id": [exchange] * len(dates),
            "session_date": dates,
            "open_ts": [_ts(d, *open_hm) for d in dates],
            "close_ts": [_ts(d, *close_hm) for d in dates],
        }
    )


def _cal() -> ExchangeCalendar:
    return build_calendars(_sessions(DATES))["XNYS"]


def test_first_open_after_premarket_is_same_day() -> None:
    cal = _cal()
    # 12:00 UTC is before the 14:30 open -> same session's open.
    point = cal.first_open_after(_ts(DATES[0], 12, 0))
    assert point == SessionPoint(DATES[0], "open", _ts(DATES[0], 14, 30))


def test_first_open_after_intraday_is_next_session() -> None:
    cal = _cal()
    # 15:00 UTC is inside the session -> next session's open (skips weekend).
    point = cal.first_open_after(_ts(DATES[3], 15, 0))  # Friday intraday
    assert point == SessionPoint(DATES[4], "open", _ts(DATES[4], 14, 30))


def test_first_open_after_is_strict_at_the_open() -> None:
    cal = _cal()
    # Exactly at the open timestamp -> strictly after means next session.
    point = cal.first_open_after(_ts(DATES[0], 14, 30))
    assert point == SessionPoint(DATES[1], "open", _ts(DATES[1], 14, 30))


def test_first_open_after_post_close_and_after_last() -> None:
    cal = _cal()
    point = cal.first_open_after(_ts(DATES[0], 22, 0))
    assert point == SessionPoint(DATES[1], "open", _ts(DATES[1], 14, 30))
    assert cal.first_open_after(_ts(DATES[-1], 23, 0)) is None


def test_first_close_after() -> None:
    cal = _cal()
    # Pre-market -> same day close.
    assert cal.first_close_after(_ts(DATES[0], 12, 0)) == SessionPoint(
        DATES[0], "close", _ts(DATES[0], 21, 0)
    )
    # Intraday before close -> same day close.
    assert cal.first_close_after(_ts(DATES[0], 15, 0)) == SessionPoint(
        DATES[0], "close", _ts(DATES[0], 21, 0)
    )
    # Exactly at close -> next session close.
    assert cal.first_close_after(_ts(DATES[0], 21, 0)) == SessionPoint(
        DATES[1], "close", _ts(DATES[1], 21, 0)
    )
    # After last close -> None.
    assert cal.first_close_after(_ts(DATES[-1], 22, 0)) is None


def test_open_after_session_skips_weekend() -> None:
    cal = _cal()
    # Friday Jan 5 -> Monday Jan 8 open.
    point = cal.open_after_session(DATES[3])
    assert point == SessionPoint(DATES[4], "open", _ts(DATES[4], 14, 30))
    assert cal.open_after_session(DATES[-1]) is None


def test_last_session_on_or_before_date() -> None:
    cal = _cal()
    # Saturday -> Friday; holiday Monday Jan 1 -> no session (None);
    # mid-week date -> that day.
    assert cal.last_session_on_or_before(date(2024, 1, 6)) == DATES[3]
    assert cal.last_session_on_or_before(date(2024, 1, 1)) is None
    assert cal.last_session_on_or_before(DATES[5]) == DATES[5]
    assert cal.last_session_on_or_before(date(2024, 1, 20)) == DATES[-1]


def test_first_session_after_and_offset() -> None:
    cal = _cal()
    assert cal.first_session_after(DATES[3]) == DATES[4]  # Fri -> Mon
    assert cal.first_session_after(date(2024, 1, 6)) == DATES[4]
    # session_offset: 2 sessions after Jan 3 (Thu) -> Jan 5? indices: Jan3=1,
    # +2 -> index 3 = Jan 5 (Fri). Wait Jan 4 is index 2, Jan 5 index 3. Yes.
    assert cal.session_offset(DATES[1], 2) == DATES[3]
    assert cal.session_offset(DATES[-1], 1) is None


def test_close_ts_for() -> None:
    cal = _cal()
    assert cal.close_ts_for(DATES[2]) == _ts(DATES[2], 21, 0)
    assert cal.close_ts_for(date(2024, 1, 6)) is None  # Saturday, not a session


def test_dst_week_uses_actual_utc_timestamps() -> None:
    # US DST starts 2024-03-10: local 09:30 open becomes 13:30 UTC (was 14:30).
    dates = [date(2024, 3, 8), date(2024, 3, 11), date(2024, 3, 12)]
    opens = [_ts(d, 14, 30) if d < date(2024, 3, 10) else _ts(d, 13, 30) for d in dates]
    closes = [_ts(d, 21, 0) if d < date(2024, 3, 10) else _ts(d, 20, 0) for d in dates]
    frame = pl.DataFrame(
        {
            "exchange_id": ["XNYS"] * 3,
            "session_date": dates,
            "open_ts": opens,
            "close_ts": closes,
        }
    )
    cal = build_calendars(frame)["XNYS"]
    # Friday Mar 8 post-close -> Monday Mar 11 open at 13:30 UTC.
    point = cal.first_open_after(_ts(dates[0], 22, 0))
    assert point == SessionPoint(dates[1], "open", _ts(dates[1], 13, 30))
    # A timestamp between the old and new UTC open still resolves by real ts.
    assert cal.first_open_after(_ts(dates[1], 13, 0)) == SessionPoint(
        dates[1], "open", _ts(dates[1], 13, 30)
    )
    assert cal.first_open_after(_ts(dates[1], 14, 0)) == SessionPoint(
        dates[2], "open", _ts(dates[2], 13, 30)
    )


def test_build_calendars_multiple_exchanges_and_global() -> None:
    frame = pl.concat(
        [
            _sessions(DATES[:3], exchange="XNYS"),
            _sessions(
                [date(2024, 1, 2), date(2024, 1, 3)],
                open_hm=(1, 0),
                close_hm=(8, 0),
                exchange="XLON",
            ),
        ]
    )
    calendars = build_calendars(frame)
    assert set(calendars) == {"XNYS", "XLON"}
    assert len(calendars["XNYS"]) == 3
    assert len(calendars["XLON"]) == 2
    gcal = global_calendar(frame)
    assert gcal.exchange_id == GLOBAL_EXCHANGE
    # Merged calendar keeps unique sorted dates (union across exchanges).
    assert gcal.session_dates == (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))


def test_non_monotonic_sessions_rejected() -> None:
    # Dates ascending but timestamps descending -> monotonic check fails.
    bad = pl.DataFrame(
        {
            "exchange_id": ["XNYS", "XNYS"],
            "session_date": DATES[:2],
            "open_ts": [_ts(DATES[1], 14, 30), _ts(DATES[0], 14, 30)],
            "close_ts": [_ts(DATES[1], 21, 0), _ts(DATES[0], 21, 0)],
        }
    )
    with pytest.raises(ValueError, match="monotonic"):
        build_calendars(bad)


def test_missing_columns_rejected() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        build_calendars(pl.DataFrame({"exchange_id": ["XNYS"]}))


def test_empty_sessions() -> None:
    empty = pl.DataFrame(
        {
            "exchange_id": pl.Series([], dtype=pl.String),
            "session_date": pl.Series([], dtype=pl.Date),
            "open_ts": pl.Series([], dtype=pl.Datetime("us", "UTC")),
            "close_ts": pl.Series([], dtype=pl.Datetime("us", "UTC")),
        }
    )
    assert build_calendars(empty) == {}
