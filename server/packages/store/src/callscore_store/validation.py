"""Pre-insert validation for ingested records.

Validators are pure functions over normalized row mappings (see
:func:`callscore_store.repositories.base.normalize_rows`). They implement the
business rules from ``callscore_core.frames`` / decision 0003 that SQLite CHECK
constraints cannot express, plus earlier/clearer failures for the rules SQLite
does enforce. Bulk loaders call :func:`validate_table` *before* touching the
database, so a batch with any violation inserts nothing.

Two severity levels are produced:

* **errors** — the row must not be inserted. Any error rejects the whole batch
  via :class:`ValidationError`.
* **warnings** — suspicious but contract-legal data (e.g. ``knowledge_ts``
  earlier than ``effective_ts``). Warnings never block an insert; callers that
  care can inspect the return value of the ``validate_*`` helpers.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from callscore_core.enums import CanonicalRating, RatingAction
from callscore_store.models import DELISTING_REASONS
from callscore_store.types import MoneyError, to_micros


@dataclass(frozen=True)
class RowIssue:
    """One validation finding.

    ``row_index`` is the 0-based position of the row in the batch passed to the
    loader (not a database row id).
    """

    row_index: int
    column: str | None
    message: str
    severity: str = "error"  # "error" | "warning"

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


class ValidationError(ValueError):
    """Raised when an ingest batch contains invalid rows; nothing was inserted."""

    def __init__(self, table: str, issues: Sequence[RowIssue]) -> None:
        self.table = table
        self.issues = tuple(issue for issue in issues if issue.is_error)
        self.warnings = tuple(issue for issue in issues if not issue.is_error)
        detail = "; ".join(
            f"row {i.row_index}" + (f".{i.column}" if i.column else "") + f": {i.message}"
            for i in self.issues[:20]
        )
        if len(self.issues) > 20:
            detail += f"; ... and {len(self.issues) - 20} more"
        super().__init__(f"{table}: {len(self.issues)} invalid row(s): {detail}")


def _value(raw: object) -> object:
    """Normalize StrEnum members to their string value for comparison."""
    return raw.value if isinstance(raw, StrEnum) else raw


def _is_valid_enum(raw: object, members: type[StrEnum] | tuple[str, ...]) -> bool:
    allowed = {m.value if isinstance(m, StrEnum) else m for m in members}
    return _value(raw) in allowed


def _money_issue(row_index: int, column: str, raw: object) -> RowIssue | None:
    """Check that a money-typed value survives ``to_micros`` (exact precision)."""
    if raw is None:
        return None
    try:
        to_micros(raw)  # type: ignore[arg-type]
    except (MoneyError, TypeError, ValueError) as exc:
        return RowIssue(row_index, column, f"invalid money value {raw!r}: {exc}")
    return None


def _required_issue(
    row_index: int, row: Mapping[str, Any], column: str, *, label: str | None = None
) -> RowIssue | None:
    if row.get(column) is None:
        return RowIssue(row_index, column, f"{label or column} is required")
    return None


def validate_rating_events(
    rows: Sequence[Mapping[str, Any]],
    *,
    known_scale_ids: Collection[str] | None = None,
    existing_event_ids: Collection[str] = (),
) -> list[RowIssue]:
    """Validate ``rating_event`` rows (contract ``RATING_EVENT_FRAME`` + ``source``).

    ``known_scale_ids`` — distinct ``scale_id`` values present in ``rating_scale``;
    when ``None`` the existence check is skipped (pure validation).
    ``existing_event_ids`` — ``event_id`` values already in the DB, used to
    resolve ``supersedes_event_id`` references.

    Checks: enum membership, known scale, sane ``expiry_date``, supersedes
    existence/self-reference/acyclicity (within the batch), price-target money
    precision and required fields. ``knowledge_ts`` ordering is *not* enforced —
    a ``knowledge_ts`` earlier than ``announced_ts``/``effective_ts`` yields a
    warning, not an error.
    """
    issues: list[RowIssue] = []
    batch_ids: dict[str, int] = {}
    supersedes: dict[str, str | None] = {}

    for idx, row in enumerate(rows):
        canonical = _value(row.get("canonical_rating"))
        if canonical is None or not _is_valid_enum(canonical, CanonicalRating):
            issues.append(
                RowIssue(idx, "canonical_rating", f"unknown canonical_rating {canonical!r}")
            )
        action = _value(row.get("action_type"))
        if action is None or not _is_valid_enum(action, RatingAction):
            issues.append(RowIssue(idx, "action_type", f"unknown action_type {action!r}"))
        prior = row.get("prior_canonical_rating")
        if prior is not None and not _is_valid_enum(prior, CanonicalRating):
            issues.append(RowIssue(idx, "prior_canonical_rating", f"unknown rating {prior!r}"))

        scale_id = row.get("scale_id")
        if not scale_id:
            issues.append(RowIssue(idx, "scale_id", "scale_id is required"))
        elif known_scale_ids is not None and scale_id not in known_scale_ids:
            issues.append(
                RowIssue(idx, "scale_id", f"unknown scale_id {scale_id!r} (no rating_scale row)")
            )

        announced = row.get("announced_ts")
        effective = row.get("effective_ts")
        knowledge = row.get("knowledge_ts")
        expiry = row.get("expiry_date")
        # "Sane" expiry: not before the day the call was made or took effect.
        expiry_day = expiry.date() if isinstance(expiry, datetime) else expiry
        announced_day = announced.date() if isinstance(announced, datetime) else announced
        effective_day = effective.date() if isinstance(effective, datetime) else effective
        if isinstance(expiry_day, date) and isinstance(effective_day, date):
            if expiry_day < effective_day:
                issues.append(
                    RowIssue(
                        idx,
                        "expiry_date",
                        f"expiry_date {expiry_day} precedes effective date {effective_day}",
                    )
                )
        elif (
            isinstance(expiry_day, date)
            and isinstance(announced_day, date)
            and expiry_day < announced_day
        ):
            issues.append(
                RowIssue(
                    idx,
                    "expiry_date",
                    f"expiry_date {expiry_day} precedes announced date {announced_day}",
                )
            )

        # Knowledge ordering is allowed to precede effective/announced
        # (restatements, backfilled PIT data); flag it for review.
        if all(isinstance(ts, datetime) for ts in (knowledge, announced, effective)):
            k = knowledge
            assert isinstance(k, datetime)
            for name, ts in (("announced_ts", announced), ("effective_ts", effective)):
                other = ts
                assert isinstance(other, datetime)
                if k < other:
                    issues.append(
                        RowIssue(
                            idx,
                            "knowledge_ts",
                            f"knowledge_ts precedes {name}; allowed but flagged",
                            severity="warning",
                        )
                    )

        pt = row.get("price_target")
        money_issue = _money_issue(idx, "price_target", pt)
        if money_issue is not None:
            issues.append(money_issue)
        pt_ccy = row.get("pt_currency")
        if pt_ccy is not None and (not isinstance(pt_ccy, str) or len(pt_ccy) != 3):
            issues.append(RowIssue(idx, "pt_currency", "pt_currency must be a 3-letter code"))
        horizon = row.get("pt_horizon_months")
        if horizon is not None and (not isinstance(horizon, int) or isinstance(horizon, bool)):
            issues.append(RowIssue(idx, "pt_horizon_months", "pt_horizon_months must be an int"))
        elif isinstance(horizon, int) and horizon <= 0:
            issues.append(RowIssue(idx, "pt_horizon_months", "pt_horizon_months must be > 0"))

        for col in (
            "event_id",
            "analyst_id",
            "firm_id",
            "security_id",
            "announced_ts",
            "effective_ts",
            "knowledge_ts",
            "native_rating",
        ):
            req = _required_issue(idx, row, col)
            if req is not None:
                issues.append(req)

        event_id = row.get("event_id")
        if isinstance(event_id, str):
            if event_id in batch_ids:
                issues.append(
                    RowIssue(idx, "event_id", f"duplicate event_id {event_id!r} in batch")
                )
            batch_ids[event_id] = idx
            supersedes[event_id] = row.get("supersedes_event_id")

    # Supersedes references: must exist in the DB or earlier in the batch, and
    # the within-batch chain must be acyclic.
    known_ids = set(existing_event_ids) | set(batch_ids)
    for event_id, sup in supersedes.items():
        idx = batch_ids[event_id]
        if sup is None:
            continue
        if sup == event_id:
            issues.append(RowIssue(idx, "supersedes_event_id", "event cannot supersede itself"))
            continue
        if sup not in known_ids:
            issues.append(
                RowIssue(
                    idx,
                    "supersedes_event_id",
                    f"supersedes_event_id {sup!r} not found in database or batch",
                )
            )
            continue
        # Cycle detection restricted to batch-internal edges.
        seen: set[str] = set()
        cur: str | None = sup
        while cur is not None and cur in supersedes:
            if cur == event_id or cur in seen:
                issues.append(
                    RowIssue(
                        idx,
                        "supersedes_event_id",
                        f"supersedes chain for {event_id!r} is cyclic",
                    )
                )
                break
            seen.add(cur)
            cur = supersedes[cur]

    return issues


def validate_security_daily(rows: Sequence[Mapping[str, Any]]) -> list[RowIssue]:
    """OHLC consistency for ``security_daily`` (``SECURITY_DAILY_FRAME``)."""
    issues: list[RowIssue] = []
    for idx, row in enumerate(rows):
        o, h, lo, c = (
            row.get("open"),
            row.get("high"),
            row.get("low"),
            row.get("close"),
        )
        prices = {"open": o, "high": h, "low": lo, "close": c}
        for name, raw in prices.items():
            req = _required_issue(idx, row, name)
            if req is not None:
                issues.append(req)
            elif isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool):
                if raw <= 0:
                    issues.append(RowIssue(idx, name, f"{name} must be > 0, got {raw!r}"))
            elif raw is not None:
                issues.append(RowIssue(idx, name, f"{name} must be numeric, got {raw!r}"))
        if all(
            isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)
            for v in prices.values()
        ):
            o_f = float(o)  # type: ignore[arg-type]
            h_f = float(h)  # type: ignore[arg-type]
            lo_f = float(lo)  # type: ignore[arg-type]
            c_f = float(c)  # type: ignore[arg-type]
            if h_f < max(o_f, c_f):
                issues.append(
                    RowIssue(
                        idx,
                        "high",
                        f"high {h_f} < max(open, close) = {max(o_f, c_f)}",
                    )
                )
            if lo_f > min(o_f, c_f):
                issues.append(
                    RowIssue(
                        idx,
                        "low",
                        f"low {lo_f} > min(open, close) = {min(o_f, c_f)}",
                    )
                )
        adj = row.get("adj_factor")
        if adj is None or not isinstance(adj, (int, float)) or isinstance(adj, bool) or adj <= 0:
            issues.append(RowIssue(idx, "adj_factor", f"adj_factor must be > 0, got {adj!r}"))
        tr = row.get("tr_index")
        if tr is None or not isinstance(tr, (int, float)) or isinstance(tr, bool) or tr <= 0:
            issues.append(RowIssue(idx, "tr_index", f"tr_index must be > 0, got {tr!r}"))
        div = row.get("cash_dividend")
        if div is not None and (
            not isinstance(div, (int, float, Decimal)) or isinstance(div, bool) or div < 0
        ):
            issues.append(
                RowIssue(idx, "cash_dividend", f"cash_dividend must be >= 0, got {div!r}")
            )
        for col in ("security_id", "session_date", "is_halted"):
            req = _required_issue(idx, row, col)
            if req is not None:
                issues.append(req)
    return issues


def validate_delistings(rows: Sequence[Mapping[str, Any]]) -> list[RowIssue]:
    """``DELISTING_FRAME``: ``final_return`` in [-1, ~5]."""
    issues: list[RowIssue] = []
    for idx, row in enumerate(rows):
        fr = row.get("final_return")
        if fr is None or not isinstance(fr, (int, float)) or isinstance(fr, bool):
            issues.append(
                RowIssue(idx, "final_return", f"final_return must be numeric, got {fr!r}")
            )
        elif not (-1.0 <= float(fr) <= 5.0):
            issues.append(RowIssue(idx, "final_return", f"final_return {fr} outside [-1, 5]"))
        reason = row.get("reason")
        if reason is not None and reason not in DELISTING_REASONS:
            issues.append(RowIssue(idx, "reason", f"unknown delisting reason {reason!r}"))
        for col in ("security_id", "delisting_date"):
            req = _required_issue(idx, row, col)
            if req is not None:
                issues.append(req)
    return issues


def validate_benchmark_daily(rows: Sequence[Mapping[str, Any]]) -> list[RowIssue]:
    """``BENCHMARK_DAILY_FRAME``: positive open/close levels."""
    issues: list[RowIssue] = []
    for idx, row in enumerate(rows):
        for col in ("open_level", "close_level"):
            raw = row.get(col)
            if raw is None or not isinstance(raw, (int, float)) or isinstance(raw, bool):
                issues.append(RowIssue(idx, col, f"{col} must be numeric, got {raw!r}"))
            elif raw <= 0:
                issues.append(RowIssue(idx, col, f"{col} must be > 0, got {raw!r}"))
        for col in ("benchmark_id", "session_date"):
            req = _required_issue(idx, row, col)
            if req is not None:
                issues.append(req)
    return issues


def validate_security_classifications(
    rows: Sequence[Mapping[str, Any]],
) -> list[RowIssue]:
    """``SECURITY_CLASSIFICATION_FRAME``: per-security spell non-overlap.

    Spans are ``[from_date, to_date]`` with ``to_date=None`` open-ended
    (inclusive). Overlaps *within the batch* are rejected; conflicts with
    already-stored spells are left to the caller (the PK permits adjacent
    versions and the engine resolves PIT overlaps deterministically).
    """
    issues: list[RowIssue] = []
    spans: dict[str, list[tuple[int, date, date | None]]] = {}
    for idx, row in enumerate(rows):
        sid = row.get("security_id")
        frm = row.get("from_date")
        to = row.get("to_date")
        if sid is None or frm is None:
            issues.append(
                RowIssue(
                    idx,
                    "security_id" if sid is None else "from_date",
                    "security_id and from_date are required",
                )
            )
            continue
        if not isinstance(frm, date) or isinstance(frm, datetime):
            issues.append(RowIssue(idx, "from_date", f"from_date must be a date, got {frm!r}"))
            continue
        if to is not None and (not isinstance(to, date) or isinstance(to, datetime)):
            issues.append(RowIssue(idx, "to_date", f"to_date must be a date, got {to!r}"))
            continue
        if to is not None and to < frm:
            issues.append(RowIssue(idx, "to_date", f"to_date {to} < from_date {frm}"))
            continue
        spans.setdefault(str(sid), []).append((idx, frm, to))

    for sid, entries in spans.items():
        # Pairwise overlap check per security; ``to_date=None`` is open-ended
        # (``date.max`` sentinel). Each overlapping later row is flagged once.
        entries.sort(key=lambda e: (e[1], e[0]))
        for pos, (idx, frm, to) in enumerate(entries):
            hi = to if to is not None else date.max
            for p_idx, p_from, p_to in entries[:pos]:
                p_hi = p_to if p_to is not None else date.max
                if frm <= p_hi and p_from <= hi:
                    issues.append(
                        RowIssue(
                            idx,
                            "from_date",
                            f"classification spell for {sid!r} overlaps row {p_idx} "
                            f"([{p_from}, {p_to if p_to is not None else 'open'}])",
                        )
                    )
                    break  # each row reports at most one overlap
    return issues


def validate_fx_daily(rows: Sequence[Mapping[str, Any]]) -> list[RowIssue]:
    """``FX_DAILY_FRAME``: positive rate, 3-letter currencies."""
    issues: list[RowIssue] = []
    for idx, row in enumerate(rows):
        rate = row.get("rate")
        if rate is None or not isinstance(rate, (int, float)) or isinstance(rate, bool):
            issues.append(RowIssue(idx, "rate", f"rate must be numeric, got {rate!r}"))
        elif rate <= 0:
            issues.append(RowIssue(idx, "rate", f"rate must be > 0, got {rate!r}"))
        for col in ("from_ccy", "to_ccy"):
            raw = row.get(col)
            if not isinstance(raw, str) or len(raw) != 3:
                issues.append(RowIssue(idx, col, f"{col} must be a 3-letter code, got {raw!r}"))
        req = _required_issue(idx, row, "rate_date")
        if req is not None:
            issues.append(req)
    return issues


def validate_security_beta(rows: Sequence[Mapping[str, Any]]) -> list[RowIssue]:
    """``BETA_FRAME``: positive window, numeric beta."""
    issues: list[RowIssue] = []
    for idx, row in enumerate(rows):
        window = row.get("window_days")
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            issues.append(RowIssue(idx, "window_days", f"window_days must be > 0, got {window!r}"))
        beta = row.get("beta")
        if beta is None or not isinstance(beta, (int, float)) or isinstance(beta, bool):
            issues.append(RowIssue(idx, "beta", f"beta must be numeric, got {beta!r}"))
        for col in ("security_id", "as_of_date"):
            req = _required_issue(idx, row, col)
            if req is not None:
                issues.append(req)
    return issues


def validate_spells(rows: Sequence[Mapping[str, Any]], *, table: str) -> list[RowIssue]:
    """Shared checks for coverage/employment/identifier spells: ``to_date >= from_date``."""
    issues: list[RowIssue] = []
    for idx, row in enumerate(rows):
        frm = row.get("from_date")
        to = row.get("to_date")
        if frm is not None and to is not None and to < frm:
            issues.append(RowIssue(idx, "to_date", f"to_date {to} < from_date {frm}"))
    return issues


def validate_table(
    table: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    known_scale_ids: Collection[str] | None = None,
    existing_event_ids: Collection[str] = (),
) -> tuple[list[RowIssue], list[RowIssue]]:
    """Dispatch to the per-table validator; returns ``(errors, warnings)``."""
    if table == "rating_event":
        found = validate_rating_events(
            rows, known_scale_ids=known_scale_ids, existing_event_ids=existing_event_ids
        )
    elif table == "security_daily":
        found = validate_security_daily(rows)
    elif table == "delisting":
        found = validate_delistings(rows)
    elif table == "benchmark_daily":
        found = validate_benchmark_daily(rows)
    elif table == "security_classification":
        found = validate_security_classifications(rows)
    elif table == "fx_rate_daily":
        found = validate_fx_daily(rows)
    elif table == "security_beta":
        found = validate_security_beta(rows)
    elif table in ("analyst_coverage", "analyst_employment", "security_identifier"):
        found = validate_spells(rows, table=table)
    else:
        found = []
    errors = [i for i in found if i.is_error]
    warnings = [i for i in found if not i.is_error]
    return errors, warnings
