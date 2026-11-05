"""Rating-interval construction — the ``INTERVALS`` stage (decision 0003).

One ``INTERVAL_FRAME`` row per rating event that *changes* the canonical rating
(or drops coverage) for an ``(analyst_id, security_id)`` pair. Same-rating
actions (reiteration, ``pt_change``, upgrade/downgrade landing on the same
canonical) never open an interval; they extend the open interval's
``expiry_date`` to the newest non-null expiry in the run.

Pipeline inside :func:`build_intervals`:

1. ``visible_events`` restricts to ``knowledge_ts <= context.knowledge_cutoff``
   and resolves correction chains; ``available_ts = max(effective_ts,
   knowledge_ts)`` drives all timing.
2. Events are grouped by ``(analyst_id, security_id)`` and ordered by
   ``(available_ts, announced_ts, event_id)`` — row-order invariant.
3. Each interval's entry point follows ``methodology.entry_convention`` on the
   security's exchange (the classification spell covering the event's
   available date).
4. Exit is the earliest of: the superseding interval's / drop's entry point,
   expiry (per ``expiry_policy``), coverage spell end, employment spell end,
   delisting, or the run's as-of close. Exposure is ``[entry, exit)``.
5. Intervals with ``entry >= exit`` (or entering before ``listing_date``) are
   kept with ``is_investable = false`` and a structural
   ``non_investable_reason``.

Ambiguities resolved (documented choices):

- "Event date" for spell lookup is the UTC date of ``available_ts``.
- All exit session math uses the *entry* exchange's calendar (exits are dates,
  not exchange events; the entry exchange is the interval's home venue).
- Coverage/employment spell selection prefers the spell covering the entry
  session, else the latest spell starting on/before it, else the earliest.
- An event whose entry cannot be found (``available_ts`` past the last
  session) enters at the run-end close, producing a flagged non-positive row.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.enums import (
    CanonicalRating,
    EntryConvention,
    ExitReason,
    ExpiryPolicy,
    RatingAction,
)
from callscore_core.frames import INTERVAL_FRAME, POINT_CLOSE
from callscore_core.methodology import Methodology
from callscore_engine.calendar import (
    GLOBAL_EXCHANGE,
    ExchangeCalendar,
    SessionPoint,
    build_calendars,
    global_calendar,
)
from callscore_engine.point_in_time import visible_events, with_available_ts

_DTYPES: dict[str, pl.DataType] = {
    "String": pl.String(),
    "Date": pl.Date(),
    "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
    "Float64": pl.Float64(),
    "Int64": pl.Int64(),
    "Boolean": pl.Boolean(),
}
INTERVAL_SCHEMA = pl.Schema(
    {column.name: _DTYPES[column.dtype] for column in INTERVAL_FRAME.columns}
)

_NON_INVESTABLE = frozenset(
    {
        CanonicalRating.NOT_RATED.value,
        CanonicalRating.UNDER_REVIEW.value,
        CanonicalRating.RESTRICTED.value,
    }
)

# Tie-break precedence when two exit candidates land on the same point
# (decision 0003 / task spec).
_EXIT_PRIORITY: dict[str, int] = {
    ExitReason.DELISTED.value: 0,
    ExitReason.ANALYST_LEFT.value: 1,
    ExitReason.COVERAGE_ENDED.value: 2,
    ExitReason.DROPPED.value: 3,
    ExitReason.SUPERSEDED.value: 4,
    ExitReason.EXPIRED.value: 5,
    ExitReason.RUN_END.value: 6,
}

_STRUCTURAL_REASON: dict[str, str] = {
    ExitReason.COVERAGE_ENDED.value: "after_coverage_end",
    ExitReason.DELISTED.value: "after_delisting",
    ExitReason.RUN_END.value: "after_run_end",
}
_DEFAULT_STRUCTURAL_REASON = "shorter_than_one_session"


@dataclass
class _PendingInterval:
    """An open interval while its ``(analyst, security)`` group is scanned."""

    row: dict[str, Any]
    available_ts: datetime
    entry: SessionPoint
    exchange_id: str
    listing_date: date | None
    expiry_date: date | None
    terminator: SessionPoint | None = None
    terminator_reason: str | None = None


class _Lookups:
    """Precomputed per-key spell/calendar views shared by all groups."""

    def __init__(
        self,
        sessions: pl.DataFrame,
        coverage_spells: pl.DataFrame,
        employment_spells: pl.DataFrame,
        classifications: pl.DataFrame,
        delistings: pl.DataFrame,
        as_of: date,
    ) -> None:
        self.calendars = build_calendars(sessions)
        self.global_cal = global_calendar(sessions)
        self.as_of = as_of
        self.classifications = _group_spells(classifications, "security_id")
        self.coverage = _group_spells(coverage_spells, "analyst_id", "security_id")
        self.employment = _group_spells(employment_spells, "analyst_id", "firm_id")
        self.delisting_dates = {
            str(row["security_id"]): row["delisting_date"]
            for row in delistings.iter_rows(named=True)
        }

    def calendar(self, exchange_id: str) -> ExchangeCalendar:
        cal = self.calendars.get(exchange_id)
        return cal if cal is not None and len(cal) else self.global_cal

    def run_end_point(self, cal: ExchangeCalendar) -> SessionPoint:
        """Close of the last session on/before ``as_of`` (exchange, else global)."""
        for candidate in (cal, self.global_cal):
            session = candidate.last_session_on_or_before(self.as_of)
            if session is not None:
                ts = candidate.close_ts_for(session)
                if ts is not None:
                    return SessionPoint(session, POINT_CLOSE, ts)
        raise ValueError("sessions frame has no session on or before as_of_date")


def _group_spells(spells: pl.DataFrame, *keys: str) -> dict[tuple[str, ...], list[dict[str, Any]]]:
    """Spells grouped by key columns, each list sorted by ``from_date``."""
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    if spells.height == 0:
        return grouped
    ordered = spells.sort([*keys, "from_date"])
    for key_values, sub in ordered.group_by(list(keys), maintain_order=True):
        values = key_values if isinstance(key_values, tuple) else (key_values,)
        key = tuple(str(v) for v in values)
        grouped[key] = list(sub.iter_rows(named=True))
    return grouped


def _pick_spell(spells: list[dict[str, Any]], day: date) -> dict[str, Any] | None:
    """Spell covering ``day``; else latest starting on/before it; else earliest."""
    if not spells:
        return None
    covering = [
        s for s in spells if s["from_date"] <= day and (s["to_date"] is None or s["to_date"] >= day)
    ]
    if covering:
        return max(covering, key=lambda s: s["from_date"])
    prior = [s for s in spells if s["from_date"] <= day]
    if prior:
        return max(prior, key=lambda s: s["from_date"])
    return min(spells, key=lambda s: s["from_date"])


def _entry_point(
    cal: ExchangeCalendar,
    available_ts: datetime,
    convention: EntryConvention,
    lookups: _Lookups,
) -> SessionPoint:
    """Execution point for an event under ``methodology.entry_convention``."""
    point: SessionPoint | None = None
    if convention == EntryConvention.NEXT_OPEN:
        point = cal.first_open_after(available_ts)
    elif convention == EntryConvention.SAME_CLOSE:
        point = cal.first_close_after(available_ts)
    elif convention == EntryConvention.T_PLUS_2_OPEN:
        first = cal.first_open_after(available_ts)
        if first is not None:
            point = cal.open_after_session(first.session_date)
    if point is not None:
        return point
    # No session satisfies the convention (event after the last session):
    # enter at the run-end close so the row is kept and flagged non-positive.
    return lookups.run_end_point(cal)


def _chain_events(
    rows: list[dict[str, Any]],
    lookups: _Lookups,
    methodology: Methodology,
) -> list[_PendingInterval]:
    """Split one ``(analyst, security)`` event run into pending intervals."""
    done: list[_PendingInterval] = []
    pending: _PendingInterval | None = None
    for row in rows:
        available_ts = row["available_ts"]
        event_day = available_ts.date()
        spell = _pick_spell(lookups.classifications.get((str(row["security_id"]),), []), event_day)
        exchange_id = str(spell["exchange_id"]) if spell is not None else GLOBAL_EXCHANGE
        cal = lookups.calendar(exchange_id)
        entry = _entry_point(cal, available_ts, methodology.entry_convention, lookups)

        if row["action_type"] == RatingAction.DROP.value:
            if pending is not None:
                pending.terminator = entry
                pending.terminator_reason = ExitReason.DROPPED.value
                done.append(pending)
                pending = None
            continue

        canonical = str(row["canonical_rating"])
        if pending is not None and canonical == pending.row["canonical_rating"]:
            # Same-rating action: extend expiry to the newest non-null value.
            new_expiry = row["expiry_date"]
            if new_expiry is not None:
                pending.expiry_date = (
                    new_expiry
                    if pending.expiry_date is None
                    else max(pending.expiry_date, new_expiry)
                )
            continue

        if pending is not None:
            pending.terminator = entry
            pending.terminator_reason = ExitReason.SUPERSEDED.value
            done.append(pending)
        pending = _PendingInterval(
            row=row,
            available_ts=available_ts,
            entry=entry,
            exchange_id=exchange_id,
            listing_date=(spell["listing_date"] if spell is not None else None),
            expiry_date=row["expiry_date"],
        )
    if pending is not None:
        done.append(pending)
    return done


def _exit_candidates(
    interval: _PendingInterval,
    lookups: _Lookups,
    methodology: Methodology,
) -> tuple[list[tuple[datetime, int, date, str, str]], date | None]:
    """All exit candidates plus the decay start session, for one interval."""
    cal = lookups.calendar(interval.exchange_id)
    candidates: list[tuple[datetime, int, date, str, str]] = []

    def add(point: SessionPoint, reason: str) -> None:
        candidates.append(
            (
                point.ts,
                _EXIT_PRIORITY[reason],
                point.session_date,
                point.point,
                reason,
            )
        )

    def add_close(session: date | None, reason: str) -> None:
        if session is None:
            return
        ts = cal.close_ts_for(session)
        if ts is not None:
            add(SessionPoint(session, POINT_CLOSE, ts), reason)

    # 1. Superseding interval entry / drop entry.
    if interval.terminator is not None and interval.terminator_reason is not None:
        add(interval.terminator, interval.terminator_reason)

    # 2. Expiry, per methodology.expiry_policy.
    decay_start: date | None = None
    if interval.expiry_date is not None:
        expiry_session = cal.last_session_on_or_before(interval.expiry_date)
        if expiry_session is not None:
            if methodology.expiry_policy == ExpiryPolicy.HARD_CLOSE:
                add_close(expiry_session, ExitReason.EXPIRED.value)
            elif methodology.expiry_policy == ExpiryPolicy.DECAY:
                decay_start = cal.first_session_after(expiry_session)
                add_close(
                    cal.session_offset(expiry_session, methodology.decay_days),
                    ExitReason.EXPIRED.value,
                )
            # carry_forward: expiry is ignored.

    row = interval.row
    entry_day = interval.entry.session_date

    # 3. Coverage spell end.
    coverage = _pick_spell(
        lookups.coverage.get((str(row["analyst_id"]), str(row["security_id"])), []),
        entry_day,
    )
    if coverage is not None and coverage["to_date"] is not None:
        add_close(
            cal.last_session_on_or_before(coverage["to_date"]),
            ExitReason.COVERAGE_ENDED.value,
        )

    # 4. Employment spell end (analyst left the rating's firm).
    employment = _pick_spell(
        lookups.employment.get((str(row["analyst_id"]), str(row["firm_id"])), []),
        entry_day,
    )
    if employment is not None and employment["to_date"] is not None:
        add_close(
            cal.last_session_on_or_before(employment["to_date"]),
            ExitReason.ANALYST_LEFT.value,
        )

    # 5. Delisting (delisting_date is the last traded session).
    delisting_date = lookups.delisting_dates.get(str(row["security_id"]))
    if delisting_date is not None:
        add_close(
            cal.last_session_on_or_before(delisting_date),
            ExitReason.DELISTED.value,
        )

    # 6. Run end: last session on/before the as-of date.
    add(lookups.run_end_point(cal), ExitReason.RUN_END.value)

    return candidates, decay_start


def _finalize(
    interval: _PendingInterval,
    lookups: _Lookups,
    methodology: Methodology,
) -> dict[str, Any]:
    candidates, decay_start = _exit_candidates(interval, lookups, methodology)
    exit_ts, _, exit_session, exit_point, exit_reason = min(candidates, key=lambda c: (c[0], c[1]))

    row = interval.row
    entry = interval.entry
    canonical = str(row["canonical_rating"])
    listing_date = interval.listing_date

    flags: list[str] = []
    non_positive = entry.ts >= exit_ts
    before_listing = listing_date is not None and entry.session_date < listing_date
    if before_listing:
        flags.append("before_listing")
    if non_positive:
        flags.append("non_positive_length")
    if canonical in _NON_INVESTABLE:
        flags.append("non_investable_rating")

    non_investable_reason: str | None = None
    if before_listing:
        non_investable_reason = "before_listing"
    elif non_positive:
        non_investable_reason = _STRUCTURAL_REASON.get(exit_reason, _DEFAULT_STRUCTURAL_REASON)
    elif canonical in _NON_INVESTABLE:
        non_investable_reason = canonical

    return {
        "interval_id": row["event_id"],
        "event_id": row["event_id"],
        "analyst_id": row["analyst_id"],
        "firm_id": row["firm_id"],
        "security_id": row["security_id"],
        "native_rating": row["native_rating"],
        "canonical_rating": canonical,
        "action_type": row["action_type"],
        "announced_ts": row["announced_ts"],
        "effective_ts": row["effective_ts"],
        "knowledge_ts": row["knowledge_ts"],
        "available_ts": interval.available_ts,
        "entry_session": entry.session_date,
        "entry_point": entry.point,
        "entry_ts": entry.ts,
        "exit_session": exit_session,
        "exit_point": exit_point,
        "exit_ts": exit_ts,
        "exit_reason": exit_reason,
        "expiry_date": interval.expiry_date,
        "decay_start_session": decay_start,
        "is_investable": non_investable_reason is None,
        "non_investable_reason": non_investable_reason,
        "flags": ",".join(flags) if flags else None,
    }


def _assert_interval_frame(df: pl.DataFrame) -> None:
    """Private conformance check against ``frames.INTERVAL_FRAME``."""
    contract = INTERVAL_FRAME
    assert list(df.columns) == list(contract.column_names), (
        f"interval columns {df.columns} != {contract.column_names}"
    )
    assert list(df.schema.items()) == list(INTERVAL_SCHEMA.items()), (
        f"interval dtypes {df.schema} != {INTERVAL_SCHEMA}"
    )
    for key in contract.primary_key:
        assert df[key].null_count() == 0, f"primary key column {key} has nulls"
    assert df["interval_id"].n_unique() == df.height, "interval_id is not unique"
    assert df.equals(df.sort(*contract.sort_by)), "intervals not sorted by sort_by"


def build_intervals(
    events: pl.DataFrame,
    sessions: pl.DataFrame,
    coverage_spells: pl.DataFrame,
    employment_spells: pl.DataFrame,
    classifications: pl.DataFrame,
    delistings: pl.DataFrame,
    methodology: Methodology,
    context: RunContext,
) -> pl.DataFrame:
    """Build ``INTERVAL_FRAME`` rows for every rating-changing event.

    ``events`` may be the raw ``RATING_EVENT_FRAME``; visibility at
    ``context.knowledge_cutoff`` is enforced here. Output conforms exactly to
    ``frames.INTERVAL_FRAME`` (columns, order, dtypes, sort, unique key).
    """
    if sessions.height == 0:
        raise ValueError("sessions frame is empty")

    lookups = _Lookups(
        sessions=sessions,
        coverage_spells=coverage_spells,
        employment_spells=employment_spells,
        classifications=classifications,
        delistings=delistings,
        as_of=context.as_of_date,
    )

    visible = with_available_ts(visible_events(events, context.knowledge_cutoff))
    rows_out: list[dict[str, Any]] = []
    if visible.height:
        ordered = visible.sort(
            ["analyst_id", "security_id", "available_ts", "announced_ts", "event_id"]
        )
        for _keys, group in ordered.group_by(["analyst_id", "security_id"], maintain_order=True):
            pending = _chain_events(list(group.iter_rows(named=True)), lookups, methodology)
            for interval in pending:
                rows_out.append(_finalize(interval, lookups, methodology))

    df = pl.DataFrame(rows_out, schema=INTERVAL_SCHEMA).sort(*INTERVAL_FRAME.sort_by)
    _assert_interval_frame(df)
    return df
