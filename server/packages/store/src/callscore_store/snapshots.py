"""Point-in-time input snapshots for engine runs.

``load_input_snapshot`` reads the store and returns the engine *input* frames
(``callscore_core.frames`` contracts) as Polars DataFrames, plus deterministic
``source_fingerprint`` / ``universe_fingerprint`` values.

Clipping rules (documented choices):

* ``rating_events`` — ALL rows are returned, unfiltered. PIT visibility is the
  engine's job (``visible_events`` applies ``knowledge_cutoff``); the cutoff is
  recorded on the :class:`Snapshot` for provenance only.
* Spell frames (coverage, employment, classification) — all rows; spells are
  resolved by date inside the engine.
* ``security_daily``, ``benchmark_daily``, ``fx_daily`` — only rows with their
  date column ``<= as_of_date``: a run cannot observe prices after its as-of.
* ``security_beta`` — only ``as_of_date <= as_of_date`` snapshots.
* ``delistings`` — only ``delisting_date <= as_of_date`` (a future delisting is
  not yet knowable).
* ``trading_session`` — only ``session_date <= as_of_date``.

``universe_filter`` optionally restricts security-scoped frames to a set (or
predicate) of ``security_id`` values; analyst-only frames (employment) and
currency benchmarks are unaffected.
"""

import hashlib
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import polars as pl
from sqlalchemy import Connection, Engine, Select, select
from sqlalchemy.orm import Session

from callscore_core.frames import (
    BENCHMARK_DAILY_FRAME,
    BETA_FRAME,
    COVERAGE_SPELL_FRAME,
    DELISTING_FRAME,
    EMPLOYMENT_SPELL_FRAME,
    FX_DAILY_FRAME,
    RATING_EVENT_FRAME,
    SECURITY_CLASSIFICATION_FRAME,
    SECURITY_DAILY_FRAME,
    SESSION_FRAME,
    FrameContract,
)
from callscore_core.methodology import canonical_dumps
from callscore_store.models import (
    AnalystCoverage,
    AnalystEmployment,
    BenchmarkDaily,
    Delisting,
    FxRateDaily,
    RatingEvent,
    SecurityBeta,
    SecurityClassification,
    SecurityDaily,
    TradingSession,
)
from callscore_store.repositories.base import Context

SecurityFilter = Iterable[str] | Callable[[str], bool] | None

_DTYPES: dict[str, Any] = {
    "String": pl.String,
    "Date": pl.Date,
    "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
    "Float64": pl.Float64,
    "Int64": pl.Int64,
    "Boolean": pl.Boolean,
}

# (frame contract, model, date column used for as_of clipping or None).
# Contract column names match model attribute names for every input frame.
_SOURCES: tuple[tuple[FrameContract, type, str | None], ...] = (
    (SESSION_FRAME, TradingSession, "session_date"),
    (RATING_EVENT_FRAME, RatingEvent, None),
    (COVERAGE_SPELL_FRAME, AnalystCoverage, None),
    (EMPLOYMENT_SPELL_FRAME, AnalystEmployment, None),
    (SECURITY_CLASSIFICATION_FRAME, SecurityClassification, None),
    (SECURITY_DAILY_FRAME, SecurityDaily, "session_date"),
    (BENCHMARK_DAILY_FRAME, BenchmarkDaily, "session_date"),
    (BETA_FRAME, SecurityBeta, "as_of_date"),
    (DELISTING_FRAME, Delisting, "delisting_date"),
    (FX_DAILY_FRAME, FxRateDaily, "rate_date"),
)

# Frames whose rows carry a security_id subject to universe_filter.
_SECURITY_SCOPED = frozenset(
    {
        "rating_events",
        "coverage_spells",
        "security_classifications",
        "security_daily",
        "delistings",
        "security_beta",
    }
)

_ANALYST_SCOPED = frozenset({"rating_events", "coverage_spells", "employment_spells"})
_SECURITY_ONLY = frozenset(
    {
        "security_classifications",
        "security_daily",
        "delistings",
        "security_beta",
    }
)


@dataclass(frozen=True)
class Snapshot:
    """Point-in-time engine inputs plus provenance fingerprints."""

    frames: Mapping[str, pl.DataFrame]
    as_of_date: date
    knowledge_cutoff: datetime
    source_fingerprint: str
    universe_fingerprint: str
    filter_description: str | None = None

    def frame(self, contract: FrameContract) -> pl.DataFrame:
        """Return the frame for a contract (keyed by ``contract.name``)."""
        return self.frames[contract.name]

    def __getitem__(self, name: str) -> pl.DataFrame:
        return self.frames[name]


def _security_filter(flt: SecurityFilter) -> Callable[[str], bool] | None:
    if flt is None:
        return None
    if isinstance(flt, Iterable) and not isinstance(flt, str):
        allowed = frozenset(flt)
        return lambda security_id: security_id in allowed
    if callable(flt):
        return flt
    raise TypeError(f"unsupported universe_filter type: {type(flt).__name__}")


def _coerce_value(value: Any, dtype: str) -> Any:
    """Coerce a DB value into the contract dtype's Python representation."""
    if value is None:
        return None
    if dtype == "Float64" and isinstance(value, Decimal):
        return float(value)
    if dtype == "Int64" and isinstance(value, bool):
        return int(value)
    return value


def _frame_schema(contract: FrameContract) -> dict[str, Any]:
    return {c.name: _DTYPES[c.dtype] for c in contract.columns}


def _select_rows(
    ctx: Context,
    model: Any,
    clip_col: str | None,
    as_of: date,
) -> list[dict[str, Any]]:
    """Fetch rows as column-name dicts (works identically on all context types)."""
    from callscore_store.repositories.base import table_of

    table = table_of(model)
    stmt: Select[Any] = select(table)
    if clip_col is not None:
        stmt = stmt.where(table.c[clip_col] <= as_of)
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]
    if isinstance(ctx, Session | Connection):
        return [dict(r) for r in ctx.execute(stmt).mappings()]
    raise TypeError(f"unsupported context type: {type(ctx).__name__}")


def _apply_universe(
    rows: list[dict[str, Any]],
    contract: FrameContract,
    flt: Callable[[str], bool] | None,
) -> list[dict[str, Any]]:
    if flt is None or contract.name not in _SECURITY_SCOPED:
        return rows
    return [r for r in rows if flt(str(r["security_id"]))]


def _to_frame(contract: FrameContract, rows: list[dict[str, Any]]) -> pl.DataFrame:
    colmap = {c.name: c.dtype for c in contract.columns}
    records: list[dict[str, Any]] = []
    for row in rows:
        rec: dict[str, Any] = {}
        for name, dtype in colmap.items():
            rec[name] = _coerce_value(row.get(name), dtype)
        records.append(rec)
    frame = pl.DataFrame(records, schema=_frame_schema(contract))
    # Deterministic row order: contract sort keys (pk is a tie-breaker suffix).
    return frame.sort(list(contract.sort_by))


def _jsonable(value: Any) -> Any:
    """Convert frame values to JSON-safe primitives for fingerprinting."""
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _frame_digest(contract: FrameContract, frame: pl.DataFrame) -> str:
    """Canonical digest of one frame sorted by its contract ``sort_by``."""
    sorted_frame = frame.sort(list(contract.sort_by))
    rows = [{k: _jsonable(v) for k, v in record.items()} for record in sorted_frame.to_dicts()]
    payload = {
        "frame": contract.name,
        "version": contract.version,
        "columns": [c.name for c in contract.columns],
        "rows": rows,
    }
    return canonical_dumps(payload)


def _collect_universe_ids(frames: Mapping[str, pl.DataFrame]) -> tuple[list[str], list[str]]:
    analysts: set[str] = set()
    securities: set[str] = set()
    for name, frame in frames.items():
        if frame.height == 0:
            continue
        if name in _ANALYST_SCOPED and "analyst_id" in frame.columns:
            analysts.update(str(v) for v in frame["analyst_id"].to_list())
        has_security_ids = "security_id" in frame.columns
        if has_security_ids and (
            name in _SECURITY_ONLY or name in {"rating_events", "coverage_spells"}
        ):
            securities.update(str(v) for v in frame["security_id"].to_list())
    return sorted(analysts), sorted(securities)


def load_input_snapshot(
    ctx: Context,
    as_of_date: date,
    knowledge_cutoff: datetime,
    universe_filter: SecurityFilter = None,
) -> Snapshot:
    """Load the point-in-time input frames for an engine run.

    ``knowledge_cutoff`` must be timezone-aware; it is recorded on the
    snapshot but does NOT filter ``rating_events`` (the engine's
    ``visible_events`` applies it). See the module docstring for clipping
    rules.
    """
    if knowledge_cutoff.tzinfo is None or knowledge_cutoff.utcoffset() is None:
        raise ValueError("knowledge_cutoff must be timezone-aware")
    flt = _security_filter(universe_filter)

    frames: dict[str, pl.DataFrame] = {}
    contracts: dict[str, FrameContract] = {}
    for contract, model, clip_col in _SOURCES:
        rows = _select_rows(ctx, model, clip_col, as_of_date)
        rows = _apply_universe(rows, contract, flt)
        frame = _to_frame(contract, rows)
        frames[contract.name] = frame
        contracts[contract.name] = contract

    # source_fingerprint: canonical digest over every frame, in contract order.
    digests = [
        _frame_digest(contracts[contract.name], frames[contract.name])
        for contract, _, _ in _SOURCES
    ]
    source_fingerprint = hashlib.sha256(canonical_dumps({"frames": digests}).encode()).hexdigest()

    analysts, securities = _collect_universe_ids(frames)
    universe_fingerprint = hashlib.sha256(
        canonical_dumps({"analysts": analysts, "securities": securities}).encode()
    ).hexdigest()

    filter_desc: str | None = None
    if universe_filter is not None:
        filter_desc = (
            universe_filter
            if isinstance(universe_filter, str)
            else getattr(universe_filter, "__name__", repr(universe_filter))
        )

    return Snapshot(
        frames=frames,
        as_of_date=as_of_date,
        knowledge_cutoff=knowledge_cutoff,
        source_fingerprint=source_fingerprint,
        universe_fingerprint=universe_fingerprint,
        filter_description=filter_desc,
    )


__all__ = [
    "Snapshot",
    "SecurityFilter",
    "load_input_snapshot",
]
