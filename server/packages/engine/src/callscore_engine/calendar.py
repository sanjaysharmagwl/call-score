"""Per-exchange trading calendars over ``SESSION_FRAME`` rows (decision 0003).

Every lookup runs on session arrays sorted by ``session_date`` via binary
search — never weekday arithmetic — and all timestamps are UTC, so DST
transitions need no special handling (a US DST week simply has different
``open_ts``/``close_ts`` UTC values on the same local clock times).

Spells and events are resolved against *dates*; execution points carry the
session's actual UTC timestamp, so a "strictly after" comparison is always a
timestamp comparison.
"""

import bisect
from dataclasses import dataclass
from datetime import date, datetime
from typing import NamedTuple

import polars as pl

from callscore_core.frames import POINT_CLOSE, POINT_OPEN


class SessionPoint(NamedTuple):
    """An execution point: a session date, ``"open"``/``"close"`` and its UTC ts."""

    session_date: date
    point: str
    ts: datetime


@dataclass(frozen=True)
class ExchangeCalendar:
    """Sorted session arrays for one exchange (or the ``"*"`` fallback merge)."""

    exchange_id: str
    session_dates: tuple[date, ...]
    open_ts: tuple[datetime, ...]
    close_ts: tuple[datetime, ...]

    def __len__(self) -> int:
        return len(self.session_dates)

    def _index_on_or_before(self, day: date) -> int | None:
        i = bisect.bisect_right(self.session_dates, day) - 1
        return i if i >= 0 else None

    # -- timestamp lookups ("strictly after", per entry conventions) -------------

    def first_open_after(self, ts: datetime) -> SessionPoint | None:
        """First session whose open is strictly after ``ts`` (``next_open``)."""
        i = bisect.bisect_right(self.open_ts, ts)
        if i >= len(self.session_dates):
            return None
        return SessionPoint(self.session_dates[i], POINT_OPEN, self.open_ts[i])

    def first_close_after(self, ts: datetime) -> SessionPoint | None:
        """First session whose close is strictly after ``ts`` (``same_close``)."""
        i = bisect.bisect_right(self.close_ts, ts)
        if i >= len(self.session_dates):
            return None
        return SessionPoint(self.session_dates[i], POINT_CLOSE, self.close_ts[i])

    def open_after_session(self, session_date: date) -> SessionPoint | None:
        """Open of the first session strictly after ``session_date``."""
        i = bisect.bisect_right(self.session_dates, session_date)
        if i >= len(self.session_dates):
            return None
        return SessionPoint(self.session_dates[i], POINT_OPEN, self.open_ts[i])

    # -- date lookups ------------------------------------------------------------

    def last_session_on_or_before(self, day: date) -> date | None:
        """Last session date on or before ``day`` (spell ends are inclusive)."""
        i = self._index_on_or_before(day)
        return self.session_dates[i] if i is not None else None

    def first_session_after(self, day: date) -> date | None:
        """First session date strictly after ``day``."""
        i = bisect.bisect_right(self.session_dates, day)
        return self.session_dates[i] if i < len(self.session_dates) else None

    def session_offset(self, session_date: date, n: int) -> date | None:
        """Session ``n`` positions after ``session_date`` (``n=1`` is next session)."""
        i = self._index_on_or_before(session_date)
        if i is None:
            return None
        j = i + n
        return self.session_dates[j] if j < len(self.session_dates) else None

    def close_ts_for(self, session_date: date) -> datetime | None:
        i = bisect.bisect_left(self.session_dates, session_date)
        if i < len(self.session_dates) and self.session_dates[i] == session_date:
            return self.close_ts[i]
        return None

    def open_ts_for(self, session_date: date) -> datetime | None:
        i = bisect.bisect_left(self.session_dates, session_date)
        if i < len(self.session_dates) and self.session_dates[i] == session_date:
            return self.open_ts[i]
        return None


def _calendar_from_rows(
    exchange_id: str, rows: list[tuple[date, datetime, datetime]]
) -> ExchangeCalendar:
    rows = sorted(rows, key=lambda r: r[0])
    dates = tuple(r[0] for r in rows)
    opens = tuple(r[1] for r in rows)
    closes = tuple(r[2] for r in rows)
    if opens != tuple(sorted(opens)) or closes != tuple(sorted(closes)):
        raise ValueError(f"Session timestamps are not monotonic for exchange {exchange_id!r}")
    return ExchangeCalendar(exchange_id, dates, opens, closes)


def build_calendars(sessions: pl.DataFrame) -> dict[str, ExchangeCalendar]:
    """One ``ExchangeCalendar`` per ``exchange_id`` in ``sessions``, sorted by date.

    The input must carry the ``SESSION_FRAME`` columns. An empty frame yields an
    empty mapping; callers that need a fallback should use ``GLOBAL_EXCHANGE``.
    """
    required = {"exchange_id", "session_date", "open_ts", "close_ts"}
    missing = required - set(sessions.columns)
    if missing:
        raise ValueError(f"sessions frame is missing columns: {sorted(missing)}")
    calendars: dict[str, ExchangeCalendar] = {}
    for exchange_id in sessions["exchange_id"].unique().sort().to_list():
        sub = sessions.filter(pl.col("exchange_id") == exchange_id)
        rows = list(
            zip(
                sub["session_date"].to_list(),
                sub["open_ts"].to_list(),
                sub["close_ts"].to_list(),
                strict=True,
            )
        )
        calendars[str(exchange_id)] = _calendar_from_rows(str(exchange_id), rows)
    return calendars


GLOBAL_EXCHANGE = "*"
"""Pseudo exchange id: all sessions merged, used only when an event's exchange is
unknown or has no calendar. Deterministic but mixes exchanges — a last resort."""


def global_calendar(sessions: pl.DataFrame) -> ExchangeCalendar:
    """Merged calendar over every session (see ``GLOBAL_EXCHANGE``).

    Sessions are collapsed to one row per ``session_date`` (earliest open, latest
    close) so the date arrays stay unique and sorted.
    """
    merged = (
        sessions.group_by("session_date")
        .agg(pl.col("open_ts").min().alias("open_ts"), pl.col("close_ts").max().alias("close_ts"))
        .sort("session_date")
    )
    rows = list(
        zip(
            merged["session_date"].to_list(),
            merged["open_ts"].to_list(),
            merged["close_ts"].to_list(),
            strict=True,
        )
    )
    return _calendar_from_rows(GLOBAL_EXCHANGE, rows)
