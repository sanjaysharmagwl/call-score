"""Bulk ingest for ``rating_event`` rows with dedupe and chain validation."""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import Connection, select
from sqlalchemy.orm import Session

from callscore_store.models import RatingEvent
from callscore_store.repositories.base import (
    Context,
    normalize_rows,
    table_of,
    unit_of_work,
)
from callscore_store.repositories.reference import known_scale_ids
from callscore_store.validation import RowIssue, ValidationError, validate_table


class DuplicateRowError(ValidationError):
    """A non-correction event collides on the natural key.

    Idempotency choice (documented): ``insert_rating_events`` REJECTS duplicate
    non-correction events — natural key ``(analyst_id, security_id,
    announced_ts)`` — rather than silently deduplicating, so re-ingesting a
    feed surfaces data problems instead of hiding them. Correction events
    (``is_correction`` true) may share a timestamp legitimately and are
    exempt. The check covers both the existing DB and earlier rows in the
    same batch.
    """


def _existing_event_ids(ctx: Connection | Session, event_ids: set[str]) -> set[str]:
    """Return the subset of ``event_ids`` already present as primary keys."""
    if not event_ids:
        return set()
    stmt = select(RatingEvent.event_id).where(RatingEvent.event_id.in_(event_ids))
    return {r[0] for r in ctx.execute(stmt)}


def _natural_key(row: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (row.get("analyst_id"), row.get("security_id"), row.get("announced_ts"))


def _order_by_supersedes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable topological order: a row's ``supersedes_event_id`` target (when
    also in the batch) is inserted first, satisfying the immediate FK.
    Validation has already rejected cycles, so this always terminates."""
    by_id = {r["event_id"]: r for r in rows if r.get("event_id")}
    ordered: list[dict[str, Any]] = []
    placed: set[Any] = set()

    def place(row: dict[str, Any], stack: set[Any]) -> None:
        eid = row.get("event_id")
        if eid in placed or eid in stack:
            return
        sup = row.get("supersedes_event_id")
        if sup in by_id and sup != eid:
            stack.add(eid)
            place(by_id[sup], stack)
            stack.discard(eid)
        if eid not in placed:
            placed.add(eid)
            ordered.append(row)

    for row in rows:
        place(row, set())
    # Rows without event_id (invalid anyway) keep their relative position at end.
    ordered.extend(r for r in rows if not r.get("event_id"))
    return ordered


def insert_rating_events(
    ctx: Context,
    rows: Iterable[object],
    *,
    source: str = "manual",
) -> int:
    """Validate and bulk-insert rating events in ONE transaction.

    ``rows`` may be mappings, dataclasses or Polars row dicts (the
    ``RATING_EVENT_FRAME`` shape works; ``source`` defaults to ``"manual"``
    and ``is_correction`` to ``False`` when absent).

    Raises :class:`ValidationError` (bad rows), :class:`DuplicateRowError`
    (natural-key collision) or an ``IntegrityError`` (missing FK) — in every
    case nothing is inserted.
    """
    normalized = normalize_rows(RatingEvent, rows)
    if not normalized:
        return 0

    for row in normalized:
        if row.get("source") is None:
            row["source"] = source
        if row.get("is_correction") is None:
            row["is_correction"] = False

    scales = known_scale_ids(ctx)

    with unit_of_work(ctx) as conn:
        # DB-side context for supersedes existence and dedupe checks.
        batch_ids = {r["event_id"] for r in normalized if r.get("event_id")}
        existing_ids = _existing_event_ids(conn, batch_ids)
        referenced = {r["supersedes_event_id"] for r in normalized if r.get("supersedes_event_id")}
        existing_supersedes = _existing_event_ids(conn, referenced)

        errors, _warnings = validate_table(
            "rating_event",
            normalized,
            known_scale_ids=scales,
            existing_event_ids=existing_supersedes,
        )
        if errors:
            raise ValidationError("rating_event", errors)

        # Natural-key dedupe: non-correction rows only.
        seen_keys: set[tuple[Any, Any, Any]] = set()
        dup_issues: list[RowIssue] = []
        for idx, row in enumerate(normalized):
            if row.get("is_correction"):
                continue
            key = _natural_key(row)
            if key in seen_keys or _key_exists_in_db(conn, key):
                dup_issues.append(
                    RowIssue(
                        idx,
                        "event_id",
                        f"duplicate non-correction event for analyst/security/"
                        f"announced_ts: {key!r}",
                    )
                )
            seen_keys.add(key)
        if dup_issues:
            raise DuplicateRowError("rating_event", dup_issues)

        if existing_ids:
            issues = [
                RowIssue(i, "event_id", f"event_id {eid!r} already exists")
                for i, eid in enumerate(normalized)
                if eid.get("event_id") in existing_ids
            ]
            if issues:
                raise DuplicateRowError("rating_event", issues)

        ordered = _order_by_supersedes(normalized)
        conn.execute(table_of(RatingEvent).insert(), ordered)
    return len(normalized)


def _key_exists_in_db(
    conn: Connection | Session,
    key: tuple[Any, Any, Any],
) -> bool:
    """True if a non-correction row with this natural key already exists."""
    analyst_id, security_id, announced_ts = key
    if analyst_id is None or security_id is None or announced_ts is None:
        return False
    stmt = (
        select(RatingEvent.event_id)
        .where(
            RatingEvent.analyst_id == analyst_id,
            RatingEvent.security_id == security_id,
            RatingEvent.announced_ts == announced_ts,
            RatingEvent.is_correction.is_(False),
        )
        .limit(1)
    )
    return conn.execute(stmt).first() is not None
